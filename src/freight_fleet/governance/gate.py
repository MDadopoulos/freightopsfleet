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
        payload = self._pending.pop(approval_id, None)
        if payload is not None:
            self._granted.add(approval_id)
            if "fingerprint" in payload:
                self._fingerprints[approval_id] = payload["fingerprint"]
        self._save()
        return payload

    def reject(self, approval_id: str) -> dict[str, Any] | None:
        payload = self._pending.pop(approval_id, None)
        self._save()
        return payload

    def is_granted(self, approval_id: str | None) -> bool:
        return bool(approval_id) and approval_id in self._granted

    def consume(self, approval_id: str) -> None:
        """Retire a grant after its one permitted execution."""
        self._granted.discard(approval_id)
        self._fingerprints.pop(approval_id, None)
        self._save()

    def _save(self) -> None:  # no-op in memory; FileApprovalStore persists
        pass


class FileApprovalStore(ApprovalStore):
    """ApprovalStore persisted as JSON, so holds survive the one-turn CLI process
    and a later `approvals grant` can find them. Same contract, same single-use
    grants - durability must not loosen anything."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        super().__init__()
        self.path = Path(path)
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._pending = dict(raw.get("pending", {}))
            self._granted = set(raw.get("granted", []))
            self._fingerprints = dict(raw.get("fingerprints", {}))

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"pending": self._pending, "granted": sorted(self._granted),
                        "fingerprints": self._fingerprints}, indent=2),
            encoding="utf-8",
        )


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

    status: str                 # executed | rejected | not_pending | not_executable | gate_refused
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
    """
    payload = approvals.reject(approval_id)
    if payload is None:
        return ReplayResult("not_pending", approval_id,
                            detail=f"no pending approval {approval_id}")
    ledger.append(_decision_row(payload, approval_id, "rejected", "operator rejected", source))
    return ReplayResult("rejected", approval_id, tool=str(payload.get("tool", "")),
                        path=str((payload.get("args") or {}).get("path", "")),
                        detail="nothing was written")
