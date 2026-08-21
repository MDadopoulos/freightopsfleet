"""Seals on the trust boundary. These are the tests that must never go red.

Each asserts a rule from AGENTS.md. If you change behavior such that one of
these fails, you have removed a safety property — go read why it was there.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

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
    ledger, approvals, gate = gate_parts
    held = gate(_Tool("write_file"), {"path": "outbox/x.md", "content": "hi"}, _Ctx())
    aid = held["approval_id"]
    approvals.approve(aid)
    assert gate(_Tool("write_file"), {"path": "outbox/x.md", "_approval_id": aid}, _Ctx()) is None
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
