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

!! VERIFY BEFORE BUILDING !!
The ADK import paths and constructor kwargs below match google-adk 1.x as
documented at the time of writing. Pin your version, run `adk --version`, and
check these three contracts first — everything else in the repo is framework-
independent, but the whole fleet hangs off them:
  1. `before_tool_callback` returning a dict short-circuits the tool body.
  2. `AgentTool(agent=...)` wraps an LlmAgent as a callable tool.
  3. Callback signature is `(tool, args, tool_context)`.
If any differs, adapt HERE. Do not weaken the gate to fit the framework.
"""

from __future__ import annotations

from pathlib import Path

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool
from google.adk.tools.agent_tool import AgentTool

from ..catalog.registry import FLEET, AgentCard
from ..governance.gate import ApprovalStore, make_before_tool_gate
from ..governance.ledger import Ledger
from ..tools import workspace

_PROMPTS = Path(__file__).resolve().parent.parent / "prompts"

#: Tool name -> the plain function ADK wraps. Names MUST match
#: governance.policy.TOOL_SPECS, or the gate cannot classify the call.
_TOOL_FNS = {
    "read_file": workspace.read_file,
    "list_files": workspace.list_files,
    "glob_files": workspace.glob_files,
    "grep_files": workspace.grep_files,
    "write_file": workspace.write_file,
}

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


def build_fleet(
    model: str = "gemini-2.5-flash",
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
