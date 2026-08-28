#!/usr/bin/env python3
"""Seed the agent workspace from fixtures.

    python scripts_seed_workspace.py                    # hero + clean control
    python scripts_seed_workspace.py --all              # + inbox, quotes and raw
    python scripts_seed_workspace.py --clean            # wipe the workspace first

Without `--all` you get the hero pair and nothing else — no `raw/`, so
`python -m freight_fleet.cli ingest` has nothing to plan. That is deliberate:
the default seed is the smallest workspace the hero task needs.

ANSWER KEYS ARE NEVER COPIED. They do not live under fixtures/ at all — they are
in eval/answer_keys/, a directory this script cannot reach by construction. An
agent that can read the answer key is not doing the work, it is reciting it, and
every regression run after that is worthless.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
DEFAULT_PAIR = ("shp-002-hero", "shp-001-pristine")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", default="./workspace")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--clean", action="store_true")
    args = ap.parse_args()

    ws = Path(args.workspace).resolve()
    if args.clean and ws.exists():
        shutil.rmtree(ws)
    ws.mkdir(parents=True, exist_ok=True)

    shipments = sorted(p.name for p in (FIXTURES / "shipments").iterdir() if p.is_dir())
    if not args.all:
        shipments = [s for s in shipments if s in DEFAULT_PAIR]

    for s in shipments:
        dst = ws / "shipments" / s
        shutil.copytree(FIXTURES / "shipments" / s, dst, dirs_exist_ok=True)
        print(f"  shipments/{s}/  ({len(list(dst.iterdir()))} documents)")

    if args.all:
        # raw/ holds the rendered originals the ingest step reads — the PDFs and
        # scans the markdown in inbox/ and shipments/ was rendered from. Counted
        # with rglob because raw/ is a tree, not a flat directory.
        for extra in ("inbox", "quotes", "raw"):
            src = FIXTURES / extra
            if src.is_dir():
                shutil.copytree(src, ws / extra, dirs_exist_ok=True)
                print(f"  {extra}/  ({sum(1 for p in (ws / extra).rglob('*') if p.is_file())} files)")

    (ws / "outbox").mkdir(exist_ok=True)

    leaked = list(ws.rglob("answer_key*")) + list(ws.rglob("*.json"))
    if leaked:
        print(f"\n  !! ANSWER KEY LEAK: {leaked}")
        return 1
    print(f"\n  workspace ready at {ws}")
    print(f"  export FREIGHT_WORKSPACE_ROOT={ws}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
