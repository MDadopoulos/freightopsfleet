"""An invite-code gate in front of the chat surface.

IAP answers *who* a visitor is; it cannot answer *whether the operator invited
them*. Any Google account can sign in once the consent screen is published, and
every signed-in visitor can make the fleet spend money. This layer adds the one
thing IAP lacks: a code the operator hands out (in the submission form, to the
judges) and can rotate in one secret update.

What it is not. It is not an identity: the IAP middleware still pins every
`user_id` to the signed-in email, so two judges sharing one code still get their
own sessions. It is not a password store: there is one code, held in Secret
Manager, compared in constant time, never written anywhere. And it is not
JavaScript: the form is plain HTML, like every other page this project serves.

The cookie is `<expiry>.<hmac>`, signed with a key DERIVED from the code, so
rotating the code invalidates every cookie without a second secret to manage.
It is `HttpOnly; Secure; SameSite=Lax`, which is exactly enough for the dev
UI's same-origin fetches and its websocket, and nothing else.

Failures are counted per client address in this process only. The chat runs
with `--max-instances 1`, so "this process" is the whole service; if that ever
changes the limit degrades to per-instance, which is weaker but never wrong.

`FREIGHT_CHAT_ACCESS_CODE` unset means local development: the gate passes
everything through, the same convention `FREIGHT_IAP_AUDIENCE` follows.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode

COOKIE = "fleet_access"
FORM_PATH = "/access"
TTL_SECONDS = 7 * 24 * 3600
MAX_FAILURES = 5
FAILURE_WINDOW = 15 * 60
_MAX_FORM_BYTES = 4096


def normalize(code: str) -> str:
    """Judges type codes by hand: whitespace and case are not part of the secret."""
    return "".join(code.split()).upper()


class AccessCodeMiddleware:
    """Pure ASGI, for the same reason as `IapIdentityMiddleware`: the streaming
    endpoints must not be buffered, and a websocket must be refused as one."""

    def __init__(self, app: Any, code: str | None, *, clock: Callable[[], float] = time.time) -> None:
        self.app = app
        self.code = normalize(code) if code else None
        self.key = hashlib.sha256(b"freight-chat-access:" + (self.code or "").encode()).digest()
        self.clock = clock
        self.failures: dict[str, deque[float]] = defaultdict(deque)

    # -- cookie ---------------------------------------------------------------

    def issue(self) -> str:
        expiry = str(int(self.clock()) + TTL_SECONDS)
        return f"{expiry}.{self._sign(expiry)}"

    def valid(self, value: str | None) -> bool:
        if not value or "." not in value:
            return False
        expiry, sig = value.split(".", 1)
        if not expiry.isdigit() or int(expiry) <= self.clock():
            return False
        return hmac.compare_digest(self._sign(expiry), sig)

    def _sign(self, expiry: str) -> str:
        return hmac.new(self.key, expiry.encode(), hashlib.sha256).hexdigest()

    def matches(self, submitted: str) -> bool:
        return self.code is not None and hmac.compare_digest(
            normalize(submitted).encode(), self.code.encode()
        )

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
        if scope["type"] not in ("http", "websocket") or self.code is None:
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        if scope["type"] == "http" and path == FORM_PATH:
            await self._form(scope, receive, send)
            return

        if self.valid(_cookie(scope)):
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            await receive()
            await send({"type": "websocket.close", "code": 4403})
            return
        if scope.get("method") in ("GET", "HEAD"):
            target = path
            qs = scope.get("query_string", b"")
            if qs:
                target += "?" + qs.decode("latin-1")
            await _respond(send, 303, b"", [(b"location", _form_url(target).encode())])
            return
        body = json.dumps({"detail": "access code required"}).encode()
        await _respond(send, 403, body, [(b"content-type", b"application/json")])

    async def _form(self, scope: dict, receive: Callable, send: Callable) -> None:
        params = dict(parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True))
        if scope.get("method") != "POST":
            await _page(send, 200, _form_html(_safe_next(params.get("next")), ""))
            return

        client = _client(scope)
        if self._limited(client):
            await _page(send, 429, _form_html("/", "Too many attempts. Try again in fifteen minutes."))
            return

        raw = (await _read(receive)).decode("utf-8", "replace")
        form = dict(parse_qsl(raw, keep_blank_values=True))
        nxt = _safe_next(form.get("next"))
        if not self.matches(form.get("code", "")):
            self._record_failure(client)
            await _page(send, 403, _form_html(nxt, "That code was not accepted."))
            return

        cookie = f"{COOKIE}={self.issue()}; Path=/; Max-Age={TTL_SECONDS}; HttpOnly; Secure; SameSite=Lax"
        await _respond(send, 303, b"", [(b"location", nxt.encode()), (b"set-cookie", cookie.encode())])


# --- helpers -----------------------------------------------------------------


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
        return "/"
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
    ":root{--bg:#F7F6F3;--surface:#FFFFFF;--line:#DCD9D2;--ink:#16181D;--ink-2:#454B54;"
    "--accent:#0B5FFF;--blocked:#A31515;--blocked-tint:#FDE4E4}"
    "@media (prefers-color-scheme:dark){:root{--bg:#14161A;--surface:#1C1F25;--line:#2C313A;"
    "--ink:#F3F4F6;--ink-2:#C3C8D1;--accent:#7EA6FF;--blocked:#FCA5A5;--blocked-tint:#3A1A18}}"
    "body{margin:0;background:var(--bg);color:var(--ink);font:400 17.5px/1.55 ui-sans-serif,system-ui,"
    "sans-serif;display:flex;min-height:100vh;align-items:center;justify-content:center;padding:20px}"
    ".card{background:var(--surface);border:1px solid var(--line);border-left:6px solid var(--accent);"
    "border-radius:12px;padding:28px;max-width:440px;width:100%}"
    "h1{font:650 30px/1.15 'Iowan Old Style','Palatino Linotype',Palatino,Georgia,serif;margin:0 0 8px}"
    "p{margin:0 0 14px;color:var(--ink-2)}.label{font:700 12.5px/1 ui-sans-serif,system-ui,sans-serif;"
    "letter-spacing:.08em;text-transform:uppercase;color:var(--ink-2)}"
    "input{width:100%;box-sizing:border-box;font:600 20px/1.2 ui-monospace,Menlo,Consolas,monospace;"
    "letter-spacing:.06em;padding:12px 14px;margin:8px 0 14px;border:1px solid var(--line);"
    "border-radius:10px;background:var(--bg);color:var(--ink)}"
    "button{width:100%;min-height:52px;border-radius:10px;border:1px solid var(--accent);"
    "background:var(--accent);color:#fff;font:700 15px/1.2 ui-sans-serif,system-ui,sans-serif;cursor:pointer}"
    ".warn{background:var(--blocked-tint);color:var(--blocked);padding:10px 12px;border-radius:8px;"
    "font-weight:600}.meta{font-size:14px;margin:14px 0 0}"
)


def _form_html(nxt: str, message: str) -> str:
    """The gate's one page. Same palette as the console, no script tag, and the
    message is escaped like everything else that came from a request."""
    note = f'<p class="warn">{html.escape(message)}</p>' if message else ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Access code</title><style>{_STYLE}</style></head><body>"
        '<div class="card"><span class="label">Freight Ops Fleet · Ask the fleet</span>'
        "<h1>Enter the access code</h1>"
        "<p>You are signed in. The chat additionally needs the code from the submission, so that "
        "only invited visitors can put the fleet to work.</p>"
        f"{note}"
        f'<form method="post" action="{FORM_PATH}">'
        f'<input type="hidden" name="next" value="{html.escape(nxt, quote=True)}">'
        '<input name="code" autocomplete="off" autocapitalize="characters" spellcheck="false" '
        'placeholder="FLEET-XXXXXX-XXXXXX" required autofocus>'
        '<button type="submit">Continue</button></form>'
        '<p class="meta">Every shipment here is fictional. Conversations are stored so they can be '
        "resumed; see the privacy page on the public console.</p>"
        "</div></body></html>"
    )
