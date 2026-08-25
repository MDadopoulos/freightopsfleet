"""The approval gate — LucidOwl's ChangeSet trust boundary, ported to ADK callbacks.

ONE seam. Every tool call the model makes passes through `before_tool_gate`
before the tool body runs, and every result passes `after_tool_audit` on the way
back. There is no second path; a tool that bypasses this is a bug, not a feature.

Flow for a consequential call (verdict == ASK):
    model calls write_file
      -> gate classifies it HIGH -> ASK
      -> gate writes a `held` ledger row and returns a dict
      -> ADK short-circuits: the tool body NEVER runs, the dict becomes the
         tool result, and the model reports the pending approval to the operator
      -> operator approves out of band (CLI / UI)
      -> the approved call is replayed with `approval_id` in state

Returning a dict from `before_tool_callback` is what makes this work: ADK treats
it as the tool's result and skips the body. Verify that contract against the ADK
version you pin — it is the single load-bearing framework assumption here.

`execute_approved` / `reject_approved` at the foot of this file are the operator
half of the same seam. They live HERE, not in the CLI and not in the console,
because a second replay path is a second policy path — and policy in two places
is policy in neither (AGENTS.md #3). The tool bodies are injected as `tool_fns`
so `governance/` still imports nothing from `agents/`.

`reconcile` and `abandon_stranded` are the third piece: the ledger and the
approval store are two files written by two unsynchronised writes, and nothing
used to compare them anywhere an operator looks. `reconcile` reads both and
reports the disagreement; it classifies nothing and writes nothing, so it is not
a second policy check. `abandon_stranded` can only ever append one row — it
takes no `tool_fns`, which is the structural guarantee that a hold the store lost
can be closed in the record but never executed.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .ledger import Ledger, LedgerEntry
from .policy import Verdict, classify

#: A hold is resolved when a later ledger row carries its id as one of these.
#: ONE rule, in one place: `cli.py` and `console.py` each carried their own copy
#: and had already drifted apart ({approved, rejected} vs {approved, rejected,
#: executed}). They agreed on the data at hand by luck, which is the same disease
#: as policy in two files.
RESOLVING_OUTCOMES = frozenset({"approved", "rejected", "executed", "abandoned"})

#: Args worth recording verbatim in the ledger. Everything else is summarized to
#: a length, so a 40 KB file body never bloats the audit trail.
_DIGEST_KEYS = ("path", "pattern", "prefix", "to", "subject")


def action_fingerprint(tool: str, agent: str, args: dict[str, Any]) -> str:
    """A stable hash of the EXACT action a human approved.

    An approval id on its own answers "did a human say yes?" but not "yes to
    WHAT?" - and those are different questions. Without this, a grant issued for
    a benign draft authorizes any later call carrying the same id: a different
    agent, a different path, different content. The ledger would record that
    substitution as `approved`, which is worse than not logging it at all.

    `_approval_id` is excluded because it is the carrier of the grant, not part
    of the action being authorized.
    """
    material = {k: v for k, v in (args or {}).items() if k != "_approval_id"}
    canonical = json.dumps(
        {"tool": tool, "agent": agent, "args": digest_args(material)},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def digest_args(args: dict[str, Any]) -> dict[str, Any]:
    """A small, honest summary of a tool call's arguments."""
    out: dict[str, Any] = {}
    for k, v in (args or {}).items():
        if k in _DIGEST_KEYS:
            out[k] = v
        elif isinstance(v, str):
            out[f"{k}_chars"] = len(v)
        else:
            out[k] = repr(v)[:120]
    return out


class ApprovalStore:
    """Pending approvals, keyed by id. In-memory base; FileApprovalStore persists.

    A grant is SINGLE-USE: `consume` retires it the moment the gate lets the
    replay through. A reusable grant would let anything that ever saw the id
    replay the action forever - with a durable store that stops being a
    theoretical problem, so the grant tightens to one execution per approval.
    """

    def __init__(self) -> None:
        self._pending: dict[str, dict[str, Any]] = {}
        self._granted: set[str] = set()
        #: approval_id -> the fingerprint of the action it authorizes. Survives
        #: approve() because the grant outlives the pending entry.
        self._fingerprints: dict[str, str] = {}

    def hold(self, approval_id: str, payload: dict[str, Any]) -> None:
        self._reload()
        payload = dict(payload)
        payload.setdefault("fingerprint", action_fingerprint(
            payload.get("tool", ""), payload.get("agent", ""), payload.get("args") or {}))
        self._pending[approval_id] = payload
        self._save()

    def fingerprint_of(self, approval_id: str) -> str | None:
        """The action a grant was issued for. None if it predates fingerprinting."""
        return self._fingerprints.get(approval_id)

    def pending(self) -> dict[str, dict[str, Any]]:
        return dict(self._pending)

    def approve(self, approval_id: str) -> dict[str, Any] | None:
        self._reload()
        payload = self._pending.pop(approval_id, None)
        if payload is not None:
            self._granted.add(approval_id)
            if "fingerprint" in payload:
                self._fingerprints[approval_id] = payload["fingerprint"]
        self._save()
        return payload

    def reject(self, approval_id: str) -> dict[str, Any] | None:
        self._reload()
        payload = self._pending.pop(approval_id, None)
        self._save()
        return payload

    def granted(self) -> frozenset[str]:
        """The live grants. Public because a grant nobody can see is the
        fail-open half of this store: a standing, durable authorization."""
        return frozenset(self._granted)

    def is_granted(self, approval_id: str | None) -> bool:
        return bool(approval_id) and approval_id in self._granted

    def consume(self, approval_id: str) -> None:
        """Retire a grant after its one permitted execution."""
        self._reload()
        self._granted.discard(approval_id)
        self._fingerprints.pop(approval_id, None)
        self._save()

    def _reload(self) -> None:  # no-op in memory; FileApprovalStore re-reads
        """Refresh from the shared medium before mutating. See FileApprovalStore."""

    def _save(self) -> None:  # no-op in memory; FileApprovalStore persists
        pass


class FileApprovalStore(ApprovalStore):
    """ApprovalStore persisted as JSON, so holds survive the one-turn CLI process
    and a later `approvals grant` can find them. Same contract, same single-use
    grants - durability must not loosen anything."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        super().__init__()
        self.path = Path(path)
        self._reload()

    def _reload(self) -> None:
        """Re-read the file before mutating it, so a write applies to what is on
        disk now rather than to what was on disk when this object was built.

        The store is shared by unsynchronised processes: the sweep holds one
        store object open for its whole run while an operator decides in the
        console. `_save` serialises this object's ENTIRE state, so without this
        refresh the sweep's next `hold` writes back its stale snapshot and
        silently restores every entry the operator retired in the meantime -- a
        rejected draft reappears in the queue as pending. Re-reading first makes
        the file, not this object's memory, the authority for what is still
        queued.

        This narrows the window to the gap between this read and the `os.replace`
        in `_save`; there is no lock, so it does not close it. That is why the
        binding safety check lives in `execute_approved`, which asks the LEDGER
        whether the action was already decided. This refresh keeps the operator's
        queue honest; the ledger check is what keeps a rejection un-runnable.
        """
        if not self.path.exists():
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            # ValueError, not TypeError: `open_store` catches this to report the
            # store UNREADABLE rather than empty, and that distinction is a seal.
            raise ValueError(f"{self.path} is not an approval store")  # noqa: TRY004
        self._pending = dict(raw.get("pending", {}))
        self._granted = set(raw.get("granted", []))
        self._fingerprints = dict(raw.get("fingerprints", {}))

    def _save(self) -> None:
        """Write via a temp file and one atomic rename.

        `write_text` truncates first, so a process killed mid-write leaves
        partial JSON — and a partial store reads back as an EMPTY queue on the
        console, which is the worst possible failure: every pending approval
        silently disappears and the screen says the desk is clear. With
        `os.replace` a reader sees either the whole old store or the whole new
        one, never half of one.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(
            json.dumps({"pending": self._pending, "granted": sorted(self._granted),
                        "fingerprints": self._fingerprints}, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)


def make_before_tool_gate(ledger: Ledger, approvals: ApprovalStore, session_id: str):
    """Build the ADK `before_tool_callback`.

    Returns None to let the tool run; returns a dict to short-circuit it.
    """

    def before_tool_gate(tool, args: dict[str, Any], tool_context) -> dict[str, Any] | None:
        name = getattr(tool, "name", str(tool))
        spec, verdict = classify(name)
        agent = getattr(getattr(tool_context, "agent_name", None), "__str__", lambda: "unknown")()
        digest = digest_args(args)

        # An already-approved replay carries its grant in state. A grant is a
        # statement about ONE action, so the replay must BE that action.
        approval_id = (args or {}).get("_approval_id")
        if verdict is Verdict.ASK and approvals.is_granted(approval_id):
            approved = approvals.fingerprint_of(approval_id)
            presented = action_fingerprint(name, agent, args)
            if approved is not None and approved != presented:
                # Same id, different action. Refuse, retire the grant so the
                # substitution cannot simply be retried, and leave the original
                # hold pending for the operator.
                approvals.consume(approval_id)
                ledger.append(LedgerEntry.new(
                    session_id=session_id, agent=agent, tool=name,
                    risk=spec.risk.value if spec else "unknown", verdict=verdict.value,
                    outcome="blocked", args_digest=digest, approval_id=approval_id,
                    detail="approval id presented for a DIFFERENT action than the one approved",
                ))
                return {"status": "blocked",
                        "message": (f"approval {approval_id} authorized a different action; "
                                    "this call was not approved.")}
            approvals.consume(approval_id)  # single-use: a grant buys ONE execution
            ledger.append(LedgerEntry.new(
                session_id=session_id, agent=agent, tool=name,
                risk=spec.risk.value if spec else "unknown", verdict=verdict.value,
                outcome="approved", args_digest=digest, approval_id=approval_id,
                detail="human-approved replay",
            ))
            return None  # let it run

        if verdict is Verdict.BLOCK:
            ledger.append(LedgerEntry.new(
                session_id=session_id, agent=agent, tool=name,
                risk=spec.risk.value if spec else "unknown", verdict=verdict.value,
                outcome="blocked", args_digest=digest,
                detail="tool is not in the classified surface",
            ))
            return {"status": "blocked",
                    "message": f"'{name}' is not a permitted tool for this fleet."}

        if verdict is Verdict.ASK:
            entry = ledger.append(LedgerEntry.new(
                session_id=session_id, agent=agent, tool=name,
                risk=spec.risk.value if spec else "unknown", verdict=verdict.value,
                outcome="held", args_digest=digest,
                detail="consequential action held for operator approval",
            ))
            approvals.hold(entry.entry_id, {"tool": name, "args": args, "agent": agent})
            return {
                "status": "pending_approval",
                "approval_id": entry.entry_id,
                "tool": name,
                "summary": digest,
                "message": (
                    f"This action ({name}) is consequential and is held for approval. "
                    f"Tell the operator what it will do and that approval id "
                    f"{entry.entry_id} is waiting. Do not retry it."
                ),
            }

        ledger.append(LedgerEntry.new(
            session_id=session_id, agent=agent, tool=name,
            risk=spec.risk.value if spec else "unknown", verdict=verdict.value,
            outcome="auto_ran", args_digest=digest,
        ))
        return None

    return before_tool_gate


# --- the operator half of the seam -------------------------------------------
#
# A held action becomes a real one exactly here, and nowhere else. The CLI
# (`approvals grant`) and the console (POST /decision/{id}/approve) are two
# front doors onto ONE function; neither carries a copy of the rules.


@dataclass(frozen=True)
class ReplayResult:
    """The outcome of one operator decision.

    A value, not an exception, so a web handler can render an outcome instead of
    a 500 — and so the four ways a decision can fail to execute stay visible to
    the operator rather than being collapsed into "something went wrong".

    `detail` on an `executed` result carries the TOOL's own status, verbatim
    (`status=ok`, or `status=error: <message>`). A write that did not happen must
    never be reported as a write that did.
    """

    status: str                 # executed | rejected | abandoned | not_pending |
                                # not_executable | gate_refused | not_held |
                                # already_resolved | still_pending
    approval_id: str
    tool: str = ""
    path: str = ""
    bytes: int | None = None    # real bytes from the tool result, not the draft length
    detail: str = ""
    gate: dict[str, Any] | None = None   # the gate's own dict when status == gate_refused


def _decision_row(payload: dict[str, Any], approval_id: str, outcome: str,
                  detail: str, source: str) -> LedgerEntry:
    """The ledger row for an operator decision.

    `source` becomes the session id, so the record says WHERE a human decided —
    `approval-cli` or `approval-console` — rather than asserting one surface for
    every decision the fleet ever recorded.
    """
    return LedgerEntry.new(
        session_id=source, agent="operator", tool=payload.get("tool", "?"),
        risk="high", verdict="ask", outcome=outcome,
        args_digest=digest_args(payload.get("args") or {}),
        approval_id=approval_id, detail=detail,
    )


class _ReplayTool:
    """The minimal shape `before_tool_gate` reads off a tool: a name."""

    def __init__(self, name: str) -> None:
        self.name = name


class _ReplayCtx:
    """The minimal shape `before_tool_gate` reads off a tool context."""

    def __init__(self, agent_name: str) -> None:
        self.agent_name = agent_name


def execute_approved(approval_id: str, *, ledger: Ledger, approvals: ApprovalStore,
                     tool_fns: dict[str, Any], source: str) -> ReplayResult:
    """Grant, then REPLAY THROUGH THE GATE. The only path by which a held call runs.

    The replay is not a shortcut around the gate — it is a second trip through
    it, carrying the approval id. If the gate refuses, nothing executes, however
    freshly granted the id is.
    """
    payload = approvals.pending().get(approval_id)
    if payload is None:
        return ReplayResult("not_pending", approval_id,
                            detail=f"no pending approval {approval_id}")

    # THE RECORD OUTRANKS THE QUEUE. The store holds what is still actionable;
    # the ledger records what was decided. When the two disagree the ledger wins,
    # because a decision that provably happened cannot be undone by a file that
    # merely forgot it.
    #
    # Without this check a rejection is reversible by accident. `cmd_sweep` builds
    # one store object and keeps it for the whole run, and `_save` serialises that
    # entire in-memory snapshot on every `hold` -- so an operator who rejects a
    # draft in the console while the sweep is still running has their "no"
    # overwritten by the sweep's next hold, and the id reappears as pending.
    # Nothing downstream re-checked it, so the file was written and the ledger
    # read `held -> rejected -> approved -> executed` for a single action. A crash
    # between `reject_approved`'s two writes reaches the same state by a second
    # route. One check closes both, and it strictly tightens: it can only ever
    # turn an execution into a refusal, never the reverse (AGENTS.md #1).
    if any(e.approval_id == approval_id and e.outcome in RESOLVING_OUTCOMES
           for e in ledger.read()):
        return ReplayResult("already_resolved", approval_id,
                            tool=str(payload.get("tool", "")),
                            path=str((payload.get("args") or {}).get("path", "")),
                            detail="the record already resolves this hold; it is "
                                   "still queued -- run `approvals reconcile`")

    name = str(payload.get("tool", ""))
    args = dict(payload.get("args") or {})
    path = str(args.get("path", ""))

    # Look the tool body up BEFORE granting. Granting first and discovering the
    # tool is unwired second would retire the hold and leave a dangling grant —
    # the operator's action would vanish from the queue without ever running.
    # Failing here leaves the action still held, which is the tighter outcome.
    fn = tool_fns.get(name)
    if fn is None:
        return ReplayResult("not_executable", approval_id, tool=name, path=path,
                            detail=f"no executable body is wired for {name}")

    approvals.approve(approval_id)
    gate = make_before_tool_gate(ledger, approvals, session_id=source)
    replay_args = dict(args)
    replay_args["_approval_id"] = approval_id

    verdict = gate(_ReplayTool(name), replay_args, _ReplayCtx(str(payload.get("agent", "operator"))))
    if verdict is not None:
        # The gate refused, so the grant must not survive it. Put the hold back
        # and discard the grant: leaving the id in `granted` would be a standing
        # authorization — durable, single-use only in name — for the one action
        # the gate just declined, and it would silently vanish from the
        # operator's queue while the screen says the hold stands.
        approvals.consume(approval_id)
        approvals.hold(approval_id, payload)
        return ReplayResult("gate_refused", approval_id, tool=name, path=path, gate=verdict,
                            detail=f"the gate refused the replay: {verdict.get('status')}")

    replay_args.pop("_approval_id", None)
    result = fn(**replay_args)
    status = str(result.get("status", "unknown"))
    detail = f"status={status}" if status == "ok" else f"status={status}: {result.get('message', '')}".strip()
    ledger.append(_decision_row(payload, approval_id, "executed",
                                f"replayed after approval; result status={status}", source))
    return ReplayResult("executed", approval_id, tool=name,
                        path=str(result.get("path", path)),
                        bytes=result.get("bytes"), detail=detail)


def reject_approved(approval_id: str, *, ledger: Ledger, approvals: ApprovalStore,
                    source: str) -> ReplayResult:
    """Retire a hold without running it. Records `rejected`; writes nothing.

    A rejection is a decision, so it is evidence — the ledger row exists even
    though the world did not change. "Nothing happened" and "somebody decided
    nothing should happen" are different facts.

    The RECORD is written before the queue is touched, matching the hold path.
    The other order fails open on the record: a crash between the two writes
    removed the hold from the store with no `rejected` row anywhere, leaving a
    decision that provably happened and is indistinguishable, afterwards, from
    one that was silently lost. Writing the row first can at worst leave a
    rejection recorded twice-decidable, never un-recorded.
    """
    payload = approvals.pending().get(approval_id)
    if payload is None:
        return ReplayResult("not_pending", approval_id,
                            detail=f"no pending approval {approval_id}")
    ledger.append(_decision_row(payload, approval_id, "rejected", "operator rejected", source))
    approvals.reject(approval_id)
    return ReplayResult("rejected", approval_id, tool=str(payload.get("tool", "")),
                        path=str((payload.get("args") or {}).get("path", "")),
                        detail="nothing was written")


# --- the record vs the queue --------------------------------------------------
#
# The ledger and the approval store are two files, written by two unsynchronised
# writes, and until now nothing compared them anywhere an operator looks. The
# hold path writes the ledger row first and the store second on purpose — a crash
# between them strands the action, which fails CLOSED: nothing runs and the record
# is complete. The bug was never the ordering. It was that the divergence was
# invisible: `approvals list` printed "no actions awaiting approval" in the same
# second the ledger held six.
#
# `reconcile` is a READ. It classifies no tool, loosens no verdict and cannot turn
# `ask` into `auto`, so it is not a second policy check (AGENTS.md #1, #3).


@dataclass(frozen=True)
class Stranded:
    """A hold the ledger records and the store cannot act on.

    Every field comes from the ledger row. There is deliberately NO `content`
    field: the draft lived only in the store, `digest_args` kept its length and
    not its body, and inventing a replacement is precisely the failure this type
    exists to prevent. A stranded hold can be described completely and can never
    be executed.
    """

    approval_id: str
    ts: str
    session_id: str
    agent: str
    tool: str
    path: str
    content_chars: int | None
    reason: str          # absent_from_store | store_unreadable

    @staticmethod
    def from_entry(entry: LedgerEntry, reason: str) -> Stranded:
        digest = entry.args_digest or {}
        chars = digest.get("content_chars")
        return Stranded(
            approval_id=entry.entry_id, ts=entry.ts, session_id=entry.session_id,
            agent=entry.agent, tool=entry.tool, path=str(digest.get("path", "")),
            content_chars=chars if isinstance(chars, int) else None, reason=reason,
        )


@dataclass(frozen=True)
class Reconciliation:
    """What the two stores agree and disagree about.

    The two disagreements fail in opposite directions, which is why they are
    counted separately rather than summed into one "problems" number:

    * `stranded` fails CLOSED — in the record, absent from the queue. Nothing can
      run it. The cost is a decision the operator can never make.
    * `orphaned` fails OPEN — in the queue, with no `held` row in the record.
      Something is approvable that the ledger never authorized, and granting it
      would write a file with no preceding `held` row anywhere.
    * `dangling_grants` also fails OPEN — a persisted grant with no pending entry.
      A durable standing authorization is single-use in name only.
    * `resolved_but_pending` fails OPEN and is the worst of the four — the record
      says a human already decided this action, and the queue is still offering
      it. That is a rejection the operator can be asked to re-approve. It is
      counted apart from `orphaned` because the id DOES have a `held` row: the
      ledger authorized the hold, then resolved it, and only the queue disagrees.

    A note on why this type enumerates four and not three: the first version of
    this dataclass documented three failure modes as though they were exhaustive,
    and `resolved_but_pending` fell through every one of them. An id resolved in
    the record drops out of `unresolved`, so it reaches neither `awaiting` nor
    `stranded`; it has a `held` row, so it is not `orphaned`. `reconcile`
    reported `diverged=False` over a rejected action sitting executable in the
    queue. Treat the list above as the modes found so far, not as a proof.
    """

    awaiting: list[str]
    stranded: list[Stranded]
    orphaned: list[str]
    dangling_grants: list[str]
    resolved_but_pending: list[str]
    store_readable: bool

    @property
    def diverged(self) -> bool:
        return bool(self.stranded or self.orphaned or self.dangling_grants
                    or self.resolved_but_pending) \
            or not self.store_readable


def open_store(path: str | os.PathLike[str]) -> FileApprovalStore | None:
    """Load a store, distinguishing "no store yet" from "store unreadable".

    `None` means UNREADABLE, and callers must say so rather than rendering an
    empty queue. A missing file is a legitimately empty store and returns one:
    "nothing is pending" and "the queue could not be read" are different facts,
    and collapsing them is how a corrupted store reports a clear desk.
    """
    try:
        return FileApprovalStore(path)
    except (OSError, ValueError, TypeError):
        return None


def reconcile(ledger: Ledger, approvals: ApprovalStore | None, *,
              rows: list[LedgerEntry] | None = None) -> Reconciliation:
    """Compare the record against the queue. Reads both; writes neither.

    `approvals=None` means the store could not be read — every unresolved hold is
    then stranded, because nothing can act on any of them.

    `rows` lets a caller inject ledger rows it has already read. The console
    needs it twice over: its loader tolerates a line that will not parse (where
    `Ledger.read` raises), and a reconciler that re-read the file would be a
    second, differently-tolerant view of the same evidence on the same page.
    """
    if rows is None:
        rows = list(ledger.read())
    resolved = {e.approval_id for e in rows
                if e.outcome in RESOLVING_OUTCOMES and e.approval_id}
    held = [e for e in rows if e.outcome == "held"]
    unresolved = [e for e in held if e.entry_id not in resolved]

    pending = approvals.pending() if approvals is not None else {}
    grants = approvals.granted() if approvals is not None else frozenset()
    reason = "absent_from_store" if approvals is not None else "store_unreadable"
    held_ids = {e.entry_id for e in held}

    return Reconciliation(
        awaiting=[e.entry_id for e in unresolved if e.entry_id in pending],
        stranded=[Stranded.from_entry(e, reason) for e in unresolved
                  if e.entry_id not in pending],
        orphaned=sorted(aid for aid in pending if aid not in held_ids),
        dangling_grants=sorted(g for g in grants
                               if g not in pending and g not in resolved),
        resolved_but_pending=sorted(aid for aid in pending
                                    if aid in resolved and aid in held_ids),
        store_readable=approvals is not None,
    )


def abandon_stranded(approval_id: str, *, ledger: Ledger, approvals: ApprovalStore,
                     source: str, note: str = "") -> ReplayResult:
    """Retire a hold the store lost, WITHOUT executing it.

    There is no execution branch here and no `tool_fns` parameter, on purpose.
    A stranded hold's draft is unrecoverable — the ledger kept its length, not
    its body — and `action_fingerprint` hashes `digest_args`, so a same-length
    substitute fingerprints IDENTICALLY to the original. The gate therefore
    cannot be the thing that refuses a fabricated recovery. This function refuses
    structurally instead: the only thing it can do is append one row.

    Tighten-only (AGENTS.md #1): nothing here makes anything executable. The
    ledger stays append-only — the hold row is not rewritten, a later row records
    that a human retired it.
    """
    rows = list(ledger.read())
    held = next((e for e in rows
                 if e.entry_id == approval_id and e.outcome == "held"), None)
    if held is None:
        return ReplayResult("not_held", approval_id,
                            detail=f"no held row in the ledger carries id {approval_id}")
    if any(e.approval_id == approval_id and e.outcome in RESOLVING_OUTCOMES for e in rows):
        return ReplayResult("already_resolved", approval_id, tool=held.tool,
                            detail="the record already resolves this hold")
    if approval_id in approvals.pending():
        # Still actionable. One condition, one function: a hold the operator can
        # still see belongs to `reject_approved`, which records a DECISION.
        # Abandoning it would record "the store lost this" about a store that
        # did not lose it.
        return ReplayResult("still_pending", approval_id, tool=held.tool,
                            detail="still in the approval store — reject it instead")

    digest = dict(held.args_digest or {})
    detail = "stranded: absent from the approval store; retired without execution"
    if note:
        detail = f"{detail} — {note}"
    ledger.append(LedgerEntry.new(
        session_id=source, agent="operator", tool=held.tool, risk=held.risk,
        verdict=held.verdict, outcome="abandoned",
        # Copied verbatim from the held row, never recomputed: there is nothing
        # left to recompute it from, and a recomputed digest would be a claim
        # about args this process never saw.
        args_digest=digest, approval_id=approval_id, detail=detail,
    ))
    return ReplayResult("abandoned", approval_id, tool=held.tool,
                        path=str(digest.get("path", "")),
                        detail="nothing was written; the draft was not recoverable")
