"""Structural seals on what the operator console is allowed to touch.

The console is the surface a judge opens without credentials. It must render
from the ledger, the approval store, the catalog and the committed runs and from
nothing else — no ADK, no `google.genai`, no ingest path, so no code route from
a page view to a paid model call or to a Vertex credential lookup.

These checks are at the IMPORT level, twice, because the two failures they catch
are different: the subprocess catches a transitive import (console imports X,
which imports ADK), and the AST walk catches a direct one added inside a
function where a `sys.modules` check would only see it after that function ran.

A substring search of the source would be the obvious third check and is
deliberately absent: `console.py`'s own docstring says the words "google.adk"
and "google.genai" while explaining that it does not import them, so a grep seal
fails on day one and gets deleted, which is worse than not having it.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONSOLE = ROOT / "src" / "freight_fleet" / "console.py"

#: The four things a console import must not drag in. `freight_fleet.ingest`
#: joins the model packages here because it is the one module in the tree whose
#: whole purpose is to spend money on a model call.
FORBIDDEN = ("google.adk", "google.genai", "freight_fleet.devui", "freight_fleet.access", "freight_fleet.ingest")


def test_console_imports_no_model_or_ingest_code():
    """Import the console in a clean interpreter and look at what came with it."""
    code = (
        "import sys, freight_fleet.console as c; "
        f"print(sorted(m for m in sys.modules if m.startswith({FORBIDDEN!r})))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, check=False
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "[]", (
        f"importing freight_fleet.console pulled in {proc.stdout.strip()}"
    )


def test_console_source_import_statements():
    """No direct import of a forbidden module anywhere in the file — including
    inside a function body, which the subprocess check would miss until that
    function ran on a page nobody clicked during the demo."""
    tree = ast.parse(CONSOLE.read_text(encoding="utf-8"))
    named: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            named.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            named.append(node.module)
    offenders = [name for name in named if name.startswith(FORBIDDEN)]
    assert offenders == [], f"console.py imports {offenders}"


def test_seed_carries_raw(tmp_path):
    """`--all` must copy the rendered originals too.

    Counted against `fixtures/raw/` rather than a literal, because the failure
    this guards is a seed that silently stops carrying files someone added — an
    ingest plan that is quietly short is indistinguishable from one that is
    complete.
    """
    ws = tmp_path / "workspace"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "seed_workspace.py"),
         "--all", "--clean", "--workspace", str(ws)],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    seeded = {p.relative_to(ws / "raw").as_posix() for p in (ws / "raw").rglob("*") if p.is_file()}
    fixtures = {
        p.relative_to(ROOT / "fixtures" / "raw").as_posix()
        for p in (ROOT / "fixtures" / "raw").rglob("*")
        if p.is_file()
    }
    assert seeded == fixtures and len(seeded) == 26


def test_asyncpg_is_declared():
    """The Cloud SQL session store needs an async driver in the RUNTIME image.

    `DatabaseSessionService` opens the URI with SQLAlchemy's async engine, and a
    missing `asyncpg` surfaces at the first session write on Cloud Run — after
    the deploy, in a log line nobody is reading. Declared here means the image
    build fails instead, which is the failure you want.
    """
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    deps = project["dependencies"]
    assert any(d.split(">")[0].split("=")[0].split("[")[0].strip() == "asyncpg" for d in deps), (
        f"asyncpg is not a runtime dependency: {deps}"
    )
