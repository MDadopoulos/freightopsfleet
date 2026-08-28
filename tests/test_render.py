"""Seals on the rendered originals under `fixtures/raw/`.

`fixtures/raw/` is the arrival surface: the PDF a carrier's system would email
and the crooked photo an operator would forward. It is generated from the
canonical markdown and CSV by `scripts/render_documents.py`, so the only way it
can be wrong is by falling out of step with them — a fixture added and never
rendered, an answer key that wandered in, a PDF that renders as empty boxes.
Those are the four things this file checks.

The expensive checks need the pinned `render` extra. They skip rather than fail
without it, because a plain dev venv still has to be able to run the suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from render_documents import PNG_SOURCES, sources, target_for

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
RAW = FIXTURES / "raw"


def test_raw_tree_mirrors_the_sources():
    """Exactly one rendered original per canonical document, and nothing else.

    Counted from the fixtures tree rather than written down, so adding a
    seventh shipment goes red here instead of quietly shipping a document the
    ingest path can never see.
    """
    expected = {target_for(rel) for rel in sources()}
    found = {path for path in RAW.rglob("*") if path.is_file()}
    assert found == expected, (
        f"missing: {sorted(p.relative_to(RAW).as_posix() for p in expected - found)}; "
        f"unexpected: {sorted(p.relative_to(RAW).as_posix() for p in found - expected)}"
    )
    assert sum(1 for rel in sources() if rel in PNG_SOURCES) == len(PNG_SOURCES)


def test_no_answer_key_or_json_under_raw():
    """AGENTS.md #4 again, for the binaries.

    `fixtures/` is copied wholesale into the image and into the workspace, so a
    key that reached `raw/` would reach an agent exactly as fast as one in
    `inbox/`. Binary files are easier to not look at, which is why this is here.
    """
    assert list(RAW.rglob("answer_key*")) == []
    assert list(RAW.rglob("*.json")) == []


def test_scan_001_pdf_is_the_committed_render(tmp_path):
    """The seal `--check` enforces, in the suite: same source, same bytes.

    Also asserts the words survive as extractable text. A PDF that renders as
    blank boxes still has plausible bytes and a plausible size; only pulling the
    reference number back out proves the render is a document.
    """
    pytest.importorskip("fpdf")
    pytest.importorskip("pypdfium2")
    from render_documents import extract_text, render_pdf

    fresh = tmp_path / "scan_001.pdf"
    fresh.write_bytes(render_pdf(FIXTURES / "inbox" / "scan_001.md"))

    text = extract_text(fresh.read_bytes())
    assert "999-30112062" in text
    assert "Meridian Air Cargo" in text

    committed = RAW / "inbox" / "scan_001.pdf"
    assert fresh.read_bytes() == committed.read_bytes(), (
        "fixtures/raw/inbox/scan_001.pdf is stale — run scripts/render_documents.py"
    )


@pytest.mark.parametrize("rel", sorted(PNG_SOURCES))
def test_scans_are_greyscale_page_sized_images(rel):
    """The three photographed documents are actually photograph-shaped.

    Greyscale because a phone scan of paper carries no useful colour, and wide
    because a page rendered below ~1000 px stops being legible to a vision
    model — which would make the ingest task fail for a reason that has nothing
    to do with the model.
    """
    Image = pytest.importorskip("PIL.Image")
    with Image.open(target_for(rel)) as image:
        assert image.mode == "L"
        assert image.width > 1000
