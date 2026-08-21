"""Fleet assembly — builds the ADK agent graph from the catalog.

    coordinator (LlmAgent)
      +-- cross_check       (AgentTool)   <- the hero
      +-- doc_intake        (AgentTool)
      +-- quote_intake      (AgentTool)
      +-- tracking_triage   (AgentTool)
      +-- doc_chaser        (AgentTool)

Specialists are exposed as AgentTool rather than `sub_agents` transfer so the
coordinator keeps the thread and can run two desks on one shipment without
handing the conversation away. Each specialist gets ONLY the tools its catalog
card declares — the card is the allowlist, enforced here at build time.

VERIFIED against google-adk 2.7.1 (BUILD-PLAN step 1, 2026-08-19). All three
contracts this file assumed hold; nothing here needed adapting. Re-run
`python scripts/adk_spike.py` after any ADK upgrade — `tests/test_adk_contract.py`
seals the same checks under pytest, so an upgrade goes red there, not in the
ledger.

  1. A dict from `before_tool_callback` short-circuits the tool body. HOLDS.
     `flows/llm_flows/functions.py` assigns the callback's return to
     `function_response` and calls the tool only `if function_response is None`
     — in both the async and the live dispatch paths.
  2. `AgentTool(agent=...)` wraps an LlmAgent as a callable tool. HOLDS.
  3. Callback signature `(tool, args, tool_context)`. HOLDS — but ADK invokes it
     BY KEYWORD, so the parameter NAMES in `make_before_tool_gate` are part of
     the contract, not decoration. Renaming one breaks the gate at runtime.

Two sharp edges found in that dispatch loop, neither triggered by today's code:
  - The short-circuit tests `is None`, not truthiness, so a falsy-but-not-None
    return ({} , "") also skips the body — it just doesn't stop the callback
    chain.
  - With a LIST of before_tool_callbacks, each result overwrites the last
    unconditionally, so a later callback returning None ERASES an earlier
    falsy hold and the tool runs. Attach exactly one callback here. If that
    ever changes, this is where the gate springs a leak.

ADK 2.x's AgentTool docstring now nudges toward `sub_agents=[...]` with
`mode='single_turn'` instead. That is inline execution, not the transfer
BUILD-PLAN §5 ruled out, so it is a live option for step 5 — but it is a step-5
decision, not a step-1 one, and AgentTool works today.

If a future version differs, adapt HERE. Do not weaken the gate to fit the
framework.
"""

from __future__ import annotations

import os
from pathlib import Path

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool
from google.adk.tools.agent_tool import AgentTool

from ..catalog.registry import FLEET, AgentCard
from ..governance.gate import ApprovalStore, make_before_tool_gate
from ..governance.ledger import Ledger
from ..tools import workspace

_PROMPTS = Path(__file__).resolve().parent.parent / "prompts"

#: Tool name -> the plain function ADK wraps. Defined once in `tools.workspace`
#: so the CLI and the console replay exactly the bodies the agents ran.
_TOOL_FNS = workspace.TOOL_FNS

COORDINATOR_INSTRUCTION = """
You run the ops desk of a freight forwarder. You do not do the specialist work
yourself — you route it to the right desk and report back in the operator's words.

Routing:
- One shipment's documents to check against each other -> cross_check
- A pile of unsorted paperwork to identify and group -> doc_intake
- Competing rate quotes to compare -> quote_intake
- A delay, rollover, hold or demurrage question -> tracking_triage
- Something missing that somebody owes -> doc_chaser

Rules that outrank being helpful:
- Never invent a document, a figure, a date, or a status. If it is not in the
  documents, say it is not in the documents.
- Consequential actions (writing a file, sending anything) are HELD for the
  operator's approval. When a tool returns status "pending_approval", tell the
  operator plainly what is waiting and its approval id. Do not retry it, and do
  not try another route to the same effect.
- Lead with the worst confirmed fact, not the most reassuring one.
""".strip()


def _specialist(card: AgentCard, model: str, gate) -> LlmAgent:
    """One specialist agent, built from its catalog card."""
    instruction = (_PROMPTS / card.prompt_file).read_text(encoding="utf-8")
    tools = [FunctionTool(_TOOL_FNS[t]) for t in card.tools if t in _TOOL_FNS]
    return LlmAgent(
        name=card.key,
        model=model,
        description=card.description,
        instruction=instruction,
        tools=tools,
        before_tool_callback=gate,
    )


#: The fleet's model, one place. Deployed containers set FREIGHT_MODEL; a stale
#: hardcoded default here is how a deploy silently runs a model the scoreboard
#: never graded.
DEFAULT_MODEL = os.environ.get("FREIGHT_MODEL", "gemini-3.7-flash")


def build_fleet(
    model: str = DEFAULT_MODEL,
    ledger: Ledger | None = None,
    approvals: ApprovalStore | None = None,
    session_id: str = "local",
) -> tuple[LlmAgent, Ledger, ApprovalStore]:
    """Build the coordinator with every catalog specialist attached.

    Returns `(coordinator, ledger, approvals)` — the caller keeps the ledger and
    approval store so a CLI or web layer can list and grant pending approvals.
    """
    ledger = ledger or Ledger()
    approvals = approvals or ApprovalStore()
    gate = make_before_tool_gate(ledger, approvals, session_id)

    specialists = [_specialist(card, model, gate) for card in FLEET]
    coordinator = LlmAgent(
        name="freight_ops_coordinator",
        model=model,
        description="Routes freight ops work to the right specialist desk.",
        instruction=COORDINATOR_INSTRUCTION,
        tools=[AgentTool(agent=a) for a in specialists],
        before_tool_callback=gate,
    )
    return coordinator, ledger, approvals


# ADK's `adk web` / `adk run` discover a module-level `root_agent`.
# Built lazily so importing this module never requires credentials.
def _root():
    agent, _, _ = build_fleet()
    return agent
