#!/usr/bin/env python3
"""Run ONE golden task by hand against a real specialist (BUILD-PLAN steps 2-3).

    python scripts/run_golden.py g1_hero_crosscheck
    python scripts/run_golden.py g2_clean_control --model gemini-3.7-flash

This is the step-2/3 harness: one specialist from its catalog card, the real
gate, the real ledger, the real grader. It exists so the hero loop (run, read
the report like an operator, adjust, re-run) has a one-command cycle before
run_task() is wired into the eval runner in step 4.

The task prompt's trailing `*contract` is replaced with the YAML's
`report_contract` text. That marker is INSIDE a folded scalar, so YAML never
expands it as an alias -- the runner has to splice it, or the mandate the
grader depends on silently never reaches the model.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "eval"))

os.environ.setdefault("FREIGHT_WORKSPACE_ROOT", str(ROOT / "workspace"))

import yaml
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from grader import grade_clean, grade_discrepant

from freight_fleet.agents.fleet import _specialist
from freight_fleet.catalog.registry import get_card
from freight_fleet.governance.gate import ApprovalStore, make_before_tool_gate
from freight_fleet.governance.ledger import Ledger

TASKS = ROOT / "eval" / "golden_tasks.yaml"


def load_task(task_id: str) -> tuple[dict, str]:
    spec = yaml.safe_load(TASKS.read_text(encoding="utf-8"))
    task = next((t for t in spec["tasks"] if t["id"] == task_id), None)
    if task is None:
        raise SystemExit(f"no task {task_id!r} in {TASKS}")
    prompt = task["prompt"].replace("*contract", spec["report_contract"].strip())
    return task, prompt


async def run(task_id: str, model: str) -> int:
    task, prompt = load_task(task_id)
    card = get_card(task["agent"])
    if card is None:
        raise SystemExit(f"task {task_id} names unknown agent {task['agent']!r}")

    ledger = Ledger(ROOT / "audit" / f"golden-{task_id}.jsonl")
    ledger.path.unlink(missing_ok=True)
    approvals = ApprovalStore()
    gate = make_before_tool_gate(ledger, approvals, session_id=task_id)
    agent = _specialist(card, model, gate)

    session_service = InMemorySessionService()
    await session_service.create_session(
        app_name="freight_fleet", user_id="operator", session_id=task_id
    )
    runner = Runner(agent=agent, app_name="freight_fleet", session_service=session_service)

    final = ""
    async for event in runner.run_async(
        user_id="operator",
        session_id=task_id,
        new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
    ):
        if event.is_final_response() and event.content and event.content.parts:
            final = "".join(p.text or "" for p in event.content.parts)

    print(f"\n=== {task_id}  agent={card.key}  model={model} ===\n")
    print(final)

    rows = list(ledger.read())
    reads = [r.args_digest.get("path") or r.args_digest.get("pattern") or r.args_digest.get("prefix", "")
             for r in rows if r.outcome == "auto_ran"]
    held = [r for r in rows if r.outcome == "held"]
    print(f"\n--- ledger: {len(rows)} gate decisions ---")
    print(f"    auto_ran: {reads}")
    if held:
        print(f"    held: {[(r.tool, r.args_digest) for r in held]}")

    kind = task.get("grader")
    if kind == "discrepant":
        result = grade_discrepant(final, task["shipment"])
    elif kind == "clean":
        result = grade_clean(final, task.get("shipment", "shp-001-pristine"))
    else:
        print(f"\n[{task_id}] grader={kind!r}: review by eye above.")
        return 0

    flag = "PASS" if result.passed else "FAIL"
    print(f"\n[{task_id}] {flag}  score={result.score:.2f}  {result.details}")
    return 0 if result.passed else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("task_id")
    ap.add_argument("--model", default=os.environ.get("FREIGHT_MODEL", "gemini-3.7-flash"))
    args = ap.parse_args()
    return asyncio.run(run(args.task_id, args.model))


if __name__ == "__main__":
    raise SystemExit(main())
