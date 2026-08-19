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
"""

from __future__ import annotations

from typing import Any

from .ledger import Ledger, LedgerEntry
from .policy import Verdict, classify

#: Args worth recording verbatim in the ledger. Everything else is summarized to
#: a length, so a 40 KB file body never bloats the audit trail.
_DIGEST_KEYS = ("path", "pattern", "prefix", "to", "subject")


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
    """Pending approvals, keyed by id. In-memory for V1; swap for Firestore later."""

    def __init__(self) -> None:
        self._pending: dict[str, dict[str, Any]] = {}
        self._granted: set[str] = set()

    def hold(self, approval_id: str, payload: dict[str, Any]) -> None:
        self._pending[approval_id] = payload

    def pending(self) -> dict[str, dict[str, Any]]:
        return dict(self._pending)

    def approve(self, approval_id: str) -> dict[str, Any] | None:
        payload = self._pending.pop(approval_id, None)
        if payload is not None:
            self._granted.add(approval_id)
        return payload

    def reject(self, approval_id: str) -> dict[str, Any] | None:
        return self._pending.pop(approval_id, None)

    def is_granted(self, approval_id: str | None) -> bool:
        return bool(approval_id) and approval_id in self._granted


def make_before_tool_gate(ledger: Ledger, approvals: ApprovalStore, session_id: str):
    """Build the ADK `before_tool_callback`.

    Returns None to let the tool run; returns a dict to short-circuit it.
    """

    def before_tool_gate(tool, args: dict[str, Any], tool_context) -> dict[str, Any] | None:
        name = getattr(tool, "name", str(tool))
        spec, verdict = classify(name)
        agent = getattr(getattr(tool_context, "agent_name", None), "__str__", lambda: "unknown")()
        digest = digest_args(args)

        # An already-approved replay carries its grant in state.
        approval_id = (args or {}).get("_approval_id")
        if verdict is Verdict.ASK and approvals.is_granted(approval_id):
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
