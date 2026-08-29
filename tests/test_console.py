"""The operator console, exercised against a real (temporary) ledger.

The console's whole claim is that it renders records and causes exactly one kind
of write. These tests hold it to both halves: every screen must render from real
artifacts, and the only path to a file on disk must be the gated replay.

The world each test runs in is built by the REAL gate, so an approval id here is
a ledger entry id, exactly as in production. Fabricating the fixture by hand
would test a shape the fleet never produces.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from freight_fleet import console
from freight_fleet.governance.gate import FileApprovalStore, make_before_tool_gate
from freight_fleet.governance.ledger import Ledger
from freight_fleet.tools import workspace

DRAFT = """# Discrepancy notice — SHP-T01

**Severity: CRITICAL** — the packing list and the waybill disagree on weight.

| Field | Waybill | Packing list |
|---|---|---|
| Gross weight | 6,098.0 kg | 5,384.0 kg |

- The 714 kg gap is a customs problem.
- <script>alert(1)</script> is text, not markup.
"""

RUN_RECORD = {
    "model": "gemini-3.7-flash",
    "ts": "2026-08-21T08:18:23Z",
    "results": [
        {"id": "g1_hero_crosscheck", "passed": True, "score": 1.0, "details": "all 4 reported",
         "final_text": "# Report\n\nDISCREPANCIES FOUND: 4\n"},
        {"id": "g2_clean_control", "passed": True, "score": 1.0,
         "details": "clean control correctly reported DISCREPANCIES FOUND: 0"},
        {"id": "g3_container_refs", "passed": True, "score": 1.0, "details": "all 3 reported"},
        {"id": "g4_quote_vs_invoice", "passed": True, "score": 1.0, "details": "all 3 reported"},
        {"id": "g5_air_dangerous_goods", "passed": True, "score": 1.0, "details": "all 2 reported"},
        {"id": "g6_missing_document", "passed": True, "score": 1.0, "details": "all 1 reported"},
        {"id": "g7_intake_sorting", "status": "manual"},
        {"id": "g8_quote_comparison", "status": "manual"},
    ],
}


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name


class _Ctx:
    agent_name = "cross_check"


@pytest.fixture()
def world(tmp_path, monkeypatch):
    """One sweep session, one clean shipment, one held draft — the smallest
    world in which every screen has something true to say."""
    root = tmp_path / "workspace"
    for name in ("shp-t01", "shp-t02-clean"):
        (root / "shipments" / name).mkdir(parents=True)
        (root / "shipments" / name / "waybill.md").write_text(
            "# Waybill\n\nGross weight: 6,098.0 kg\n", encoding="utf-8")
        (root / "shipments" / name / "packing_list.csv").write_text(
            "carton,weight_kg\n1,120.5\n2,118.0\n", encoding="utf-8")
    (root / "outbox").mkdir()
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "20260821T081823Z.json").write_text(json.dumps(RUN_RECORD), encoding="utf-8")

    monkeypatch.setattr(workspace, "WORKSPACE_ROOT", root.resolve())
    monkeypatch.setenv("FREIGHT_LEDGER_PATH", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("FREIGHT_APPROVALS_PATH", str(tmp_path / "approvals.json"))
    monkeypatch.setenv("FREIGHT_EVAL_RUNS", str(runs))
    monkeypatch.delenv("FREIGHT_CONSOLE_READONLY", raising=False)

    ledger = Ledger(tmp_path / "ledger.jsonl")
    store = FileApprovalStore(tmp_path / "approvals.json")
    gate = make_before_tool_gate(ledger, store, "sweep-2026-08-21")
    # The clean control: read, nothing held. This is what /? renders as CLEARED.
    gate(_Tool("list_files"), {"prefix": "shipments/shp-t02-clean"}, _Ctx())
    gate(_Tool("read_file"), {"path": "shipments/shp-t02-clean/waybill.md"}, _Ctx())
    gate(_Tool("read_file"), {"path": "shipments/shp-t02-clean/packing_list.csv"}, _Ctx())
    # The discrepant one: three reads, then a consequential draft.
    gate(_Tool("list_files"), {"prefix": "shipments/shp-t01"}, _Ctx())
    gate(_Tool("read_file"), {"path": "shipments/shp-t01/waybill.md"}, _Ctx())
    gate(_Tool("read_file"), {"path": "shipments/shp-t01/packing_list.csv"}, _Ctx())
    held = gate(_Tool("write_file"),
                {"path": "outbox/shp-t01-discrepancy-notice.md", "content": DRAFT}, _Ctx())

    return SimpleNamespace(
        tmp=tmp_path, root=root, ledger=ledger, approval_id=held["approval_id"],
        client=TestClient(console.app),
        store=lambda: FileApprovalStore(tmp_path / "approvals.json"),
        target=root / "outbox" / "shp-t01-discrepancy-notice.md",
    )


# --- every screen renders ----------------------------------------------------

@pytest.mark.parametrize("path", ["/", "/desk", "/ledger", "/ledger.jsonl", "/sent", "/fleet",
                                  "/evidence", "/healthz"])
def test_every_route_renders(world, path):
    assert world.client.get(path).status_code == 200


def test_the_desk_leads_with_the_waiting_count(world):
    body = world.client.get("/").text
    assert '<div class="count">1</div>' in body
    assert "gate decisions" in body, "the brief must be computed, not hardcoded"
    assert "shp-t02-clean" in body, "the false-positive control belongs on the morning screen"


def test_the_record_renders_a_row_for_every_line(world):
    """47 of 56 real rows are reads; runs of them collapse. Nothing is hidden —
    the count is on the closed row and the raw file is one link away."""
    body = world.client.get("/ledger").text
    entries = list(world.ledger.read())
    assert f'<div class="count3">{len(entries)}</div>' in body
    for entry in entries:
        assert f'id="e-{entry.entry_id}"' in body
    assert console.ledger_sha256()[:8] in body


def test_the_raw_ledger_is_served_unedited(world):
    served = world.client.get("/ledger.jsonl").text
    assert served == (world.tmp / "ledger.jsonl").read_text(encoding="utf-8")


def test_a_pending_approval_renders_with_its_draft(world):
    body = world.client.get(f"/decision/{world.approval_id}").text
    assert "HELD — THIS HAS NOT RUN" in body
    assert "risk HIGH" in body and "verdict ASK" in body
    assert "6,098.0 kg" in body, "the draft's own table must reach the page"
    assert f"{len(DRAFT):,} characters" in body
    assert "The file does not exist yet." in body
    assert body.count("/doc?path=") == 2, "both documents the agent read, and nothing else"


def test_the_draft_is_escaped_not_executed(world):
    """Drafts are model-generated text on a public URL. This is the whole XSS
    surface, and md_lite escapes before it renders anything."""
    body = world.client.get(f"/decision/{world.approval_id}").text
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body


def test_the_console_ships_no_javascript(world):
    """A claim a reviewer verifies with one grep, so it had better hold."""
    for path in ("/", "/ledger", "/fleet", "/evidence", f"/decision/{world.approval_id}"):
        body = world.client.get(path).text
        assert "<script" not in body.lower()
        assert "onclick" not in body.lower()


# --- the one write path ------------------------------------------------------

def test_approving_executes_and_writes_the_file(world):
    assert not world.target.exists()

    response = world.client.post(f"/decision/{world.approval_id}/approve")

    assert response.status_code == 200  # 303 followed back to the desk
    assert world.target.read_text(encoding="utf-8") == DRAFT
    outcomes = [e.outcome for e in world.ledger.read()]
    assert outcomes[-2:] == ["approved", "executed"]
    assert world.approval_id not in world.store().pending()
    assert not world.store().is_granted(world.approval_id), "grants are single-use"
    # And the numbers move, which is the whole feedback mechanism.
    desk = world.client.get("/").text
    assert '<div class="count">0</div>' in desk
    assert '<div class="count2">1</div>' in desk, "IN OUTBOX must go 0 -> 1"


def test_a_second_approval_of_the_same_id_does_not_execute_twice(world):
    world.client.post(f"/decision/{world.approval_id}/approve")
    before = world.target.read_text(encoding="utf-8")

    world.target.write_text("tampered", encoding="utf-8")
    world.client.post(f"/decision/{world.approval_id}/approve")

    assert world.target.read_text(encoding="utf-8") == "tampered", "no second execution"
    assert [e.outcome for e in world.ledger.read()].count("executed") == 1
    assert before == DRAFT


def test_a_fabricated_id_is_a_404_and_writes_nothing(world):
    before = len(list(world.ledger.read()))

    assert world.client.post("/decision/00000000-dead-beef/approve").status_code == 404
    assert world.client.get("/decision/00000000-dead-beef").status_code == 404

    assert len(list(world.ledger.read())) == before
    assert not world.target.exists()


def test_rejecting_records_the_decision_and_writes_nothing(world):
    world.client.post(f"/decision/{world.approval_id}/reject")

    assert not world.target.exists()
    rows = list(world.ledger.read())
    assert rows[-1].outcome == "rejected"
    assert rows[-1].session_id == "approval-console"
    assert world.approval_id not in world.store().pending()


def test_read_only_console_refuses_both_buttons(world, monkeypatch):
    monkeypatch.setenv("FREIGHT_CONSOLE_READONLY", "1")

    assert world.client.post(f"/decision/{world.approval_id}/approve").status_code == 403
    assert world.client.post(f"/decision/{world.approval_id}/reject").status_code == 403
    assert not world.target.exists()
    assert world.approval_id in world.store().pending(), "no store was touched"
    assert "READ-ONLY CONSOLE" in world.client.get(f"/decision/{world.approval_id}").text


# --- /doc is jailed, allowlisted and evidence-gated --------------------------

def test_doc_opens_a_document_the_ledger_records(world):
    response = world.client.get("/doc", params={"path": "shipments/shp-t01/packing_list.csv"})
    assert response.status_code == 200
    assert "carton" in response.text and "120.5" in response.text
    assert "read by cross_check" in response.text


@pytest.mark.parametrize("path", [
    "/etc/passwd",                      # absolute, outside the jail
    "../../secrets.md",                 # traversal
    "shipments/shp-t01/waybill.exe",    # extension not allowlisted
    "shipments/shp-t01/notes.md",       # inside the jail but not in evidence
])
def test_doc_refuses_anything_outside_the_evidence_set(world, path):
    assert world.client.get("/doc", params={"path": path}).status_code == 404


def test_doc_explains_a_held_but_unwritten_target(world):
    body = world.client.get("/doc",
                            params={"path": "outbox/shp-t01-discrepancy-notice.md"}).text
    assert "Nothing exists at this path." in body
    assert "held for approval since" in body


# --- empty and broken states -------------------------------------------------

def test_an_empty_ledger_renders_a_considered_screen(tmp_path, monkeypatch):
    """A judge clones the repo with no ledger. That must look designed."""
    monkeypatch.setenv("FREIGHT_LEDGER_PATH", str(tmp_path / "nothing.jsonl"))
    monkeypatch.setenv("FREIGHT_APPROVALS_PATH", str(tmp_path / "nothing.json"))
    client = TestClient(console.app)

    desk = client.get("/")
    assert desk.status_code == 200
    assert "No decisions recorded yet" in desk.text
    assert "python -m freight_fleet.cli sweep" in desk.text
    assert client.get("/ledger").status_code == 200
    assert "does not exist yet" in client.get("/ledger").text


def test_a_malformed_line_is_rendered_never_skipped(world):
    """A skipped line in an append-only ledger is precisely the thing this
    project promises never happens."""
    with (world.tmp / "ledger.jsonl").open("a", encoding="utf-8") as fh:
        fh.write("{this is not json\n")

    body = world.client.get("/ledger").text
    assert "UNREADABLE LINE" in body
    assert "{this is not json" in body


def test_a_missing_approval_store_leaves_the_record_intact(world, monkeypatch):
    """The ledger is the authority for what happened; the store only says what
    is still actionable. Losing the store must not lose the history."""
    monkeypatch.setenv("FREIGHT_APPROVALS_PATH", str(world.tmp / "gone.json"))

    assert world.client.get("/").status_code == 200
    body = world.client.get("/ledger").text
    assert "LAPSED" in body, "a hold with no store entry is lapsed, and says so"


def test_the_scoreboard_never_counts_a_manual_row_as_a_pass(world):
    body = world.client.get("/evidence").text
    assert '<div class="count">6 / 6</div>' in body
    assert body.count("EYE-REVIEWED") == 2
    assert "g2_clean_control" in body


def test_the_catalog_shows_every_agent_and_the_hazard_row(world):
    from freight_fleet.catalog.registry import FLEET
    from freight_fleet.governance.policy import TOOL_SPECS

    body = world.client.get("/fleet").text
    for card in FLEET:
        assert f'id="{card.key}"' in body
    for name in TOOL_SPECS:
        assert name in body
    assert "ANY TOOL NOT IN THIS TABLE" in body
    assert "HIGH·HOLDS" in body, "the chip that connects the catalog to the gate"


@pytest.mark.parametrize("path", ["/", "/ledger", "/fleet", "/evidence"])
def test_every_page_is_well_formed_html(world, path):
    """No templating engine means no engine catching an unbalanced tag. A page
    that renders wrong in the judge's browser is a bug the tests should own."""
    from html.parser import HTMLParser

    void = {"meta", "br", "hr", "link", "img", "input", "source", "col"}

    class _Check(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.stack: list[str] = []
            self.errors: list[str] = []

        def handle_starttag(self, tag, attrs):
            if tag not in void:
                self.stack.append(tag)

        def handle_endtag(self, tag):
            if tag in void:
                return
            if not self.stack or self.stack[-1] != tag:
                self.errors.append(f"</{tag}> does not close <{self.stack[-1:]}>")
                return
            self.stack.pop()

    checker = _Check()
    checker.feed(world.client.get(path).text)
    assert checker.errors == []
    assert checker.stack == []


# --- a repeated run must not be reported as a better run ----------------------
#
# `--repeat N` writes N rows per task. Rendered raw, that turned 7 tasks into a
# `21 / 21` headline while README and the writeup both said 7/7, and the clean
# control card — the screen's own "number to look at first" — read `results[0]`
# and so showed attempt 1 with no hint the other attempts existed.


def _repeat_record(*, failing_attempt: int | None = None, repeat: int = 3) -> dict:
    """A record shaped exactly as `run_eval.py --repeat` writes one."""
    results = []
    for task in ("g1_hero_crosscheck", "g2_clean_control", "g3_container_refs",
                 "g4_quote_vs_invoice", "g5_air_dangerous_goods", "g6_missing_document"):
        for attempt in range(1, repeat + 1):
            bad = failing_attempt == attempt and task == "g2_clean_control"
            results.append({
                "id": task, "attempt": attempt,
                "passed": not bad, "score": 0.0 if bad else 1.0,
                "details": "invented a discrepancy" if bad else "clean control correct",
                "final_text": f"answer for {task} attempt {attempt}",
            })
    return {"model": "gemini-3.7-flash", "ts": "20260821T205755Z",
            "repeat": repeat, "results": results}


def _write_run(world, record: dict, name: str = "20260821T205755Z.json") -> None:
    import os
    (pytest.importorskip("pathlib").Path(os.environ["FREIGHT_EVAL_RUNS"]) / name).write_text(
        json.dumps(record), encoding="utf-8")


def test_the_scoreboard_counts_tasks_not_attempts(world):
    """Three attempts at seven tasks is still seven tasks. A headline that reads
    21/21 against a writeup that says 7/7 makes a reader check which one lied."""
    _write_run(world, _repeat_record())

    body = world.client.get("/evidence").text
    assert ">6 / 6<" in body, "the headline must count tasks"
    assert "18 / 18" not in body, "attempts must not become the denominator"


def test_one_failing_attempt_fails_the_task(world):
    """The cherry-pick, sealed. Attempt 3 of the clean control invents a
    discrepancy; the screen must not report the task as passed because attempt 1
    happened to be first in the list."""
    _write_run(world, _repeat_record(failing_attempt=3))

    body = world.client.get("/evidence").text
    assert ">5 / 6<" in body, "a task that failed one of three attempts counted as passed"
    assert "2/3 attempts" in body, "the per-attempt tally must be on the row"


def test_the_clean_control_card_shows_the_worst_attempt(world):
    """`results[0]` is attempt 1. The card is the writeup's centrepiece claim, so
    reading the first row and calling it the verdict is the whole defect."""
    _write_run(world, _repeat_record(failing_attempt=3))

    body = world.client.get("/evidence").text
    card = body[body.index("The number to look at first"):]
    card = card[:card.index("Every task in the run")]
    assert "FAIL" in card and "PASS" not in card
    assert "invented a discrepancy" in card


def test_a_single_run_is_unchanged_by_the_collapse(world):
    """The repeat-aware path must not alter how an ordinary run reads — the
    history bar compares them side by side."""
    _write_run(world, _repeat_record(repeat=1))

    body = world.client.get("/evidence").text
    assert ">6 / 6<" in body
    assert "attempts</span>" not in body, "no attempt tally when there is one attempt"
    assert "Every task was run" not in body


# --- the schedule is the operator's, displayed and never editable ------------
#
# A settings screen that could change the cadence would need credentials that
# mutate infrastructure, and the console's safety claim is that it holds none.
# So the schedule arrives as one env var, display-only.


def test_the_desk_repeats_the_operator_schedule(world, monkeypatch):
    monkeypatch.setenv("FREIGHT_SWEEP_SCHEDULE", "weekdays 06:00 Europe/Athens")

    body = world.client.get("/").text
    assert "Unattended sweep schedule: weekdays 06:00 Europe/Athens" in body
    assert "never from this console" in body


def test_no_schedule_configured_means_no_schedule_claimed(world, monkeypatch):
    """An empty env var must not render an empty label — a blank schedule line
    would read as 'there is a schedule and it is nothing'."""
    monkeypatch.delenv("FREIGHT_SWEEP_SCHEDULE", raising=False)

    body = world.client.get("/").text
    assert "Unattended sweep schedule" not in body


def test_the_schedule_is_escaped_like_any_other_input(world, monkeypatch):
    """The env var is operator-controlled, but the console escapes every value
    it prints; a schedule string is not an exception to that rule."""
    monkeypatch.setenv("FREIGHT_SWEEP_SCHEDULE", 'daily <script>alert(1)</script>')

    body = world.client.get("/").text
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_privacy_page_is_public_static_and_true(monkeypatch):
    """The OAuth consent screen points at /privacy. It must render with no
    state at all (no ledger, no store), say what the chat collects, and carry
    the same zero-JS shell as every other page."""
    from freight_fleet import console

    monkeypatch.setenv("FREIGHT_CONTACT_EMAIL", "privacy@example.test")
    r = TestClient(console.app).get("/privacy")
    assert r.status_code == 200
    assert "email address of the Google account" in r.text
    assert "mailto:privacy@example.test" in r.text
    assert "<script" not in r.text
    # discoverable from every footer, not only from the consent screen
    assert 'href="/privacy"' in TestClient(console.app).get("/").text


def test_the_nav_offers_the_chat_and_a_sign_out_from_env_alone(monkeypatch):
    """One nav for every page. The chat link and the sign-out come from env: a
    console served on its own has no chat to link and no session to end."""
    from freight_fleet import console

    monkeypatch.delenv("FREIGHT_CHAT_URL", raising=False)
    monkeypatch.delenv("FREIGHT_GATED", raising=False)
    page = TestClient(console.app).get("/desk").text
    assert 'href="/chat"' not in page and 'href="/logout"' not in page
    assert 'href="/sent"' in page and 'href="/desk"' in page

    monkeypatch.setenv("FREIGHT_CHAT_URL", "/chat")
    monkeypatch.setenv("FREIGHT_GATED", "1")
    page = TestClient(console.app).get("/desk").text
    assert 'href="/chat"' in page and "Ask the fleet" in page
    assert 'href="/logout"' in page
    assert "How this desk works" in page, "the brief is for everyone now"
    assert "/sweep/run" not in page, "no job configured, no button"
    monkeypatch.setenv("FREIGHT_SWEEP_JOB", "projects/p/locations/r/jobs/j")
    assert 'action="/sweep/run"' in TestClient(console.app).get("/desk").text


def test_the_root_lands_on_the_desk():
    from freight_fleet import console

    r = TestClient(console.app).get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/desk"


# --- send_email on the desk ---------------------------------------------------

@pytest.fixture()
def held_send(world, monkeypatch):
    """The world plus one held send, and a spool the test owns."""
    from freight_fleet.governance.gate import FileApprovalStore

    monkeypatch.setenv("FREIGHT_MAIL_SPOOL", str(world.tmp / "sent"))
    monkeypatch.delenv("FREIGHT_MAIL_TRANSPORT", raising=False)
    monkeypatch.delenv("FREIGHT_MAIL_SINK", raising=False)
    gate = make_before_tool_gate(world.ledger, FileApprovalStore(world.tmp / "approvals.json"),
                                 "sweep-2026-08-21")
    gate(_Tool("read_file"), {"path": "shipments/shp-t01/waybill.md"}, _Ctx())
    held = gate(_Tool("send_email"), {"to": "ops@carrier.example",
                                      "subject": "[BK4471] Discrepancy notice",
                                      "body": "Dear team,\n\nthe weights differ.\n"}, _Ctx())
    return held["approval_id"]


def test_a_held_send_is_a_send_on_every_screen(world, held_send):
    desk = world.client.get("/desk").text
    assert "Send an email" in desk and "Discrepancy notice" in desk
    assert "never to that address" in desk
    page = world.client.get(f"/decision/{held_send}").text
    assert "Approve — send it" in page and "Reject — send nothing" in page
    assert "ops@carrier.example" in page and "will <strong>not</strong> go there" in page
    assert "The email" in page and "<script" not in page


def test_approving_a_send_delivers_to_the_spool_and_names_the_approver(world, held_send):
    from freight_fleet.tools import mail

    r = world.client.post(f"/decision/{held_send}/approve", headers={"x-fleet-identity": "judge2"})
    assert r.status_code == 200 and "send_email executed" in r.text
    rows = list(world.ledger.read())
    assert [x.outcome for x in rows[-2:]] == ["approved", "executed"]
    assert "by judge2" in rows[-1].detail and "status=ok" in rows[-1].detail
    sent = mail.list_sent()
    assert len(sent) == 1
    assert sent[0]["intended_to"] == "ops@carrier.example"
    assert sent[0]["delivered_to"] == [], "no sink, no approver address: the spool is the mailbox"
    assert sent[0]["approved_by"] == ""
    page = world.client.get("/sent").text
    assert "Discrepancy notice" in page and "ops@carrier.example" in page and "1 message left" in page
    # the desk's transmitted count is a fact now
    assert 'count2">1</div>' in world.client.get("/desk").text


def test_an_approver_with_an_address_gets_the_copy(world, held_send, monkeypatch):
    from freight_fleet.tools import mail

    monkeypatch.setenv("FREIGHT_MAIL_SINK", "demo@example.test")
    world.client.post(f"/decision/{held_send}/approve", headers={"x-fleet-identity": "judge@gmail.test"})
    sent = mail.list_sent()[0]
    assert sent["delivered_to"] == ["demo@example.test", "judge@gmail.test"]
    assert sent["approved_by"] == "judge@gmail.test"
    assert "ops@carrier.example" not in sent["delivered_to"]


def test_rejecting_a_send_sends_nothing(world, held_send):
    from freight_fleet.tools import mail

    r = world.client.post(f"/decision/{held_send}/reject", headers={"x-fleet-identity": "judge1"})
    assert r.status_code == 200 and "REJECTED" in r.text
    assert mail.list_sent() == []
    assert "by judge1" in list(world.ledger.read())[-1].detail
