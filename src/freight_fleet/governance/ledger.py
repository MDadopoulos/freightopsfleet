"""Append-only audit ledger — the record an operator can defend to their boss.

Every gate decision lands here, whichever way it went: auto-run, held, approved,
rejected, blocked. The ledger is APPEND-ONLY by contract; there is no update and
no delete, because a record that can be edited is not evidence.

V1 storage is JSONL on local disk (zero infrastructure, greppable, diffable).
The `LedgerEntry` shape is storage-agnostic — swapping in Firestore for the
cloud demo is a new `Ledger` subclass, not a schema change.

READING DAMAGE. A line that cannot be read back — a partial write's bad byte,
a hand-edit, a newer writer's shape — degrades to a row with outcome
`UNREADABLE`, never to a skip and never to a raised exception. A reader that
raises turns one bad byte into a record nobody can render and a queue nobody
can decide; a reader that skips edits the evidence by omission. Every consumer
(console, CLI, gate) reads through here, so damage looks the same everywhere.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


#: The outcome carried by a ledger line that would not read back. A skipped
#: line in an append-only ledger is precisely the thing this project promises
#: never happens, so the damage becomes a row and the row says why.
UNREADABLE = "unreadable"


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
    outcome: str           # auto_ran | held | approved | rejected | blocked | executed | abandoned
    args_digest: dict[str, Any]
    detail: str = ""
    approval_id: str | None = None

    @staticmethod
    def new(**kw: Any) -> LedgerEntry:
        return LedgerEntry(entry_id=str(uuid.uuid4()), ts=_now(), **kw)


_STR_FIELDS = ("entry_id", "ts", "session_id", "agent", "tool",
               "risk", "verdict", "outcome", "detail")


def _read_line(raw: bytes, n: int) -> LedgerEntry:
    """One line back off disk, tolerantly. Anything that is not a well-shaped
    entry becomes an UNREADABLE row carrying the line number and the raw text,
    because the dataclass constructor checks key names but not value types —
    `"session_id": 123` builds fine and then crashes the first caller that
    treats the field as the string the schema says it is.
    """

    def damaged(why: str, text: str) -> LedgerEntry:
        return LedgerEntry(
            entry_id=f"line-{n}", ts="", session_id="(unreadable)", agent="",
            tool="", risk="unknown", verdict="unknown", outcome=UNREADABLE,
            args_digest={"line": n, "raw": text[:2000]}, detail=why,
        )

    try:
        line = raw.decode("utf-8")
    except UnicodeDecodeError:
        return damaged("this line is not valid UTF-8",
                       raw.decode("utf-8", errors="replace"))
    try:
        payload = json.loads(line)
    except ValueError:
        return damaged("this line could not be parsed as JSON", line)
    if not isinstance(payload, dict):
        return damaged("this line is JSON, but not a JSON object", line)
    try:
        entry = LedgerEntry(**payload)
    except TypeError:
        return damaged("this line does not have the LedgerEntry shape", line)
    if any(not isinstance(getattr(entry, f), str) for f in _STR_FIELDS) \
            or not isinstance(entry.args_digest, dict) \
            or not (entry.approval_id is None or isinstance(entry.approval_id, str)):
        return damaged("this line parsed, but a field has the wrong type", line)
    return entry


class Ledger:
    """Append-only JSONL ledger. Thread-safe; process-safe enough for a demo."""

    def __init__(self, path: str | os.PathLike[str] = "audit/ledger.jsonl") -> None:
        # No mkdir here: read-only surfaces (the console constructs a Ledger on
        # every GET just to name the path) must not write to the filesystem,
        # and a read-only deployment must not 500 constructing one.
        self.path = Path(path)
        self._lock = threading.Lock()

    def append(self, entry: LedgerEntry) -> LedgerEntry:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # newline="\n" pinned: the console serves and HASHES these exact bytes,
            # and text mode would write CRLF on Windows - the same run would hash
            # differently by OS.
            with self.path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
        return entry

    def read(self) -> Iterator[LedgerEntry]:
        """Every line, in file order, tolerantly (see the module header).

        A damaged line can never RESOLVE a hold — it carries no approval_id —
        so for the gate's resolution scans this errs closed: the approval
        store's pending set stays the primary guard on replay. The alternative,
        refusing every decision while any damage exists, would brick an
        append-only queue permanently: one bad byte, no remedy.
        """
        try:
            blob = self.path.read_bytes()
        except OSError:
            return iter(())
        return iter([_read_line(raw, n)
                     for n, raw in enumerate(blob.split(b"\n"), 1) if raw.strip()])

    def for_session(self, session_id: str) -> list[LedgerEntry]:
        return [e for e in self.read() if e.session_id == session_id]
