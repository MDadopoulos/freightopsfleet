"""The one process: console routes first, ADK last, our routes in between, one
login around all of it. ADK is a stand-in FastAPI here; nothing needs a model."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from freight_fleet import webapp
from freight_fleet.tools import workspace


def fake_adk() -> FastAPI:
    adk = FastAPI()

    @adk.get("/list-apps")
    def list_apps():
        return ["freight_ops"]

    @adk.get("/")
    def root():
        return {"adk": "root"}

    return adk


@pytest.fixture()
def world(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    (root / "inbox").mkdir(parents=True)
    monkeypatch.setattr(workspace, "WORKSPACE_ROOT", root.resolve())
    monkeypatch.setenv("FREIGHT_LEDGER_PATH", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("FREIGHT_APPROVALS_PATH", str(tmp_path / "approvals.json"))
    monkeypatch.setenv("FREIGHT_UPLOADS_DIR", str(tmp_path / "durable"))
    for var in ("FREIGHT_CHAT_USERS", "FREIGHT_CHAT_ACCESS_CODE", "FREIGHT_GOOGLE_CLIENT_ID",
                "FREIGHT_SWEEP_JOB", "FREIGHT_IAP_AUDIENCE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(webapp, "_uploads", webapp._Limiter(webapp.UPLOADS_PER_HOUR))
    monkeypatch.setattr(webapp, "_last_sweep_trigger", 0.0)
    return tmp_path, root


def stub_transcriber(monkeypatch, text="# Waybill\n\nGross 6,098 kg\n"):
    from freight_fleet import ingest

    calls: list[tuple[int, str]] = []

    def factory(model):
        def transcribe(data, mime):
            calls.append((len(data), mime))
            return text
        return transcribe

    monkeypatch.setattr(ingest, "transcribe_with_genai", factory)
    return calls


def test_console_routes_win_and_adk_takes_the_rest(world):
    client = TestClient(webapp.build_app(fake_adk()))
    assert client.get("/", follow_redirects=False).headers["location"] == "/desk"
    assert "Operator desk" in client.get("/desk").text
    assert client.get("/list-apps").json() == ["freight_ops"]
    assert "Ask the fleet" in client.get("/chat").text
    assert client.get("/desk").headers["cache-control"] == "no-store"


def test_behind_a_login_the_root_is_the_homepage(world, monkeypatch):
    from freight_fleet.access import mint_users

    table, passwords = mint_users(["judge1"])
    monkeypatch.setenv("FREIGHT_CHAT_USERS", json.dumps(table))
    client = TestClient(webapp.build_app(fake_adk()), base_url="https://testserver")  # the cookie is Secure
    home = client.get("/")
    assert home.status_code == 200 and 'href="/access"' in home.text
    assert client.get("/desk", follow_redirects=False).status_code == 303
    assert client.get("/list-apps", follow_redirects=False).status_code == 303
    r = client.post("/access", data={"username": "judge1", "password": passwords["judge1"], "next": "/chat"},
                    follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/chat").text
    assert "You are <span class=\"mono\">judge1</span>" in page


def test_upload_lands_in_the_jail_the_durable_copy_and_the_inbox(world, monkeypatch):
    tmp, root = world
    calls = stub_transcriber(monkeypatch)
    client = TestClient(webapp.build_app(fake_adk()))
    r = client.post("/upload?name=../My Waybill (1).pdf", content=b"%PDF-1.4 fake",
                    headers={"x-fleet-identity": "judge@x.test"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["raw"] == "raw/uploads/judge-x.test/My-Waybill-1-.pdf"
    assert body["inbox"] == "inbox/judge-x.test__My-Waybill-1-.md"
    assert calls == [(13, "application/pdf")]
    assert (root / body["raw"]).read_bytes() == b"%PDF-1.4 fake"
    inbox = (root / body["inbox"]).read_text(encoding="utf-8")
    assert inbox.startswith("<!-- transcribed from raw/uploads/judge-x.test/My-Waybill-1-.pdf by ")
    assert "Gross 6,098 kg" in inbox
    assert (tmp / "durable" / body["raw"]).exists() and (tmp / "durable" / body["inbox"]).exists()
    # the agents can read it now, through the same jailed tool
    assert workspace.read_file(body["inbox"])["status"] == "ok"


def test_upload_refuses_what_ingest_cannot_read_and_what_is_too_big(world, monkeypatch):
    calls = stub_transcriber(monkeypatch)
    client = TestClient(webapp.build_app(fake_adk()))
    assert client.post("/upload?name=notes.docx", content=b"x").status_code == 415
    assert client.post("/upload?name=scan.png", content=b"").status_code == 400
    monkeypatch.setattr(webapp, "UPLOAD_MAX_BYTES", 10)
    assert client.post("/upload?name=scan.png", content=b"x" * 11).status_code == 413
    assert calls == []


def test_upload_is_rate_limited_per_identity(world, monkeypatch):
    stub_transcriber(monkeypatch)
    monkeypatch.setattr(webapp, "_uploads", webapp._Limiter(2))
    client = TestClient(webapp.build_app(fake_adk()))
    for _ in range(2):
        assert client.post("/upload?name=a.png", content=b"x", headers={"x-fleet-identity": "j1"}).status_code == 200
    assert client.post("/upload?name=a.png", content=b"x", headers={"x-fleet-identity": "j1"}).status_code == 429
    assert client.post("/upload?name=a.png", content=b"x", headers={"x-fleet-identity": "j2"}).status_code == 200


def test_a_failed_transcription_is_reported_not_hidden(world, monkeypatch):
    from freight_fleet import ingest

    def factory(model):
        def boom(data, mime):
            raise RuntimeError("model said no")
        return boom

    monkeypatch.setattr(ingest, "transcribe_with_genai", factory)
    client = TestClient(webapp.build_app(fake_adk()))
    r = client.post("/upload?name=a.pdf", content=b"x")
    assert r.status_code == 502 and "model said no" in r.json()["message"]


def test_restore_puts_durable_uploads_back_after_a_restart(world):
    tmp, root = world
    durable = tmp / "durable"
    (durable / "raw" / "uploads" / "j1").mkdir(parents=True)
    (durable / "raw" / "uploads" / "j1" / "a.pdf").write_bytes(b"pdf")
    (durable / "inbox").mkdir()
    (durable / "inbox" / "j1__a.md").write_text("# a\n", encoding="utf-8")
    assert webapp.restore_uploads(root) == 2
    assert (root / "raw" / "uploads" / "j1" / "a.pdf").read_bytes() == b"pdf"
    assert (root / "inbox" / "j1__a.md").read_text(encoding="utf-8") == "# a\n"
    assert webapp.restore_uploads(root) == 2, "idempotent"


def test_the_sweep_trigger_reports_every_outcome_to_the_desk(world, monkeypatch):
    client = TestClient(webapp.build_app(fake_adk()))
    assert client.post("/sweep/run", follow_redirects=False).headers["location"] == "/desk?run=unavailable"

    monkeypatch.setenv("FREIGHT_SWEEP_JOB", "projects/p/locations/r/jobs/sweep")
    seen: list[str] = []
    monkeypatch.setattr(webapp, "run_job", lambda job: (seen.append(job), (True, "exec-1"))[1])
    assert client.post("/sweep/run", follow_redirects=False).headers["location"] == "/desk?run=started"
    assert seen == ["projects/p/locations/r/jobs/sweep"]
    # a second click inside the cooldown starts nothing
    assert client.post("/sweep/run", follow_redirects=False).headers["location"] == "/desk?run=busy"
    assert seen == ["projects/p/locations/r/jobs/sweep"]

    monkeypatch.setattr(webapp, "_last_sweep_trigger", 0.0)
    monkeypatch.setattr(webapp, "run_job", lambda job: (False, "busy"))
    assert client.post("/sweep/run", follow_redirects=False).headers["location"] == "/desk?run=busy"

    def boom(job):
        raise RuntimeError("no credentials")

    monkeypatch.setattr(webapp, "run_job", boom)
    assert client.post("/sweep/run", follow_redirects=False).headers["location"] == "/desk?run=error"
    assert "SWEEP STARTED" in client.get("/desk?run=started").text
    assert "NOT STARTED" in client.get("/desk?run=error").text
