"""The deployed root agent — what `adk api_server` serves on Cloud Run.

ADK's agent loader discovers `root_agent` in `agents/<app_name>/agent.py`, so
this module is the deployment's entry point. It is deliberately thin: the fleet
is built by `build_fleet()` exactly as it is locally, with the same gate, the
same ledger and the same approval store. A deployment that assembled the fleet
differently from the scoreboard would be a different system wearing its name.

The workspace is seeded at import time when it is empty, because a Cloud Run
container starts with nothing and an agent with no documents to read is a demo
that 404s. Seeding is idempotent and never touches eval/answer_keys - the
seeding script cannot reach them by construction.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from freight_fleet.agents.fleet import DEFAULT_MODEL, build_fleet
from freight_fleet.governance.gate import FileApprovalStore
from freight_fleet.governance.ledger import Ledger

_FIXTURES = Path(os.environ.get("FREIGHT_FIXTURES", "/app/fixtures"))
_WORKSPACE = Path(os.environ.get("FREIGHT_WORKSPACE_ROOT", "/app/workspace"))


def _seed_workspace() -> None:
    """Copy fixtures into the workspace if it is empty. Idempotent."""
    if (_WORKSPACE / "shipments").is_dir() or not _FIXTURES.is_dir():
        return
    # raw/ holds the rendered originals the ingest step reads. It is carried in
    # so the deployed container shows the same arrival surface as a local
    # workspace: read_file refuses them as `binary`, which is the honest answer.
    for sub in ("shipments", "inbox", "quotes", "raw"):
        src = _FIXTURES / sub
        if src.is_dir():
            shutil.copytree(src, _WORKSPACE / sub, dirs_exist_ok=True)
    (_WORKSPACE / "outbox").mkdir(parents=True, exist_ok=True)


_seed_workspace()

root_agent, _ledger, _approvals = build_fleet(
    model=DEFAULT_MODEL,
    ledger=Ledger(os.environ.get("FREIGHT_LEDGER_PATH", "/app/audit/ledger.jsonl")),
    approvals=FileApprovalStore(os.environ.get("FREIGHT_APPROVALS_PATH", "/app/data/approvals.json")),
    session_id="cloudrun",
)
