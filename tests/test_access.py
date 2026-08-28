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
        assert header(login(gate, CODE, evil), b"location") == "/"


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
