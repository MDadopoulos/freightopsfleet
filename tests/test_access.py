"""The invite-code gate, driven as raw ASGI like the IAP middleware's tests.

No server, no ADK app, no clock: `clock` is injected so expiry and the failure
window are tested by moving time, not by sleeping.
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import quote

from freight_fleet.access import COOKIE, AccessCodeMiddleware, normalize

CODE = "FLEET-AB12CD-EF34GH"


class EchoApp:
    def __init__(self) -> None:
        self.hits = 0

    async def __call__(self, scope, receive, send):
        self.hits += 1
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})
        else:
            await receive()
            await send({"type": "websocket.accept"})


class Clock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def scope(path: str, *, kind: str = "http", method: str = "GET", headers=(), query: bytes = b"") -> dict:
    return {
        "type": kind,
        "method": method,
        "path": path,
        "query_string": query,
        "headers": [(k.lower(), v) for k, v in headers],
        "client": ("203.0.113.9", 4711),
    }


def drive(app, sc: dict, body: bytes = b"") -> list[dict]:
    sent: list[dict] = []
    first = {"type": "websocket.connect"} if sc["type"] == "websocket" else {"type": "http.request", "body": body}
    inbound = [first]

    async def receive():
        return inbound.pop(0) if inbound else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(app(sc, receive, send))
    return sent


def status(sent: list[dict]) -> int:
    return sent[0]["status"]


def header(sent: list[dict], name: bytes) -> str | None:
    for k, v in sent[0].get("headers", []):
        if k == name:
            return v.decode()
    return None


def login(gate: AccessCodeMiddleware, code: str, nxt: str = "/dev-ui/") -> list[dict]:
    form = f"code={quote(code)}&next={quote(nxt, safe='')}".encode()
    return drive(gate, scope("/access", method="POST"), form)


def cookie_from(sent: list[dict]) -> str:
    value = header(sent, b"set-cookie")
    assert value and value.startswith(f"{COOKIE}=")
    return value.split(";", 1)[0]


# --- pass-through ------------------------------------------------------------


def test_no_code_configured_means_no_gate():
    inner = EchoApp()
    sent = drive(AccessCodeMiddleware(inner, None), scope("/apps/x/users/u/sessions"))
    assert status(sent) == 200 and inner.hits == 1


def test_lifespan_is_never_gated():
    inner = EchoApp()

    async def channel(*_):
        return {"type": "lifespan.startup"}

    asyncio.run(AccessCodeMiddleware(inner, CODE)({"type": "lifespan"}, channel, channel))
    assert inner.hits == 1


# --- refusal -----------------------------------------------------------------


def test_a_browser_navigation_without_a_cookie_is_sent_to_the_form_with_its_destination():
    inner = EchoApp()
    sent = drive(AccessCodeMiddleware(inner, CODE), scope("/dev-ui/", query=b"a=1"))
    assert status(sent) == 303
    assert header(sent, b"location") == "/access?next=%2Fdev-ui%2F%3Fa%3D1"
    assert inner.hits == 0


def test_an_api_post_without_a_cookie_is_a_403_json_body():
    sent = drive(AccessCodeMiddleware(EchoApp(), CODE), scope("/run", method="POST"), b"{}")
    assert status(sent) == 403
    assert json.loads(sent[1]["body"]) == {"detail": "access code required"}


def test_a_websocket_without_a_cookie_is_closed_with_4403():
    sent = drive(AccessCodeMiddleware(EchoApp(), CODE), scope("/run_live", kind="websocket"))
    assert sent == [{"type": "websocket.close", "code": 4403}]


# --- the form ----------------------------------------------------------------


def test_the_form_renders_without_a_cookie_and_carries_no_script():
    sent = drive(AccessCodeMiddleware(EchoApp(), CODE), scope("/access", query=b"next=%2Fdev-ui%2F"))
    page = sent[1]["body"].decode()
    assert status(sent) == 200
    assert 'name="code"' in page and 'value="/dev-ui/"' in page
    assert "<script" not in page


def test_the_right_code_sets_a_hardened_cookie_and_redirects_to_next():
    gate = AccessCodeMiddleware(EchoApp(), CODE)
    sent = login(gate, CODE)
    assert status(sent) == 303 and header(sent, b"location") == "/dev-ui/"
    raw = header(sent, b"set-cookie")
    assert raw is not None
    for flag in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/"):
        assert flag in raw
    assert gate.valid(cookie_from(sent).split("=", 1)[1])


def test_whitespace_and_case_are_not_part_of_the_code():
    assert normalize(" fleet-ab12cd -ef34gh ") == CODE
    assert status(login(AccessCodeMiddleware(EchoApp(), CODE), " fleet-ab12cd-ef34gh")) == 303


def test_the_wrong_code_is_a_403_with_no_cookie():
    sent = login(AccessCodeMiddleware(EchoApp(), CODE), "FLEET-000000-000000")
    assert status(sent) == 403
    assert header(sent, b"set-cookie") is None
    assert "not accepted" in sent[1]["body"].decode()


def test_next_cannot_leave_the_origin():
    gate = AccessCodeMiddleware(EchoApp(), CODE)
    for evil in ("https://evil.example/", "//evil.example", "/\\evil.example", ""):
        assert header(login(gate, CODE, evil), b"location") == "/desk"


# --- the cookie --------------------------------------------------------------


def test_a_valid_cookie_reaches_the_app_over_http_and_websocket():
    inner = EchoApp()
    gate = AccessCodeMiddleware(inner, CODE)
    jar = cookie_from(login(gate, CODE)).encode()
    assert status(drive(gate, scope("/run", method="POST", headers=[(b"cookie", jar)]), b"{}")) == 200
    ws = drive(gate, scope("/run_live", kind="websocket", headers=[(b"cookie", jar)]))
    assert ws == [{"type": "websocket.accept"}] and inner.hits == 2


def test_a_tampered_or_expired_cookie_is_refused():
    clock = Clock()
    gate = AccessCodeMiddleware(EchoApp(), CODE, clock=clock)
    good = cookie_from(login(gate, CODE))
    forged = good[:-1] + ("0" if good[-1] != "0" else "1")
    assert status(drive(gate, scope("/dev-ui/", headers=[(b"cookie", forged.encode())]))) == 303
    clock.now += 8 * 24 * 3600
    assert status(drive(gate, scope("/dev-ui/", headers=[(b"cookie", good.encode())]))) == 303


def test_rotating_the_code_invalidates_every_cookie():
    old = cookie_from(login(AccessCodeMiddleware(EchoApp(), CODE), CODE))
    rotated = AccessCodeMiddleware(EchoApp(), "FLEET-NEW000-NEW000")
    assert status(drive(rotated, scope("/dev-ui/", headers=[(b"cookie", old.encode())]))) == 303


# --- rate limit --------------------------------------------------------------


def test_five_failures_lock_the_client_out_until_the_window_passes():
    clock = Clock()
    gate = AccessCodeMiddleware(EchoApp(), CODE, clock=clock)
    for _ in range(5):
        assert status(login(gate, "FLEET-WRONG0-WRONG0")) == 403
    assert status(login(gate, CODE)) == 429, "even the right code waits once locked"
    clock.now += 16 * 60
    assert status(login(gate, CODE)) == 303


def test_failures_are_counted_per_forwarded_client():
    gate = AccessCodeMiddleware(EchoApp(), CODE)
    for _ in range(5):
        sc = scope("/access", method="POST", headers=[(b"x-forwarded-for", b"198.51.100.7, 10.0.0.1")])
        drive(gate, sc, b"code=nope")
    other = scope("/access", method="POST", headers=[(b"x-forwarded-for", b"198.51.100.8")])
    assert status(drive(gate, other, f"code={quote(CODE)}".encode())) == 303


# --- wiring ------------------------------------------------------------------


def test_create_app_puts_the_gate_inside_the_iap_layer(monkeypatch):
    from google.adk.cli import fast_api

    from freight_fleet import devui

    monkeypatch.setattr(fast_api, "get_fast_api_app", lambda **kw: EchoApp())
    monkeypatch.setenv("FREIGHT_IAP_AUDIENCE", "/projects/1/locations/r/services/s")
    monkeypatch.setenv("FREIGHT_CHAT_ACCESS_CODE", CODE)
    app = devui.create_app()
    assert isinstance(app, devui.IapIdentityMiddleware)
    assert isinstance(app.app, AccessCodeMiddleware) and app.app.code == CODE


def test_code_mode_never_serves_a_homepage():
    """An invite code alone is the shape behind IAP: something else owns `/`."""
    gate = AccessCodeMiddleware(EchoApp(), CODE)
    assert status(drive(gate, scope("/"))) == 303


# --- demo login (users mode) -------------------------------------------------

from freight_fleet.access import hash_password, load_users, mint_users, verify_password


def users_gate(inner=None, clock=None):
    table, passwords = mint_users(["Judge1", "judge2"])
    kw = {"clock": clock} if clock else {}
    return AccessCodeMiddleware(inner or EchoApp(), users=table, **kw), passwords


def user_login(gate, username, password, nxt="/dev-ui/"):
    form = f"username={quote(username)}&password={quote(password)}&next={quote(nxt, safe='')}".encode()
    return drive(gate, scope("/access", method="POST"), form)


def test_password_hashes_verify_and_are_salted():
    h1, h2 = hash_password("s3cret"), hash_password("s3cret")
    assert h1 != h2 and h1.startswith("scrypt$")
    assert verify_password("s3cret", h1) and not verify_password("S3cret", h1)
    assert not verify_password("s3cret", "garbage") and not verify_password("x", "md5$a$b$c$d$e")


def test_load_users_normalizes_names_and_rejects_nonsense():
    assert load_users(None) is None and load_users("  ") is None
    assert load_users('{" Judge1 ": "scrypt$x"}') == {"judge1": "scrypt$x"}
    for bad in ("[]", "{}", '"str"'):
        try:
            load_users(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_users_mode_form_asks_for_username_and_password():
    gate, _ = users_gate()
    page = drive(gate, scope("/access"))[1]["body"].decode()
    assert gate.mode == "gated"
    assert 'name="username"' in page and 'name="password"' in page and 'name="code"' not in page
    assert "Continue with Google" not in page, "no client configured, no Google panel"
    assert "<script" not in page


def test_users_mode_refuses_wrong_password_and_unknown_user_alike():
    gate, pw = users_gate()
    assert status(user_login(gate, "judge1", pw["judge1"] + "x")) == 403
    assert status(user_login(gate, "nobody", pw["judge1"])) == 403
    assert status(drive(gate, scope("/run", method="POST"), b"{}")) == 403


def test_users_mode_login_pins_the_username_into_every_user_id():
    inner = EchoApp()
    gate, pw = users_gate(inner)
    sent = user_login(gate, " JUDGE1 ", pw["judge1"])
    assert status(sent) == 303
    jar = cookie_from(sent).encode()

    class Recorder(EchoApp):
        async def __call__(self, sc, receive, send):
            self.seen = sc
            msg = await receive()
            self.body = msg.get("body", b"")
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

    rec = Recorder()
    gate.app = rec
    sc = scope("/apps/freight_ops/users/whatever/sessions", headers=[(b"cookie", jar)])
    drive(gate, sc)
    assert rec.seen["path"] == "/apps/freight_ops/users/judge1/sessions"
    drive(gate, scope("/run", method="POST", headers=[(b"cookie", jar)]), b'{"user_id": "spoof"}')
    assert json.loads(rec.body)["user_id"] == "judge1"


def test_users_cookie_is_bound_to_the_user_table():
    gate, pw = users_gate()
    jar = cookie_from(user_login(gate, "judge1", pw["judge1"]))
    forged = f"{COOKIE}={gate.issue('judge3')}"  # signed with the real key, for a name not in the table
    assert status(drive(gate, scope("/dev-ui/", headers=[(b"cookie", forged.encode())]))) == 303
    rotated, _ = users_gate()  # a new table: different key, every old cookie dies
    assert status(drive(rotated, scope("/dev-ui/", headers=[(b"cookie", jar.encode())]))) == 303


def test_a_code_alongside_users_is_kept_for_the_google_door_only():
    table, _ = mint_users(["judge1"])
    gate = AccessCodeMiddleware(EchoApp(), code="FLEET-1", users=table)
    assert gate.mode == "gated" and gate.code == "FLEET-1"
    # the password form does not take a code, and a code alone opens nothing
    assert status(login(gate, "FLEET-1")) == 403


# --- the one door: homepage, sign-out, identity header, Google -----------------

from urllib.parse import parse_qs, urlparse

from freight_fleet.access import GoogleSignIn


class Recorder(EchoApp):
    """Echoes, and keeps every scope so a test can read what reached the app."""

    def __init__(self) -> None:
        super().__init__()
        self.scopes: list[dict] = []

    async def __call__(self, scope, receive, send):
        self.scopes.append(scope)
        await super().__call__(scope, receive, send)


def google_gate(code=CODE, exchange=None, clock=None):
    table, passwords = mint_users(["judge1"])
    g = GoogleSignIn("cid.apps.googleusercontent.com", "secret", "https://fleet.test/auth/google/callback",
                     exchange=exchange or (lambda c: {"email": "Judge@Gmail.test", "email_verified": True}))
    kw = {"clock": clock} if clock else {}
    inner = Recorder()
    return AccessCodeMiddleware(inner, code=code, users=table, google=g, **kw), inner, passwords


def state_of(start: list[dict]) -> str:
    return parse_qs(urlparse(header(start, b"location")).query)["state"][0]


def test_the_gated_root_is_a_homepage_and_the_desk_needs_a_login():
    gate, inner, _ = google_gate()
    sent = drive(gate, scope("/"))
    assert status(sent) == 200 and inner.hits == 0
    page = sent[1]["body"].decode()
    assert "Sign in" in page and 'href="/access"' in page and 'href="/privacy"' in page and "<script" not in page
    assert header(drive(gate, scope("/desk")), b"location").startswith("/access?next=%2Fdesk")
    # the privacy page and the probes need no cookie; the app sees no identity header
    assert status(drive(gate, scope("/privacy", headers=[(b"x-fleet-identity", b"forged")]))) == 200
    assert not any(k == b"x-fleet-identity" for k, _ in inner.scopes[-1]["headers"])


def test_the_login_page_offers_both_doors():
    gate, _, _ = google_gate()
    page = drive(gate, scope("/access"))[1]["body"].decode()
    assert 'name="username"' in page and 'action="/auth/google"' in page and 'name="code"' in page
    assert "Continue with Google" in page and "<script" not in page


def test_a_demo_login_sets_the_identity_header_and_strips_a_forged_one():
    gate, inner, passwords = google_gate()
    jar = cookie_from(user_login(gate, "judge1", passwords["judge1"]))
    drive(gate, scope("/desk", headers=[(b"cookie", jar.encode()), (b"x-fleet-identity", b"root")]))
    hdrs = dict(inner.scopes[-1]["headers"])
    assert hdrs[b"x-fleet-identity"] == b"judge1" and hdrs[b"x-fleet-kind"] == b"u"
    page = drive(gate, scope("/", headers=[(b"cookie", jar.encode())]))[1]["body"].decode()
    assert "Signed in as <strong>judge1" in page and 'href="/desk"' in page


def test_sign_out_clears_the_cookie_and_goes_home():
    gate, _, _ = google_gate()
    sent = drive(gate, scope("/logout"))
    assert status(sent) == 303 and header(sent, b"location") == "/"
    assert "Max-Age=0" in header(sent, b"set-cookie")


def test_google_needs_the_invite_code_before_the_round_trip():
    gate, inner, _ = google_gate()
    sent = drive(gate, scope("/auth/google", method="POST"), b"code=WRONG&next=%2Fchat")
    assert status(sent) == 403 and inner.hits == 0
    sent = drive(gate, scope("/auth/google", method="POST"), f"code={quote(CODE)}&next=%2Fchat".encode())
    assert status(sent) == 303
    loc = header(sent, b"location")
    assert loc.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "client_id=cid.apps.googleusercontent.com" in loc and "state=" in loc and "scope=openid+email" in loc


def test_the_callback_verifies_state_then_pins_the_verified_email():
    clock = Clock()
    gate, inner, _ = google_gate(clock=clock)
    start = drive(gate, scope("/auth/google", method="POST"), f"code={quote(CODE)}&next=%2Fchat".encode())
    state = state_of(start)

    forged = drive(gate, scope("/auth/google/callback", query=b"code=abc&state=nonsense.sig"))
    assert status(forged) == 403 and header(forged, b"set-cookie") is None

    sent = drive(gate, scope("/auth/google/callback", query=f"code=abc&state={quote(state)}".encode()))
    assert status(sent) == 303 and header(sent, b"location") == "/chat"
    jar = cookie_from(sent)
    assert gate.parse(jar.split("=", 1)[1]) == ("g", "judge@gmail.test")
    drive(gate, scope("/apps/freight_ops/users/me/sessions", headers=[(b"cookie", jar.encode())]))
    assert inner.scopes[-1]["path"] == "/apps/freight_ops/users/judge%40gmail.test/sessions"
    assert dict(inner.scopes[-1]["headers"])[b"x-fleet-identity"] == b"judge@gmail.test"

    clock.now += 11 * 60
    late = drive(gate, scope("/auth/google/callback", query=f"code=abc&state={quote(state)}".encode()))
    assert status(late) == 403, "a state older than ten minutes is refused"


def test_an_unverified_or_failed_google_account_is_refused():
    gate, _, _ = google_gate(exchange=lambda c: {"email": "x@y.test", "email_verified": False})
    start = drive(gate, scope("/auth/google", method="POST"), f"code={quote(CODE)}".encode())
    sent = drive(gate, scope("/auth/google/callback", query=f"code=abc&state={quote(state_of(start))}".encode()))
    assert status(sent) == 403 and header(sent, b"set-cookie") is None

    def boom(c):
        raise ValueError("nope")

    gate, _, _ = google_gate(exchange=boom)
    start = drive(gate, scope("/auth/google", method="POST"), f"code={quote(CODE)}".encode())
    sent = drive(gate, scope("/auth/google/callback", query=f"code=abc&state={quote(state_of(start))}".encode()))
    assert status(sent) == 403


def test_rotating_the_client_id_invalidates_google_cookies():
    gate, _, _ = google_gate()
    jar = gate.issue("judge@gmail.test", "g")
    assert gate.session(jar) == "judge@gmail.test"
    table, _ = mint_users(["judge1"])
    rotated = AccessCodeMiddleware(EchoApp(), code=CODE, users=table,
                                   google=GoogleSignIn("other-client", "s", "https://fleet.test/cb"))
    assert rotated.session(jar) is None
