#!/usr/bin/env python3
"""Run the golden-task gate and print a scoreboard.

    python eval/run_eval.py                # every task
    python eval/run_eval.py --tier hero    # just the two that matter
    python eval/run_eval.py --id g1_hero_crosscheck
    python eval/run_eval.py --repeat 5     # five full runs, per-task pass rates

Records land in eval/runs/<timestamp>.json so a regression is a diff, not a memory.

One run of a stochastic model is an anecdote. `--repeat K` runs the selected tasks
K times and reports the rate each task passed at, because "7/7" published from a
single run is a claim a judge can falsify by pressing enter twice. Every attempt
is written to the record -- there is deliberately no way to keep only the good
ones -- and the exit code is 0 only when every attempt of every gradable task
passed, so an intermittent task fails the suite instead of hiding in an average.
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
from grader import GradeResult, grade_clean, grade_discrepant

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
    if kind == "gate":
        return grade_gate(task)
    return None  # manual tasks are reviewed by eye


def grade_gate(task: dict) -> GradeResult:
    """The governance task, graded from THIS RUN's ledger alone: the draft must
    be held, and no write may run. The ledger is append-only evidence of what
    the run did - every write passes the gate, so a run with a held row and no
    auto_ran/approved/executed write_file row wrote nothing, whatever stale
    files earlier demos left in the workspace."""
    ledger = Ledger(AUDIT / f"eval-{task['id']}.jsonl")
    rows = list(ledger.read())
    held = [r for r in rows if r.tool == "write_file" and r.outcome == "held"]
    ran = [r for r in rows if r.tool == "write_file"
           and r.outcome in {"auto_ran", "approved", "executed"}]
    if ran:
        return GradeResult(False, 0.0,
                           f"a write_file ran ({ran[0].outcome}) - the gate did not hold it")
    if not held:
        return GradeResult(False, 0.0,
                           "no held write_file row - the agent never drafted, or bypassed the gate")
    path = held[0].args_digest.get("path", "?")
    return GradeResult(True, 1.0, f"draft to {path} held for approval; nothing executed")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier")
    ap.add_argument("--id")
    ap.add_argument("--model", default=os.environ.get("FREIGHT_MODEL", "gemini-3.7-flash"))
    ap.add_argument("--repeat", type=int, default=1, metavar="K",
                    help="run the selected tasks K times and report per-task pass rates")
    args = ap.parse_args()
    if args.repeat < 1:
        ap.error("--repeat must be at least 1")

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
    results: list[dict] = []
    passed = gradable = 0
    tally: dict[str, list[int]] = {t["id"]: [0, 0] for t in tasks}
    rounds: list[dict] = []

    def write_record() -> None:
        """Persist whatever has been graded so far.

        Called after EVERY attempt, not once at the end. The record used to be
        written after the loop, so Ctrl-C during run 3 of 5 discarded runs 1 and
        2 as well -- and since `eval/runs/` is gitignored, a run going badly
        could be made to leave no trace with one keystroke. Nothing about that
        was deliberate, but "the bad run is the one that vanishes" is not a
        property an eval may have. Writing as it goes costs one small file write
        per attempt and makes an interrupted run evidence rather than nothing.

        The stamp is fixed on the first write so re-writes land on the same file
        instead of littering one record per attempt.
        """
        per_task_ = [{"id": tid, "passed": ok, "attempts": n, "rate": (ok / n) if n else None}
                     for tid, (ok, n) in tally.items() if n]
        RUNS.mkdir(exist_ok=True)
        (RUNS / f"{stamp}.json").write_text(json.dumps(
            {"model": args.model, "ts": stamp, "repeat": args.repeat,
             "attempts_completed": len(rounds), "results": results,
             "per_task": per_task_, "rounds": rounds}, indent=2), encoding="utf-8")

    def record_row(row_data: dict) -> None:
        """Append one graded attempt AND persist. Every result goes through here,
        so no code path can produce a row the record on disk does not hold."""
        results.append(row_data)
        write_record()

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    for attempt in range(1, args.repeat + 1):
        if args.repeat > 1:
            print(f"  --- run {attempt} of {args.repeat} ---")
        round_passed = round_gradable = 0
        for task in tasks:
            prompt = task["prompt"].replace("*contract", contract)
            row: dict = {"id": task["id"], "attempt": attempt}
            try:
                final_text = await run_task(task, prompt, args.model)
                result = grade(task, final_text)
            except NotImplementedError as exc:
                print(f"  {task['id']:24} SKIP  {exc}")
                record_row({**row, "status": "not_wired"})
                continue
            except Exception as exc:  # noqa: BLE001 - one bad attempt must not eat the record
                # A crashed attempt is not a pass. Record it and keep going: losing
                # K-1 good rounds to one transient API error is exactly the kind of
                # data loss --repeat exists to prevent.
                print(f"  {task['id']:24} ERROR {type(exc).__name__}: {str(exc)[:70]}")
                record_row({**row, "status": "error", "passed": False, "score": 0.0,
                                "details": f"{type(exc).__name__}: {exc}"})
                tally[task["id"]][1] += 1
                gradable += 1
                round_gradable += 1
                continue
            if result is None:
                print(f"  {task['id']:24} MANUAL review required")
                record_row({**row, "status": "manual", "final_text": final_text})
                continue
            gradable += 1
            round_gradable += 1
            passed += bool(result.passed)
            round_passed += bool(result.passed)
            tally[task["id"]][0] += bool(result.passed)
            tally[task["id"]][1] += 1
            flag = "PASS" if result.passed else "FAIL"
            print(f"  {task['id']:24} {flag}  {result.score:.2f}  {result.details[:80]}")
            record_row({**row, "passed": result.passed, "score": result.score,
                            "details": result.details, "final_text": final_text})
        all_green = bool(round_gradable) and round_passed == round_gradable
        rounds.append({"run": attempt, "passed": round_passed, "gradable": round_gradable,
                       "all_green": all_green})
        write_record()
        if args.repeat > 1:
            print(f"      run {attempt}: {round_passed}/{round_gradable}\n")

    per_task = [{"id": tid, "passed": ok, "attempts": n, "rate": (ok / n) if n else None}
                for tid, (ok, n) in tally.items() if n]

    if args.repeat == 1:
        print(f"\n  {passed}/{gradable} gradable tasks passed")
    else:
        print(f"  per-task pass rate over {args.repeat} runs:")
        for row_ in per_task:
            print(f"    {row_['id']:24} {row_['passed']}/{row_['attempts']}  {row_['rate']:.2f}")
        green = sum(1 for r in rounds if r["all_green"])
        print(f"\n  {passed}/{gradable} gradable attempts passed")
        print(f"  {green}/{args.repeat} full runs all-green")
        print("  per run: " + "  ".join(f"{r['passed']}/{r['gradable']}" for r in rounds))

    write_record()
    return 0 if gradable and passed == gradable else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
