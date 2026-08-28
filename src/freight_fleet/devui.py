"""The ADK dev UI, served behind an IAP identity middleware.

This is the CHAT surface — a separate Cloud Run service from the operator
console, and separate on purpose. `adk web` does not only serve a chat box: the
same app carries the eval runner, the trace viewer, the agent builder and the
artifact endpoints, all of which read and write server state with no
authorisation of their own. Any of those on the public URL would let a passer-by
run the eval against a paid model, read another visitor's conversation, or edit
the agent. So the public read-only console (`console.py`) stays public, and this
one lives behind IAP.

Identity comes from the signed JWT and from nothing else. ADK reads `user_id`
out of the URL path, the JSON body or the query string — all three are chosen by
the browser, and the dev UI's own sidebar lets a visitor type whatever they like
into it. IAP's `x-goog-iap-jwt-assertion` is the only value in the request that
IAP signed; `x-goog-authenticated-user-email` is NOT (it is unsigned, and a
client can set it if it ever reaches the container by another route). So the
middleware verifies the assertion and then OVERWRITES every `user_id` on the way
in. Rewriting unconditionally, rather than checking that the claimed id matches,
is deliberate: a check has a branch that can be wrong, an overwrite does not,
and the browser has no legitimate reason to pick its own id here.

`FREIGHT_SESSIONS_DB` must be the same URI form `cli.py` uses
(`sqlite+aiosqlite://...` locally, `postgresql+asyncpg://...` on Cloud SQL) —
one durable store shared by chat, sweep and this UI, opened through the same
`DatabaseSessionService`. Never a bare `sqlite://`: ADK selects a different
service for that scheme with a colliding table layout, so the failure looks like
"my sessions are gone" rather than like a configuration error. Sessions are
still scoped per app name, and the dev UI's app name is the agent directory
(`freight_ops`) while `cli.py` uses `freight_fleet` — same database, different
conversations, which is what you want.

Environment:

* `FREIGHT_IAP_AUDIENCE` — the IAP audience string,
  `/projects/PROJECT_NUMBER/locations/REGION/services/SERVICE`. UNSET means
  local development: the middleware passes every request through untouched and
  the UI's self-asserted user id stands. Never leave it unset in a deployment.
* `FREIGHT_SESSIONS_DB` — session store URI (default local SQLite).
* `FREIGHT_AGENTS_DIR` — the ADK app directory (default `/app/agents`, the
  image's copy of `deploy/agents/`).
* `PORT` — Cloud Run's port.

Served with::

    uvicorn freight_fleet.devui:app_factory --factory --host 0.0.0.0 --port $PORT

`--factory` rather than a module-level `app`, because building ADK's FastAPI app
scans the agent directory and opens the session database at import time. As a
factory, importing this module to test the middleware costs nothing and needs
neither an agents directory nor a database.

Governance state on this service is deliberately disposable: the ledger and
approval store sit on the container's own disk, so a hold raised in chat is
visible in the reply and gone with the container. It is NOT the governed record
the ops console approves — see `docs/DEPLOY.md` §4d.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode

#: IAP signs its assertions with its own key set, not with Google's general
#: OAuth certs, so the URL is part of the verification and not a detail.
IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"

#: The one header IAP signs. Everything else it adds is advisory.
IAP_HEADER = b"x-goog-iap-jwt-assertion"

#: The `user_id` path segment ADK routes on: `/apps/<app>/users/<user_id>/...`
#: and `/dev/apps/<app>/users/<user_id>/...`. `[^/]*` matches an empty segment
#: too, so `/users//sessions` is rewritten rather than passed through.
_USER_SEGMENT = re.compile(r"(?<=/users/)[^/]*")

#: The two JSON endpoints that carry `user_id` in the body, and the websocket
#: that carries it in the query string.
_BODY_ROUTES = ("/run", "/run_sse")
_QUERY_ROUTES = ("/run_live",)

Verifier = Callable[[str, str], str]


def verify_iap_jwt(token: str, audience: str) -> str:
    """Return the verified caller's email, or raise `ValueError`.

    The audience is passed in rather than derived, because guessing it wrong is
    the one mistake that fails OPEN in the obvious implementations: an
    unverified audience means a valid IAP token minted for somebody else's
    service is accepted here. `docs/DEPLOY.md` §4d prints the exact string.

    `verify_token` fetches and caches the IAP key set over HTTPS on first call,
    which is why the tests inject a stub verifier instead of calling this.
    """
    import google.auth.transport.requests
    from google.oauth2 import id_token

    try:
        claims = id_token.verify_token(
            token,
            google.auth.transport.requests.Request(),
            audience=audience,
            certs_url=IAP_CERTS_URL,
        )
    except Exception as exc:  # google-auth raises several unrelated types here
        raise ValueError(f"IAP assertion rejected: {exc}") from exc
    email = claims.get("email")
    if not email:
        # A signed assertion with no email is a service-to-service token, not a
        # person. There is nobody to scope a session to, so it is refused.
        raise ValueError("IAP assertion carries no email claim")
    return str(email)


class IapIdentityMiddleware:
    """Pure-ASGI wrapper that pins every request's `user_id` to the IAP identity.

    Pure ASGI rather than Starlette's `BaseHTTPMiddleware` because the body
    rewrite has to happen on the receive channel, and `BaseHTTPMiddleware`
    buffers streaming responses — which would break `/run_sse`, the endpoint the
    chat box actually streams through.
    """

    def __init__(self, app: Any, audience: str | None, verifier: Verifier = verify_iap_jwt) -> None:
        self.app = app
        self.audience = audience
        self.verifier = verifier

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        # `lifespan`, and anything else ADK grows later, has no identity to pin.
        if scope["type"] not in ("http", "websocket") or self.audience is None:
            await self.app(scope, receive, send)
            return

        token = _header(scope, IAP_HEADER)
        email: str | None = None
        if token:
            try:
                email = self.verifier(token, self.audience)
            except ValueError:
                email = None
        if not email:
            await _refuse(scope, receive, send)
            return

        scope = dict(scope)
        path: str = scope.get("path", "")
        quoted = quote(email, safe="")
        scope["path"] = _USER_SEGMENT.sub(quoted, path)
        raw_path = scope.get("raw_path")
        if raw_path:
            rewritten = _USER_SEGMENT.sub(quoted, raw_path.decode("latin-1"))
            scope["raw_path"] = rewritten.encode("latin-1")

        if path.endswith(_QUERY_ROUTES):
            scope["query_string"] = _with_user_id(scope.get("query_string", b""), email)

        if scope["type"] == "http" and scope.get("method") == "POST" and path.endswith(_BODY_ROUTES):
            receive = await _rewrite_body(scope, receive, email)

        await self.app(scope, receive, send)


def _header(scope: dict, name: bytes) -> str | None:
    for key, value in scope.get("headers", ()):
        if key.lower() == name:
            return value.decode("latin-1")
    return None


async def _refuse(scope: dict, receive: Callable, send: Callable) -> None:
    """Refuse in the protocol the caller is speaking.

    A websocket client handed an HTTP 403 sees a generic handshake failure;
    closing with 4403 after reading the connect message gives the browser a code
    it can show. The `await receive()` is not optional — the ASGI server holds
    the `websocket.connect` message until someone reads it.
    """
    if scope["type"] == "websocket":
        await receive()
        await send({"type": "websocket.close", "code": 4403})
        return
    body = json.dumps({"detail": "IAP identity required"}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _with_user_id(query_string: bytes, email: str) -> bytes:
    """Replace — or add — `user_id` in a query string, preserving everything else."""
    pairs = parse_qsl(query_string.decode("latin-1"), keep_blank_values=True)
    replaced = [(k, email if k == "user_id" else v) for k, v in pairs]
    if not any(k == "user_id" for k, _ in replaced):
        replaced.append(("user_id", email))
    return urlencode(replaced).encode("latin-1")


async def _rewrite_body(
    scope: dict, receive: Callable[[], Awaitable[dict]], email: str
) -> Callable[[], Awaitable[dict]]:
    """Buffer the request body, pin `user_id`, and hand back a replacement receive.

    The whole body is read here rather than streamed because `/run` and
    `/run_sse` take one small JSON object and the field can be anywhere in it.
    A body that is not a JSON object is passed through untouched: ADK's own
    validation gives a better error than anything this layer could invent, and a
    middleware that swallowed malformed input would hide it.
    """
    chunks: list[bytes] = []
    more = True
    while more:
        message = await receive()
        if message["type"] != "http.request":
            # A disconnect mid-body: replay it and let the app unwind.
            return _replay(message)
        chunks.append(message.get("body", b""))
        more = message.get("more_body", False)
    body = b"".join(chunks)

    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        payload = None
    if isinstance(payload, dict):
        payload["user_id"] = email
        body = json.dumps(payload).encode()
        # Content-length was measured against the old body. FastAPI reads the
        # ASGI messages, but anything downstream that trusts the header (a
        # proxy, a logger, ADK's own request models in a later release) would
        # see a truncated object.
        kept = [(k, v) for k, v in scope.get("headers", ()) if k.lower() != b"content-length"]
        scope["headers"] = [*kept, (b"content-length", str(len(body)).encode())]

    return _replay({"type": "http.request", "body": body, "more_body": False})


def _replay(message: dict) -> Callable[[], Awaitable[dict]]:
    """A receive channel that yields `message` once, then reports a disconnect."""
    sent = False

    async def receive() -> dict:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return message

    return receive


def create_app() -> Any:
    """Build the ADK dev UI app wrapped in the identity middleware.

    `get_fast_api_app` is imported inside the function so that this module can
    be imported — by the tests, by the seals — without ADK scanning an agents
    directory or opening a session database.
    """
    from google.adk.cli.fast_api import get_fast_api_app

    agents_dir = os.environ.get("FREIGHT_AGENTS_DIR", "/app/agents")
    sessions_db = os.environ.get("FREIGHT_SESSIONS_DB", "sqlite+aiosqlite:///./data/sessions.db")
    port = int(os.environ.get("PORT", "8080"))
    app = get_fast_api_app(
        agents_dir=agents_dir,
        web=True,
        session_service_uri=sessions_db,
        # Cloud Run routes to the container on every interface, and ADK compares
        # the request's Host against `bind_host` in its DNS-rebinding check, so
        # the loopback default would refuse the load balancer's own requests.
        # IAP, not the bind address, is this service's boundary.
        host="0.0.0.0",
        bind_host="0.0.0.0",
        port=port,
    )
    return IapIdentityMiddleware(app, audience=os.environ.get("FREIGHT_IAP_AUDIENCE"))


def app_factory() -> Any:
    """Entry point for `uvicorn freight_fleet.devui:app_factory --factory`."""
    return create_app()
