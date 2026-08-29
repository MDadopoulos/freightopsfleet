"""The deployed application — one process, one door, everything behind it.

Before this module the project ran as four Cloud Run services: a read-only
console, a sandbox, a chat behind IAP and a chat with a demo login. Each was
defensible on its own and together they were a maze. This is the one shape:

    login (access.py)
      └─ this app
           ├─ the operator console's routes   (console.py — never imports ADK)
           ├─ /chat                            (chatui.py — the one page with JS)
           ├─ /upload                          (a document into the workspace, transcribed)
           ├─ /sweep/run                       (start the unattended job now)
           └─ everything else → ADK's API      (/run_sse, /apps/…, /dev-ui/)

The console module still imports no model code — the seals prove it — because
composition happens HERE: this module imports both halves and mounts them. So
the console can be run on its own for a local operator exactly as before, and
the seal on it means exactly what it meant.

State is SHARED on this service, deliberately: `FREIGHT_LEDGER_PATH` and
`FREIGHT_APPROVALS_PATH` point at the mounted state bucket, so a hold raised in
chat is the same hold the desk approves. That is the loop the four-service
shape could not close. The cost is that every gate decision is a small write
through GCS-FUSE, which this repo has seen lose concurrent appends (the sweep,
DEPLOY.md §5) — so the service runs with `--max-instances 1`, the sweep still
publishes once at its end, and the desk's reconcile page is the check.

Uploads go to two places: the fleet's workspace jail (so the agents can read
them now) and `FREIGHT_UPLOADS_DIR` on the state bucket (so they survive the
container). On start, whatever is in the durable copy is put back into the
workspace — a restart forgets nothing a judge uploaded.

Served with::

    uvicorn freight_fleet.webapp:app_factory --factory --host 0.0.0.0 --port $PORT
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

# Module-level on purpose: FastAPI resolves the `Request` annotation on the
# route functions through this module's globals (annotations are strings
# here), and a locally imported name reads as a missing query parameter.
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

log = logging.getLogger("freight_fleet.webapp")

#: What a judge may upload: what `ingest` can read, and nothing else.
UPLOAD_SUFFIXES = (".pdf", ".png", ".jpg", ".jpeg")
UPLOAD_MAX_BYTES = 6_000_000  # `ingest.MAX_BYTES`, the model's inline cap
UPLOADS_PER_HOUR = 10
SWEEP_COOLDOWN = 10 * 60

_NAME_RX = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(value: str, limit: int = 48) -> str:
    """A filesystem-safe fragment of an identity or a filename."""
    cleaned = _NAME_RX.sub("-", value).strip("-.") or "x"
    return cleaned[:limit]


def uploads_dir() -> Path | None:
    raw = os.environ.get("FREIGHT_UPLOADS_DIR", "").strip()
    return Path(raw) if raw else None


def restore_uploads(workspace: Path) -> int:
    """Put the durable copy of every upload back into the workspace jail.
    Idempotent; returns how many files were restored."""
    durable = uploads_dir()
    if durable is None or not durable.is_dir():
        return 0
    n = 0
    for sub in ("raw", "inbox"):
        src = durable / sub
        if not src.is_dir():
            continue
        for path in src.rglob("*"):
            if path.is_file():
                target = workspace / sub / path.relative_to(src)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
                n += 1
    return n


class _Limiter:
    def __init__(self, per_hour: int) -> None:
        self.per_hour = per_hour
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        window = self.hits[key]
        now = time.time()
        while window and window[0] < now - 3600:
            window.popleft()
        if len(window) >= self.per_hour:
            return False
        window.append(now)
        return True


_uploads = _Limiter(UPLOADS_PER_HOUR)
_last_sweep_trigger = 0.0


def run_job(job: str) -> tuple[bool, str]:
    """Start one execution of a Cloud Run Job by its full resource name, via the
    REST API with the service's own credentials. Returns (started, detail).

    A job with an execution still running is not started again — a second
    sweep racing the first would write the same holds twice.
    """
    import google.auth
    import google.auth.transport.requests

    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    session = google.auth.transport.requests.AuthorizedSession(credentials)
    base = f"https://run.googleapis.com/v2/{job}"
    listing = session.get(f"{base}/executions", params={"pageSize": 10}, timeout=20)
    if listing.ok:
        for execution in listing.json().get("executions", []):
            if not execution.get("completionTime"):
                return False, "busy"
    started = session.post(f"{base}:run", json={}, timeout=20)
    if not started.ok:
        return False, f"{started.status_code} {started.text[:200]}"
    return True, started.json().get("name", "")


def create_app() -> Any:
    """Build the whole thing for a deployment: ADK's app from the agents
    directory and the session store, then everything else around it."""
    from google.adk.cli.fast_api import get_fast_api_app

    agents_dir = os.environ.get("FREIGHT_AGENTS_DIR", "/app/agents")
    sessions_db = os.environ.get("FREIGHT_SESSIONS_DB", "sqlite+aiosqlite:///./data/sessions.db")
    port = int(os.environ.get("PORT", "8080"))
    adk = get_fast_api_app(
        agents_dir=agents_dir,
        web=True,
        session_service_uri=sessions_db,
        # Cloud Run routes to the container on every interface, and ADK compares
        # the request's Host against `bind_host` in its DNS-rebinding check.
        host="0.0.0.0",
        bind_host="0.0.0.0",
        port=port,
    )
    return build_app(adk)


def build_app(adk: Any) -> Any:
    """Everything around ADK's app, given ADK's app. Separate from `create_app`
    so the tests can hand in a stand-in and drive every route of ours without
    an agents directory, a database or a model. Imports happen inside so that
    importing this module — for the seals — costs nothing."""
    from . import console
    from .access import AccessCodeMiddleware, GoogleSignIn, load_users
    from .chatui import page
    from .tools import workspace

    restored = restore_uploads(workspace.WORKSPACE_ROOT)
    if restored:
        log.info("restored %d uploaded file(s) into the workspace", restored)

    app = FastAPI(title="Freight Ops Fleet", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def _no_store(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.get("/chat", response_class=HTMLResponse, include_in_schema=False)
    def chat(request: Request) -> HTMLResponse:
        return HTMLResponse(page(identity=request.headers.get("x-fleet-identity", "")))

    @app.post("/upload", include_in_schema=False)
    async def upload(request: Request, name: str = "") -> JSONResponse:
        """One document into the workspace, transcribed on the spot.

        The body is the raw file (no multipart: one file, one request, nothing
        to parse). It lands under `raw/uploads/<who>/` in the jail — which is
        where `ingest` looks — and its transcription under `inbox/`, with the
        same provenance marker every ingested page carries.
        """
        from . import ingest

        who = _slug(request.headers.get("x-fleet-identity", "") or "anonymous")
        if not _uploads.allow(who):
            return JSONResponse({"status": "error", "message": f"{UPLOADS_PER_HOUR} uploads an hour per person; try later"},
                                status_code=429)
        filename = _slug(Path(name or "").name)
        suffix = Path(filename).suffix.lower()
        if suffix not in UPLOAD_SUFFIXES:
            return JSONResponse({"status": "error", "message": "PDF, PNG or JPEG only"}, status_code=415)
        data = await request.body()
        if not data:
            return JSONResponse({"status": "error", "message": "empty upload"}, status_code=400)
        if len(data) > UPLOAD_MAX_BYTES:
            return JSONResponse({"status": "error", "message": f"larger than {UPLOAD_MAX_BYTES // 1_000_000} MB"},
                                status_code=413)

        rel = Path("uploads") / who / filename
        target = workspace.WORKSPACE_ROOT / "raw" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        durable = uploads_dir()
        if durable is not None:
            keep = durable / "raw" / rel
            keep.parent.mkdir(parents=True, exist_ok=True)
            keep.write_bytes(data)

        model = os.environ.get("FREIGHT_MODEL", "gemini-3.7-flash")
        import asyncio

        report = await asyncio.to_thread(
            ingest.run, workspace.WORKSPACE_ROOT, ingest.transcribe_with_genai(model),
            only=rel.as_posix(), force=True, model_label=model,
        )
        if report.failed:
            _item, reason = report.failed[0]
            return JSONResponse({"status": "error", "message": reason, "raw": f"raw/{rel.as_posix()}"},
                                status_code=502)
        if not report.written:
            return JSONResponse({"status": "error", "message": "nothing was transcribed"}, status_code=502)
        written = report.written[0].target
        inbox_rel = written.relative_to(workspace.WORKSPACE_ROOT).as_posix()
        if durable is not None:
            keep = durable / inbox_rel
            keep.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(written, keep)
        return JSONResponse({
            "status": "ok",
            "raw": f"raw/{rel.as_posix()}",
            "inbox": inbox_rel,
            "chars": len(written.read_text(encoding="utf-8")),
        })

    @app.post("/sweep/run", include_in_schema=False)
    def sweep_run() -> RedirectResponse:
        """Start the unattended job now. The desk renders the button only when
        `FREIGHT_SWEEP_JOB` is set; this is the half that holds credentials."""
        global _last_sweep_trigger
        job = os.environ.get("FREIGHT_SWEEP_JOB", "").strip()
        if not job:
            return RedirectResponse("/desk?run=unavailable", status_code=303)
        if time.time() - _last_sweep_trigger < SWEEP_COOLDOWN:
            return RedirectResponse("/desk?run=busy", status_code=303)
        try:
            started, detail = run_job(job)
        except Exception:  # the desk says "not started"; the log says why
            log.exception("sweep trigger failed")
            return RedirectResponse("/desk?run=error", status_code=303)
        if not started:
            log.warning("sweep not started: %s", detail)
            return RedirectResponse("/desk?run=busy" if detail == "busy" else "/desk?run=error", status_code=303)
        _last_sweep_trigger = time.time()
        log.info("sweep started: %s", detail)
        return RedirectResponse("/desk?run=started", status_code=303)

    # The console's own routes, then ADK's app for everything unmatched. Order
    # is the whole trick: FastAPI tries routes in registration order, and a
    # mount at "/" is the catch-all only because it is registered last.
    app.include_router(console.app.router)
    app.mount("/", adk)

    gated = AccessCodeMiddleware(
        app,
        code=os.environ.get("FREIGHT_CHAT_ACCESS_CODE"),
        users=load_users(os.environ.get("FREIGHT_CHAT_USERS")),
        google=GoogleSignIn.from_env(),
    )
    return gated


def app_factory() -> Any:
    """Entry point for `uvicorn freight_fleet.webapp:app_factory --factory`."""
    return create_app()


def describe() -> dict[str, Any]:
    """What this deployment has switched on — for a smoke test, never a page."""
    return {
        "users": bool(os.environ.get("FREIGHT_CHAT_USERS")),
        "google": bool(os.environ.get("FREIGHT_GOOGLE_CLIENT_ID")),
        "code": bool(os.environ.get("FREIGHT_CHAT_ACCESS_CODE")),
        "sweep_job": os.environ.get("FREIGHT_SWEEP_JOB", ""),
        "uploads": str(uploads_dir() or ""),
        "mail": os.environ.get("FREIGHT_MAIL_TRANSPORT", "spool"),
    }


if __name__ == "__main__":  # pragma: no cover
    print(json.dumps(describe(), indent=1))
