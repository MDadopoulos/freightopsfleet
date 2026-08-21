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

import json
import os
from pathlib import Path
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
    """Pending approvals, keyed by id. In-memory base; FileApprovalStore persists.

    A grant is SINGLE-USE: `consume` retires it the moment the gate lets the
    replay through. A reusable grant would let anything that ever saw the id
    replay the action forever - with a durable store that stops being a
    theoretical problem, so the grant tightens to one execution per approval.
    """

    def __init__(self) -> None:
        self._pending: dict[str, dict[str, Any]] = {}
        self._granted: set[str] = set()

    def hold(self, approval_id: str, payload: dict[str, Any]) -> None:
        self._pending[approval_id] = payload
        self._save()

    def pending(self) -> dict[str, dict[str, Any]]:
        return dict(self._pending)

    def approve(self, approval_id: str) -> dict[str, Any] | None:
        payload = self._pending.pop(approval_id, None)
        if payload is not None:
            self._granted.add(approval_id)
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

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"pending": self._pending, "granted": sorted(self._granted)}, indent=2),
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

        # An already-approved replay carries its grant in state.
        approval_id = (args or {}).get("_approval_id")
        if verdict is Verdict.ASK and approvals.is_granted(approval_id):
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
