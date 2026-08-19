"""Seals on the fixtures and the grader.

The answer-key leak test is the important one: it is the difference between a
regression gate and a memorization exercise.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from grader import grade_clean, grade_discrepant

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
KEYS = ROOT / "eval" / "answer_keys"

EXPECTED = [
    "shp-001-pristine", "shp-002-hero", "shp-003-container-refs",
    "shp-004-quote-invoice", "shp-005-air-dg", "shp-006-missing-doc",
]


def test_no_answer_key_anywhere_under_fixtures():
    """AGENTS.md #4 — the structural guarantee the whole gate rests on."""
    assert list(FIXTURES.rglob("answer_key*")) == []
    assert list(FIXTURES.rglob("*.json")) == []


def test_every_shipment_has_a_key():
    assert sorted(p.name for p in (FIXTURES / "shipments").iterdir() if p.is_dir()) == EXPECTED
    assert sorted(p.stem for p in KEYS.glob("*.json")) == EXPECTED


@pytest.mark.parametrize("shipment", EXPECTED)
def test_key_evidence_really_appears_in_the_documents(shipment):
    """Every printed value an answer key cites must exist in the file it names.

    This is what makes the fixtures tamper-evident: edit a figure in a document
    and this goes red instead of the gate silently grading against fiction.
    """
    key = json.loads((KEYS / f"{shipment}.json").read_text(encoding="utf-8"))
    docs = key.get("docs") or {}
    for finding in key.get("findings") or []:
        for ev in finding.get("evidence") or []:
            doc = docs.get(ev["doc"])
            if not doc:
                continue
            body = (FIXTURES / "shipments" / shipment / doc).read_text(encoding="utf-8")
            normalized = body.replace(",", "").replace(" ", "")
            value = str(ev["value"]).replace(",", "").replace(" ", "")
            assert value in normalized or str(ev["value"]) in body, (
                f"{shipment}/{doc} does not contain {ev['value']!r} "
                f"cited by finding {finding['id']}"
            )


def test_grader_passes_a_correct_hero_report():
    report = (
        "DISCREPANCIES FOUND: 4\n"
        "- Gross weight: waybill 6,098.0 kg vs packing list 5,384.0 kg\n"
        "- Cartons: invoice 740 vs packing list 720\n"
        "- Incoterm FOB Shanghai but B/L marked FREIGHT PREPAID\n"
        "- Invoice line 2: 6,000 x 7.10 = 42,600.00 printed as 43,620.00\n"
    )
    assert grade_discrepant(report, "shp-002-hero").passed


def test_grader_fails_a_report_missing_findings():
    report = "DISCREPANCIES FOUND: 1\n- Gross weight 6,098.0 vs 5,384.0\n"
    result = grade_discrepant(report, "shp-002-hero")
    assert not result.passed and 0 < result.score < 1


def test_grader_requires_the_mandated_block():
    assert not grade_discrepant("I found some weight problems.", "shp-002-hero").passed


def test_clean_control_rejects_invented_findings():
    assert not grade_clean("DISCREPANCIES FOUND: 1\n- weight looks odd").passed


def test_clean_control_tolerates_a_checks_performed_section():
    """A thorough report must not be punished for showing its working."""
    report = (
        "All three documents agree.\n\n"
        "DISCREPANCIES FOUND: 0\n\n"
        "Checks performed:\n- gross weight\n- container check digit\n- incoterm coherence\n"
    )
    assert grade_clean(report).passed
