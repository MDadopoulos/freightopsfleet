"""Tool risk classification and the approval verdict.

Ported in SHAPE (not code) from LucidOwl's `app/policy/` decision core. The rule
that matters and must not be softened:

    PRECEDENCE IS TIGHTEN-ONLY.

Every layer that adjusts a verdict may only ADD friction (auto -> ask -> block).
No layer may ever loosen one. LucidOwl paid for this rule three times over; a
"helpful" loosening path is how an unattended agent mails a customer.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final


class Risk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Verdict(str, Enum):
    AUTO = "auto"    # run it, log it
    ASK = "ask"      # hold for a human
    BLOCK = "block"  # refuse outright


#: block > ask > auto. Used by `stricter()` — the ONLY way verdicts combine.
_RANK: Final[dict[Verdict, int]] = {Verdict.AUTO: 0, Verdict.ASK: 1, Verdict.BLOCK: 2}


def stricter(a: Verdict, b: Verdict) -> Verdict:
    """The safer of two verdicts. Never returns something looser than either input."""
    return a if _RANK[a] >= _RANK[b] else b


@dataclass(frozen=True)
class ToolSpec:
    """What a tool is allowed to do, declared once, at the tool.

    `external_side_effect` is the field that decides whether a mistake is
    recoverable. A file written into the workspace can be undone; an email that
    left the building cannot.
    """

    name: str
    risk: Risk
    external_side_effect: bool
    description: str


#: The fleet's whole tool surface. A tool absent from this table is UNKNOWN and
#: fails closed (see `classify`) — adding a tool without classifying it is the
#: mistake this table exists to make impossible.
TOOL_SPECS: Final[dict[str, ToolSpec]] = {
    "read_file":  ToolSpec("read_file",  Risk.LOW,      False, "Read one workspace file as text."),
    "list_files": ToolSpec("list_files", Risk.LOW,      False, "List workspace files under a prefix."),
    "glob_files": ToolSpec("glob_files", Risk.LOW,      False, "Find workspace files by glob pattern."),
    "grep_files": ToolSpec("grep_files", Risk.LOW,      False, "Search workspace file contents."),
    "write_file": ToolSpec("write_file", Risk.HIGH,     False, "Create or overwrite a workspace file."),
    # Pure arithmetic over its argument: reads nothing, writes nothing, touches
    # nothing outside the call. LOW is not a concession here — a tool that cannot
    # act is the only kind that is genuinely safe to run unattended, and making
    # the model ASK before it may check a checksum would just push it back to
    # doing the sum in its head.
    "check_container_number": ToolSpec(
        "check_container_number", Risk.LOW, False,
        "Verify a container number's ISO 6346 check digit."),
    "send_email": ToolSpec("send_email", Risk.CRITICAL, True,
                           "Transmit a drafted notice. Always held; delivered only to the "
                           "operator's demo mailbox and the approving human, never to the drafted address."),
    # Delegation to a specialist desk. LOW because the delegation itself only
    # moves text: every tool call the specialist then makes re-enters this same
    # gate under the specialist's own name, so nothing consequential can hide
    # behind a hand-off. An agent NOT listed here stays BLOCK - adding a desk
    # to the fleet means classifying it, same as any tool.
    "cross_check":     ToolSpec("cross_check",     Risk.LOW, False, "Delegate to the document cross-check desk."),
    "doc_intake":      ToolSpec("doc_intake",      Risk.LOW, False, "Delegate to the document intake desk."),
    "quote_intake":    ToolSpec("quote_intake",    Risk.LOW, False, "Delegate to the quote intake desk."),
    "tracking_triage": ToolSpec("tracking_triage", Risk.LOW, False, "Delegate to the tracking triage desk."),
    "doc_chaser":      ToolSpec("doc_chaser",      Risk.LOW, False, "Delegate to the document chaser desk."),
}

#: Verdict floor by risk. Anything at or above HIGH stops for a human.
_RISK_FLOOR: Final[dict[Risk, Verdict]] = {
    Risk.LOW: Verdict.AUTO,
    Risk.MEDIUM: Verdict.AUTO,
    Risk.HIGH: Verdict.ASK,
    Risk.CRITICAL: Verdict.ASK,
}


def classify(tool_name: str) -> tuple[ToolSpec | None, Verdict]:
    """`(spec, verdict)` for a tool call. An unknown tool is BLOCKED, never allowed.

    Fail-closed on the unknown is deliberate: the alternative is that a tool
    added without a risk classification silently runs unattended.
    """
    spec = TOOL_SPECS.get(tool_name)
    if spec is None:
        return None, Verdict.BLOCK

    verdict = _RISK_FLOOR[spec.risk]
    # Tighten-only adjustment: anything that leaves the building always asks,
    # whatever its risk row says.
    if spec.external_side_effect:
        verdict = stricter(verdict, Verdict.ASK)
    return spec, verdict
