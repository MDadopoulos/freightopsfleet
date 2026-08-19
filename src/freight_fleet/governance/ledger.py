"""Append-only audit ledger — the record an operator can defend to their boss.

Every gate decision lands here, whichever way it went: auto-run, held, approved,
rejected, blocked. The ledger is APPEND-ONLY by contract; there is no update and
no delete, because a record that can be edited is not evidence.

V1 storage is JSONL on local disk (zero infrastructure, greppable, diffable).
The `LedgerEntry` shape is storage-agnostic — swapping in Firestore for the
cloud demo is a new `Ledger` subclass, not a schema change.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class LedgerEntry:
    """One governance decision. Immutable once written."""

    entry_id: str
    ts: str
    session_id: str
    agent: str
    tool: str
    risk: str
    verdict: str
    outcome: str           # auto_ran | held | approved | rejected | blocked | executed
    args_digest: dict[str, Any]
    detail: str = ""
    approval_id: str | None = None

    @staticmethod
    def new(**kw: Any) -> "LedgerEntry":
        return LedgerEntry(entry_id=str(uuid.uuid4()), ts=_now(), **kw)


class Ledger:
    """Append-only JSONL ledger. Thread-safe; process-safe enough for a demo."""

    def __init__(self, path: str | os.PathLike[str] = "audit/ledger.jsonl") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, entry: LedgerEntry) -> LedgerEntry:
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
        return entry

    def read(self) -> Iterator[LedgerEntry]:
        if not self.path.exists():
            return iter(())
        with self.path.open(encoding="utf-8") as fh:
            return iter([LedgerEntry(**json.loads(line)) for line in fh if line.strip()])

    def for_session(self, session_id: str) -> list[LedgerEntry]:
        return [e for e in self.read() if e.session_id == session_id]
