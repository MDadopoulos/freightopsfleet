"""Seals on the FRAMEWORK contract the trust boundary depends on.

`governance/gate.py` holds a consequential call by returning a dict from
`before_tool_callback` and trusting ADK to skip the tool body. Verified against
google-adk 2.7.1 (BUILD-PLAN step 1). If a future ADK ran the body anyway, the
gate would log a hold and let the write happen — green ledger, sent notice.
These tests exist so that upgrade goes red here instead of in production.

The probes drive a real `Runner` over a real `LlmAgent` with a scripted model:
the contract under test is ADK's tool dispatch, not Gemini's judgment, and a
test that needs credentials is a test that stops running.
"""

from __future__ import annotations

import asyncio
import inspect
import tempfile
from pathlib import Path

import adk_spike
import pytest

from freight_fleet.governance.ledger import Ledger


def test_a_dict_from_before_tool_callback_skips_the_tool_body():
    """THE load-bearing contract. The witness is the tool body's own counter."""
    result = asyncio.run(adk_spike.probe_short_circuit())
    assert result["tool_body_runs"] == 0
    assert result["model_saw"] == {"status": "test"}


def test_returning_none_still_runs_the_tool_body():
    """The control. Without it the seal above passes even if NOTHING ever runs."""
    result = asyncio.run(adk_spike.probe_tool_runs_without_a_gate())
    assert result["tool_body_runs"] == 1


def test_the_real_gate_holds_a_write_end_to_end():
    """AGENTS.md #3 — the one seam, exercised through the framework it runs in."""
    result = asyncio.run(adk_spike.probe_real_gate_holds_a_write())
    assert result["file_created"] is False
    assert result["model_saw_status"] == "pending_approval"
    assert result["ledger_outcomes"] == ["held"]


def test_callback_parameter_names_are_load_bearing():
    """ADK invokes the callback BY KEYWORD: `cb(tool=..., args=..., tool_context=...)`.

    (google/adk/flows/llm_flows/functions.py, `_execute_single_function_call_*`.)
    Renaming a parameter in `make_before_tool_gate` therefore breaks the gate at
    runtime, not at import. Nothing else in the repo makes that requirement
    visible, so this test does.
    """
    from freight_fleet.governance.gate import ApprovalStore, make_before_tool_gate

    with tempfile.TemporaryDirectory() as d:
        gate = make_before_tool_gate(Ledger(Path(d) / "l.jsonl"), ApprovalStore(), "s")
    assert list(inspect.signature(gate).parameters) == ["tool", "args", "tool_context"]


def test_the_fleet_still_assembles_on_the_pinned_adk():
    """Assumptions 2 and 3: AgentTool(agent=...) wraps a specialist, and the gate
    attaches to every agent in the graph."""
    from freight_fleet.agents.fleet import build_fleet
    from freight_fleet.catalog.registry import FLEET

    coordinator, _, _ = build_fleet()
    assert [t.name for t in coordinator.tools] == [c.key for c in FLEET]
    assert len(coordinator.canonical_before_tool_callbacks) == 1


@pytest.mark.parametrize("tool_name", sorted(adk_spike.GATED_TOOLS))
def test_every_workspace_tool_reaches_the_gate(tool_name: str):
    """AGENTS.md #3 — no tool may reach its body without passing the callback.

    Parametrized over the tool surface rather than asserted once, so adding a
    tool that bypasses the seam fails a named test instead of nothing.
    """
    result = asyncio.run(adk_spike.probe_gate_sees(tool_name))
    assert result["callback_fired_for"] == [tool_name]
    assert result["tool_body_runs"] == 0
