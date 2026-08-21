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


# --- the record vs the queue -------------------------------------------------
# The ledger and the approval store are two files written by two unsynchronised
# writes. These seal the two directions they can come apart, and seal the fact
# that a hold the store lost can be DESCRIBED but never EXECUTED.

def _held_row(ledger, approvals, path="outbox/n.md", content="x" * 2467, agent="cross_check"):
    """One real hold, placed by the real gate, so the approval id IS the entry id."""
    gate = make_before_tool_gate(ledger, approvals, "sweep-test")

    class _A:
        agent_name = agent

    return gate(_Tool("write_file"), {"path": path, "content": content}, _A())["approval_id"]


@pytest.fixture()
def two_stores(tmp_path):
    from freight_fleet.governance.gate import FileApprovalStore

    ledger = Ledger(tmp_path / "ledger.jsonl")
    store = FileApprovalStore(tmp_path / "approvals.json")
    return SimpleNamespace(
        tmp=tmp_path, ledger=ledger, store=store,
        reopen=lambda: FileApprovalStore(tmp_path / "approvals.json"),
    )


def test_a_lost_store_is_a_divergence_not_an_empty_queue(two_stores):
    """The reported defect: `approvals list` printed "no actions awaiting
    approval" in the same second the ledger held six unresolved rows. Nothing
    executes either way — that half already failed closed — but the operator's
    decision surface must not report a clear desk."""
    from freight_fleet.governance.gate import open_store, reconcile

    aid = _held_row(two_stores.ledger, two_stores.store)
    (two_stores.tmp / "approvals.json").unlink()

    recon = reconcile(two_stores.ledger, open_store(two_stores.tmp / "approvals.json"))
    assert [st.approval_id for st in recon.stranded] == [aid]
    assert recon.awaiting == []
    assert recon.diverged is True
    assert recon.store_readable is True, "a MISSING store is an empty queue, not an unreadable one"


def test_a_store_entry_with_no_held_row_fails_open_and_is_reported(two_stores):
    """The direction nobody had looked at. Stranded fails closed; ORPHANED fails
    open — something is approvable that the record never authorized, and
    granting it would write a file with no preceding `held` row anywhere."""
    from freight_fleet.governance.gate import reconcile

    two_stores.store.hold("never-held", {"tool": "write_file", "args": {"path": "outbox/x.md"},
                                         "agent": "cross_check"})
    recon = reconcile(two_stores.ledger, two_stores.store)

    assert recon.orphaned == ["never-held"]
    assert recon.stranded == []
    assert recon.diverged is True


def test_a_grant_with_nothing_pending_is_reported(two_stores):
    """A persisted grant that outlives its pending entry is a durable standing
    authorization — single-use in name only. It has no ledger row and no card on
    the desk, so the reconciler is the only place it can surface."""
    from freight_fleet.governance.gate import reconcile

    aid = _held_row(two_stores.ledger, two_stores.store)
    two_stores.store.approve(aid)  # granted, pending popped, replay never happened

    recon = reconcile(two_stores.ledger, two_stores.store)
    assert recon.dangling_grants == [aid]
    assert recon.diverged is True


def test_an_unreadable_store_is_never_reported_as_an_empty_queue(two_stores):
    """A truncated store used to read back as `{}` on the console — every pending
    approval silently vanishing while the screen said the desk was clear."""
    from freight_fleet.governance.gate import open_store, reconcile

    aid = _held_row(two_stores.ledger, two_stores.store)
    (two_stores.tmp / "approvals.json").write_text('{"pending": {"a": ', encoding="utf-8")

    store = open_store(two_stores.tmp / "approvals.json")
    assert store is None, "an unreadable store must not masquerade as an empty one"

    recon = reconcile(two_stores.ledger, store)
    assert recon.store_readable is False
    assert [st.reason for st in recon.stranded] == ["store_unreadable"]
    assert [st.approval_id for st in recon.stranded] == [aid]
    assert recon.diverged is True


def test_the_store_write_is_atomic(two_stores):
    """`write_text` truncates first, so a kill mid-write left partial JSON — and
    partial JSON is the input to the failure above. One rename means a reader
    sees the whole old store or the whole new one, never half of one."""
    aid = _held_row(two_stores.ledger, two_stores.store)
    two_stores.store.hold("second", {"tool": "write_file", "args": {"path": "outbox/b.md"},
                                     "agent": "cross_check"})

    assert set(two_stores.reopen().pending()) == {aid, "second"}
    assert not list(two_stores.tmp.glob("*.tmp")), "the temp file must not survive the rename"


def test_a_stranded_hold_carries_its_shape_and_never_its_draft(two_stores):
    """Everything recoverable, recovered; the one unrecoverable thing named as
    unrecoverable. `Stranded` has no `content` field, so there is nowhere for a
    fabricated body to live."""
    import dataclasses

    from freight_fleet.governance.gate import Stranded, open_store, reconcile

    aid = _held_row(two_stores.ledger, two_stores.store, path="outbox/notice.md")
    (two_stores.tmp / "approvals.json").unlink()

    st = reconcile(two_stores.ledger, open_store(two_stores.tmp / "approvals.json")).stranded[0]
    assert (st.approval_id, st.tool, st.path) == (aid, "write_file", "outbox/notice.md")
    assert (st.agent, st.session_id) == ("cross_check", "sweep-test")
    assert st.content_chars == 2467
    assert "content" not in {f.name for f in dataclasses.fields(Stranded)}


def test_fingerprint_is_blind_to_content_of_equal_length():
    """The weakness that decides the recovery design, asserted so it is on the
    record and a future fix has a test to flip.

    `action_fingerprint` hashes `digest_args`, which keeps a string's LENGTH and
    not its bytes. So the grant is bound to (tool, agent, path, content length) —
    a same-length substitute is byte-for-byte indistinguishable to the gate. This
    is why no recovery path may reconstruct a payload and let the gate decide:
    the gate cannot tell. The fix is a content hash in the digest, which changes
    every existing fingerprint and therefore belongs in its own change.
    """
    from freight_fleet.governance.gate import action_fingerprint

    a = action_fingerprint("write_file", "cross_check",
                           {"path": "outbox/n.md", "content": "A" * 2467})
    b = action_fingerprint("write_file", "cross_check",
                           {"path": "outbox/n.md", "content": "Z" * 2467})
    assert a == b, "documents the CURRENT behaviour; if this goes red the weakness was fixed"

    different_length = action_fingerprint("write_file", "cross_check",
                                          {"path": "outbox/n.md", "content": "A" * 2468})
    assert a != different_length


# --- abandoning a stranded hold ----------------------------------------------
# The only thing `abandon_stranded` can do is append one row. These seal that
# it stays that way: tighten-only (AGENTS.md #1) means a recovery mechanism may
# never make executable something that was not already approved.

def test_abandon_writes_one_row_and_never_a_file(two_stores):
    from freight_fleet.governance.gate import RESOLVING_OUTCOMES, abandon_stranded, reconcile

    aid = _held_row(two_stores.ledger, two_stores.store, path="outbox/notice.md")
    (two_stores.tmp / "approvals.json").unlink()
    store = two_stores.reopen()
    before = list(two_stores.ledger.read())

    result = abandon_stranded(aid, ledger=two_stores.ledger, approvals=store,
                              source="approval-cli", note="lost in the incident")

    assert result.status == "abandoned"
    rows = list(two_stores.ledger.read())
    assert len(rows) == len(before) + 1, "exactly one row"
    row = rows[-1]
    assert (row.outcome, row.agent, row.approval_id) == ("abandoned", "operator", aid)
    assert row.args_digest == {"path": "outbox/notice.md", "content_chars": 2467}, \
        "copied verbatim from the held row — there is nothing left to recompute from"
    assert "lost in the incident" in row.detail
    assert row.outcome in RESOLVING_OUTCOMES

    assert store.granted() == frozenset(), "abandoning must never grant anything"
    assert store.pending() == {}
    assert not list(two_stores.tmp.rglob("*.md")), "nothing may be written to the workspace"
    assert reconcile(two_stores.ledger, two_stores.reopen()).diverged is False


def test_abandon_takes_no_tool_bodies_at_all():
    """The structural guarantee. A stranded draft is unrecoverable and a
    same-length substitute fingerprints identically, so the gate cannot be what
    refuses a fabricated recovery — the absence of an execution branch is."""
    import inspect

    from freight_fleet.governance.gate import abandon_stranded, execute_approved

    assert "tool_fns" in inspect.signature(execute_approved).parameters
    assert "tool_fns" not in inspect.signature(abandon_stranded).parameters
    code = inspect.getsource(abandon_stranded).replace(abandon_stranded.__doc__ or "", "")
    assert "tool_fns" not in code, "no tool body may be reachable from here"
    assert "(**" not in code and "fn(" not in code


def test_abandon_refuses_a_hold_the_store_still_has(two_stores):
    """One condition, one function. A hold the operator can still see is a
    DECISION for `reject_approved`; abandoning it would record "the store lost
    this" about a store that did not lose it."""
    from freight_fleet.governance.gate import abandon_stranded

    aid = _held_row(two_stores.ledger, two_stores.store)
    before = len(list(two_stores.ledger.read()))

    result = abandon_stranded(aid, ledger=two_stores.ledger, approvals=two_stores.store,
                              source="approval-cli")

    assert result.status == "still_pending"
    assert len(list(two_stores.ledger.read())) == before, "a refusal writes no row"
    assert aid in two_stores.reopen().pending(), "the hold must stand"


def test_abandon_refuses_an_already_resolved_or_unknown_id(two_stores):
    from freight_fleet.governance.gate import abandon_stranded, reject_approved

    aid = _held_row(two_stores.ledger, two_stores.store)
    reject_approved(aid, ledger=two_stores.ledger, approvals=two_stores.store,
                    source="approval-cli")
    before = len(list(two_stores.ledger.read()))

    assert abandon_stranded(aid, ledger=two_stores.ledger, approvals=two_stores.store,
                            source="approval-cli").status == "already_resolved"
    assert abandon_stranded("fabricated", ledger=two_stores.ledger, approvals=two_stores.store,
                            source="approval-cli").status == "not_held"
    assert len(list(two_stores.ledger.read())) == before


def test_rejection_records_before_it_dequeues(two_stores):
    """The record is written first, matching the hold path. The other order fails
    OPEN on the record: a crash between the two writes removed the hold from the
    store with no `rejected` row anywhere."""
    from freight_fleet.governance import gate as gatemod
    from freight_fleet.governance.gate import reject_approved

    aid = _held_row(two_stores.ledger, two_stores.store)
    seen: list[int] = []
    real_reject = gatemod.ApprovalStore.reject

    def spy(self, approval_id):
        seen.append(sum(1 for e in two_stores.ledger.read() if e.outcome == "rejected"))
        return real_reject(self, approval_id)

    gatemod.ApprovalStore.reject = spy
    try:
        assert reject_approved(aid, ledger=two_stores.ledger, approvals=two_stores.store,
                               source="approval-cli").status == "rejected"
    finally:
        gatemod.ApprovalStore.reject = real_reject

    assert seen == [1], "the `rejected` row must already exist when the queue is touched"
    assert aid not in two_stores.reopen().pending()


def test_one_definition_of_resolved(two_stores):
    """`cli.py` said {approved, rejected} and `console.py` said {approved,
    rejected, executed}. One rule in two files is the same disease as policy in
    two places; both now import this set."""
    import inspect

    from freight_fleet import cli, console
    from freight_fleet.governance.gate import RESOLVING_OUTCOMES

    assert RESOLVING_OUTCOMES == frozenset({"approved", "rejected", "executed", "abandoned"})
    for module in (cli, console):
        source = inspect.getsource(module)
        assert '{"approved", "rejected"}' not in source
        assert '{"approved", "rejected", "executed"}' not in source
        assert "RESOLVING_OUTCOMES" in source


# --- the unattended half remembers -------------------------------------------

def _body(fn) -> str:
    """A function's source with its docstring removed.

    These tests read source because the properties they seal are structural —
    which session service, which id — and a docstring that DESCRIBES the old
    behaviour must not satisfy or break an assertion about the new one.
    """
    import inspect

    return inspect.getsource(fn).replace(fn.__doc__ or "\0", "")


def test_the_sweep_conversation_id_does_not_move_with_the_calendar():
    """The continuity property, stated as a test.

    The sweep has TWO ids on two different axes: the LEDGER session is the run
    (`sweep-<date>`, must change every morning) and the ADK conversation is the
    subject (`shipment-<dir>`, must NOT). Collapsing them is what made the
    unattended half forget: a sweep that ran every morning for three weeks
    started from nothing twenty-one times.
    """
    from freight_fleet.cli import sweep_session_id

    assert sweep_session_id("shp-003-container-refs") == "shipment-shp-003-container-refs"
    assert sweep_session_id("a") != sweep_session_id("b"), (
        "one conversation per shipment: a shared one would carry one shipment's "
        "documents into the next shipment's context")
    # A date parameter it does not have is a way it cannot drift.
    import inspect

    assert set(inspect.signature(sweep_session_id).parameters) == {"shipment"}


def test_the_sweep_uses_a_durable_session_service():
    """`chat` was durable and `sweep` was not, so the two halves of the entry's
    claim never intersected. The sweep must use the same DatabaseSessionService
    over the same SQLite file, or "sessions survive process death" is true only
    of the half a human is watching."""
    from freight_fleet import cli

    source = _body(cli.cmd_sweep)
    assert "DatabaseSessionService" in source
    assert "InMemorySessionService" not in source
    assert "sweep_session_id(shipment)" in source
    # The same URL requirement and parent mkdir `cmd_chat` has: without them
    # DatabaseSessionService fails on a fresh clone.
    assert 'startswith("sqlite")' in source
    assert "mkdir(parents=True, exist_ok=True)" in source


def test_the_sweep_prompt_claims_no_history_it_cannot_prove():
    """Honesty rule for the continuity prompt: it may report the event count the
    session service actually returned, and nothing else. No weeks, no run count,
    no "as usual" on a first run."""
    from freight_fleet import cli

    source = _body(cli.cmd_sweep)
    assert "len(session.events)" in source
    assert "first check of this shipment" in source, "a new conversation must say so"
    for invented in ("three weeks", "every morning for", "as usual", "yesterday"):
        assert invented not in source, f"unprovable claim in the prompt: {invented}"


# --- the operator's decision surface -----------------------------------------
# The defect was never that a hold could be lost — it was that losing one was
# undetectable from the screen that answers "is anything waiting?".

@pytest.fixture()
def desk(tmp_path, monkeypatch):
    """A console pointed at a temp world with one real hold, and a switch to
    strand it: delete the store the way a lost `data/` directory does."""
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
    gate = make_before_tool_gate(ledger, store, "sweep-2026-08-21")
    gate(_Tool("read_file"), {"path": "shipments/shp-t01/waybill.md"}, _Ctx())
    aid = gate(_Tool("write_file"),
               {"path": "outbox/shp-t01-notice.md", "content": "x" * 2467},
               _Ctx())["approval_id"]
    return SimpleNamespace(
        tmp=tmp_path, root=root, ledger=ledger, approval_id=aid,
        client=TestClient(console.app),
        strand=lambda: (tmp_path / "approvals.json").unlink(),
    )


def test_the_desk_does_not_claim_a_clear_desk_over_a_stranded_hold(desk):
    """The reported falsehood, verbatim: the front screen said the last sweep
    "held nothing. Your desk is clear." while the same ledger held five. The
    Record screen already computed the truth; it just never reached the screen
    that answers the question."""
    desk.strand()
    body = desk.client.get("/").text

    assert "desk is clear" not in body
    assert "held nothing" not in body
    assert "Nothing waiting" not in body
    assert "STRANDED" in body
    assert "not a backlog" in body, "a divergence must not read as a queue"


def test_the_desk_still_reads_normally_when_the_two_stores_agree(desk):
    """The strip must not cry wolf. With the store intact there is no divergence
    and nothing extra on the page."""
    body = desk.client.get("/").text

    assert "STRANDED" not in body
    assert "COULD NOT BE READ" not in body
    assert "Decisions waiting" in body


def test_an_unreadable_store_never_renders_as_a_clear_desk(desk):
    """Verified as the original failure: a truncated store made the console
    report zero pending, with no warning, because `load_pending` swallowed the
    ValueError and returned `{}`."""
    (desk.tmp / "approvals.json").write_text('{"pending": {"a": ', encoding="utf-8")
    body = desk.client.get("/").text

    assert "Nothing waiting" not in body
    assert "desk is clear" not in body
    assert "COULD NOT BE READ" in body
    assert desk.client.get("/reconcile.json").json()["store_readable"] is False


def test_a_stranded_hold_offers_no_way_to_execute_it(desk):
    """The exhibit page describes what was lost and offers two honest actions —
    redo the work, or close the row — and no third one. Approving it from the
    browser must fail closed and must not be reported as a decision."""
    desk.strand()
    page = desk.client.get(f"/decision/{desk.approval_id}").text

    assert "cannot be restored" in page
    assert "approvals abandon" in page
    assert "2,467 characters" in page, "the shape is recoverable and is stated"
    assert "/approve" not in page, "no execute affordance may exist on a stranded hold"

    before = len(list(desk.ledger.read()))
    flash = desk.client.post(f"/decision/{desk.approval_id}/approve").text
    assert "NOT EXECUTED — STRANDED" in flash
    assert "ALREADY DECIDED" not in flash, "nobody decided anything; the queue lost it"
    assert len(list(desk.ledger.read())) == before, "a failed approve writes no row"
    assert not list(desk.root.rglob("*.md")), "nothing may be written"


def test_reconcile_json_never_serves_a_draft_body(desk):
    """There is no draft to serve, and a placeholder of the right length would
    fingerprint identically to the original action."""
    desk.strand()
    payload = desk.client.get("/reconcile.json").json()

    assert payload["diverged"] is True
    assert payload["stranded"] == 1
    row = payload["stranded_detail"][0]
    assert row["content_chars"] == 2467
    assert row["draft_recoverable"] is False
    assert "content" not in row and "draft" not in row
    assert desk.client.get("/healthz").json() == {"ok": True}, "liveness stays file-free"


# --- a human's "no" is final (AGENTS.md #1) -----------------------------------
#
# Reproduced live before these were written: the sweep held a draft, the operator
# rejected it in the console, the sweep held the NEXT shipment, and the rejected
# id came back pending. `execute_approved` consulted only the queue, so the file
# was written and the ledger read `held -> rejected -> approved -> executed` for
# one action. `reconcile` reported `diverged=False` throughout.


def _reject(aid, ledger, store_path):
    from freight_fleet.governance.gate import open_store, reject_approved

    return reject_approved(aid, ledger=ledger, approvals=open_store(store_path),
                           source="approval-console")


def test_a_rejection_is_not_undone_by_the_next_hold(two_stores):
    """The sweep keeps ONE store object for its whole run and `_save` writes that
    object's entire state. Without a refresh before each mutation, the sweep's
    next `hold` writes back a snapshot taken before the operator decided, and
    every entry they retired in the meantime silently returns to the queue."""
    path = two_stores.tmp / "approvals.json"
    sweep = two_stores.store                      # held open, as cmd_sweep does
    first = _held_row(two_stores.ledger, sweep, path="outbox/shp-002.md")

    assert _reject(first, two_stores.ledger, path).status == "rejected"
    assert list(two_stores.reopen().pending()) == []

    second = _held_row(two_stores.ledger, sweep, path="outbox/shp-003.md")

    assert list(two_stores.reopen().pending()) == [second], \
        "the rejected draft came back into the operator's queue"


def test_the_record_outranks_the_queue(two_stores):
    """Defense in depth: even with the queue corrupted by hand — which the
    unlocked window between reload and rename still permits, and which a crash
    between `reject_approved`'s two writes reaches by a second route — a decided
    action must not run. The LEDGER is asked, not the store."""
    import json

    from freight_fleet.governance.gate import execute_approved, open_store

    path = two_stores.tmp / "approvals.json"
    aid = _held_row(two_stores.ledger, two_stores.store)
    payload = two_stores.reopen().pending()[aid]
    assert _reject(aid, two_stores.ledger, path).status == "rejected"

    # Forcibly resurrect it, exactly as the stale snapshot used to.
    raw = json.loads(path.read_text())
    raw["pending"][aid] = payload
    path.write_text(json.dumps(raw))
    assert aid in two_stores.reopen().pending()

    wrote: list[str] = []
    out = execute_approved(
        aid, ledger=two_stores.ledger, approvals=open_store(path),
        tool_fns={"write_file": lambda path, content: wrote.append(path) or {"status": "ok"}},
        source="approval-cli",
    )

    assert out.status == "already_resolved"
    assert wrote == [], "a rejected action was executed"
    outcomes = [e.outcome for e in two_stores.ledger.read()
                if (e.approval_id or e.entry_id) == aid]
    assert outcomes == ["held", "rejected"], f"the record was extended past the decision: {outcomes}"


def test_reconcile_reports_a_decided_action_that_is_still_queued(two_stores):
    """It did not, and that was the worse half: the state above was certified
    healthy. A resolved id drops out of `unresolved`, so it reached neither
    `awaiting` nor `stranded`; it has a `held` row, so it was not `orphaned`."""
    import json

    from freight_fleet.governance.gate import open_store, reconcile

    path = two_stores.tmp / "approvals.json"
    aid = _held_row(two_stores.ledger, two_stores.store)
    payload = two_stores.reopen().pending()[aid]
    _reject(aid, two_stores.ledger, path)
    raw = json.loads(path.read_text())
    raw["pending"][aid] = payload
    path.write_text(json.dumps(raw))

    recon = reconcile(two_stores.ledger, open_store(path))

    assert recon.resolved_but_pending == [aid]
    assert recon.diverged is True
    assert recon.orphaned == [] and recon.stranded == [] and recon.awaiting == [], \
        "it must be counted apart from the three modes that never caught it"
