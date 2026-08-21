"""Seals on the trust boundary. These are the tests that must never go red.

Each asserts a rule from AGENTS.md. If you change behavior such that one of
these fails, you have removed a safety property — go read why it was there.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from freight_fleet.catalog.registry import FLEET, catalog
from freight_fleet.governance.gate import ApprovalStore, digest_args, make_before_tool_gate
from freight_fleet.governance.ledger import Ledger
from freight_fleet.governance.policy import TOOL_SPECS, Verdict, classify, stricter


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name


class _Ctx:
    agent_name = "cross_check"


@pytest.fixture()
def gate_parts():
    d = tempfile.mkdtemp()
    ledger = Ledger(Path(d) / "ledger.jsonl")
    approvals = ApprovalStore()
    return ledger, approvals, make_before_tool_gate(ledger, approvals, "test-session")


# --- policy ------------------------------------------------------------------

def test_unknown_tool_fails_closed():
    """AGENTS.md #2 — an unclassified tool must never run."""
    assert classify("definitely_not_a_tool")[1] is Verdict.BLOCK


def test_read_tools_auto_and_writes_ask():
    assert classify("read_file")[1] is Verdict.AUTO
    assert classify("write_file")[1] is Verdict.ASK


def test_external_side_effect_always_asks():
    assert classify("send_email")[1] is Verdict.ASK


@pytest.mark.parametrize(
    "a,b,expected",
    [
        (Verdict.AUTO, Verdict.ASK, Verdict.ASK),
        (Verdict.ASK, Verdict.AUTO, Verdict.ASK),
        (Verdict.ASK, Verdict.BLOCK, Verdict.BLOCK),
        (Verdict.AUTO, Verdict.AUTO, Verdict.AUTO),
    ],
)
def test_stricter_never_loosens(a, b, expected):
    """AGENTS.md #1 — verdicts combine tighten-only."""
    assert stricter(a, b) is expected


# --- gate --------------------------------------------------------------------

def test_read_runs_and_is_logged(gate_parts):
    ledger, _, gate = gate_parts
    assert gate(_Tool("read_file"), {"path": "a.md"}, _Ctx()) is None
    rows = list(ledger.read())
    assert [r.outcome for r in rows] == ["auto_ran"]


def test_write_is_held_not_run(gate_parts):
    ledger, approvals, gate = gate_parts
    result = gate(_Tool("write_file"), {"path": "outbox/x.md", "content": "hi"}, _Ctx())
    assert result["status"] == "pending_approval"
    assert result["approval_id"] in approvals.pending()
    assert [r.outcome for r in ledger.read()] == ["held"]


def test_approved_replay_runs_and_records_the_grant(gate_parts):
    # The replay carries the FULL approved args, which is what execute_approved
    # does. This assertion used to omit `content`; once a grant became bound to
    # the action it approved, a replay missing an argument is a DIFFERENT action
    # and is correctly refused. The seal's intent - an approved replay runs and
    # is recorded - is unchanged.
    ledger, approvals, gate = gate_parts
    args = {"path": "outbox/x.md", "content": "hi"}
    held = gate(_Tool("write_file"), dict(args), _Ctx())
    aid = held["approval_id"]
    approvals.approve(aid)
    assert gate(_Tool("write_file"), {**args, "_approval_id": aid}, _Ctx()) is None
    rows = list(ledger.read())
    assert rows[-1].outcome == "approved"
    assert rows[-1].approval_id == aid


def test_unapproved_id_does_not_launder_a_hold(gate_parts):
    """A made-up approval id must not convert ask into auto."""
    _, _, gate = gate_parts
    result = gate(_Tool("write_file"), {"path": "p", "_approval_id": "fabricated"}, _Ctx())
    assert result["status"] == "pending_approval"


def test_rejected_approval_is_not_granted(gate_parts):
    _, approvals, gate = gate_parts
    held = gate(_Tool("write_file"), {"path": "p", "content": "c"}, _Ctx())
    approvals.reject(held["approval_id"])
    assert not approvals.is_granted(held["approval_id"])


def test_digest_does_not_store_file_bodies():
    """The ledger records shape, not payload — a 40 KB body must not land in it."""
    digested = digest_args({"path": "a.md", "content": "x" * 40_000})
    assert digested == {"path": "a.md", "content_chars": 40_000}


# --- catalog -----------------------------------------------------------------

def test_every_catalog_tool_is_classified():
    """A card may not grant a tool the gate cannot classify."""
    unclassified = [(c.key, t) for c in FLEET for t in c.tools if t not in TOOL_SPECS]
    assert unclassified == []


def test_no_agent_is_autonomous_for_consequential_work():
    assert all(c.autonomy in {"read-only", "drafts-for-approval"} for c in FLEET)


def test_catalog_serializes_every_agent():
    assert len(catalog()) == len(FLEET)


def test_every_fleet_desk_is_classified_for_delegation():
    """The coordinator reaches specialists through the SAME gate seam, so each
    desk's AgentTool name must be classified or fail-closed blocks all routing.
    Derived from FLEET so adding a sixth desk without classifying it goes red."""
    for card in FLEET:
        spec, verdict = classify(card.key)
        assert spec is not None, f"desk {card.key} is unclassified - routing would be blocked"
        assert verdict is Verdict.AUTO, (
            f"desk {card.key} classified {verdict}; delegation is AUTO because every "
            "downstream tool call re-enters the gate under the specialist's name"
        )


def test_grant_is_single_use(gate_parts):
    """A durable grant must buy exactly ONE execution. A reusable id would let
    anything that ever saw it replay the action forever."""
    _, approvals, gate = gate_parts
    args = {"path": "p", "content": "c"}
    held = gate(_Tool("write_file"), dict(args), _Ctx())
    aid = held["approval_id"]
    approvals.approve(aid)
    assert gate(_Tool("write_file"), {**args, "_approval_id": aid}, _Ctx()) is None
    again = gate(_Tool("write_file"), {**args, "_approval_id": aid}, _Ctx())
    assert again["status"] == "pending_approval", "a consumed grant must hold again"


def test_file_store_survives_the_process(tmp_path):
    """Holds and grants persist across store instances - the one-turn CLI dies
    between hold and grant, so durability IS the approval surface."""
    from freight_fleet.governance.gate import FileApprovalStore

    path = tmp_path / "approvals.json"
    store_a = FileApprovalStore(path)
    store_a.hold("id-1", {"tool": "write_file", "args": {"path": "x"}, "agent": "cross_check"})

    store_b = FileApprovalStore(path)  # "restart"
    assert "id-1" in store_b.pending()
    assert store_b.approve("id-1") is not None

    store_c = FileApprovalStore(path)
    assert store_c.is_granted("id-1")
    assert not store_c.is_granted("fabricated")
    store_c.consume("id-1")
    assert not FileApprovalStore(path).is_granted("id-1")


# --- the operator seam -------------------------------------------------------
# One replay path, two front doors. These three seal AGENTS.md #3 against the
# console: if approving from a browser ever stops going through
# `execute_approved`, the writeup's central claim becomes false and these go red.

@pytest.fixture()
def seam(tmp_path, monkeypatch):
    """A temp world with one real hold in it, placed by the real gate — so the
    approval id IS the ledger entry id, exactly as in production."""
    from fastapi.testclient import TestClient

    from freight_fleet import console
    from freight_fleet.governance.gate import FileApprovalStore
    from freight_fleet.tools import workspace

    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(workspace, "WORKSPACE_ROOT", root.resolve())
    monkeypatch.setenv("FREIGHT_LEDGER_PATH", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("FREIGHT_APPROVALS_PATH", str(tmp_path / "approvals.json"))
    monkeypatch.delenv("FREIGHT_CONSOLE_READONLY", raising=False)

    ledger = Ledger(tmp_path / "ledger.jsonl")
    store = FileApprovalStore(tmp_path / "approvals.json")
    gate = make_before_tool_gate(ledger, store, "sweep-test")
    gate(_Tool("read_file"), {"path": "shipments/shp-t01/waybill.md"}, _Ctx())
    held = gate(_Tool("write_file"),
                {"path": "outbox/shp-t01-notice.md", "content": "# Notice\n\nbody\n"}, _Ctx())
    return SimpleNamespace(
        tmp=tmp_path, root=root, ledger=ledger, approval_id=held["approval_id"],
        client=TestClient(console.app),
        store=lambda: FileApprovalStore(tmp_path / "approvals.json"),
    )


def test_console_approval_uses_the_same_seam(seam):
    """The browser button and `approvals grant` are the same function. The proof
    is in the record: `approved` then `executed`, both stamped approval-console."""
    response = seam.client.post(f"/decision/{seam.approval_id}/approve")
    assert response.status_code == 200  # 303 followed to the desk

    rows = list(seam.ledger.read())
    assert [r.outcome for r in rows[-2:]] == ["approved", "executed"]
    assert {r.session_id for r in rows[-2:]} == {"approval-console"}
    assert all(r.approval_id == seam.approval_id for r in rows[-2:])
    assert (seam.root / "outbox" / "shp-t01-notice.md").read_text(encoding="utf-8") \
        == "# Notice\n\nbody\n"
    # Single-use: the grant is retired the moment the gate lets the replay through.
    assert not seam.store().is_granted(seam.approval_id)


def test_unwired_tool_leaves_the_hold_intact(seam):
    """`send_email` has no body by design. Discovering that AFTER granting would
    retire the hold and leave a dangling grant, so the lookup happens first and
    the action stays exactly where the operator left it."""
    from freight_fleet.governance.gate import execute_approved

    before = len(list(seam.ledger.read()))
    store = seam.store()
    result = execute_approved(seam.approval_id, ledger=seam.ledger, approvals=store,
                              tool_fns={}, source="approval-console")

    assert result.status == "not_executable"
    assert len(list(seam.ledger.read())) == before, "an unwired tool must write no ledger row"
    assert seam.approval_id in seam.store().pending(), "the hold must survive intact"
    assert not seam.store().is_granted(seam.approval_id), "no grant may be left dangling"


def test_console_cannot_execute_an_unknown_id(seam):
    """A fabricated id is not a decision. It is a 404, and it leaves no trace."""
    before = len(list(seam.ledger.read()))
    response = seam.client.post("/decision/not-a-real-approval-id/approve")

    assert response.status_code == 404
    assert len(list(seam.ledger.read())) == before
    assert not list((seam.root).rglob("*.md"))
    assert seam.approval_id in seam.store().pending()


def test_every_wired_tool_is_classified():
    """The approval surfaces replay whatever `TOOL_FNS` names. A body wired
    without a risk row would be a capability the gate cannot classify — and
    `classify()` would BLOCK it, so the failure is loud rather than silent.
    This asserts the loudness never has to happen."""
    from freight_fleet.tools.workspace import TOOL_FNS

    assert [name for name in TOOL_FNS if name not in TOOL_SPECS] == []
    assert "send_email" not in TOOL_FNS, "AGENTS.md #7 — the fleet drafts; it never sends"


def test_a_refused_replay_leaves_no_dangling_grant(seam, monkeypatch):
    """The gate refusing must undo the grant, not merely skip the execution.

    A grant that outlives a refusal is a standing authorization for the one
    action the gate just declined - single-use in name only, and durable,
    because the store is a file. It would also retire the hold: the queue would
    lose the card while the screen said the hold stands.
    """
    from freight_fleet.governance import gate as gatemod
    from freight_fleet.governance.gate import execute_approved
    from freight_fleet.tools import workspace

    monkeypatch.setattr(
        gatemod, "make_before_tool_gate",
        lambda ledger, approvals, session_id: (
            lambda tool, args, ctx: {"status": "blocked", "message": "refused"}),
    )
    result = execute_approved(seam.approval_id, ledger=seam.ledger, approvals=seam.store(),
                              tool_fns=workspace.TOOL_FNS, source="approval-console")

    assert result.status == "gate_refused"
    assert not list(seam.root.rglob("*.md")), "a refused replay must write nothing"
    after = seam.store()
    assert seam.approval_id in after.pending(), "the hold must stand, as the screen claims"
    assert not after.is_granted(seam.approval_id), "no grant may outlive a refusal"


def test_a_grant_does_not_authorize_a_different_action(gate_parts):
    """An approval answers "yes to WHAT?", not merely "did a human say yes?".

    Reproduced before the fix: a grant issued to cross_check for a benign
    outbox draft let doc_chaser overwrite a canonical fixture, and the ledger
    recorded the substitution as `approved`. That single defect would falsify
    the whole trust boundary, so it gets a named seal.
    """
    ledger, approvals, gate = gate_parts
    held = gate(_Tool("write_file"), {"path": "outbox/benign.md", "content": "harmless"}, _Ctx())
    aid = held["approval_id"]
    approvals.approve(aid)

    class _OtherAgent:
        agent_name = "doc_chaser"

    substituted = gate(_Tool("write_file"),
                       {"path": "fixtures/shipments/shp-002-hero/waybill.md",
                        "content": "OVERWRITTEN", "_approval_id": aid}, _OtherAgent())
    assert substituted is not None, "the tool would have run on an unapproved action"
    assert substituted["status"] == "blocked"
    assert any(r.outcome == "blocked" for r in ledger.read())


def test_the_approved_action_itself_still_replays(gate_parts):
    """The binding must not break the flow it protects."""
    _, approvals, gate = gate_parts
    args = {"path": "outbox/notice.md", "content": "the draft"}
    held = gate(_Tool("write_file"), dict(args), _Ctx())
    aid = held["approval_id"]
    approvals.approve(aid)
    assert gate(_Tool("write_file"), {**args, "_approval_id": aid}, _Ctx()) is None


def test_a_refused_substitution_burns_the_grant(gate_parts):
    """A rejected substitution must not leave the grant standing to retry."""
    _, approvals, gate = gate_parts
    held = gate(_Tool("write_file"), {"path": "outbox/a.md", "content": "x"}, _Ctx())
    aid = held["approval_id"]
    approvals.approve(aid)
    gate(_Tool("write_file"), {"path": "outbox/ELSEWHERE.md", "content": "x",
                               "_approval_id": aid}, _Ctx())
    assert not approvals.is_granted(aid)
