"""The one door — a homepage, a login page with two ways in, and a cookie.

Everything a visitor can do here (approve a send, ask the fleet to spend money,
upload a document) needs a name attached to it, so nothing behind `/` is
reachable without one. Two ways to get a name:

* **Demo login** (`users=`): usernames the operator minted with
  `python -m freight_fleet.cli chat-users`, passwords the judges type, hashes in
  Secret Manager. The username IS the identity — it is pinned into every ADK
  `user_id` and stamped on every ledger row the visitor decides.
* **Google sign-in** (`google=`): our own OAuth client, not IAP — IAP is a
  per-service switch and cannot share a page with a password form. The visitor
  types the invite code (`code=`) first, then goes to Google; the verified
  email is the identity. The code is what makes this a door rather than a
  public spend button: any Google account can sign in, only an invited one is
  let through.

What neither is: a password store with accounts, resets and e-mail. The whole
credential set is one JSON secret plus one invite string, rotated by minting
new ones. And neither page is JavaScript: plain HTML forms, like every other
page this project serves except the chat.

The cookie is signed with a key DERIVED from the credentials (the user table,
the code, the OAuth client id), so rotating any of them invalidates every
cookie with no second secret to manage. `HttpOnly; Secure; SameSite=Lax` is
exactly enough for same-origin fetches and the chat's websocket.

Identity travels downstream in one header, `x-fleet-identity`, which this layer
STRIPS from every incoming request before setting its own — so the console can
trust it exactly as far as it trusts the process in front of it.

Failures are counted per client address in this process only. The service runs
with `--max-instances 1`, so "this process" is the whole service; if that ever
changes the limit degrades to per-instance, which is weaker but never wrong.

Nothing configured means local development: the gate passes everything through.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode

COOKIE = "fleet_access"
FORM_PATH = "/access"
GOOGLE_START = "/auth/google"
GOOGLE_CALLBACK = "/auth/google/callback"
LOGOUT_PATH = "/logout"
HOME = "/desk"  # where a login lands when it was not interrupted on its way somewhere
#: Reachable with no cookie: the policy Google's consent screen links to, and
#: the two probes the deploy guide uses. The homepage is handled apart.
PUBLIC_PATHS = frozenset({"/privacy", "/healthz", "/reconcile.json", "/robots.txt"})
TTL_SECONDS = 7 * 24 * 3600
MAX_FAILURES = 5
FAILURE_WINDOW = 15 * 60
_MAX_FORM_BYTES = 4096
_SCRYPT = (2**14, 8, 1)  # n, r, p — ~50 ms on a small vCPU, the right side of "annoying"
_STATE_TTL = 10 * 60

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")


def normalize(code: str) -> str:
    """Judges type codes by hand: whitespace and case are not part of the secret."""
    return "".join(code.split()).upper()


# --- passwords ---------------------------------------------------------------


def hash_password(password: str) -> str:
    """`scrypt$n$r$p$salt$hash`, self-describing so the parameters can change later
    without invalidating what was minted before."""
    n, r, p = _SCRYPT
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    b64 = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")
    return f"scrypt${n}${r}${p}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        unb64 = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
        expected = unb64(digest)
        actual = hashlib.scrypt(password.encode(), salt=unb64(salt), n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def mint_users(usernames: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    """Fresh passwords for `usernames`. Returns (table-for-the-secret, passwords),
    and the passwords exist only in the second value — print them once."""
    passwords = {u.strip().lower(): secrets.token_urlsafe(9) for u in usernames if u.strip()}
    return {u: hash_password(pw) for u, pw in passwords.items()}, passwords


def load_users(raw: str | None) -> dict[str, str] | None:
    """The `FREIGHT_CHAT_USERS` secret: a JSON object of `{username: hash}`.
    Empty or unset is "no demo login", not an error."""
    if not raw or not raw.strip():
        return None
    table = json.loads(raw)
    if not isinstance(table, dict) or not table:
        raise ValueError("FREIGHT_CHAT_USERS must be a non-empty JSON object of username -> hash")
    return {str(k).strip().lower(): str(v) for k, v in table.items()}


# --- Google ------------------------------------------------------------------


@dataclass(frozen=True)
class GoogleSignIn:
    """Our OAuth web client. `exchange` turns an authorization code into the
    ID-token claims; injectable so the tests never reach the network."""

    client_id: str
    client_secret: str
    redirect_uri: str
    exchange: Callable[[str], dict[str, Any]] | None = None

    @staticmethod
    def from_env() -> GoogleSignIn | None:
        cid = os.environ.get("FREIGHT_GOOGLE_CLIENT_ID", "").strip()
        secret = os.environ.get("FREIGHT_GOOGLE_CLIENT_SECRET", "").strip()
        redirect = os.environ.get("FREIGHT_GOOGLE_REDIRECT_URI", "").strip()
        if not (cid and secret and redirect):
            return None
        return GoogleSignIn(cid, secret, redirect)


def _b64url_json(segment: str) -> dict[str, Any]:
    padded = segment + "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def exchange_code(google: GoogleSignIn, code: str) -> dict[str, Any]:
    """Authorization code -> ID-token claims, over TLS to Google's token endpoint.

    The ID token comes straight from Google in that response, so its signature
    need not be re-verified here (Google's own guidance for this flow); what
    MUST be checked is that it was minted for this client, by Google, and is
    not expired — an ID token for somebody else's client is still a valid
    Google signature.
    """
    import urllib.request

    data = urlencode({
        "code": code,
        "client_id": google.client_id,
        "client_secret": google.client_secret,
        "redirect_uri": google.redirect_uri,
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(GOOGLE_TOKEN_URL, data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        token = json.loads(resp.read())
    id_token = str(token.get("id_token", ""))
    parts = id_token.split(".")
    if len(parts) != 3:
        raise ValueError("token response carried no ID token")
    claims = _b64url_json(parts[1])
    if claims.get("iss") not in _GOOGLE_ISSUERS:
        raise ValueError("ID token issuer is not Google")
    if claims.get("aud") != google.client_id:
        raise ValueError("ID token was minted for a different client")
    if int(claims.get("exp", 0)) <= time.time():
        raise ValueError("ID token is expired")
    return claims


# --- the gate ----------------------------------------------------------------


class AccessCodeMiddleware:
    """Pure ASGI, for the same reason as `IapIdentityMiddleware`: the streaming
    endpoints must not be buffered, and a websocket must be refused as one."""

    def __init__(
        self,
        app: Any,
        code: str | None = None,
        users: dict[str, str] | None = None,
        google: GoogleSignIn | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.app = app
        self.users = users or None
        self.google = google
        self.code = normalize(code) if code else None
        material = json.dumps({
            "users": sorted(self.users.items()) if self.users else [],
            "code": self.code or "",
            "google": google.client_id if google else "",
        })
        self.key = hashlib.sha256(b"freight-chat-access:" + material.encode()).digest()
        self.clock = clock
        self.failures: dict[str, deque[float]] = defaultdict(deque)

    @property
    def mode(self) -> str:
        """`off` — nothing configured, everything passes. `code` — an invite code
        alone (the shape behind IAP, where something else already knows who you
        are). `gated` — a login page with a demo login, Google, or both."""
        if self.users or self.google:
            return "gated"
        return "code" if self.code else "off"

    # -- cookie ---------------------------------------------------------------

    def issue(self, identity: str = "", kind: str = "") -> str:
        """`subject.expiry.sig`, where subject is `kind:identity` base64url-encoded
        — an email has dots and an `@`, and both a `.`-delimited token and the
        cookie grammar would choke on them. Kinds: `u` username, `g` Google
        email, `c` invite code alone (no identity to pin)."""
        if not kind:
            kind = "u" if self.users and identity else "c"
        subject = base64.urlsafe_b64encode(f"{kind}:{identity}".encode()).decode().rstrip("=")
        expiry = str(int(self.clock()) + TTL_SECONDS)
        return f"{subject}.{expiry}.{self._sign(subject, expiry)}"

    def session(self, value: str | None) -> str | None:
        """The identity a cookie proves, or `None` if it proves nothing. `""` is a
        valid answer: an invite code proves invitation, not identity."""
        parsed = self.parse(value)
        return None if parsed is None else parsed[1]

    def parse(self, value: str | None) -> tuple[str, str] | None:
        """`(kind, identity)` for a cookie that verifies, else None."""
        if not value or value.count(".") != 2:
            return None
        subject, expiry, sig = value.split(".")
        if not expiry.isdigit() or int(expiry) <= self.clock():
            return None
        if not hmac.compare_digest(self._sign(subject, expiry), sig):
            return None
        try:
            decoded = base64.urlsafe_b64decode(subject + "=" * (-len(subject) % 4)).decode()
        except (ValueError, UnicodeDecodeError):
            return None
        kind, _, identity = decoded.partition(":")
        if kind == "u" and (not self.users or identity not in self.users):
            return None
        if kind == "g" and not self.google:
            return None
        if kind == "c" and self.mode != "code":
            return None
        if kind not in ("u", "g", "c"):
            return None
        return kind, identity

    def valid(self, value: str | None) -> bool:
        return self.parse(value) is not None

    def _sign(self, subject: str, expiry: str) -> str:
        return hmac.new(self.key, f"{subject}.{expiry}".encode(), hashlib.sha256).hexdigest()

    def matches(self, submitted: str) -> bool:
        return self.code is not None and hmac.compare_digest(normalize(submitted).encode(), self.code.encode())

    def authenticate(self, username: str, password: str) -> str | None:
        """The username if the password is right, else None. A missing user
        still runs one hash so the timing does not say which half was wrong."""
        user = username.strip().lower()
        stored = (self.users or {}).get(user) or hash_password("")
        return user if self.users and user in self.users and verify_password(password, stored) else None

    # -- OAuth state ----------------------------------------------------------

    def _state(self, nxt: str) -> str:
        """A signed, expiring `nonce|expiry|next` so the callback can tell a
        round-trip it started from one somebody else started."""
        nonce = secrets.token_urlsafe(12)
        expiry = str(int(self.clock()) + _STATE_TTL)
        payload = base64.urlsafe_b64encode(f"{nonce}|{expiry}|{nxt}".encode()).decode().rstrip("=")
        return f"{payload}.{self._sign('state:' + payload, expiry)}"

    def _unstate(self, state: str) -> str | None:
        """The `next` a state was issued for, or None if it does not verify."""
        if not state or "." not in state:
            return None
        payload, sig = state.rsplit(".", 1)
        try:
            raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode()
            _nonce, expiry, nxt = raw.split("|", 2)
        except (ValueError, UnicodeDecodeError):
            return None
        if not expiry.isdigit() or int(expiry) <= self.clock():
            return None
        if not hmac.compare_digest(self._sign("state:" + payload, expiry), sig):
            return None
        return _safe_next(nxt)

    # -- rate limit -----------------------------------------------------------

    def _limited(self, client: str) -> bool:
        window = self.failures[client]
        now = self.clock()
        while window and window[0] < now - FAILURE_WINDOW:
            window.popleft()
        return len(window) >= MAX_FAILURES

    def _record_failure(self, client: str) -> None:
        self.failures[client].append(self.clock())

    # -- ASGI -----------------------------------------------------------------

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] not in ("http", "websocket") or self.mode == "off":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        method = scope.get("method", "GET")
        if scope["type"] == "http":
            if path == FORM_PATH:
                await self._form(scope, receive, send)
                return
            if path == LOGOUT_PATH:
                await self._logout(send)
                return
            if self.google and path == GOOGLE_START:
                await self._google_start(scope, receive, send)
                return
            if self.google and path == GOOGLE_CALLBACK:
                await self._google_callback(scope, send)
                return
            if path == "/" and self.mode == "gated" and method in ("GET", "HEAD"):
                await _page(send, 200, home_html(self.parse(_cookie(scope))))
                return
            if path in PUBLIC_PATHS:
                await self.app(_strip(scope), receive, send)
                return

        parsed = self.parse(_cookie(scope))
        if parsed is not None:
            kind, identity = parsed
            scope = _strip(scope, identity=identity, kind=kind)
            if identity:
                from .devui import pin_identity

                scope, receive = await pin_identity(scope, receive, identity)
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            await receive()
            await send({"type": "websocket.close", "code": 4403})
            return
        if method in ("GET", "HEAD"):
            target = path
            qs = scope.get("query_string", b"")
            if qs:
                target += "?" + qs.decode("latin-1")
            await _respond(send, 303, b"", [(b"location", _form_url(target).encode())])
            return
        body = json.dumps({"detail": "login required" if self.mode == "gated" else "access code required"}).encode()
        await _respond(send, 403, body, [(b"content-type", b"application/json")])

    async def _form(self, scope: dict, receive: Callable, send: Callable) -> None:
        params = dict(parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True))
        nxt = _safe_next(params.get("next"))
        if scope.get("method") != "POST":
            await _page(send, 200, self.form_html(nxt, ""))
            return

        client = _client(scope)
        if self._limited(client):
            await _page(send, 429, self.form_html(nxt, "Too many attempts. Try again in fifteen minutes."))
            return

        raw = (await _read(receive)).decode("utf-8", "replace")
        form = dict(parse_qsl(raw, keep_blank_values=True))
        nxt = _safe_next(form.get("next"))
        if self.users and ("username" in form or "password" in form):
            identity = self.authenticate(form.get("username", ""), form.get("password", ""))
            kind, refused = "u", "That username and password were not accepted."
        elif self.mode == "code":
            identity = "" if self.matches(form.get("code", "")) else None
            kind, refused = "c", "That code was not accepted."
        else:
            identity, kind, refused = None, "", "Choose one of the ways in."
        if identity is None:
            self._record_failure(client)
            await _page(send, 403, self.form_html(nxt, refused))
            return
        await _respond(send, 303, b"", [(b"location", nxt.encode()), _set_cookie(self.issue(identity, kind))])

    async def _logout(self, send: Callable) -> None:
        gone = f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax"
        await _respond(send, 303, b"", [(b"location", b"/"), (b"set-cookie", gone.encode())])

    async def _google_start(self, scope: dict, receive: Callable, send: Callable) -> None:
        """Invite code (if one is configured) first, then off to Google with a
        signed state. The code is checked BEFORE the round trip so a stranger
        cannot even make Google show them a consent screen for this app."""
        assert self.google is not None
        if scope.get("method") == "POST":
            form = dict(parse_qsl((await _read(receive)).decode("utf-8", "replace"), keep_blank_values=True))
        else:
            form = dict(parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True))
        nxt = _safe_next(form.get("next"))
        if self.code:
            client = _client(scope)
            if self._limited(client):
                await _page(send, 429, self.form_html(nxt, "Too many attempts. Try again in fifteen minutes."))
                return
            if not self.matches(form.get("code", "")):
                self._record_failure(client)
                await _page(send, 403, self.form_html(nxt, "That invite code was not accepted."))
                return
        query = urlencode({
            "client_id": self.google.client_id,
            "redirect_uri": self.google.redirect_uri,
            "response_type": "code",
            "scope": "openid email",
            "state": self._state(nxt),
            "prompt": "select_account",
        })
        await _respond(send, 303, b"", [(b"location", f"{GOOGLE_AUTH_URL}?{query}".encode())])

    async def _google_callback(self, scope: dict, send: Callable) -> None:
        assert self.google is not None
        params = dict(parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True))
        nxt = self._unstate(params.get("state", ""))
        if nxt is None:
            await _page(send, 403, self.form_html(HOME, "That sign-in did not start here, or it took too long. Try again."))
            return
        if params.get("error") or not params.get("code"):
            await _page(send, 403, self.form_html(nxt, "Google did not complete the sign-in."))
            return
        google = self.google
        exchange = google.exchange or (lambda c: exchange_code(google, c))
        try:
            claims = await asyncio.to_thread(exchange, params["code"])
        except Exception:  # noqa: BLE001 - a failed exchange is a refused login, not a 500
            await _page(send, 403, self.form_html(nxt, "Google's sign-in could not be verified. Try again."))
            return
        email = str(claims.get("email", "")).strip().lower()
        if not email or not claims.get("email_verified", False):
            await _page(send, 403, self.form_html(nxt, "That Google account has no verified email address."))
            return
        await _respond(send, 303, b"", [(b"location", nxt.encode()), _set_cookie(self.issue(email, "g"))])

    # -- pages ----------------------------------------------------------------

    def form_html(self, nxt: str, message: str) -> str:
        """The login page in whichever shape this deployment has: one panel per
        way in, side by side, no script tag, message escaped like any input."""
        note = f'<p class="warn">{html.escape(message)}</p>' if message else ""
        hidden = f'<input type="hidden" name="next" value="{html.escape(nxt, quote=True)}">'
        panels: list[str] = []
        if self.users:
            panels.append(
                '<form method="post" action="' + FORM_PATH + '" class="panel">'
                '<span class="label">Demo login</span>'
                "<p>Use the username and password from the submission. No Google account is "
                "involved: the username is your identity here, and what you do stays yours.</p>"
                + hidden +
                '<label for="u">Username</label><input id="u" name="username" autocomplete="username" '
                'autocapitalize="none" spellcheck="false" required>'
                '<label for="p">Password</label><input id="p" name="password" type="password" '
                'autocomplete="current-password" required>'
                '<button type="submit">Enter</button></form>'
            )
        if self.google:
            code_field = (
                '<label for="c">Invite code</label>'
                '<input id="c" name="code" autocomplete="off" autocapitalize="characters" spellcheck="false" '
                'placeholder="FLEET-XXXXXX-XXXXXX" required>'
                if self.code else ""
            )
            panels.append(
                '<form method="post" action="' + GOOGLE_START + '" class="panel">'
                '<span class="label">Google sign-in</span>'
                "<p>Sign in with any Google account"
                + (" and the invite code from the submission" if self.code else "")
                + ". Your email becomes your identity here; approved sends carry a copy to it.</p>"
                + hidden + code_field +
                '<button type="submit" class="google">Continue with Google</button></form>'
            )
        if not panels:
            panels.append(
                '<form method="post" action="' + FORM_PATH + '" class="panel">'
                '<span class="label">Enter the access code</span>'
                "<p>You are signed in. The chat additionally needs the code from the submission, so "
                "that only invited visitors can put the fleet to work.</p>"
                + hidden +
                '<input name="code" autocomplete="off" autocapitalize="characters" spellcheck="false" '
                'placeholder="FLEET-XXXXXX-XXXXXX" required autofocus>'
                '<button type="submit">Continue</button></form>'
            )
        wide = " wide" if len(panels) > 1 else ""
        return (
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>Sign in — Freight Ops Fleet</title><style>{_STYLE}</style></head><body>"
            f'<div class="card{wide}"><span class="label"><a href="/">Freight Ops Fleet</a></span>'
            "<h1>Sign in</h1>"
            "<p>Behind this page is the operator's desk and the fleet itself — decisions here are "
            "real for the demo record, and every one carries your name.</p>"
            f"{note}<div class=\"panels\">{''.join(panels)}</div>"
            '<p class="meta">Every shipment here is fictional. Conversations and decisions are stored '
            'so they can be resumed; see the <a href="/privacy">privacy page</a>.</p>'
            "</div></body></html>"
        )


def home_html(session: tuple[str, str] | None) -> str:
    """The front door: what this is, in five sentences, and the one button.
    Rendered from constants and env; touches no record."""
    repo = os.environ.get("FREIGHT_REPO_URL", "").strip()
    if session is not None:
        who = session[1] or "invited visitor"
        cta = (f'<p class="meta">Signed in as <strong>{html.escape(who)}</strong>.</p>'
               f'<a class="cta" href="{HOME}">Go to the desk →</a> '
               f'<a class="cta quiet" href="{LOGOUT_PATH}">Sign out</a>')
    else:
        cta = f'<a class="cta" href="{FORM_PATH}">Sign in →</a>'
    source = f' · <a href="{html.escape(repo, quote=True)}">Source</a>' if repo else ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Freight Ops Fleet</title><style>{_STYLE}{_HOME_STYLE}</style></head><body>"
        '<div class="card home"><span class="label">Freight Ops Fleet</span>'
        "<h1>A fleet of freight agents that cannot act alone.</h1>"
        "<p class=\"lede\">Five agents on Google ADK read a shipment's waybill, packing list and "
        "commercial invoice, find where they disagree, chase what is missing and draft the "
        "correction. Every consequential action — a file, an email — stops at one policy gate "
        "for a human. Every decision lands in an append-only ledger.</p>"
        "<ul>"
        "<li><strong>The desk.</strong> A scheduled sweep checks every open shipment at 06:00 "
        "with nobody watching; what it drafts waits for you. Approve sends it — to a demo "
        "mailbox, never to the fictional address. Reject retires it.</li>"
        "<li><strong>Ask the fleet.</strong> Put a question to the agents, upload a scanned "
        "document, watch the routing and the gate in the trace, see what each turn cost.</li>"
        "<li><strong>The record.</strong> The ledger, the catalog of agents with their tool "
        "allow-lists, the evidence and the scoreboard — all readable, none editable.</li>"
        "</ul>"
        f"{cta}"
        '<p class="meta">Every shipment, party and figure is fictional. '
        f'<a href="/privacy">Privacy</a>{source}</p>'
        "</div></body></html>"
    )


def _set_cookie(value: str) -> tuple[bytes, bytes]:
    return (b"set-cookie",
            f"{COOKIE}={value}; Path=/; Max-Age={TTL_SECONDS}; HttpOnly; Secure; SameSite=Lax".encode())


def _strip(scope: dict, identity: str = "", kind: str = "") -> dict:
    """A scope whose `x-fleet-identity` is ours or absent — never the client's."""
    scope = dict(scope)
    kept = [(k, v) for k, v in scope.get("headers", ()) if k.lower() not in (b"x-fleet-identity", b"x-fleet-kind")]
    if identity:
        kept.append((b"x-fleet-identity", identity.encode("utf-8")))
        kept.append((b"x-fleet-kind", kind.encode("latin-1")))
    scope["headers"] = kept
    return scope


def _cookie(scope: dict) -> str | None:
    for key, value in scope.get("headers", ()):
        if key.lower() == b"cookie":
            jar: SimpleCookie = SimpleCookie()
            jar.load(value.decode("latin-1"))
            morsel = jar.get(COOKIE)
            return morsel.value if morsel else None
    return None


def _client(scope: dict) -> str:
    """The address failures are counted against. Cloud Run puts the real client
    first in `X-Forwarded-For`; the socket peer is the load balancer."""
    for key, value in scope.get("headers", ()):
        if key.lower() == b"x-forwarded-for":
            return value.decode("latin-1").split(",")[0].strip()
    client = scope.get("client")
    return client[0] if client else "unknown"


def _safe_next(value: str | None) -> str:
    """Only a same-origin path may follow a login. `//host` is a protocol-relative
    URL and a backslash variant is what some browsers make of it; both are refused."""
    if not value or not value.startswith("/") or value.startswith(("//", "/\\")):
        return HOME
    if value in ("/", FORM_PATH, LOGOUT_PATH) or value.startswith((GOOGLE_START, FORM_PATH + "?")):
        return HOME
    return value


def _form_url(target: str) -> str:
    return FORM_PATH + "?" + urlencode({"next": target}, quote_via=quote)


async def _read(receive: Callable[[], Awaitable[dict]]) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunk = message.get("body", b"")
        total += len(chunk)
        if total > _MAX_FORM_BYTES:
            break
        chunks.append(chunk)
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


async def _respond(send: Callable, status: int, body: bytes, headers: list[tuple[bytes, bytes]]) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                *headers,
                (b"content-length", str(len(body)).encode()),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _page(send: Callable, status: int, markup: str) -> None:
    await _respond(send, status, markup.encode(), [(b"content-type", b"text/html; charset=utf-8")])


_STYLE = (
    ":root{--bg:#F7F6F3;--surface:#FFFFFF;--sunk:#EFEDE8;--line:#DCD9D2;--ink:#16181D;--ink-2:#454B54;"
    "--ink-3:#5F6670;--accent:#0B5FFF;--blocked:#A31515;--blocked-tint:#FDE4E4}"
    "@media (prefers-color-scheme:dark){:root{--bg:#14161A;--surface:#1C1F25;--sunk:#101215;--line:#2C313A;"
    "--ink:#F3F4F6;--ink-2:#C3C8D1;--ink-3:#98A0AC;--accent:#7EA6FF;--blocked:#FCA5A5;--blocked-tint:#3A1A18}}"
    "body{margin:0;background:var(--bg);color:var(--ink);font:400 17px/1.55 ui-sans-serif,system-ui,"
    "sans-serif;display:flex;min-height:100vh;align-items:center;justify-content:center;padding:20px;box-sizing:border-box}"
    ".card{background:var(--surface);border:1px solid var(--line);border-left:6px solid var(--accent);"
    "border-radius:12px;padding:28px;max-width:460px;width:100%;box-sizing:border-box}"
    ".card.wide{max-width:860px}"
    "h1{font:650 30px/1.15 'Iowan Old Style','Palatino Linotype',Palatino,Georgia,serif;margin:0 0 8px;letter-spacing:-.01em}"
    "p{margin:0 0 14px;color:var(--ink-2)}a{color:var(--accent)}"
    ".label{font:700 12.5px/1 ui-sans-serif,system-ui,sans-serif;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-3)}"
    ".label a{color:inherit;text-decoration:none}"
    "label{display:block;font:700 12.5px/1 ui-sans-serif,system-ui,sans-serif;letter-spacing:.08em;"
    "text-transform:uppercase;color:var(--ink-2);margin-top:6px}"
    "input{width:100%;box-sizing:border-box;font:600 17px/1.2 ui-monospace,Menlo,Consolas,monospace;"
    "letter-spacing:.04em;padding:12px 14px;margin:8px 0 14px;border:1px solid var(--line);"
    "border-radius:10px;background:var(--bg);color:var(--ink)}"
    "button{width:100%;min-height:52px;border-radius:10px;border:1px solid var(--accent);"
    "background:var(--accent);color:#fff;font:700 15px/1.2 ui-sans-serif,system-ui,sans-serif;cursor:pointer}"
    "button.google{background:var(--surface);color:var(--ink);border-color:var(--line)}"
    ".panels{display:grid;gap:18px;margin-top:8px}"
    "@media (min-width:720px){.card.wide .panels{grid-template-columns:1fr 1fr}}"
    ".panel{border:1px solid var(--line);border-radius:12px;padding:18px;background:var(--bg)}"
    ".panel p{font-size:15px}"
    ".warn{background:var(--blocked-tint);color:var(--blocked);padding:10px 12px;border-radius:8px;"
    "font-weight:600}.meta{font-size:14px;margin:14px 0 0;color:var(--ink-3)}"
)

_HOME_STYLE = (
    ".card.home{max-width:720px;padding:36px}"
    ".card.home h1{font-size:clamp(30px,5vw,42px);line-height:1.1;margin-bottom:14px}"
    ".card.home .lede{font-size:18px}"
    ".card.home ul{margin:0 0 22px;padding-left:22px;color:var(--ink-2)}.card.home li{margin:0 0 10px}"
    ".cta{display:inline-flex;align-items:center;min-height:52px;padding:0 22px;border-radius:10px;"
    "background:var(--accent);color:#fff;font:700 16px/1 ui-sans-serif,system-ui,sans-serif;text-decoration:none}"
    ".cta.quiet{background:transparent;color:var(--ink-2);border:1px solid var(--line)}"
)
