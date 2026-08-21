"""Answer-key grader for the freight regression gate.

Ported from LucidOwl's `bench/src/lucidowl_bench/logistics_checks.py`. The two
design decisions that make it worth porting rather than rewriting:

* NO LLM JUDGE ANYWHERE. Putting a model on the grading path makes the gate's
  verdict a second model's opinion. Every check here is a regex against an
  answer key written by a human who knows the answer.
* TWO DELIBERATE ASYMMETRIES.
  - On a DISCREPANT shipment every seeded finding must appear; extras are
    tolerated. A miss is a document that goes out wrong; an extra costs a minute.
  - On the CLEAN shipment the bar is exactly zero. An agent that invents problems
    in a consistent document set torches operator trust faster than one that
    misses a real problem, so shp-001-pristine is the false-positive control and
    it is graded strictly.

The report contract (mandated in every task prompt, not in the agent's persona,
so the gate stays gradable even if the prompts drift):

    A final section starting with the exact heading line `DISCREPANCIES FOUND: N`
    followed by one bullet per discrepancy citing both conflicting values and the
    documents carrying them. A clean set ends with `DISCREPANCIES FOUND: 0`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_KEYS = Path(__file__).resolve().parent / "answer_keys"
_HEADING = re.compile(r"DISCREPANCIES\s+FOUND\s*:\s*(\d+)", re.IGNORECASE)
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+", re.MULTILINE)

_NO_BLOCK = (
    "the mandated 'DISCREPANCIES FOUND: N' block is absent (or N was not an "
    "integer). Every task prompt spells out that heading verbatim - without it "
    "the report is not machine-checkable, whatever the prose above says"
)


@dataclass
class GradeResult:
    passed: bool
    score: float
    details: str


def load_key(shipment: str) -> dict[str, Any]:
    return json.loads((_KEYS / f"{shipment}.json").read_text(encoding="utf-8"))


def _block(text: str) -> tuple[str, int] | None:
    """`(text after the LAST heading, parsed N)` or None. Last match wins so a
    re-issued list (after an operator correction) is graded on the final answer."""
    matches = list(_HEADING.finditer(text or ""))
    if not matches:
        return None
    last = matches[-1]
    return text[last.end():], int(last.group(1))


def _bullets(block: str) -> list[str]:
    """The bullet RUN immediately after the heading, and nothing after it.

    A real report does not end at its last discrepancy - it carries a "Checks
    performed" section and notes. Counting those as discrepancies fails a clean
    shipment for being thorough. The run ends at the first non-bullet, non-blank
    line.
    """
    out: list[str] = []
    for line in block.splitlines():
        if _BULLET.match(line):
            out.append(line)
        elif line.strip():
            if out and line.startswith((" ", "\t")):
                out[-1] += " " + line.strip()  # continuation of the current bullet
                continue
            break
    return out


def _matches_all(block: str, finding: dict[str, Any]) -> bool:
    """A finding is reported when ONE bullet carries all of its patterns.

    Scoping to a single bullet, rather than to the whole block, is what makes
    this a detection test instead of a string-presence test. Searching the block
    passes a report that FABRICATES four findings and then, in a "checks
    performed" table below them, prints every real conflicting value under the
    heading "OK - reconciles" - the exact opposite of the right answer, scored
    1.00. Verified: that report passed before this change and fails after it.

    This is a TIGHTENING, and the direction matters. AGENTS.md #5 forbids
    moving the goalposts so a failing run passes; this moves them so a wrong
    answer fails, and it was made without consulting any run it would flip.
    Re-graded against every committed run: the 7/7 is unchanged, and the
    historical g4 failure still fails. The instrument got sharper, not kinder.
    """
    patterns = finding.get("required_patterns") or []
    if not patterns:
        return True
    return any(
        all(re.search(p, bullet, re.IGNORECASE) is not None for p in patterns)
        for bullet in _bullets(block)
    )


def grade_discrepant(final_text: str, shipment: str) -> GradeResult:
    """Every seeded finding must be reported. Extras tolerated."""
    key = load_key(shipment)
    parsed = _block(final_text)
    if parsed is None:
        return GradeResult(False, 0.0, _NO_BLOCK)
    block, count = parsed

    findings = key.get("findings") or []
    found = [f for f in findings if _matches_all(block, f)]
    missed = [f for f in findings if f not in found]
    score = (len(found) / len(findings)) if findings else 1.0
    minimum = int(key.get("min_findings") or 0)

    if missed:
        summary = "; ".join(f"{f['id']}: {f['summary']}" for f in missed)
        return GradeResult(False, score, f"{len(missed)} seeded discrepancy(ies) missing - {summary}")
    if count < minimum:
        return GradeResult(False, score,
                           f"block reports {count}, below the {minimum} seeded into this shipment")
    return GradeResult(True, 1.0, f"all {len(findings)} seeded discrepancies reported (N={count})")


def grade_clean(final_text: str, shipment: str = "shp-001-pristine") -> GradeResult:
    """The false-positive control. Pass = exactly zero, with no bullets."""
    parsed = _block(final_text)
    if parsed is None:
        return GradeResult(False, 0.0, _NO_BLOCK)
    block, count = parsed
    bullets = _bullets(block)
    if count != 0 or bullets:
        return GradeResult(False, 0.0,
                           f"clean control reported {count} discrepancy(ies) and "
                           f"{len(bullets)} bullet(s); a consistent document set must yield 0")
    return GradeResult(True, 1.0, "clean control correctly reported DISCREPANCIES FOUND: 0")
