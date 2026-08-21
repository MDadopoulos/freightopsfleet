#!/usr/bin/env python3
"""Step-5 routing probe: do ambiguous requests reach the right desk?

    python scripts/route_probe.py

Runs the FULL fleet (coordinator + five AgentTool desks) against the
BUILD-PLAN done-test prompts and reads the evidence from the ledger: the
coordinator's delegation rows carry the desk name as the tool, and the
specialist's own workspace calls carry its name as the agent. Routing is
proven by what ran, not by what the coordinator says it did.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("FREIGHT_WORKSPACE_ROOT", str(ROOT / "workspace"))

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from freight_fleet.agents.fleet import build_fleet
from freight_fleet.governance.ledger import Ledger

PROBES = [
    ("route-inbox", "Sort the documents in inbox/ - identify each one and group them by shipment.", "doc_intake"),
    ("route-quotes", "Compare these quotes in quotes/ and tell me which is actually cheapest all-in.", "quote_intake"),
]


async def run_probe(session_id: str, prompt: str, model: str) -> Ledger:
    ledger = Ledger(ROOT / "audit" / f"{session_id}.jsonl")
    ledger.path.unlink(missing_ok=True)
    coordinator, _, _ = build_fleet(model=model, ledger=ledger, session_id=session_id)

    session_service = InMemorySessionService()
    await session_service.create_session(
        app_name="freight_fleet", user_id="operator", session_id=session_id
    )
    runner = Runner(agent=coordinator, app_name="freight_fleet", session_service=session_service)
    async for _ in runner.run_async(
        user_id="operator",
        session_id=session_id,
        new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
    ):
        pass
    return ledger


async def main() -> int:
    model = os.environ.get("FREIGHT_MODEL", "gemini-3.7-flash")
    failures = 0
    for session_id, prompt, expected in PROBES:
        ledger = await run_probe(session_id, prompt, model)
        rows = list(ledger.read())
        delegations = [r.tool for r in rows if r.tool in
                       {"cross_check", "doc_intake", "quote_intake", "tracking_triage", "doc_chaser"}]
        working_desks = sorted({r.agent for r in rows if r.tool.endswith("_file") or r.tool.endswith("_files")})
        ok = expected in delegations
        failures += not ok
        print(f"  [{session_id}] {'PASS' if ok else 'FAIL'}  expected={expected}")
        print(f"      delegations: {delegations}")
        print(f"      desks that ran tools: {working_desks}")
        blocked = [r.tool for r in rows if r.outcome == "blocked"]
        if blocked:
            print(f"      BLOCKED calls: {blocked}")
    print(f"\n  routing: {'ALL PASS' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
