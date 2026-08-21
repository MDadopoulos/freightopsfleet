"""Seals on the ISO 6346 checker.

These pin the arithmetic to the standard, not to anything the fleet produced.
Every expected value here was derived from the rule (letter values skipping
multiples of 11; value x 2^position; sum mod 11; 10 written as 0) and the two
`MERU410...` cases are cross-checked against `eval/answer_keys/`, which was
written before any of this existed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from freight_fleet.agents.fleet import _TOOL_FNS
from freight_fleet.catalog.registry import get_card
from freight_fleet.governance.policy import TOOL_SPECS, Verdict, classify
from freight_fleet.tools.containers import check_container_number

ROOT = Path(__file__).resolve().parents[1]


def test_the_letter_table_skips_every_multiple_of_eleven():
    """A=10..Z=38 with 11, 22 and 33 skipped. Typing this table out by hand is
    how it gets one letter wrong, so it is built — these are the anchors."""
    from freight_fleet.tools.containers import _VALUES

    assert (_VALUES["A"], _VALUES["K"], _VALUES["L"]) == (10, 21, 23)
    assert (_VALUES["U"], _VALUES["V"], _VALUES["Z"]) == (32, 34, 38)
    assert not {11, 22, 33} & set(_VALUES.values())


@pytest.mark.parametrize("number,valid,computed", [
    ("MERU4106195", True, 5),    # packing list — the correct box
    ("MERU4106915", False, 3),   # B/L — digits transposed; the answer key says 3
    ("MERU3180074", True, 4),
    ("MERU5502312", True, 2),
    ("MERU6624104", True, 4),
    ("MERU7719035", True, 5),
])
def test_check_digits_match_the_standard(number, valid, computed):
    out = check_container_number(number)
    assert out["status"] == "ok"
    assert out["valid"] is valid
    assert out["computed_check_digit"] == computed


def test_the_answer_key_agrees_with_the_checker():
    """The key was written from the documents, the checker from the standard.
    They must land in the same place — if they ever disagree, one of them is
    wrong and it is not for the fleet's output to decide which."""
    checked = 0
    for key_path in (ROOT / "eval/answer_keys").glob("*.json"):
        key = json.loads(key_path.read_text())
        for claim in key.get("facts", {}).get("containers", []):
            if "valid" not in claim:
                continue
            out = check_container_number(claim["number"])
            assert out["status"] == "ok", (key_path.name, claim)
            assert out["valid"] is claim["valid"], (key_path.name, claim, out)
            checked += 1
    assert checked >= 2, f"the keys stopped making checkable container claims ({checked})"


def test_every_container_number_in_the_fixtures_is_accounted_for():
    """No fixture may carry a number this checker cannot parse — a `malformed`
    result in a document would mean the checker, not the document, is wrong."""
    found = set()
    for path in (ROOT / "fixtures").rglob("*"):
        if path.is_file():
            found |= set(re.findall(r"\b[A-Z]{4}\d{7}\b",
                                    path.read_text(errors="replace")))
    assert len(found) >= 6
    assert all(check_container_number(n)["status"] == "ok" for n in found)


@pytest.mark.parametrize("bad", ["", "NOPE", "MSCU123456", "MER44106195", "MERU41061955"])
def test_an_uncheckable_shape_is_not_a_failed_check(bad):
    """`malformed` and `valid: False` are different findings. Collapsing them
    invents a discrepancy about a string that was never a container number."""
    out = check_container_number(bad)
    assert out["status"] == "malformed"
    assert "valid" not in out


def test_spacing_and_case_do_not_change_the_verdict():
    """Documents print `MERU 410619 5` and `meru4106195`. Same box."""
    for variant in ["MERU 410619 5", "meru4106195", "MERU-4106195", " MERU4106195 "]:
        assert check_container_number(variant)["valid"] is True


def test_the_checker_is_governed_and_reachable():
    """A tool absent from TOOL_SPECS is BLOCK (AGENTS.md), so an unclassified
    checker would be worse than none: the prompt would demand a call the gate
    refuses. AUTO because it reads nothing, writes nothing and cannot act."""
    assert classify("check_container_number") == (
        TOOL_SPECS["check_container_number"], Verdict.AUTO)
    assert TOOL_SPECS["check_container_number"].external_side_effect is False
    assert "check_container_number" in get_card("cross_check").tools
    assert "check_container_number" in _TOOL_FNS


def test_the_prompt_sends_the_desk_to_the_tool():
    """The rule and the tool have to stay wired to each other: a prompt that
    still says 'verify the check digit' without naming the tool is the old
    failure mode with extra steps."""
    prompt = (ROOT / "src/freight_fleet/prompts/cross_check.md").read_text()
    assert "check_container_number" in prompt
    assert "DO NOT do this arithmetic yourself" in prompt
