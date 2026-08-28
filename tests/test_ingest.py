"""The ingest step, exercised end to end without a single model call.

`ingest.run` takes its transcriber as an argument precisely so this file can
drive every branch — naming, skipping, forcing, filtering, refusing, dry-running
— against a stub. A test that needed credentials would be a test nobody runs on
a laptop, and a test that called Gemini would charge for the privilege of
asserting that `--dry-run` writes nothing.

The counts at the bottom are computed from `fixtures/raw/` rather than typed in,
so adding a seventh shipment moves them by itself instead of going red for the
wrong reason.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from freight_fleet import ingest
from freight_fleet.cli import main
from freight_fleet.tools import workspace as ws_tools

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


def _raw(root: Path, rel: str, body: bytes = b"%PDF-1.4 stub") -> Path:
    path = root / "raw" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


@pytest.fixture()
def tiny(tmp_path: Path) -> Path:
    """A miniature raw/ tree with the three shapes the naming rule must handle."""
    _raw(tmp_path, "inbox/scan_001.pdf")
    _raw(tmp_path, "quotes/quote_baltic.pdf")
    _raw(tmp_path, "shipments/shp-002-hero/waybill.pdf")
    _raw(tmp_path, "inbox/notes.txt", b"not an original")
    return tmp_path


def _stub(text: str = "# Transcribed\n\nvalue 1234") -> ingest.Transcriber:
    def transcribe(data: bytes, mime_type: str) -> str:
        assert data, "the file was not read before the call"
        assert mime_type in set(ingest.MIME.values())
        return text

    return transcribe


def test_plan_flattens_the_source_tree_into_inbox(tiny: Path):
    """One directory level drops its directory; deeper paths keep every segment.

    The `__` join is what stops six `waybill.pdf` files from becoming one
    `inbox/waybill.md`, so it is asserted on the exact path that would collide.
    """
    targets = [it.target.relative_to(tiny).as_posix() for it in ingest.plan(tiny)]
    assert targets == [
        "inbox/scan_001.md",
        "inbox/quote_baltic.md",
        "inbox/shp-002-hero__waybill.md",
    ]


def test_a_text_file_under_raw_is_not_an_original(tiny: Path):
    """`.txt` is readable as it stands, so transcribing it would invent a
    derived copy of a document that never needed one."""
    assert all(it.source.suffix != ".txt" for it in ingest.plan(tiny))


def test_exists_is_recorded_at_plan_time(tiny: Path):
    (tiny / "inbox").mkdir(exist_ok=True)
    (tiny / "inbox" / "scan_001.md").write_text("canonical", encoding="utf-8")
    by_target = {it.target.name: it.exists for it in ingest.plan(tiny)}
    assert by_target == {
        "scan_001.md": True,
        "quote_baltic.md": False,
        "shp-002-hero__waybill.md": False,
    }


def test_run_writes_the_marker_line_and_the_body(tiny: Path):
    """The marker is the only thing separating a transcription from a fixture."""
    report = ingest.run(tiny, _stub(), model_label="gemini-test")
    assert len(report.written) == 3 and not report.failed
    body = (tiny / "inbox" / "shp-002-hero__waybill.md").read_text(encoding="utf-8")
    assert body.splitlines()[0] == (
        "<!-- transcribed from raw/shipments/shp-002-hero/waybill.pdf by gemini-test -->"
    )
    assert body.splitlines()[1] == ""
    assert "value 1234" in body


def test_an_existing_target_is_left_alone_without_force(tiny: Path):
    (tiny / "inbox").mkdir(exist_ok=True)
    (tiny / "inbox" / "scan_001.md").write_text("canonical fixture", encoding="utf-8")
    report = ingest.run(tiny, _stub())
    assert [it.target.name for it in report.skipped] == ["scan_001.md"]
    assert (tiny / "inbox" / "scan_001.md").read_text(encoding="utf-8") == "canonical fixture"


def test_force_overwrites_the_target(tiny: Path):
    (tiny / "inbox").mkdir(exist_ok=True)
    (tiny / "inbox" / "scan_001.md").write_text("canonical fixture", encoding="utf-8")
    report = ingest.run(tiny, _stub(), force=True, model_label="m")
    assert not report.skipped and len(report.written) == 3
    assert "transcribed from" in (tiny / "inbox" / "scan_001.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("pattern", ["inbox/scan_001*", "raw/inbox/scan_001.pdf"])
def test_only_narrows_the_plan(tiny: Path, pattern: str):
    """Both the raw-relative path and the printed `raw/...` form must select —
    an operator types whichever one the plan line showed them."""
    report = ingest.run(tiny, _stub(), only=pattern)
    assert [it.target.name for it in report.written] == ["scan_001.md"]
    assert not (tiny / "inbox" / "quote_baltic.md").exists()


def test_an_oversized_original_is_refused_not_truncated(tiny: Path, monkeypatch):
    """Half a waybill transcribed as though it were whole is the worst output
    available here, so the cap refuses the file and says so."""
    monkeypatch.setattr(ingest, "MAX_BYTES", 4)
    report = ingest.run(tiny, _stub())
    assert report.written == [] and len(report.failed) == 3
    assert "exceeds" in report.failed[0][1]
    assert not (tiny / "inbox").exists()


def test_a_transcriber_error_costs_one_document_not_the_batch(tiny: Path):
    """The unattended failure mode from the sweep, repeated here: a 502 on the
    first document must not skip the other two."""
    calls: list[int] = []

    def flaky(data: bytes, mime_type: str) -> str:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("502 policy context unavailable")
        return "ok"

    report = ingest.run(tiny, flaky, model_label="m")
    assert len(report.failed) == 1 and len(report.written) == 2
    assert "RuntimeError" in report.failed[0][1]


def test_an_empty_answer_is_a_failure_not_an_empty_file(tiny: Path):
    """An empty inbox file would look like a document with nothing in it, and
    `--force`-less re-runs would then skip it forever."""
    report = ingest.run(tiny, _stub("   \n"))
    assert len(report.failed) == 3 and not report.written
    assert not (tiny / "inbox").exists()


def test_dry_run_plans_and_writes_nothing(tiny: Path):
    report = ingest.run(tiny, _stub(), dry_run=True)
    assert len(report.planned) == 3
    assert not report.written and not report.skipped and not report.failed
    assert not (tiny / "inbox").exists()


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("```markdown\n# Waybill\n\nBK4471\n```", "# Waybill\n\nBK4471"),
        ("```\n# Waybill\n```", "# Waybill"),
        ("# Waybill\n\nBK4471", "# Waybill\n\nBK4471"),
        ("```markdown\n# Waybill\n```\n\n", "# Waybill"),
    ],
)
def test_strip_fence(raw: str, expected: str):
    assert ingest.strip_fence(raw) == expected


def test_read_file_refuses_a_pdf_instead_of_decoding_it(tiny: Path, monkeypatch):
    """Fail-closed: mojibake with a few real words in it is the one failure an
    agent cannot detect, so the tool says `binary` and names the fix."""
    monkeypatch.setattr(ws_tools, "WORKSPACE_ROOT", tiny.resolve())
    result = ws_tools.read_file("raw/inbox/scan_001.pdf")
    assert result["status"] == "binary"
    assert "ingest" in result["hint"]
    assert "content" not in result


def test_read_file_still_reads_the_text_documents(tiny: Path, monkeypatch):
    monkeypatch.setattr(ws_tools, "WORKSPACE_ROOT", tiny.resolve())
    assert ws_tools.read_file("raw/inbox/notes.txt")["status"] == "ok"


def test_doc_intake_no_longer_names_a_tool_that_does_not_exist():
    """`files:read` was never a tool in `TOOL_SPECS`; a prompt that names one
    teaches the model to call something the gate would block as unknown."""
    prompt = (ROOT / "src" / "freight_fleet" / "prompts" / "doc_intake.md").read_text(
        encoding="utf-8"
    )
    assert "files:read" not in prompt
    assert "read_file" in prompt


def test_cli_dry_run_plans_every_committed_original(tmp_path, monkeypatch, capsys):
    """The acceptance check, run against a real seeded workspace.

    The expected numbers come from `fixtures/raw/` and `fixtures/inbox/`, not
    from a literal: 26 originals today, five of which already have canonical
    markdown in `inbox/` and would be overwritten only with `--force`.
    """
    ws = tmp_path / "workspace"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "seed_workspace.py"),
         "--all", "--clean", "--workspace", str(ws)],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    monkeypatch.setenv("FREIGHT_WORKSPACE_ROOT", str(ws))
    assert main(["ingest", "--dry-run"]) == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.split()[:1] == ["plan"]]

    originals = [p for p in (FIXTURES / "raw").rglob("*") if p.suffix.lower() in ingest.MIME]
    already = [
        p for p in originals
        if (FIXTURES / "inbox" / ingest.target_name(p.relative_to(FIXTURES / "raw"))).exists()
    ]
    assert len(lines) == len(originals) == 26
    assert sum(1 for ln in lines if ln.endswith("[exists]")) == len(already) == 5
    # The dry run wrote nothing: inbox/ still holds exactly the seeded fixtures.
    assert sorted(p.name for p in (ws / "inbox").glob("*.md")) == [
        f"scan_00{n}.md" for n in range(1, 6)
    ]
