"""Transcribe `workspace/raw/` originals into `workspace/inbox/` markdown.

This is an OPERATOR STEP, run from the CLI, deliberately outside the agent loop.
It could have been a tool, and it is not, for three reasons that are all the same
reason:

* A tool is a row in `governance.policy.TOOL_SPECS` and a decision the gate has
  to classify on every call. Ingestion is not something an agent should decide to
  do; it is something a human does once to a delivery of paperwork.
* A tool call is a paid model call INSIDE another model's turn. The fleet's cost
  cap is per run, and a coordinator that could spend a dollar re-OCRing a folder
  mid-answer is a cap that does not hold.
* The fixtures are canonical (AGENTS.md #8). A transcription is a *derived* copy
  of one, and the derivation must be visible in the record — which is what the
  `<!-- transcribed ... -->` marker on line one is for. A reader can always tell
  a model's reading of a scan from a hand-written fixture.

Targets land in `inbox/` rather than beside their source because that is where
`doc_intake` and the rest of the fleet already look, and because `read_file`
refuses the binary anyway: the transcription is the only readable form of the
document, so it belongs in the readable directory. Nothing here deletes or edits
an existing file unless `force` is passed, and even then it only overwrites its
own target.

The eval never runs this. It grades whatever the workspace holds, so an operator
who has ingested over the canonical inbox must reseed before trusting a score —
`scripts/seed_workspace.py --all --clean`, as `docs/DEPLOY.md` says.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

#: The arrival formats a carrier's system actually emails. Anything else in
#: `raw/` is ignored rather than guessed at: an unknown container is exactly the
#: case where a transcription would be confident fiction.
MIME = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}

#: Per-file inline cap. Vertex has no Files API, so every byte travels in the
#: request; the fixtures are kilobytes and a multi-megabyte scan is a sign of a
#: mistake upstream, not a document to spend on. Refused, never truncated —
#: half a waybill transcribed as if it were whole is the worst possible output.
MAX_BYTES = 6_000_000

#: The whole prompt. It is short on purpose: every extra instruction is another
#: thing the model can decide to be helpful about, and helpfulness is the failure
#: mode here. The document part goes FIRST in `contents` (the model reads the
#: page, then learns what to do with it), and the rules below are all refusals.
PROMPT = """Transcribe this document into markdown.

Rules:
- Preserve every number, reference, code, date, unit and currency amount EXACTLY
  as printed, including leading zeros, separators and check digits. Do not
  normalise, reformat, convert or correct anything, even if it looks wrong.
- Reproduce tables as markdown pipe tables with the same columns in the same
  order and the same rows in the same order. Where one cell holds two stacked
  lines, join them with " / ".
- Follow the page's reading order: headings, then boxes, then tables, then the
  footer.
- Where text is genuinely unreadable, write [illegible]. Never guess at a value.
- Output markdown only. No preamble, no commentary, no explanation of what the
  document is, and no code fence around the result.
"""


@dataclass(frozen=True)
class IngestItem:
    """One original and the markdown it would become.

    `exists` is recorded at plan time so `--dry-run` can say what it would
    overwrite without opening a single file for writing.
    """

    source: Path
    target: Path
    exists: bool


@dataclass
class IngestReport:
    """What a run did, in the order it considered the work.

    `failed` carries a reason per item because the CLI's exit code is the only
    thing a scheduler sees, and "3 failed" without the three reasons is a
    support ticket rather than a report.
    """

    planned: list[IngestItem] = field(default_factory=list)
    written: list[IngestItem] = field(default_factory=list)
    skipped: list[IngestItem] = field(default_factory=list)
    failed: list[tuple[IngestItem, str]] = field(default_factory=list)


#: `(data, mime_type) -> markdown`. Injected rather than imported so the tests
#: can exercise every path of `run()` — naming, skipping, forcing, refusing —
#: with a stub, and so no test can accidentally bill a real model call.
Transcriber = Callable[[bytes, str], str]


def target_name(rel: Path) -> str:
    """The inbox filename for a raw path, relative to `raw/`.

    The first segment is the source DIRECTORY (`inbox/`, `quotes/`,
    `shipments/`) and is dropped — everything lands in `inbox/` regardless.
    Every segment after it is kept and joined with `__`, so
    `shipments/shp-002-hero/waybill.pdf` becomes `shp-002-hero__waybill.md`
    rather than colliding with the five other `waybill.pdf` files in the tree.
    Flattening rather than nesting because `doc_intake`'s whole job is to work
    out which shipment a loose document belongs to; handing it the answer in a
    directory name would grade the wrong thing.
    """
    parts = rel.parts
    stems = (*parts[1:-1], Path(parts[-1]).stem)
    return "__".join(stems) + ".md"


def plan(workspace: Path) -> list[IngestItem]:
    """Every ingestable original under `workspace/raw/`, in path order.

    Sorted so two runs on the same tree print the same lines in the same order —
    a diffable plan is the point of `--dry-run`.
    """
    raw = workspace / "raw"
    if not raw.is_dir():
        return []
    inbox = workspace / "inbox"
    items: list[IngestItem] = []
    for source in sorted(p for p in raw.rglob("*") if p.is_file()):
        if source.suffix.lower() not in MIME:
            continue
        target = inbox / target_name(source.relative_to(raw))
        items.append(IngestItem(source=source, target=target, exists=target.exists()))
    return items


def strip_fence(text: str) -> str:
    """Drop a ```markdown fence the model wrapped the answer in anyway.

    The prompt forbids it and models still do it. A fence would land verbatim in
    the inbox file and the first three characters of a transcribed waybill would
    be backticks, so this is cheap insurance rather than a workaround for a bad
    prompt.
    """
    body = text.strip()
    if not body.startswith("```"):
        return body
    lines = body.splitlines()
    lines = lines[1:]
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[-1].strip().startswith("```"):
        lines.pop()
    return "\n".join(lines).strip()


def transcribe_with_genai(model: str) -> Transcriber:
    """The real transcriber: Gemini on Vertex, ADC, one call per document.

    The client is built on the FIRST call and then reused, never at import: the
    CLI constructs this for `ingest --dry-run` too, and a dry run must not need
    credentials to tell you what it would do. Everything about the call is
    pinned to what Vertex actually honours for this model — `seed` is
    best-effort, `thinking_level` is LOW because reading a page is not a
    reasoning task, and temperature/top_p/top_k are omitted entirely (ignored on
    this model) rather than set to a number that suggests they do something.
    """
    client = None

    def transcribe(data: bytes, mime_type: str) -> str:
        nonlocal client
        from google import genai
        from google.genai import types

        if client is None:
            client = genai.Client()
        # A PDF arrives as real text plus vector rules, so MEDIUM is plenty; a
        # scan is pixels of small print and needs every one of them.
        level = "MEDIA_RESOLUTION_MEDIUM" if mime_type == "application/pdf" else "MEDIA_RESOLUTION_HIGH"
        response = client.models.generate_content(
            model=model,
            contents=[
                types.Part.from_bytes(data=data, mime_type=mime_type, media_resolution=level),
                types.Part.from_text(text=PROMPT),
            ],
            config=types.GenerateContentConfig(
                seed=20260722,
                thinking_config=types.ThinkingConfig(thinking_level="LOW"),
                response_mime_type="text/plain",
                max_output_tokens=8192,
            ),
        )
        return response.text or ""

    return transcribe


def _selected(item: IngestItem, workspace: Path, only: str | None) -> bool:
    """Does `--only GLOB` name this item?

    Matched against the path relative to `raw/` (`inbox/scan_001.pdf`) and again
    with the `raw/` prefix, because both are what an operator types after
    reading a plan line, and refusing one of them would be a puzzle, not a
    policy.
    """
    if not only:
        return True
    rel = item.source.relative_to(workspace / "raw").as_posix()
    return fnmatch(rel, only) or fnmatch(f"raw/{rel}", only)


def run(
    workspace: Path,
    transcriber: Transcriber,
    *,
    only: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    model_label: str = "unknown",
) -> IngestReport:
    """Transcribe the planned originals. Writes only under `inbox/`.

    Fail-soft per file: one oversized scan or one model error must not cost the
    other twenty-five transcriptions, so failures are collected and reported
    rather than raised. Nothing is ever deleted, and an existing target is left
    alone unless `force` — a re-run after a partial failure is safe by default.
    """
    report = IngestReport()
    report.planned = [it for it in plan(workspace) if _selected(it, workspace, only)]
    if dry_run:
        return report

    raw = workspace / "raw"
    for item in report.planned:
        if item.exists and not force:
            report.skipped.append(item)
            continue
        size = item.source.stat().st_size
        if size > MAX_BYTES:
            report.failed.append(
                (item, f"{size} bytes exceeds the {MAX_BYTES}-byte inline cap; not sent")
            )
            continue
        mime_type = MIME[item.source.suffix.lower()]
        try:
            text = strip_fence(transcriber(item.source.read_bytes(), mime_type))
        except Exception as exc:  # noqa: BLE001 - one bad document must not end the batch
            report.failed.append((item, f"{type(exc).__name__}: {str(exc)[:160]}"))
            continue
        if not text:
            report.failed.append((item, "the model returned no text; nothing written"))
            continue
        rel = item.source.relative_to(raw).as_posix()
        # The marker is the honesty of this whole module: one line that says a
        # model read the page, which file it read, and which model. Without it a
        # transcription is indistinguishable from a canonical fixture.
        marker = f"<!-- transcribed from raw/{rel} by {model_label} -->"
        item.target.parent.mkdir(parents=True, exist_ok=True)
        item.target.write_text(f"{marker}\n\n{text}\n", encoding="utf-8", newline="\n")
        report.written.append(item)
    return report
