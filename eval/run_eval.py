#!/usr/bin/env python3
"""Run the golden-task gate and print a scoreboard.

    python eval/run_eval.py                # every task
    python eval/run_eval.py --tier hero    # just the two that matter
    python eval/run_eval.py --id g1_hero_crosscheck

Records land in eval/runs/<timestamp>.json so a regression is a diff, not a memory.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import yaml  # noqa: E402
from grader import grade_clean, grade_discrepant  # noqa: E402

TASKS = Path(__file__).resolve().parent / "golden_tasks.yaml"
RUNS = Path(__file__).resolve().parent / "runs"


async def run_task(task: dict) -> str:
    """Drive the fleet for one task and return the final text.

    TODO(build step 4): wire this to the ADK Runner:
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService
        from freight_fleet.agents.fleet import build_fleet
        agent, ledger, approvals = build_fleet(session_id=task["id"])
        runner = Runner(agent=agent, app_name="freight_fleet",
                        session_service=InMemorySessionService())
        ... iterate runner.run_async(...), collect the final response text ...
    Until then this raises, so a green scoreboard can never be a lie.
    """
    raise NotImplementedError("wire run_task to the ADK Runner (BUILD-PLAN step 4)")


def grade(task: dict, final_text: str):
    kind = task.get("grader")
    if kind == "discrepant":
        return grade_discrepant(final_text, task["shipment"])
    if kind == "clean":
        return grade_clean(final_text, task.get("shipment", "shp-001-pristine"))
    return None  # manual / gate graders are reviewed separately


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier")
    ap.add_argument("--id")
    args = ap.parse_args()

    spec = yaml.safe_load(TASKS.read_text(encoding="utf-8"))
    tasks = spec["tasks"]
    if args.tier:
        tasks = [t for t in tasks if t.get("tier") == args.tier]
    if args.id:
        tasks = [t for t in tasks if t["id"] == args.id]

    results, passed, gradable = [], 0, 0
    for task in tasks:
        try:
            final_text = await run_task(task)
            result = grade(task, final_text)
        except NotImplementedError as exc:
            print(f"  {task['id']:24} SKIP  {exc}")
            results.append({"id": task["id"], "status": "not_wired"})
            continue
        if result is None:
            print(f"  {task['id']:24} MANUAL review required")
            results.append({"id": task["id"], "status": "manual", "final_text": final_text})
            continue
        gradable += 1
        passed += bool(result.passed)
        flag = "PASS" if result.passed else "FAIL"
        print(f"  {task['id']:24} {flag}  {result.score:.2f}  {result.details[:80]}")
        results.append({"id": task["id"], "passed": result.passed,
                        "score": result.score, "details": result.details})

    print(f"\n  {passed}/{gradable} gradable tasks passed")
    RUNS.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (RUNS / f"{stamp}.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0 if gradable and passed == gradable else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
