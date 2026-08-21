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
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import yaml
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from grader import grade_clean, grade_discrepant

from freight_fleet.agents.fleet import _specialist
from freight_fleet.catalog.registry import get_card
from freight_fleet.governance.gate import ApprovalStore, make_before_tool_gate
from freight_fleet.governance.ledger import Ledger

TASKS = Path(__file__).resolve().parent / "golden_tasks.yaml"
RUNS = Path(__file__).resolve().parent / "runs"
AUDIT = Path(__file__).resolve().parents[1] / "audit"


async def run_task(task: dict, prompt: str, model: str) -> str:
    """Drive one golden task against its NAMED specialist and return the final text.

    The task's `agent:` field is the target, not the coordinator: the answer keys
    grade a specialist's competence, and routing has its own done test in
    BUILD-PLAN step 5 -- grading both at once would make a routing miss look like
    a reasoning failure. Each task gets a fresh ledger under audit/ so the gate
    grader (g9) and any post-mortem can read exactly what the run did.
    """
    card = get_card(task["agent"])
    if card is None:
        raise ValueError(f"task {task['id']} names unknown agent {task['agent']!r}")

    ledger = Ledger(AUDIT / f"eval-{task['id']}.jsonl")
    ledger.path.unlink(missing_ok=True)
    gate = make_before_tool_gate(ledger, ApprovalStore(), session_id=task["id"])
    agent = _specialist(card, model, gate)

    session_service = InMemorySessionService()
    await session_service.create_session(
        app_name="freight_fleet", user_id="operator", session_id=task["id"]
    )
    runner = Runner(agent=agent, app_name="freight_fleet", session_service=session_service)

    final = ""
    async for event in runner.run_async(
        user_id="operator",
        session_id=task["id"],
        new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
    ):
        if event.is_final_response() and event.content and event.content.parts:
            final = "".join(p.text or "" for p in event.content.parts)
    return final


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
    ap.add_argument("--model", default=os.environ.get("FREIGHT_MODEL", "gemini-3.7-flash"))
    args = ap.parse_args()

    spec = yaml.safe_load(TASKS.read_text(encoding="utf-8"))
    # `*contract` sits inside a folded scalar, where YAML never expands aliases.
    # The runner splices it, or the mandate the grader depends on never reaches
    # the model.
    contract = spec["report_contract"].strip()
    tasks = spec["tasks"]
    if args.tier:
        tasks = [t for t in tasks if t.get("tier") == args.tier]
    if args.id:
        tasks = [t for t in tasks if t["id"] == args.id]

    print(f"\n  model: {args.model}\n")
    results, passed, gradable = [], 0, 0
    for task in tasks:
        prompt = task["prompt"].replace("*contract", contract)
        try:
            final_text = await run_task(task, prompt, args.model)
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
                        "score": result.score, "details": result.details,
                        "final_text": final_text})

    print(f"\n  {passed}/{gradable} gradable tasks passed")
    RUNS.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    record = {"model": args.model, "ts": stamp, "results": results}
    (RUNS / f"{stamp}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return 0 if gradable and passed == gradable else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
