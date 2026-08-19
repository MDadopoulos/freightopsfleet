"""The agent catalog — the Fortified Enterprise Fleet track's central requirement.

The track asks how agents are "cataloged for cross-department use". This is that
catalog: a code-owned registry where every agent declares, in one place, who owns
it, which desk it serves, what it may touch, and what it costs.

An agent that is not in this registry is not in the fleet. The registry is the
authority for the /fleet API surface and the demo's catalog screen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final


@dataclass(frozen=True)
class AgentCard:
    """One agent's public record in the fleet catalog."""

    key: str
    name: str
    desk: str                      # the department that owns this work
    owner: str                     # accountable human role
    description: str
    prompt_file: str               # relative to freight_fleet/prompts/
    tools: tuple[str, ...]         # must all exist in governance.policy.TOOL_SPECS
    data_scope: str                # what production data it may reach
    autonomy: str                  # "read-only" | "drafts-for-approval"
    max_usd_per_run: float = 0.50


FLEET: Final[tuple[AgentCard, ...]] = (
    AgentCard(
        key="cross_check",
        name="Shipment document cross-check",
        desk="Import operations",
        owner="Ops lead",
        description=(
            "Cross-checks one shipment's waybill, packing list and commercial invoice against "
            "each other, flags every discrepancy with a severity, and drafts the correction notice."
        ),
        prompt_file="cross_check.md",
        tools=("read_file", "list_files", "glob_files", "grep_files", "write_file"),
        data_scope="shipments/** (read), outbox/** (draft)",
        autonomy="drafts-for-approval",
    ),
    AgentCard(
        key="doc_intake",
        name="Shipment document intake",
        desk="Import operations",
        owner="Ops lead",
        description="Sorts incoming paperwork into per-shipment sets; reports orphans and gaps.",
        prompt_file="doc_intake.md",
        tools=("read_file", "list_files", "glob_files", "grep_files", "write_file"),
        data_scope="inbox/** (read), shipments/** (read)",
        autonomy="drafts-for-approval",
    ),
    AgentCard(
        key="quote_intake",
        name="Freight quote intake",
        desk="Procurement",
        owner="Procurement lead",
        description="Normalizes incomparable freight quotes into one true all-in comparison grid.",
        prompt_file="quote_intake.md",
        tools=("read_file", "list_files", "glob_files", "write_file"),
        data_scope="quotes/** (read), shipments/** (read)",
        autonomy="drafts-for-approval",
    ),
    AgentCard(
        key="tracking_triage",
        name="Tracking exception triage",
        desk="Customer service",
        owner="CS lead",
        description="Triages rollovers, holds and delays into facts, demurrage exposure, and options.",
        prompt_file="tracking_triage.md",
        tools=("read_file", "list_files", "grep_files", "write_file"),
        data_scope="shipments/** (read), outbox/** (draft)",
        autonomy="drafts-for-approval",
    ),
    AgentCard(
        key="doc_chaser",
        name="Missing document chaser",
        desk="Import operations",
        owner="Ops lead",
        description="Finds missing documents, identifies who owes each, drafts escalating chasers.",
        prompt_file="doc_chaser.md",
        tools=("read_file", "list_files", "glob_files", "grep_files", "write_file"),
        data_scope="shipments/** (read), outbox/** (draft)",
        autonomy="drafts-for-approval",
    ),
)

_BY_KEY: Final[dict[str, AgentCard]] = {c.key: c for c in FLEET}


def get_card(key: str) -> AgentCard | None:
    return _BY_KEY.get(key)


def catalog() -> list[dict]:
    """Serialized catalog for the /fleet endpoint and the demo screen."""
    return [
        {
            "key": c.key, "name": c.name, "desk": c.desk, "owner": c.owner,
            "description": c.description, "tools": list(c.tools),
            "data_scope": c.data_scope, "autonomy": c.autonomy,
            "max_usd_per_run": c.max_usd_per_run,
        }
        for c in FLEET
    ]
