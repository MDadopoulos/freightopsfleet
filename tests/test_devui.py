"""What the IAP middleware does to a request before ADK ever sees it.

Every test here drives the middleware directly with a hand-built ASGI scope and
hand-written receive/send coroutines. No server is started, no ADK app is built
and `verify_iap_jwt` is never called: the real verifier fetches Google's IAP key
set over HTTPS, so a test that reached it would be a test that fails on a train.
The stub verifier is the same seam production uses — the constructor argument —
which is why it exists.
"""

from __future__ import annotations

import asyncio
import json

from freight_fleet.devui import IapIdentityMiddleware, create_app

TOKEN_HEADER = (b"x-goog-iap-jwt-assertion", b"a.signed.assertion")
EMAIL = "judge@example.com"
QUOTED = "judge%40example.com"


def stub_verifier(token: str, audience: str) -> str:
    """Stand-in for a verified assertion. Records nothing; the tests assert on
    the effect, which is the only thing the rest of the system can observe."""
    return EMAIL


def refusing_verifier(token: str, audience: str) -> str:
    raise ValueError("expired assertion")


class EchoApp:
    """Inner app: remembers what the middleware handed it.

    It answers HTTP with a 200 so a passing request is distinguishable from a
    refused one even when the scope assertions would both pass.
    """

    def __init__(self) -> None:
        self.scope: dict | None = None
        self.body = b""

    async def __call__(self, scope, receive, send):
        self.scope = scope
        if scope["type"] == "http":
            chunks = []
            more = True
            while more:
                message = await receive()
                if message["type"] != "http.request":
                    break
                chunks.append(message.get("body", b""))
                more = message.get("more_body", False)
            self.body = b"".join(chunks)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})
        else:
            await receive()
            await send({"type": "websocket.accept"})


def http_scope(path: str, *, method: str = "GET", headers=(), query: bytes = b"") -> dict:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": query,
        "headers": list(headers),
    }


def drive(app, scope: dict, body: bytes | None = None) -> list[dict]:
    """Run one request through an ASGI app and return the messages it sent."""
    sent: list[dict] = []

    incoming = [{"type": "websocket.connect"}]
    if scope["type"] == "http":
        incoming = [{"type": "http.request", "body": body or b"", "more_body": False}]

    async def receive():
        if incoming:
            return incoming.pop(0)
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent


# --- audience unset: local development passes straight through ---------------


def test_no_audience_passes_everything_through():
    inner = EchoApp()
    app = IapIdentityMiddleware(inner, audience=None, verifier=refusing_verifier)
    scope = http_scope("/apps/freight_ops/users/user/sessions")
    sent = drive(app, scope)

    assert sent[0]["status"] == 200
    assert inner.scope is not None
    # Untouched, including the id the browser chose: unset audience is the local
    # dev mode, and rewriting there would break `adk web` on a laptop.
    assert inner.scope["path"] == "/apps/freight_ops/users/user/sessions"


def test_no_audience_leaves_a_run_body_alone():
    inner = EchoApp()
    app = IapIdentityMiddleware(inner, audience=None)
    payload = {"app_name": "freight_ops", "user_id": "user", "session_id": "s"}
    drive(app, http_scope("/run", method="POST"), json.dumps(payload).encode())

    assert json.loads(inner.body) == payload


def test_lifespan_scope_passes_through_even_with_an_audience():
    """A startup message has no headers and no identity; refusing it would stop
    the server from booting at all."""
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    app = IapIdentityMiddleware(inner, audience="/projects/1/x", verifier=refusing_verifier)
    asyncio.run(app({"type": "lifespan"}, None, None))
    assert seen == ["lifespan"]


# --- audience set, no usable assertion: refuse in the caller's protocol ------


def test_missing_assertion_is_a_403_json_body():
    inner = EchoApp()
    app = IapIdentityMiddleware(inner, audience="/projects/1/x", verifier=stub_verifier)
    sent = drive(app, http_scope("/apps/freight_ops/users/user/sessions"))

    assert sent[0]["status"] == 403
    assert json.loads(sent[1]["body"]) == {"detail": "IAP identity required"}
    assert inner.scope is None, "the inner app must never be reached"


def test_a_rejected_assertion_is_also_a_403():
    inner = EchoApp()
    app = IapIdentityMiddleware(inner, audience="/projects/1/x", verifier=refusing_verifier)
    sent = drive(app, http_scope("/run", method="POST", headers=[TOKEN_HEADER]), b"{}")

    assert sent[0]["status"] == 403
    assert inner.scope is None


def test_missing_assertion_closes_a_websocket_with_4403():
    inner = EchoApp()
    app = IapIdentityMiddleware(inner, audience="/projects/1/x", verifier=stub_verifier)
    scope = {
        "type": "websocket",
        "path": "/run_live",
        "raw_path": b"/run_live",
        "query_string": b"user_id=user&session_id=s",
        "headers": [],
    }
    sent = drive(app, scope)

    assert sent == [{"type": "websocket.close", "code": 4403}]
    assert inner.scope is None


# --- audience set, valid assertion: rewrite everything ----------------------


def test_the_user_path_segment_becomes_the_verified_email():
    inner = EchoApp()
    app = IapIdentityMiddleware(inner, audience="/projects/1/x", verifier=stub_verifier)
    drive(app, http_scope("/apps/freight_ops/users/user/sessions", headers=[TOKEN_HEADER]))

    assert inner.scope["path"] == f"/apps/freight_ops/users/{QUOTED}/sessions"
    assert inner.scope["raw_path"] == f"/apps/freight_ops/users/{QUOTED}/sessions".encode()


def test_a_trailing_user_segment_is_rewritten_too():
    inner = EchoApp()
    app = IapIdentityMiddleware(inner, audience="/projects/1/x", verifier=stub_verifier)
    drive(app, http_scope("/apps/freight_ops/users/somebody", headers=[TOKEN_HEADER]))

    assert inner.scope["path"] == f"/apps/freight_ops/users/{QUOTED}"


def test_a_path_without_a_user_segment_is_left_alone():
    inner = EchoApp()
    app = IapIdentityMiddleware(inner, audience="/projects/1/x", verifier=stub_verifier)
    drive(app, http_scope("/dev-ui/", headers=[TOKEN_HEADER]))

    assert inner.scope["path"] == "/dev-ui/"


def test_run_body_user_id_is_overwritten_with_a_matching_content_length():
    inner = EchoApp()
    app = IapIdentityMiddleware(inner, audience="/projects/1/x", verifier=stub_verifier)
    payload = {"app_name": "freight_ops", "user_id": "user", "session_id": "s"}
    body = json.dumps(payload).encode()
    scope = http_scope(
        "/run",
        method="POST",
        headers=[TOKEN_HEADER, (b"content-length", str(len(body)).encode())],
    )
    drive(app, scope, body)

    received = json.loads(inner.body)
    assert received["user_id"] == EMAIL
    # The other fields survive: the middleware pins identity, it does not
    # rewrite the request.
    assert received["app_name"] == "freight_ops" and received["session_id"] == "s"
    lengths = [v for k, v in inner.scope["headers"] if k.lower() == b"content-length"]
    assert lengths == [str(len(inner.body)).encode()]


def test_run_sse_body_is_rewritten_the_same_way():
    inner = EchoApp()
    app = IapIdentityMiddleware(inner, audience="/projects/1/x", verifier=stub_verifier)
    body = json.dumps({"user_id": "user"}).encode()
    drive(app, http_scope("/run_sse", method="POST", headers=[TOKEN_HEADER]), body)

    assert json.loads(inner.body)["user_id"] == EMAIL


def test_a_run_body_with_no_user_id_gains_one():
    """The UI always sends the field, but a hand-rolled client that omits it
    must not reach the agent as an unscoped session."""
    inner = EchoApp()
    app = IapIdentityMiddleware(inner, audience="/projects/1/x", verifier=stub_verifier)
    drive(app, http_scope("/run", method="POST", headers=[TOKEN_HEADER]), b'{"session_id": "s"}')

    assert json.loads(inner.body) == {"session_id": "s", "user_id": EMAIL}


def test_a_non_json_run_body_is_passed_through_for_adk_to_reject():
    inner = EchoApp()
    app = IapIdentityMiddleware(inner, audience="/projects/1/x", verifier=stub_verifier)
    drive(app, http_scope("/run", method="POST", headers=[TOKEN_HEADER]), b"not json")

    assert inner.body == b"not json"


def test_a_chunked_run_body_is_reassembled_before_rewriting():
    """uvicorn delivers a streamed body in pieces; a middleware that read only
    the first message would truncate the request."""
    inner = EchoApp()
    app = IapIdentityMiddleware(inner, audience="/projects/1/x", verifier=stub_verifier)
    pieces = [b'{"user_id": "user", ', b'"session_id": "s"}']
    incoming = [
        {"type": "http.request", "body": pieces[0], "more_body": True},
        {"type": "http.request", "body": pieces[1], "more_body": False},
    ]

    async def receive():
        return incoming.pop(0) if incoming else {"type": "http.disconnect"}

    async def send(message):
        pass

    asyncio.run(app(http_scope("/run", method="POST", headers=[TOKEN_HEADER]), receive, send))
    assert json.loads(inner.body) == {"user_id": EMAIL, "session_id": "s"}


def test_run_live_query_string_user_id_is_rewritten():
    inner = EchoApp()
    app = IapIdentityMiddleware(inner, audience="/projects/1/x", verifier=stub_verifier)
    scope = {
        "type": "websocket",
        "path": "/run_live",
        "raw_path": b"/run_live",
        "query_string": b"user_id=user&session_id=s",
        "headers": [TOKEN_HEADER],
    }
    drive(app, scope)

    assert inner.scope["query_string"] == f"user_id={QUOTED}&session_id=s".encode()


def test_run_live_without_a_user_id_gets_one_added():
    inner = EchoApp()
    app = IapIdentityMiddleware(inner, audience="/projects/1/x", verifier=stub_verifier)
    scope = {
        "type": "websocket",
        "path": "/run_live",
        "raw_path": b"/run_live",
        "query_string": b"session_id=s",
        "headers": [TOKEN_HEADER],
    }
    drive(app, scope)

    assert inner.scope["query_string"] == f"session_id=s&user_id={QUOTED}".encode()


def test_a_get_to_run_is_not_body_rewritten():
    """Only the POST bodies carry `user_id`; buffering a GET would stall it."""
    inner = EchoApp()
    app = IapIdentityMiddleware(inner, audience="/projects/1/x", verifier=stub_verifier)
    sent = drive(app, http_scope("/run", headers=[TOKEN_HEADER]))

    assert sent[0]["status"] == 200
    assert inner.body == b""


# --- create_app wiring ------------------------------------------------------


def test_create_app_uses_the_repos_session_uri_form(monkeypatch):
    """The dev UI must open the SAME store `cli.py` opens — that is the whole
    point of a durable session on this surface — and must serve the web UI."""
    recorded: dict = {}

    def recorder(**kwargs):
        recorded.update(kwargs)
        return "adk-app"

    from google.adk.cli import fast_api

    monkeypatch.setattr(fast_api, "get_fast_api_app", recorder)
    monkeypatch.setenv("FREIGHT_SESSIONS_DB", "postgresql+asyncpg://fleet:pw@/sessions")
    monkeypatch.setenv("FREIGHT_AGENTS_DIR", "/app/agents")
    monkeypatch.setenv("FREIGHT_IAP_AUDIENCE", "/projects/1/locations/europe-west1/services/chat")
    monkeypatch.setenv("PORT", "8081")

    app = create_app()

    assert recorded["session_service_uri"] == "postgresql+asyncpg://fleet:pw@/sessions"
    assert recorded["web"] is True
    assert recorded["agents_dir"] == "/app/agents"
    assert recorded["port"] == 8081
    assert isinstance(app, IapIdentityMiddleware)
    # The access-code gate sits between IAP and ADK, inert when no code is set.
    assert app.app.code is None and app.app.app == "adk-app"
    assert app.audience == "/projects/1/locations/europe-west1/services/chat"


def test_create_app_without_an_audience_falls_back_to_the_cli_default(monkeypatch):
    """Unset audience is local dev, and the local store is the same SQLite file
    `cli.py` uses. Never a bare `sqlite://` — ADK selects a different service
    for that scheme with a colliding table layout."""
    recorded: dict = {}

    def recorder(**kwargs):
        recorded.update(kwargs)
        return "adk-app"

    monkeypatch.setattr("google.adk.cli.fast_api.get_fast_api_app", recorder)
    monkeypatch.delenv("FREIGHT_IAP_AUDIENCE", raising=False)
    monkeypatch.delenv("FREIGHT_SESSIONS_DB", raising=False)

    app = create_app()

    assert app.audience is None
    assert recorded["session_service_uri"] == "sqlite+aiosqlite:///./data/sessions.db"


def test_after_the_rewritten_body_the_channel_defers_to_the_client_not_a_fake_disconnect():
    """ADK polls receive() during a run to detect a departed client. The
    replacement channel must answer that poll with whatever the real client
    channel says — never with a fabricated `http.disconnect`, which aborted
    every turn with a 499 in production."""
    from freight_fleet.devui import _rewrite_body

    inbound = [{"type": "http.request", "body": b'{"user_id": "x"}', "more_body": False},
               {"type": "custom.still-connected"}]

    async def original():
        return inbound.pop(0)

    scope = http_scope("/run", method="POST")
    replacement = asyncio.run(_rewrite_body(scope, original, EMAIL))
    first = asyncio.run(replacement())
    assert json.loads(first["body"])["user_id"] == EMAIL
    assert asyncio.run(replacement()) == {"type": "custom.still-connected"}
