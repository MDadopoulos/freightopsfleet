"""Workspace file tools — plain functions ADK wraps as FunctionTools.

Every path is jailed under WORKSPACE_ROOT. The jail is `Path.resolve()` +
containment check, NOT string prefix matching: `../` and symlinks both defeat a
prefix check and both are defeated by resolve-then-contain.

These functions are deliberately governance-unaware. Risk classification and the
approval gate live in `freight_fleet.governance`, at one seam, for every tool at
once. A tool that enforces its own policy is a tool that can forget to.
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

WORKSPACE_ROOT = Path(os.environ.get("FREIGHT_WORKSPACE_ROOT", "./workspace")).resolve()

MAX_READ_BYTES = 512_000


class WorkspaceError(Exception):
    """Raised for a path outside the jail or an unreadable file."""


def _safe(path: str) -> Path:
    """Resolve `path` under the workspace root, or raise. The only path seam."""
    candidate = (WORKSPACE_ROOT / path).resolve()
    if candidate != WORKSPACE_ROOT and WORKSPACE_ROOT not in candidate.parents:
        raise WorkspaceError(f"path escapes the workspace: {path!r}")
    return candidate


def read_file(path: str) -> dict:
    """Read one workspace file as UTF-8 text.

    Args:
        path: Workspace-relative path, e.g. "shipments/shp-002-hero/waybill.md".
    """
    try:
        target = _safe(path)
        if not target.is_file():
            return {"status": "not_found", "path": path}
        raw = target.read_bytes()[:MAX_READ_BYTES]
        return {"status": "ok", "path": path, "content": raw.decode("utf-8", errors="replace")}
    except WorkspaceError as exc:
        return {"status": "error", "message": str(exc)}


def list_files(prefix: str = "") -> dict:
    """List workspace files under a directory prefix.

    Args:
        prefix: Workspace-relative directory, e.g. "shipments". Empty = root.
    """
    try:
        base = _safe(prefix) if prefix else WORKSPACE_ROOT
        if not base.is_dir():
            return {"status": "not_found", "prefix": prefix}
        files = sorted(
            str(p.relative_to(WORKSPACE_ROOT)) for p in base.rglob("*") if p.is_file()
        )
        return {"status": "ok", "prefix": prefix, "files": files}
    except WorkspaceError as exc:
        return {"status": "error", "message": str(exc)}


def glob_files(pattern: str) -> dict:
    """Find workspace files matching a glob pattern.

    Args:
        pattern: e.g. "shipments/**/*.csv".
    """
    matches = sorted(
        str(p.relative_to(WORKSPACE_ROOT))
        for p in WORKSPACE_ROOT.rglob("*")
        if p.is_file() and fnmatch.fnmatch(str(p.relative_to(WORKSPACE_ROOT)), pattern)
    )
    return {"status": "ok", "pattern": pattern, "files": matches}


def grep_files(pattern: str, prefix: str = "") -> dict:
    """Search workspace file contents for a regular expression.

    Args:
        pattern: Python regular expression.
        prefix: Optional workspace-relative directory to scope the search.
    """
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        return {"status": "error", "message": f"bad pattern: {exc}"}
    base = _safe(prefix) if prefix else WORKSPACE_ROOT
    hits: list[dict] = []
    for p in base.rglob("*"):
        if not p.is_file():
            continue
        try:
            for n, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    hits.append({"path": str(p.relative_to(WORKSPACE_ROOT)), "line": n, "text": line.strip()[:200]})
        except OSError:
            continue
    return {"status": "ok", "pattern": pattern, "matches": hits[:200]}


def write_file(path: str, content: str) -> dict:
    """Create or overwrite a workspace file. CONSEQUENTIAL — held by the gate.

    Args:
        path: Workspace-relative path, e.g. "outbox/BK4471-notice.md".
        content: Full file body.
    """
    try:
        target = _safe(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"status": "ok", "path": path, "bytes": len(content.encode("utf-8"))}
    except WorkspaceError as exc:
        return {"status": "error", "message": str(exc)}
