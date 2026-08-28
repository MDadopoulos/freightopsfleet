#!/usr/bin/env python3
"""Render every canonical fixture into a PDF (or scan-like PNG) original.

    python scripts/render_documents.py                  # regenerate fixtures/raw/
    python scripts/render_documents.py --check          # seal: nothing drifted
    python scripts/render_documents.py --only 'inbox/*' # one subtree

WHY THESE FILES EXIST. The markdown and CSV under `fixtures/` are the canonical
documents: the answer keys cite them, the graders read them, and AGENTS.md #8
forbids editing one to make a test pass. But a freight back office does not
receive markdown — it receives a PDF from a carrier's system and a crooked
phone photo of a printout. `fixtures/raw/` is that arrival surface, rendered
*from* the canonical text so the two can never disagree about a figure.

WHY THEY ARE COMMITTED AND NOT BUILT. Two reasons, both about honesty. First,
rendering at image-build time would drag ~45 MB of PDF and raster libraries
into python:3.11-slim for files that never change, and would make the bytes a
judge sees depend on whichever library versions the builder happened to
resolve. Second, a committed artefact is reviewable: you can open it, and a
diff tells you it moved. The render extra is pinned exactly (see pyproject.toml)
because that is what makes `--check` mean anything.

WHY PDFs ARE BYTE-SEALED AND PNGs ARE NOT. fpdf2 is pure Python and we fix the
two things that would otherwise vary — the creation date and the producer
string — so the same source produces the same bytes on Windows and on the
ubuntu runner. A rasteriser is different: pdfium's anti-aliasing and Pillow's
resampling are platform- and build-sensitive at the pixel level, so a byte
comparison there would go red for reasons no one can act on. The PNGs are
sealed on the properties that actually matter for an ingest fixture instead —
they exist, they have the dimensions a fresh render produces, they are the same
size to within 15%, and the document they were rendered from still carries its
reference number in extractable text.
"""

from __future__ import annotations

import argparse
import csv
import fnmatch
import io
import random
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
RAW = FIXTURES / "raw"

# The three documents that arrive as photographed paper rather than as a clean
# carrier PDF. One from each shape the fleet has to cope with: an invoice whose
# figures must be read off a skewed page, a packing list with a wide table, and
# a hero-adjacent air waybill.
PNG_SOURCES = frozenset(
    {
        "inbox/scan_002.md",
        "inbox/scan_004.md",
        "shipments/shp-005-air-dg/air_waybill.md",
    }
)

# Frozen so the PDFs are reproducible. The date is the fixtures' own "today" —
# the day the hero shipment's air waybill is executed — so metadata and content
# tell the same story.
FIXED_DATE = datetime(2026, 7, 22, tzinfo=UTC)
PRODUCER = "freight-ops-fleet render"
SEED = 20260722

# Where the letterhead's party name comes from, most specific first. A waybill
# is issued by its carrier, an invoice by its seller; picking the first row that
# exists gets that right without a per-document table to maintain.
PARTY_LABELS = (
    "Issuing Carrier",
    "Issued by",
    "Carrier",
    "Seller / Exporter",
    "Exporter",
    "Issuing Agent",
    "Shipper",
)

PAGE_FORMAT = "A4"
RENDER_DPI = 150
SKEW_DEGREES = 1.2
BLUR_RADIUS = 0.6
PNG_SIZE_TOLERANCE = 0.15


# --------------------------------------------------------------------------
# reading the canonical sources
# --------------------------------------------------------------------------


def sources() -> list[str]:
    """Every canonical document, as a path relative to `fixtures/`.

    Sorted, because the render order decides nothing but a stable order makes
    `--check` output diffable.
    """
    found: list[str] = []
    for subtree in ("inbox", "quotes", "shipments"):
        base = FIXTURES / subtree
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.suffix in (".md", ".csv"):
                found.append(path.relative_to(FIXTURES).as_posix())
    return sorted(found)


def target_for(rel: str) -> Path:
    """fixtures/<rel>.md|csv -> fixtures/raw/<rel>.pdf (or .png)."""
    suffix = ".png" if rel in PNG_SOURCES else ".pdf"
    return RAW / Path(rel).with_suffix(suffix)


def csv_to_markdown(text: str) -> str:
    """Turn a CSV into a pipe table so one markdown path renders everything.

    The packing lists are not uniform grids — a title row, some key/value rows,
    then the real table — so the first row becomes the header and every row is
    padded to the widest. That reproduces on paper exactly what a spreadsheet
    print-out looks like, which is the point.
    """
    rows = [row for row in csv.reader(io.StringIO(text)) if any(cell.strip() for cell in row)]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    padded = [[cell.replace("|", r"\|").strip() for cell in row] + [""] * (width - len(row)) for row in rows]
    head, *body = padded
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(row) + " |" for row in body]
    return "\n".join(lines)


def first_reference(text: str) -> str:
    """The document's first reference string: earliest 6+ char token with a digit.

    Every one of these documents leads with its own number — a B/L number, an
    invoice number, an AWB number. That token is what `--check` looks for in the
    extracted text, so a PDF that renders as blank boxes fails loudly instead of
    passing because the file exists.
    """
    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9./_-]{5,}", text):
        if any(ch.isdigit() for ch in token):
            return token
    return ""


def letterhead_for(rel: str, text: str) -> tuple[str, str, str]:
    """(party, address, doc type) for the letterhead block.

    Everything here is lifted out of the document itself. Nothing is invented:
    if the source does not name an address, the letterhead does not print one,
    because a rendered copy that adds a fact the canonical file lacks would be a
    fixture that contradicts its own answer key.
    """
    if rel.endswith(".csv"):
        rows = list(csv.reader(io.StringIO(text)))
        doc_type = rows[0][0].strip() if rows and rows[0] else "DOCUMENT"
        lookup = {row[0].strip(): row[1].strip() for row in rows if len(row) > 1}
    else:
        heading = re.search(r"^#\s+(.+?)\s*$", text, re.MULTILINE)
        doc_type = heading.group(1) if heading else "DOCUMENT"
        lookup = {
            m.group(1).strip(): m.group(2).strip()
            for m in re.finditer(r"^\|\s*\*\*(.+?)\*\*\s*\|\s*(.+?)\s*\|\s*$", text, re.MULTILINE)
        }

    raw_party = next((lookup[label] for label in PARTY_LABELS if lookup.get(label)), "")
    party, _, tail = raw_party.partition("—")
    address = tail.strip() or lookup.get("Issuing Agent", "").strip()
    if address == raw_party:  # the agent row was the party row; do not repeat it
        address = ""
    return party.strip() or doc_type.title(), address, doc_type


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------


def _letterhead_pdf(party: str, address: str, doc_type: str, reference: str):
    """A tiny FPDF subclass so the letterhead is a page property, not a prelude.

    Putting it in `header()` means a document that flows onto a second page
    still looks like it came off the same carrier's stationery, and `footer()`
    can carry the page count that makes a missing page obvious.
    """
    from fpdf import FPDF

    class Letterhead(FPDF):
        def header(self) -> None:
            if self.page_no() == 1:
                self.set_font("helvetica", "B", 15)
                self.cell(0, 8, party, new_x="LMARGIN", new_y="NEXT")
                if address:
                    self.set_font("helvetica", "", 8)
                    self.multi_cell(0, 4, address, new_x="LMARGIN", new_y="NEXT")
                self.ln(2)
                self.set_font("helvetica", "B", 11)
                self.cell(120, 6, doc_type)
                self.set_font("helvetica", "", 9)
                self.cell(0, 6, f"Ref. {reference}", align="R", new_x="LMARGIN", new_y="NEXT")
            else:
                self.set_font("helvetica", "", 8)
                self.cell(120, 5, f"{doc_type} — {party}")
                self.cell(0, 5, f"Ref. {reference}", align="R", new_x="LMARGIN", new_y="NEXT")
            self.ln(1)
            self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
            self.ln(4)

        def footer(self) -> None:
            self.set_y(-14)
            self.set_font("helvetica", "", 7)
            self.cell(120, 5, party)
            self.cell(0, 5, f"Page {self.page_no()} of {{nb}}", align="R")

    return Letterhead(format=PAGE_FORMAT)


def _section_heading_styles() -> dict:
    """Heading styles for the body. Imported lazily so the module loads without
    the render extra — the fixture-shape tests must run in a plain dev venv."""
    from fpdf.fonts import TextStyle

    return {
        level: TextStyle(color="#000000", font_style="B", font_size_pt=size, t_margin=4, b_margin=1)
        for level, size in (("h1", 11), ("h2", 10), ("h3", 9), ("h4", 9), ("h5", 8), ("h6", 8))
    }


def render_pdf(src: Path) -> bytes:
    """Render one canonical document to PDF bytes, reproducibly."""
    from markdown_it import MarkdownIt

    rel = src.relative_to(FIXTURES).as_posix()
    text = src.read_text(encoding="utf-8")
    party, address, doc_type = letterhead_for(rel, text)
    reference = first_reference(text)

    if src.suffix == ".csv":
        body_md = csv_to_markdown(text)
    else:
        # The H1 is already the letterhead's doc-type banner; printing it twice
        # would be the one visible sign that a human never looked at these.
        body_md = re.sub(r"^#\s+.+?$", "", text, count=1, flags=re.MULTILINE).lstrip()

    html = MarkdownIt("commonmark").enable("table").render(body_md)
    # fpdf2 justifies table cells unless the cell says otherwise, which in a
    # narrow column stretches "USB-C POWER ADAPTERS" across the full width and
    # strands the slashes in "NORDHAVEN / HAMBURG" against the next column.
    # Freight paperwork is left-aligned; say so.
    html = html.replace("<td>", '<td align="left">')
    # Inside a <td>, fpdf2 recognises only <b>/<i>/<u> — a <strong> is silently
    # dropped, which would flatten every "**Invoice No.**" label on the page.
    for source_tag, target_tag in (("strong", "b"), ("em", "i")):
        html = html.replace(f"<{source_tag}>", f"<{target_tag}>").replace(
            f"</{source_tag}>", f"</{target_tag}>"
        )

    pdf = _letterhead_pdf(party, address, doc_type, reference)
    # Core Helvetica has no embedded font file to vary, and cp1252 is what makes
    # the em-dash these documents use everywhere survive the encode.
    pdf.core_fonts_encoding = "cp1252"
    pdf.set_creation_date(FIXED_DATE)
    pdf.set_producer(PRODUCER)
    pdf.set_title(f"{doc_type} {reference}".strip())
    pdf.set_author(party)
    pdf.set_auto_page_break(True, margin=18)
    pdf.add_page()
    pdf.set_font("helvetica", "", 8)
    # fpdf2's default list bullet is "\x95" — the cp1252 *byte* for a bullet,
    # written as a codepoint, which cp1252 then refuses to encode. Ask for the
    # real character instead.
    pdf.write_html(
        html,
        table_line_separators=True,
        ul_bullet_char="•",
        li_prefix_color="#000000",
        # fpdf2 styles headings dark red at 24/18/14 pt, which is a blog post,
        # not a bill of lading. Section headings on these documents are small,
        # black and bold.
        tag_styles=_section_heading_styles(),
    )
    return bytes(pdf.output())


# --------------------------------------------------------------------------
# PNG
# --------------------------------------------------------------------------


def render_png(pdf_bytes: bytes, seed: str) -> bytes:
    """Rasterise page 1 and rough it up until it looks like a phone photo.

    The degradations are the ones that actually break naive extraction — paper
    grain, a page that is not square to the camera, a slightly soft focus — and
    they are seeded per file so the same source always produces the same
    scan. This is the input that justifies a vision model in the ingest path;
    a clean render would prove nothing.
    """
    import pypdfium2 as pdfium
    from PIL import Image, ImageChops, ImageFilter, ImageOps

    rng = random.Random(seed)

    document = pdfium.PdfDocument(pdf_bytes)
    try:
        page = document[0]
        image = page.render(scale=RENDER_DPI / 72, grayscale=True).to_pil().convert("L")
    finally:
        document.close()

    # Multiply by near-white noise: full-range bytes would render the page grey,
    # a narrow band reads as paper texture.
    table = bytes(255 - (value % 20) for value in range(256))
    grain = Image.frombytes("L", image.size, rng.randbytes(image.width * image.height).translate(table))
    image = ImageChops.multiply(image, grain)

    angle = rng.uniform(-SKEW_DEGREES, SKEW_DEGREES)
    image = image.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=255)
    image = image.filter(ImageFilter.GaussianBlur(BLUR_RADIUS))
    image = ImageOps.autocontrast(image.convert("L"), cutoff=1)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def extract_text(pdf_bytes: bytes) -> str:
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(pdf_bytes)
    try:
        return "\n".join(page.get_textpage().get_text_range() for page in document)
    finally:
        document.close()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _render(rel: str) -> tuple[bytes, bytes | None]:
    """(pdf bytes, png bytes or None) for one source."""
    pdf_bytes = render_pdf(FIXTURES / rel)
    if rel in PNG_SOURCES:
        return pdf_bytes, render_png(pdf_bytes, f"{SEED}:{rel}")
    return pdf_bytes, None


def write_all(selected: list[str]) -> int:
    for rel in selected:
        pdf_bytes, png_bytes = _render(rel)
        target = target_for(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = png_bytes if png_bytes is not None else pdf_bytes
        target.write_bytes(payload)
        print(f"  {target.relative_to(ROOT).as_posix()}  ({len(payload):,} bytes)")
    pdfs = sum(1 for rel in selected if rel not in PNG_SOURCES)
    print(f"\n{len(selected)} sources: {pdfs} pdf, {len(selected) - pdfs} png, written")
    return 0


def _describe_byte_drift(committed: bytes, rendered: bytes) -> str:
    """Say where two PDFs part company, not just that they did.

    Equal lengths with different bytes is the interesting case — it means the
    layout is unchanged and something in the metadata moved — and a bare size
    comparison would print two identical numbers and explain nothing.
    """
    if len(committed) != len(rendered):
        return f"{len(committed):,} committed bytes != {len(rendered):,} rendered bytes"
    offset = next(i for i, (a, b) in enumerate(zip(committed, rendered)) if a != b)
    return f"same length, first differing byte at offset {offset:,}"


def check_all(selected: list[str]) -> int:
    """Re-render everything and report what drifted.

    PDFs are compared byte for byte. PNGs are compared on the invariants a
    rasteriser will actually hold across platforms, plus the one content check
    that catches a silently empty render.
    """
    problems: list[str] = []
    for rel in selected:
        target = target_for(rel)
        shown = target.relative_to(ROOT).as_posix()
        if not target.exists():
            problems.append(f"{shown}: missing — run scripts/render_documents.py")
            continue
        committed = target.read_bytes()
        pdf_bytes, png_bytes = _render(rel)

        if png_bytes is None:
            if committed != pdf_bytes:
                problems.append(f"{shown}: {_describe_byte_drift(committed, pdf_bytes)}")
            continue

        from PIL import Image

        with Image.open(io.BytesIO(png_bytes)) as fresh, Image.open(io.BytesIO(committed)) as have:
            if have.size != fresh.size:
                problems.append(f"{shown}: {have.size} committed != {fresh.size} rendered")
        drift = abs(len(committed) - len(png_bytes)) / max(len(png_bytes), 1)
        if drift > PNG_SIZE_TOLERANCE:
            problems.append(
                f"{shown}: {len(committed):,} bytes is {drift:.0%} off the {len(png_bytes):,} rendered"
            )
        reference = first_reference((FIXTURES / rel).read_text(encoding="utf-8"))
        if reference and reference not in extract_text(pdf_bytes):
            problems.append(f"{shown}: source PDF text does not contain {reference!r}")

    if problems:
        print("DRIFT:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1

    pdfs = sum(1 for rel in selected if rel not in PNG_SOURCES)
    print(f"{len(selected)} sources: {pdfs} pdf, {len(selected) - pdfs} png, all sealed")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="verify fixtures/raw/ instead of rewriting it")
    ap.add_argument("--only", metavar="GLOB", help="restrict to sources matching this glob, e.g. 'inbox/*'")
    args = ap.parse_args(argv)

    selected = sources()
    if args.only:
        selected = [rel for rel in selected if fnmatch.fnmatch(rel, args.only)]
        if not selected:
            print(f"no fixture matches {args.only!r}", file=sys.stderr)
            return 1
    return check_all(selected) if args.check else write_all(selected)


if __name__ == "__main__":
    raise SystemExit(main())
