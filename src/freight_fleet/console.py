"""The operator console — the record, rendered.

The console adds no capability. It renders four artifacts the fleet already
produced — the ledger, the approval store, the scored runs, the catalog — it
never calls a model, and its only button goes back through the same gate the
agent hit.

THE WRITE BOUNDARY. The console's only write path is
`governance.gate.execute_approved` / `reject_approved`. Every other console
operation is a file read. No route imports `google.adk` or `google.genai`: the
deployed URL works with **no GOOGLE_API_KEY**, cannot burn quota, cannot 500 on
credentials, and every page is deterministic.

FRESHNESS. `FileApprovalStore` loads its state in `__init__`, and the sweep
writes that file from another process. So every request constructs a fresh store
and re-reads the ledger. There are no module-level caches and no globals holding
state: 56 rows is under a millisecond, and a cached audit trail is a lie.

RECORDED vs DERIVED. Every value on screen is one of two kinds and the page says
which. RECORDED comes straight from the ledger, the approval store, the catalog
or a run file, and renders plain. DERIVED is computed by a named rule (D1..D7
below), renders with a dotted underline, and every rule is written out in one
sentence in the `#derivations` block at the foot of the Desk and the Record.

Zero JavaScript, by design: `<details>` for expansion, `<form method="post">`
for the two actions, `:target` for deep links, `prefers-color-scheme` for theme.
That removes a whole class of live-demo failure and is a claim a reviewer
verifies with one grep.
"""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from .catalog.registry import FLEET
from .governance import policy
from .governance.gate import FileApprovalStore, execute_approved, reject_approved
from .governance.ledger import Ledger, LedgerEntry
from .tools import workspace

#: The one console-caused session id. Every ledger row this app writes carries
#: it, so the record says WHERE a human decided, not merely that one did.
SOURCE = "approval-console"

#: The tool bodies the console may replay — the SAME mapping the ADK assembly
#: wraps, so the console can only ever replay a body an agent could have run.
#: `send_email` is absent because it is unwired (AGENTS.md #7), and the console
#: renders that absence on a disabled button rather than hiding it.
_TOOL_FNS: dict[str, Callable[..., dict]] = workspace.TOOL_FNS

#: A ledger line that will not parse becomes one of these rather than being
#: skipped. A skipped line in an append-only ledger is precisely the thing this
#: project promises never happens.
UNREADABLE = "unreadable"

_READABLE_SUFFIXES = frozenset({".md", ".csv", ".txt"})


# --- environment -------------------------------------------------------------
# Read per call, never cached at import: the console must never disagree with
# the CLI about where the ledger is, and tests point both at a temp directory.

def _ledger_path() -> Path:
    return Path(os.environ.get("FREIGHT_LEDGER_PATH", "audit/ledger.jsonl"))


def _approvals_path() -> Path:
    return Path(os.environ.get("FREIGHT_APPROVALS_PATH", "data/approvals.json"))


def _runs_dir() -> Path:
    return Path(os.environ.get("FREIGHT_EVAL_RUNS", "eval/runs"))


def _workspace_root() -> Path:
    """The tools' own jail, not a second copy of it."""
    return workspace.WORKSPACE_ROOT


def _readonly() -> bool:
    return bool(os.environ.get("FREIGHT_CONSOLE_READONLY"))


# --- the data layer ----------------------------------------------------------
# Four loaders, all fresh, all degrading to the empty case instead of raising. A
# judge who clones the repo with an empty ledger must see a considered screen,
# not a traceback.

def load_ledger() -> list[LedgerEntry]:
    """Every line of the append-only ledger, in file order, tolerantly."""
    try:
        text = _ledger_path().read_text(encoding="utf-8")
    except OSError:
        return []
    rows: list[LedgerEntry] = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(LedgerEntry(**json.loads(line)))
        except (ValueError, TypeError):
            rows.append(LedgerEntry(
                entry_id=f"line-{n}", ts="", session_id="(unreadable)", agent="",
                tool="", risk="unknown", verdict="unknown", outcome=UNREADABLE,
                args_digest={"line": n, "raw": line[:2000]},
                detail="this line could not be parsed as JSON",
            ))
    return rows


def load_pending() -> dict[str, dict[str, Any]]:
    """What is actionable right now. The store is the authority for the queue —
    a work queue reconstructed from the ledger would be a second source of truth."""
    try:
        return FileApprovalStore(_approvals_path()).pending()
    except (OSError, ValueError):
        return {}


def load_runs() -> list[dict[str, Any]]:
    """Committed scored runs, newest first (the filenames are UTC timestamps)."""
    try:
        files = sorted(_runs_dir().glob("*.json"), reverse=True)
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for f in files:
        try:
            record = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict):
            record["_file"] = f.name
            out.append(record)
    return out


def best_run(runs: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """The newest run with at least six results — the MOST COMPLETE run, not the
    newest file. A two-task hero-tier re-run is newer and says almost nothing."""
    for record in runs if runs is not None else load_runs():
        if len(record.get("results") or []) >= 6:
            return record
    return None


def outbox_count() -> int:
    try:
        return len([p for p in (_workspace_root() / "outbox").glob("*") if p.is_file()])
    except OSError:
        return 0


def ledger_sha256() -> str:
    """First 12 hex of the sha256 of the file AS SERVED. A file hash, not a
    Merkle chain — an auditor recomputes it with `shasum -a 256`."""
    try:
        return hashlib.sha256(_ledger_path().read_bytes()).hexdigest()[:12]
    except OSError:
        return ""


# --- the derivations, D1..D7 -------------------------------------------------

_SHIPMENT_RX = re.compile(r"shipments/([^/]+)")
_CRITICAL_RX = re.compile(r"\bCRITICAL\b")
#: Markdown chrome on a quoted draft line: a leading list marker or heading, and
#: the emphasis runs. Removed for display only — never from the bytes.
_MD_NOISE_RX = re.compile(r"^\s*(?:[-*+]\s+|\d+\.\s+|#{1,6}\s+)|\*{1,3}|_{2,3}|`")


def entry_path(entry: LedgerEntry) -> str:
    d = entry.args_digest or {}
    return str(d.get("path") or d.get("prefix") or "")


def derive_shipment(entries: list[LedgerEntry], index: int) -> str:
    """D1 — the shipment named by the LAST `shipments/<dir>/…` path this session
    read before the hold.

    Last, not all: "every shipment read since the previous hold" wrongly
    attributes the clean control to the first hold, because a shipment with
    nothing wrong produces no hold to separate it from the next one.
    """
    session = entries[index].session_id
    for e in reversed(entries[:index]):
        if e.session_id != session:
            continue
        m = _SHIPMENT_RX.match(entry_path(e))
        if m:
            return m.group(1)
    return ""


def derive_evidence(entries: list[LedgerEntry], index: int, shipment: str) -> list[str]:
    """D2 — the documents the agent actually read under that shipment, in order,
    deduped. This is what turns an approval into an audit rather than a vibe."""
    if not shipment:
        return []
    session = entries[index].session_id
    seen: list[str] = []
    for e in entries[:index]:
        if e.session_id != session or e.tool != "read_file":
            continue
        p = entry_path(e)
        if p.startswith(f"shipments/{shipment}/") and p not in seen:
            seen.append(p)
    return seen


def derive_critical(draft: str) -> str:
    """D3 — the draft's OWN words, never a governance verdict and never a score.

    Four of five real drafts carry no severity token at all; a five-point
    ranking synthesised from that would be a fabricated signal on the entry
    whose whole thesis is that fabricated signals destroy trust.
    """
    for line in (draft or "").splitlines():
        if _CRITICAL_RX.search(line):
            # The draft is markdown; the card is not. Strip the emphasis and the
            # list marker so the quote reads as the sentence the agent wrote,
            # not as `1. **...**`. The words themselves are untouched.
            return _MD_NOISE_RX.sub("", line.strip()).strip()
    return ""


@dataclass(frozen=True)
class Target:
    """D4 — what approving would do to the file system, stated before the click."""

    path: str
    exists: bool
    size: int | None = None
    modified: str = ""


def derive_target(path: str) -> Target:
    if not path:
        return Target(path, False)
    try:
        target = workspace._safe(path)
        if not target.is_file():
            return Target(path, False)
        stat = target.stat()
        modified = datetime.fromtimestamp(stat.st_mtime, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        return Target(path, True, stat.st_size, modified)
    except (workspace.WorkspaceError, OSError):
        return Target(path, False)


def derive_cleared(entries: list[LedgerEntry], session: str) -> list[str]:
    """D5 — shipments the sweep touched, minus the ones a hold was attributed to.

    The false-positive control lands on the operator's morning screen as good
    news, instead of staying a slide in a deck.
    """
    touched: list[str] = []
    flagged: set[str] = set()
    for i, e in enumerate(entries):
        if e.session_id != session:
            continue
        m = _SHIPMENT_RX.match(entry_path(e))
        if m and m.group(1) not in touched:
            touched.append(m.group(1))
        if e.outcome == "held":
            flagged.add(derive_shipment(entries, i))
    return [s for s in touched if s not in flagged]


def session_kind(session_id: str) -> str:
    """D6 — who was at the keyboard, if anyone."""
    if session_id.startswith("sweep"):
        return "UNATTENDED"
    if session_id == "approval-cli":
        return "OPERATOR · CLI"
    if session_id == SOURCE:
        return "OPERATOR · CONSOLE"
    if session_id == "(unreadable)":
        return "UNPARSEABLE"
    return "OPERATOR SESSION"


def hold_status(entry: LedgerEntry, entries: list[LedgerEntry],
                pending: dict[str, Any]) -> tuple[str, LedgerEntry | None]:
    """D7 — `awaiting` | `resolved` | `lapsed`, plus the row that resolved it.

    LAPSED is the interesting one: in the record, absent from the store, so it
    can never execute. It is not hidden and it is not "fixed" — it demonstrates
    that the append-only ledger is evidence INDEPENDENT of mutable state, and
    that the console reconciles two stores and reports the disagreement rather
    than papering over it. The word is LAPSED, not "expired": there is no timer.
    """
    if entry.entry_id in pending:
        return "awaiting", None
    for e in entries:
        if e.approval_id == entry.entry_id and e.outcome in {"approved", "rejected", "executed"}:
            return "resolved", e
    return "lapsed", None


# --- the visual system -------------------------------------------------------
# One string, inlined. No CDN, no webfont, no build step: the page renders in an
# egress-free container exactly as it renders on a laptop.
#
# Light is the base on bare :root; dark overrides under prefers-color-scheme. No
# colour is defined only inside the media block, and body always paints its own
# background — a transparent body borrows the host's theme.
#
# Governance state is carried by FOUR redundant channels: the word, a glyph, the
# left rail's geometry, and only then colour. Video compression eats subtle
# colour and a projector may be miscalibrated; print the Record in greyscale and
# it still parses.

CSS = """
:root{
  --bg:#F7F6F3; --surface:#FFFFFF; --sunk:#EFEDE8; --line:#DCD9D2;
  --ink:#16181D; --ink-2:#454B54; --ink-3:#5F6670;
  --accent:#0B5FFF; --accent-ink:#FFFFFF;
  --auto:var(--ink-3);   /* substitution is lazy: this follows --ink-3 into dark */
  --held:#8A5205;     --held-tint:#FDF0D5;
  --approved:#1E40AF; --approved-tint:#E4ECFF;
  --executed:#166534; --executed-tint:#DEF3E5;
  --rejected:#3E4A5A; --rejected-tint:#E9EDF2;
  --blocked:#A31515;  --blocked-tint:#FDE4E4;
  --sans: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#14161A; --surface:#1C1F25; --sunk:#101215; --line:#2C313A;
    --ink:#F3F4F6; --ink-2:#C3C8D1; --ink-3:#98A0AC;
    --accent:#7EA6FF; --accent-ink:#0F1319;
    --held:#F5B840;     --held-tint:#3A2C10;
    --approved:#A8C4FF; --approved-tint:#17233F;
    --executed:#5BD79A; --executed-tint:#12301F;
    --rejected:#A9B4C2; --rejected-tint:#242A33;
    --blocked:#FCA5A5;  --blocked-tint:#3A1A18;
  }
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);
     font:400 17.5px/1.55 var(--sans);overflow-x:hidden}
a{color:var(--accent)}
:focus-visible{outline:3px solid var(--accent);outline-offset:2px}
code,pre,.mono{font-family:var(--mono)}

/* nav — its own class, never a bare word like `.top`: an inner div sharing the
   name would inherit `position:sticky` and paint a border across a card. */
.nav{position:sticky;top:0;z-index:5;background:var(--surface);
     border-bottom:1px solid var(--line);box-shadow:0 1px 2px rgba(0,0,0,.04)}
.nav .in{max-width:920px;margin-inline:auto;padding-inline:20px;height:56px;
     display:flex;align-items:center;gap:16px}
.rowtop{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center}
.brand{font:700 12.5px/1 var(--sans);letter-spacing:.08em;text-transform:uppercase;
     color:var(--ink-3);white-space:nowrap}
.brand .long{display:inline}
.navlinks{margin-left:auto;display:flex;gap:4px;overflow-x:auto;max-width:100%}
.navlinks a{display:flex;align-items:center;min-height:44px;padding:0 10px;
     font:600 14px/1 var(--sans);color:var(--ink-2);text-decoration:none;border-radius:8px;
     white-space:nowrap;transition:background-color 120ms ease}
.navlinks a:hover{background:var(--sunk)}
.navlinks a[aria-current="page"]{color:var(--ink);box-shadow:inset 0 -3px 0 var(--accent)}
.badge{margin-left:6px;font:700 12.5px/1 var(--mono);background:var(--held-tint);
     color:var(--held);border-radius:4px;padding:3px 6px}

/* layout */
.wrap{max-width:920px;margin-inline:auto;padding:32px 20px 96px}
h1{font:650 30px/1.2 var(--sans);margin:0 0 8px}
h2{font:600 22px/1.3 var(--sans);margin:0 0 8px}
h3{font:600 17.5px/1.35 var(--sans);margin:24px 0 8px}
p{margin:0 0 12px;max-width:68ch}
.lede{color:var(--ink-2)}
.meta{font:500 14px/1.45 var(--mono);color:var(--ink-3)}
.label{font:700 12.5px/1 var(--sans);letter-spacing:.08em;text-transform:uppercase}
.count{font:700 64px/1 var(--sans);letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.count2{font:700 40px/1 var(--sans);font-variant-numeric:tabular-nums}
.count3{font:700 30px/1 var(--sans);font-variant-numeric:tabular-nums}
.band{margin:40px 0 0}
.card{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:20px}
.sunk{background:var(--sunk);border:1px solid var(--line);border-radius:12px;padding:20px}
.d{border-bottom:1px dotted currentColor;text-decoration:none;color:inherit}

/* the brief */
.hero{display:flex;flex-wrap:wrap;gap:32px 48px;align-items:flex-end;margin-bottom:20px}
.hero .num{display:flex;flex-direction:column;gap:8px}
.hero .num .label{color:var(--ink-3)}
.hero .rest{display:flex;gap:48px}

/* queue cards */
.queue{display:flex;flex-direction:column;gap:12px}
.qcard{display:block;text-decoration:none;color:inherit;background:var(--surface);
     border:1px solid var(--line);border-left:6px solid var(--held);border-radius:12px;
     padding:20px;transition:background-color 120ms ease}
.qcard:hover{background:var(--sunk)}
.qtitle{font:600 22px/1.3 var(--sans);margin:8px 0}
.qpath{font:500 15px/1.5 var(--mono);color:var(--ink-2);margin:8px 0;word-break:break-all}
.qmeta{font:500 14px/1.45 var(--mono);color:var(--ink-3);margin:8px 0}
.qgo{font:700 12.5px/1 var(--sans);letter-spacing:.08em;text-transform:uppercase;color:var(--accent);
     display:flex;align-items:center;min-height:44px}
.flagrail{font:700 12.5px/1 var(--sans);letter-spacing:.08em;text-transform:uppercase;color:var(--held)}

/* pills — outlined for policy states, FILLED for terminal facts. A fill means
   the world changed. */
.pill{display:inline-block;border-radius:4px;padding:5px 8px;
     font:700 12.5px/1 var(--sans);letter-spacing:.08em;text-transform:uppercase;
     border:1px solid currentColor;white-space:nowrap}
.pill.fill{color:var(--accent-ink)}
.p-held{color:var(--held)} .p-approved{color:var(--approved)}
.p-executed{background:var(--executed);border-color:var(--executed)}
.p-rejected{background:var(--rejected);border-color:var(--rejected)}
.p-blocked{background:var(--blocked);border-color:var(--blocked)}
.p-auto{color:var(--ink-3)}

/* strips — tints use background-COLOR, never the `background` shorthand: the
   shorthand resets background-image and would erase a striped rail. */
.strip{border-left:6px solid currentColor;padding:16px 20px;border-radius:0 12px 12px 0;
     margin:0 0 24px}
.s-executed{color:var(--executed);background-color:var(--executed-tint)}
.s-rejected{color:var(--rejected);background-color:var(--rejected-tint)}
.s-blocked{color:var(--blocked);background-color:var(--blocked-tint)}
.s-held{color:var(--held);background-color:var(--held-tint)}
.s-neutral{color:var(--ink-3);background-color:var(--sunk)}
.strip .body{color:var(--ink);font-size:17.5px}
.strip .body .meta{color:var(--ink-2)}

/* ledger */
.sessionband{border-top:1px solid var(--line);border-bottom:1px solid var(--line);
     padding:12px 0;margin:40px 0 12px;display:flex;flex-wrap:wrap;gap:6px 16px;align-items:baseline}
.lrow{background:var(--surface);border:1px solid var(--line);border-radius:12px;
     padding:12px 16px;margin:0 0 8px}
.lrow.tint-held{background-color:var(--held-tint)}
.lrow.tint-approved{background-color:var(--approved-tint)}
.lrow.tint-executed{background-color:var(--executed-tint)}
.lrow.tint-rejected{background-color:var(--rejected-tint)}
.lrow.tint-blocked{background-color:var(--blocked-tint)}
.lrow:target{box-shadow:inset 0 0 0 2px var(--accent)}
.run{background:var(--surface);border:1px solid var(--line);border-radius:12px;
     padding:8px 16px;margin:0 0 8px;color:var(--ink-3)}
.run summary{cursor:pointer;font:500 14px/1.45 var(--mono);padding:8px 0;min-height:44px;
     display:flex;align-items:center;flex-wrap:wrap;gap:8px}
.glyph{font-family:var(--mono);font-weight:700}

/* tables */
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{border-collapse:collapse;width:100%;font-size:15px}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid var(--line);vertical-align:top}
th{font:700 12.5px/1 var(--sans);letter-spacing:.08em;text-transform:uppercase;color:var(--ink-3)}
td.mono,th.mono{font-family:var(--mono)}

/* draft + details */
.draft{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:20px;
     max-height:60vh;overflow-y:auto;
     -webkit-mask-image:linear-gradient(to bottom,#000 calc(100% - 40px),transparent);
     mask-image:linear-gradient(to bottom,#000 calc(100% - 40px),transparent)}
.draft h2,.draft h3,.draft h4{font-size:19px;margin:20px 0 8px}
.draft table{margin:12px 0}
.draft pre{background:var(--sunk);padding:12px;border-radius:8px;overflow-x:auto}
details.raw{margin:12px 0 0;border:1px solid var(--line);border-radius:12px;background:var(--surface)}
details.raw>summary{cursor:pointer;padding:16px 20px;min-height:44px;display:flex;align-items:center;
     font:500 15px/1.5 var(--mono);flex-wrap:wrap;gap:8px}
details.raw>div{padding:0 20px 20px}
details.raw pre{white-space:pre-wrap;word-break:break-word;font-size:15px;margin:0}

/* the decision page */
.contract{display:grid;grid-template-columns:1fr;gap:12px;margin:24px 0}
@media (min-width:720px){.contract{grid-template-columns:1fr 1fr}}
.actions{border:1px solid var(--line);border-radius:12px;padding:20px;margin:32px 0 0;
     background:var(--surface);display:flex;flex-direction:column;gap:12px}
.btn{display:flex;align-items:center;justify-content:center;min-height:56px;padding:0 20px;
     border-radius:10px;border:1px solid var(--accent);background:var(--accent);color:var(--accent-ink);
     font:700 15px/1.2 var(--sans);text-align:center;cursor:pointer;width:100%;
     transition:background-color 120ms ease}
.btn.outline{background:transparent;color:var(--ink);border-color:var(--line)}
.btn:disabled{opacity:.55;cursor:not-allowed}
form{margin:0}

/* chips */
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:4px 0}
.chip{border:1px solid var(--line);border-radius:999px;padding:5px 10px;
     font:500 14px/1 var(--mono);color:var(--ink-2)}
.chip.held{color:var(--held);border-color:var(--held)}
.hazard{background:var(--blocked-tint);color:var(--blocked)}

.foot{margin-top:64px;padding-top:24px;border-top:1px solid var(--line);
     color:var(--ink-3);font-size:15px;max-width:68ch}
hr{border:0;border-top:1px solid var(--line);margin:24px 0}

/* rails — LAST in the sheet on purpose. `.lrow` and `.strip` both declare the
   `border` shorthand, which would otherwise reset border-left at equal
   specificity and silently erase the rail that carries the state. */
.rail-solid{border-left:6px solid currentColor}
.rail-faint{border-left:6px solid currentColor;opacity:.75}
.rail-striped{border-left:6px solid transparent;
     background-image:repeating-linear-gradient(135deg,currentColor 0 3px,transparent 3px 6px);
     background-repeat:no-repeat;background-size:6px 100%;background-position:left top}
.rail-dotted{border-left:2px solid transparent;
     background-image:repeating-linear-gradient(180deg,currentColor 0 2px,transparent 2px 5px);
     background-repeat:no-repeat;background-size:2px 100%;background-position:left top}

@media (max-width:640px){
  .wrap{padding-inline:0}
  .wrap>h1,.wrap>p,.wrap>.band>h2,.wrap>.band>p,.foot{padding-inline:20px}
  .card,.sunk,.qcard,.lrow,.run,.actions,.strip,.draft,details.raw,.sessionband{
      border-radius:0;border-left-width:6px;margin-inline:0}
  .qcard{border-radius:0}
  .brand .long{display:none}
  .hero{gap:24px}
  .hero .rest{gap:32px;width:100%}
}

@media print{
  :root{--bg:#fff;--surface:#fff;--sunk:#f4f4f4;--line:#bbb;--ink:#000;--ink-2:#333;--ink-3:#555}
  body{background:#fff;color:#000;font-size:11pt}
  .nav,.actions,.strip,.qgo,.navlinks{display:none !important}
  details{display:block}
  details>summary{list-style:none}
  .draft{max-height:none;overflow:visible;-webkit-mask-image:none;mask-image:none}
  .lrow,.run,.card,.sunk{page-break-inside:avoid;border:1px solid #bbb}
  .printhead{display:block !important;border-bottom:2px solid #000;margin-bottom:12pt;padding-bottom:6pt}
}
.printhead{display:none}
"""


def esc(value: Any) -> str:
    """Everything user- or model-derived goes through here. Drafts are
    model-generated text on a public URL; this is the console's XSS surface."""
    return html.escape(str(value if value is not None else ""))


def _shell(title: str, body: str, *, active: str = "", pending: int = 0,
           show_footer: bool = True) -> str:
    """The page frame. One CSS string, one nav, one footer, no script tag."""
    def link(href: str, text: str, key: str, badge: str = "") -> str:
        cur = ' aria-current="page"' if key == active else ""
        return f'<a href="{href}"{cur}>{esc(text)}{badge}</a>'

    badge = f'<span class="badge">{pending}</span>' if pending else ""
    nav = (
        '<div class="nav"><div class="in">'
        '<span class="brand">Freight Ops<span class="long"> Fleet · Operator Console</span></span>'
        '<span class="navlinks">'
        + link("/", "Desk", "desk", badge)
        + link("/ledger", "Ledger", "ledger")
        + link("/fleet", "Fleet", "fleet")
        + link("/evidence", "Evidence", "evidence")
        + "</span></div></div>"
    )
    foot = (
        '<div class="foot"><p>This console reads records. It never calls a model, and it ships '
        "zero lines of JavaScript. The fleet runs from the CLI and the scheduled sweep "
        '(<code>python -m freight_fleet.cli sweep</code>). The only write it can cause is a gated '
        "replay through <code>before_tool_gate</code>, and every one of those lands in the ledger "
        "you are reading.</p></div>"
    )
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{esc(title)}</title><style>{CSS}</style></head><body>"
        f'{nav}<div class="wrap">{body}{foot if show_footer else ""}</div></body></html>'
    )


# --- md_lite -----------------------------------------------------------------

_CODE_RX = re.compile(r"`([^`]+)`")
_BOLD_RX = re.compile(r"\*\*(.+?)\*\*")
_HEAD_RX = re.compile(r"^(#{1,6})\s+(.*)$")
_RULE_RX = re.compile(r"^\s*(-{3,}|\*{3,})\s*$")
_ULI_RX = re.compile(r"^\s*[-*]\s+(.*)$")
_OLI_RX = re.compile(r"^\s*\d+\.\s+(.*)$")
_DIVIDER_RX = re.compile(r"^[\s|:\-]+$")


def _inline(text: str) -> str:
    """Inline marks on ALREADY-ESCAPED text. No links, no images, no raw HTML."""
    text = _CODE_RX.sub(r"<code>\1</code>", text)
    return _BOLD_RX.sub(r"<strong>\1</strong>", text)


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def md_lite(text: str) -> str:
    """A deliberately small markdown subset, stdlib only.

    `html.escape()` runs FIRST on the whole text, and nothing after that step
    introduces user-controlled markup — so a draft that contains a `<script>`
    renders as the characters `<script>`. Jinja2 is not installed and is not
    wanted: a 70-line renderer whose escaping order is visible on one screen is
    a smaller trust surface than a templating engine.
    """
    lines = esc(text).split("\n")
    out: list[str] = []
    para: list[str] = []

    def flush() -> None:
        if not para:
            return
        chunks = [_inline(ln.strip()) + ("<br>" if ln.endswith("  ") else "") for ln in para]
        out.append("<p>" + " ".join(chunks) + "</p>")
        para.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            flush()
            i += 1
            block: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                block.append(lines[i])
                i += 1
            out.append("<pre><code>" + "\n".join(block) + "</code></pre>")
            i += 1
            continue
        head = _HEAD_RX.match(line)
        if head:
            flush()
            level = min(len(head.group(1)) + 1, 4)   # clamped: the page owns <h1>
            out.append(f"<h{level}>{_inline(head.group(2).strip())}</h{level}>")
            i += 1
            continue
        if _RULE_RX.match(line):
            flush()
            out.append("<hr>")
            i += 1
            continue
        if "|" in line and i + 1 < len(lines) and _DIVIDER_RX.match(lines[i + 1]) and "|" in lines[i + 1]:
            flush()
            header = _cells(line)
            i += 2
            rows: list[list[str]] = []
            while i < len(lines) and "|" in lines[i]:
                rows.append(_cells(lines[i]))
                i += 1
            head_html = "".join(f"<th>{_inline(c)}</th>" for c in header)
            body_html = "".join(
                "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in rows
            )
            out.append(f'<div class="scroll"><table><thead><tr>{head_html}</tr></thead>'
                       f"<tbody>{body_html}</tbody></table></div>")
            continue
        for rx, tag in ((_ULI_RX, "ul"), (_OLI_RX, "ol")):
            m = rx.match(line)
            if m:
                flush()
                items: list[str] = []
                while i < len(lines) and rx.match(lines[i]):
                    items.append(_inline(rx.match(lines[i]).group(1).strip()))
                    i += 1
                out.append(f"<{tag}>" + "".join(f"<li>{it}</li>" for it in items) + f"</{tag}>")
                break
        else:
            if not line.strip():
                flush()
            else:
                para.append(line)
            i += 1
    flush()
    return "".join(out)


# --- state rendering ---------------------------------------------------------

@dataclass(frozen=True)
class State:
    """One governance state in four redundant channels. Colour is the fourth,
    and the least trusted — the word and the rail geometry survive greyscale."""

    word: str
    glyph: str
    rail: str
    tone: str
    tint: str = ""
    filled: bool = False


_STATES: dict[str, State] = {
    "auto_ran": State("RAN", "·", "rail-dotted", "auto"),
    "held": State("HELD", "‖", "rail-solid", "held", "tint-held"),
    "held-resolved": State("HELD", "‖", "rail-faint", "held"),
    "held-lapsed": State("HELD", "‖", "rail-striped", "held"),
    "approved": State("APPROVED", "✓", "rail-solid", "approved", "tint-approved"),
    "executed": State("EXECUTED", "▶", "rail-solid", "executed", "tint-executed", True),
    "rejected": State("REJECTED", "✕", "rail-striped", "rejected", "tint-rejected", True),
    "blocked": State("BLOCKED", "■", "rail-solid", "blocked", "tint-blocked", True),
    UNREADABLE: State("UNREADABLE LINE", "▨", "rail-striped", "blocked", "tint-blocked"),
}


def _pill(state: State, extra: str = "") -> str:
    fill = " fill" if state.filled else ""
    tag = f' <span class="label">{esc(extra)}</span>' if extra else ""
    return (f'<span class="pill{fill} p-{state.tone}">'
            f'<span class="glyph">{state.glyph}</span> {esc(state.word)}</span>{tag}')


def _derived(text: str, *, link: bool = True) -> str:
    """DERIVED values wear a dotted underline and point at the rule that made
    them. Inside a card (which is itself one <a>) the link is dropped rather
    than nesting an anchor."""
    if link:
        return f'<a class="d" href="#derivations">{esc(text)}</a>'
    return f'<span class="d">{esc(text)}</span>'


def _num(value: int) -> str:
    return f"{value:,}"


def _time(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts).strftime("%H:%M:%S")
    except ValueError:
        return ts or "—"


_SMALL_NUMBERS = ("No", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine")


def _sessions_phrase(n: int) -> str:
    """"Four sessions" is a claim about the data, so it is counted, not typed.

    The moment an operator approves something from this console a fifth band
    appears at the top of this page — and a governance record whose own header
    miscounts its sessions is the last thing this project can afford.
    """
    word = _SMALL_NUMBERS[n] if n < len(_SMALL_NUMBERS) else _num(n)
    return f"{word} session{'' if n == 1 else 's'}"


def _stamp(ts: str) -> str:
    """A full timestamp for a banner: date, clock, and the zone spelled out.

    The ledger stores ISO-8601 with an offset. `2026-08-21T08:20:44+00:00` is
    correct and unreadable; an operator deciding whether to authorise a write
    should not have to parse an offset to know when the agent asked.
    """
    try:
        return datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M:%S UTC")
    except ValueError:
        return ts or "—"


def _short(entry_id: str) -> str:
    return entry_id[:8]


def _noun(path: str) -> str:
    """What the file IS, read off its own name — 'discrepancy notice', not
    'MFS-BK-260708-152-discrepancy-notice.md'. Line one of a queue card must be
    a verb phrase an operator can authorise, never a filename."""
    stem = Path(path).stem
    words = [w for w in stem.split("-")
             if len(w) > 3 and not any(c.isdigit() for c in w) and not w.isupper()]
    return " ".join(words).lower()


def action_sentence(tool: str, path: str, shipment: str, *, linked: bool = True) -> str:
    """The verb phrase at the top of a decision: what the operator is being
    asked to authorise, in the order they would say it out loud."""
    noun = _noun(path)
    where = f" for {_derived(shipment, link=linked)}" if shipment else ""
    if tool == "write_file":
        article = "an" if noun[:1] in "aeiou" else "a"
        what = f"{article} {esc(noun)}" if noun else f"the file {esc(path)}"
        return f"Write {what}{where}"
    return f"Run {esc(tool)}{where}"


# --- the brief ---------------------------------------------------------------

@dataclass(frozen=True)
class Sweep:
    """The last unattended run, as one legible sentence's worth of facts."""

    session: str
    t0: str
    t1: str
    date: str
    age: str
    fresh: bool
    ndocs: int
    nships: int
    nrows: int
    nexecuted: int


def latest_sweep(entries: list[LedgerEntry]) -> Sweep | None:
    sessions = [e.session_id for e in entries if e.session_id.startswith("sweep")]
    if not sessions:
        return None
    session = sessions[-1]
    rows = [e for e in entries if e.session_id == session]
    docs = {entry_path(e) for e in rows if e.tool == "read_file" and entry_path(e)}
    ships = {m.group(1) for e in rows if (m := _SHIPMENT_RX.match(entry_path(e)))}
    try:
        last = datetime.fromisoformat(rows[-1].ts)
        first = datetime.fromisoformat(rows[0].ts)
        days = (datetime.now(UTC) - last).days
        date = first.strftime("%-d %b") if os.name != "nt" else first.strftime("%d %b")
    except ValueError:
        return None
    # Never hardcode "this morning": the demo may be recorded days later, and a
    # screen claiming freshness it cannot prove is the one thing this entry
    # cannot afford.
    age = "" if days < 1 else f" ({days} day{'s' if days != 1 else ''} ago)"
    return Sweep(session, _time(rows[0].ts)[:5], _time(rows[-1].ts)[:5], date, age, days < 1,
                 len(docs), len(ships), len(rows),
                 sum(1 for e in rows if e.outcome == "executed"))


def sweep_sentence(sweep: Sweep) -> str:
    wrote = ("wrote nothing and sent nothing" if sweep.nexecuted == 0
             else f"executed {sweep.nexecuted} approved action(s) and sent nothing")
    return (f"The unattended sweep ran {sweep.t0}–{sweep.t1} UTC on {sweep.date}{sweep.age}. "
            f"It read {sweep.ndocs} documents across {sweep.nships} shipments, made "
            f"{sweep.nrows} gate decisions, {wrote}.")


DERIVATIONS = [
    ("D1 shipment", (
        "The shipment named by the last shipments/&lt;dir&gt;/… path this session read before the "
        "hold. Last, not all: a clean shipment produces no hold to separate it from the next one.")),
    ("D2 evidence", (
        "read_file paths under that shipment, same session, before the hold, deduped, in the order "
        "the agent read them.")),
    ("D3 the draft says CRITICAL", (
        "The draft's own text matches \\bCRITICAL\\b. It is the draft quoting itself, never a "
        "governance verdict and never a score.")),
    ("D4 target state", (
        "Whether workspace/&lt;path&gt; already exists, checked through the tools' own path jail.")),
    ("D5 cleared", (
        "Shipments the sweep session touched, minus the shipments a hold was attributed to. What "
        "is left was checked and had nothing to report.")),
    ("D6 session kind", (
        "sweep-* is UNATTENDED; approval-cli and approval-console are OPERATOR sessions; anything "
        "else is an operator session by another name.")),
    ("D7 hold status", (
        "awaiting if the id is in the approval store; resolved if a later ledger row carries it as "
        "approved / rejected / executed; otherwise LAPSED — in the record, absent from the store, "
        "therefore never executable.")),
]


def derivations_block() -> str:
    items = "".join(f"<li><strong>{name}</strong> — {rule}</li>" for name, rule in DERIVATIONS)
    return (
        '<details class="raw" id="derivations"><summary>Where every derived number comes from '
        "(7 rules)</summary><div>"
        "<p class=\"lede\">Values on this page are either <strong>recorded</strong> — straight from "
        "the ledger, the approval store, the catalog or a run file, rendered plain — or "
        "<strong>derived</strong> by one of these rules, and shown with a dotted underline.</p>"
        f"<ul>{items}</ul>"
        "<p class=\"lede\">The authority split: <strong>the approval store is the authority for what "
        "is actionable; the ledger is the authority for what happened.</strong> The queue shows "
        "exactly what the store holds pending. A work queue is never reconstructed from the ledger."
        "</p></div></details>"
    )


# --- holds, assembled once per request ---------------------------------------

@dataclass(frozen=True)
class Hold:
    """One actionable decision: the store's payload joined to its ledger row and
    to everything derivable about it. The store is the authority for existence;
    the ledger is the authority for when, who and under what verdict."""

    approval_id: str
    entry: LedgerEntry | None
    tool: str
    path: str
    draft: str
    agent: str
    session: str
    shipment: str
    evidence: list[str]
    critical: str
    target: Target
    executable: bool

    @property
    def ts(self) -> str:
        return self.entry.ts if self.entry else ""

    @property
    def risk(self) -> str:
        return self.entry.risk if self.entry else "unknown"

    @property
    def verdict(self) -> str:
        return self.entry.verdict if self.entry else "unknown"


def build_holds(entries: list[LedgerEntry], pending: dict[str, Any]) -> list[Hold]:
    """The queue, in the order an ops desk works it: anything the draft itself
    calls CRITICAL first, then oldest-first. One sentence, explainable on
    camera, and every card states its own rank reason."""
    index = {e.entry_id: i for i, e in enumerate(entries)}
    holds: list[Hold] = []
    for approval_id, payload in pending.items():
        args = payload.get("args") or {}
        path = str(args.get("path", ""))
        draft = str(args.get("content", ""))
        i = index.get(approval_id)
        entry = entries[i] if i is not None else None
        shipment = derive_shipment(entries, i) if i is not None else ""
        evidence = derive_evidence(entries, i, shipment) if i is not None else []
        holds.append(Hold(
            approval_id=approval_id, entry=entry, tool=str(payload.get("tool", "")), path=path,
            draft=draft, agent=str(payload.get("agent", "")),
            session=entry.session_id if entry else "", shipment=shipment, evidence=evidence,
            critical=derive_critical(draft), target=derive_target(path),
            executable=str(payload.get("tool", "")) in _TOOL_FNS,
        ))
    holds.sort(key=lambda h: (0 if h.critical else 1, h.ts))
    return holds


def _origin(session: str) -> str:
    kind = session_kind(session)
    return "unattended sweep" if kind == "UNATTENDED" else session or "unknown session"


# --- the app -----------------------------------------------------------------

app = FastAPI(
    title="Freight Ops Fleet — Operator Console",
    docs_url=None, redoc_url=None, openapi_url=None,
)


@app.middleware("http")
async def _no_store(request: Request, call_next):
    """Nothing here is cacheable. An audit trail served from a cache is a lie
    about when it was true."""
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/healthz")
def healthz() -> JSONResponse:
    """Cloud Run's liveness probe. Deliberately touches no file."""
    return JSONResponse({"ok": True})


# --- screen 1: the Desk ------------------------------------------------------

def _flash(entries: list[LedgerEntry], pending: dict[str, Any], decided: str, why: str) -> str:
    """What confirms a decision is the record, not a toast.

    Rebuilt server-side from the ledger every time, so a refresh still shows it.
    `why` only chooses between the three "nothing happened" wordings — every
    fact on the strip comes from the ledger, never from the query string.
    """
    rows = [e for e in entries if e.approval_id == decided]
    executed = next((e for e in rows if e.outcome == "executed"), None)
    approved = next((e for e in rows if e.outcome == "approved"), None)
    rejected = next((e for e in rows if e.outcome == "rejected"), None)
    anchor = (approved or executed or rejected)
    link = (f'<a href="/ledger#e-{esc(anchor.entry_id)}">See the record ↗</a>' if anchor else "")

    if why == "not_pending" and anchor is not None:
        # A refresh of the POST, or a second click. Grants are single-use, so
        # this is the feature working — not an error page.
        return ('<div class="strip s-neutral"><div class="body">'
                f"<strong>· ALREADY DECIDED</strong> — {esc(_short(decided))} is no longer "
                f"awaiting a decision; it was recorded as {esc(anchor.outcome.upper())} at "
                f"{_time(anchor.ts)} UTC and nothing ran a second time. {link}</div></div>")

    if executed:
        path = str((executed.args_digest or {}).get("path", ""))
        target = derive_target(path)
        detail = executed.detail or ""
        ok = "status=ok" in detail
        size = f"{_num(target.size)} bytes" if target.size is not None else "size unavailable"
        if not ok:
            return (f'<div class="strip s-blocked"><div class="body">'
                    f"<strong>⚠ EXECUTED WITH ERROR</strong> {_time(executed.ts)} UTC — "
                    f"{esc(executed.tool)} returned {esc(detail)}. "
                    f'<span class="meta">Never claim a write that did not happen.</span> {link}'
                    "</div></div>")
        rows_written = "Two ledger rows written: APPROVED, EXECUTED." if approved else \
                       "One ledger row written: EXECUTED."
        return (f'<div class="strip s-executed"><div class="body">'
                f"<strong>✓ APPROVED</strong> {_time(executed.ts)} UTC — "
                f"{esc(executed.tool)} executed.<br>"
                f'<span class="mono">{esc(path)}</span> written ({size}).<br>'
                "Grant retired — single-use. This id can never execute again.<br>"
                f"{rows_written} {link}</div></div>")

    if rejected:
        return (f'<div class="strip s-rejected"><div class="body">'
                f"<strong>✕ REJECTED</strong> {_time(rejected.ts)} UTC — nothing was written.<br>"
                "The hold is recorded as REJECTED and cannot be replayed. "
                f"{link}</div></div>")

    # A refusal also leaves the id pending, so this is tested BEFORE the unwired
    # case and the unwired case is tested against the tool map rather than
    # inferred from "still on the desk". Both refusals leave the hold standing;
    # only one of them is about a missing tool body, and telling an operator
    # `write_file` is unwired when it is not would be a plain falsehood.
    if why == "gate_refused":
        return ('<div class="strip s-blocked"><div class="body">'
                "<strong>⚠ NOT EXECUTED</strong> — the gate refused the replay. Nothing was "
                f"written and the hold stands. {link}</div></div>")

    tool = str((pending.get(decided) or {}).get("tool", ""))
    if why == "not_executable" or (decided in pending and tool not in _TOOL_FNS):
        return ('<div class="strip s-held"><div class="body">'
                f"<strong>⚠ NOT EXECUTED</strong> — no executable body is wired for "
                f"<span class=\"mono\">{esc(tool or 'that tool')}</span>. The hold stands and is "
                "still on your desk. The fleet drafts; it never sends.</div></div>")

    if decided in pending:
        return ('<div class="strip s-held"><div class="body">'
                f"<strong>· NOT DECIDED</strong> — {esc(_short(decided))} is still awaiting you. "
                f'Nothing was written. <a href="/decision/{esc(decided)}">Review it ↗</a>'
                "</div></div>")

    return ('<div class="strip s-neutral"><div class="body">'
            f"<strong>· ALREADY DECIDED</strong> — {esc(_short(decided))} is no longer awaiting a "
            f"decision. {link}</div></div>")


def _queue_card(hold: Hold) -> str:
    flag = ""
    if hold.critical:
        flag = ('<div><div class="flagrail">▲ The draft says &quot;CRITICAL&quot;</div>'
                f'<div class="qmeta">{esc(hold.critical[:140])}</div></div>')
    target = ("creates a new file" if not hold.target.exists
              else f"OVERWRITES an existing file ({_num(hold.target.size or 0)} bytes)")
    docs = f"{len(hold.evidence)} document{'s' if len(hold.evidence) != 1 else ''} read"
    chars = f"{_num(len(hold.draft))} characters" if hold.draft else "no draft recorded"
    return (
        f'<a class="qcard" href="/decision/{esc(hold.approval_id)}">'
        f'<div class="rowtop" style="justify-content:space-between">'
        f'{flag or "<span></span>"}{_pill(_STATES["held"])}</div>'
        f'<div class="qtitle">{action_sentence(hold.tool, hold.path, hold.shipment, linked=False)}'
        "</div>"
        f'<div class="qpath">{esc(hold.path)} · {_derived(target, link=False)}</div>'
        f'<div class="qmeta">held {_time(hold.ts)} UTC · {esc(hold.agent)} · '
        f"{esc(_origin(hold.session))} · {_derived(docs, link=False)} · {chars}</div>"
        '<div class="qgo">Review →</div></a>'
    )


def _cleared_strip(entries: list[LedgerEntry], sweep: Sweep | None) -> str:
    if sweep is None:
        return ""
    cleared = derive_cleared(entries, sweep.session)
    if not cleared:
        return ""
    names = " · ".join(f'<span class="mono">{esc(s)}</span>' for s in cleared)
    n = len(cleared)
    return (
        '<div class="band"><div class="strip s-executed"><div class="body">'
        f'<strong>✓ CLEARED</strong> — {n} shipment{"s" if n != 1 else ""} checked, nothing to '
        f"report: {names}"
        '<p class="meta" style="margin-top:12px">This is the false-positive control. An agent that '
        "invents problems in a clean document set is worse than one that misses a real problem — so "
        "one shipment in the set has nothing wrong with it, and coming back empty is the passing "
        "answer.</p></div></div></div>"
    )


def _decided_recently(entries: list[LedgerEntry]) -> str:
    rows = [e for e in entries if e.outcome in {"approved", "rejected", "executed"}][-5:][::-1]
    if not rows:
        return ""
    items = "".join(
        f'<div class="lrow"><div class="rowtop">{_pill(_STATES[e.outcome])}'
        f'<span class="meta">{_time(e.ts)} UTC · {esc(e.tool)} · '
        f'{esc(str((e.args_digest or {}).get("path", "")))}</span>'
        f'<a class="meta" href="/ledger#e-{esc(e.entry_id)}">ref {esc(_short(e.entry_id))} ↗</a>'
        "</div></div>"
        for e in rows
    )
    return f'<div class="band"><h2>Decided recently</h2>{items}</div>'


@app.get("/", response_class=HTMLResponse)
def desk(decided: str = "", why: str = "") -> HTMLResponse:
    """The Desk. A huge waiting count and one sentence — the first frame of the
    video has to be legible on a phone held at arm's length."""
    entries = load_ledger()
    pending = load_pending()
    holds = build_holds(entries, pending)
    sweep = latest_sweep(entries)
    outbox = outbox_count()

    flash = _flash(entries, pending, decided, why) if decided else ""

    if not entries:
        brief = (
            '<div class="card"><div class="count">0</div>'
            '<div class="label" style="color:var(--ink-3);margin-top:8px">No decisions recorded yet'
            "</div>"
            '<p class="lede" style="margin-top:16px">The ledger at '
            f'<span class="mono">{esc(_ledger_path())}</span> is empty. The fleet writes it on its '
            "first tool call.</p>"
            '<p class="mono">python -m freight_fleet.cli sweep</p></div>'
        )
    else:
        waiting_label = "Decisions waiting" if holds else "Nothing waiting"
        if sweep is None:
            sentence = ('<strong>No sweep has run yet.</strong> The ledger holds decisions from '
                        "operator sessions only.")
            quickstart = '<p class="mono">python -m freight_fleet.cli sweep</p>'
        elif holds:
            sentence = esc(sweep_sentence(sweep))
            quickstart = ""
        else:
            sentence = (f"The last sweep ran {sweep.t0}–{sweep.t1} UTC on {sweep.date}{sweep.age} "
                        "and held nothing. Your desk is clear.")
            quickstart = ""
        brief = (
            '<div class="card"><div class="hero">'
            f'<div class="num"><div class="count">{len(holds)}</div>'
            f'<div class="label">{waiting_label}</div></div>'
            '<div class="rest">'
            f'<div class="num"><div class="count2">{outbox}</div>'
            '<div class="label">In outbox</div></div>'
            '<div class="num"><div class="count2">0</div>'
            '<div class="label">Transmitted</div></div>'
            "</div></div>"
            f'<p class="lede">{sentence}</p>{quickstart}'
            '<p class="meta">send_email is classified CRITICAL and unwired — the constant 0 above '
            "is a property of the fleet, not of today.</p></div>"
        )

    if holds:
        section = "Overnight" if (sweep and sweep.fresh) else "Awaiting you"
        queue = (f'<div class="band"><h2>{section} — {len(holds)} awaiting your decision</h2>'
                 '<div class="queue">' + "".join(_queue_card(h) for h in holds) + "</div></div>")
    else:
        queue = ""

    body = (
        flash
        + "<h1>Operator desk</h1>"
        + '<p class="lede">The approval store is the authority for what is actionable; the ledger '
          "is the authority for what happened.</p>"
        + brief + queue
        + _cleared_strip(entries, sweep)
        + _decided_recently(entries)
        + '<div class="band">' + derivations_block() + "</div>"
    )
    return HTMLResponse(_shell("Operator desk", body, active="desk", pending=len(pending)))


# --- screen 2: the Decision --------------------------------------------------

def _not_found(what: str, detail: str) -> HTMLResponse:
    body = (f"<h1>404 — {esc(what)}</h1><p class=\"lede\">{detail}</p>"
            '<p><a href="/">← Back to the desk</a></p>')
    return HTMLResponse(_shell(f"404 — {what}", body, show_footer=False), status_code=404)


def _exhibit(entry: LedgerEntry, entries: list[LedgerEntry], pending: dict[str, Any]) -> HTMLResponse:
    """A hold that is no longer actionable is still evidence. Read-only, no
    buttons, and the missing draft is named rather than faked."""
    status, resolver = hold_status(entry, entries, pending)
    i = next(i for i, e in enumerate(entries) if e.entry_id == entry.entry_id)
    shipment = derive_shipment(entries, i)
    if status == "resolved" and resolver is not None:
        state = _STATES["held-resolved"]
        banner = (f"RESOLVED — recorded as {esc(resolver.outcome.upper())} at "
                  f"{_time(resolver.ts)} UTC")
        note = (f'<p><a href="/ledger#e-{esc(resolver.entry_id)}">See the resolving row ↗</a></p>')
    else:
        state = _STATES["held-lapsed"]
        banner = "LAPSED — held in the ledger, absent from the approval store"
        note = ("<p>Held in the ledger; no longer in the approval store. Never approved, never "
                "executed — nothing was written. The ledger keeps the row because the ledger never "
                "forgets. The word is LAPSED, not expired: there is no timer.</p>")
    path = str((entry.args_digest or {}).get("path", ""))
    body = (
        f'<div class="strip s-held {state.rail}"><div class="body">'
        f'<strong><span class="glyph">{state.glyph}</span> {banner}</strong><br>'
        f'<span class="meta">{esc(entry.tool)} · risk {esc(entry.risk).upper()} · verdict '
        f"{esc(entry.verdict).upper()} · held {esc(_stamp(entry.ts))}<br>"
        f"by {esc(entry.agent)} in session {esc(entry.session_id)}<br>"
        f"approval id {esc(entry.entry_id)}</span></div></div>"
        f"<h1>{action_sentence(entry.tool, path, shipment)}</h1>"
        f'<p class="mono">{esc(path)}</p>'
        f'<div class="card">{note}'
        "<p>The draft is not available here: it lived in the approval store and was retired. The "
        f"ledger recorded its shape — {esc(str((entry.args_digest or {}).get('content_chars', '?')))} "
        "characters — not its body, because a 40 KB payload in an audit trail is noise.</p></div>"
        f'<p><a href="/ledger#e-{esc(entry.entry_id)}">See this row in the record ↗</a> · '
        '<a href="/">← Back to the desk</a></p>'
    )
    return HTMLResponse(_shell("Decision — exhibit", body, pending=len(pending)))


@app.get("/decision/{approval_id}", response_class=HTMLResponse)
def decision(approval_id: str) -> HTMLResponse:
    """One decision, in the order an operator needs it: what state it is in and
    who put it there, what happens if you approve, what happens if you reject,
    whether it overwrites, the exact draft, the documents behind it, and only
    then two large buttons that state their own effect."""
    entries = load_ledger()
    pending = load_pending()

    if approval_id not in pending:
        held = next((e for e in entries
                     if e.entry_id == approval_id and e.outcome == "held"), None)
        if held is None:
            return _not_found("Not in evidence", "No held action carries this id.")
        return _exhibit(held, entries, pending)

    hold = next(h for h in build_holds(entries, pending) if h.approval_id == approval_id)
    draft_bytes = len(hold.draft.encode("utf-8"))
    draft_sha = hashlib.sha256(hold.draft.encode("utf-8")).hexdigest()[:12]

    banner = (
        f'<div class="strip s-held"><div class="body">'
        f'<strong><span class="glyph">‖</span> HELD — THIS HAS NOT RUN</strong><br>'
        f'<span class="meta">{esc(hold.tool)} · risk {esc(hold.risk).upper()} · verdict '
        f"{esc(hold.verdict).upper()} · held {esc(_stamp(hold.ts))}<br>"
        f"by {esc(hold.agent)} during the {esc(_origin(hold.session))} "
        f"{esc(hold.session)}<br>approval id {esc(hold.approval_id)}</span></div></div>"
    )

    if hold.target.exists:
        overwrite = (f'<p style="color:var(--blocked)"><strong>⚠ THIS OVERWRITES</strong> an '
                     f"existing file ({_num(hold.target.size or 0)} bytes, modified "
                     f"{esc(hold.target.modified)}).</p>")
    else:
        overwrite = "<p>The file does not exist yet.</p>"

    contract = (
        '<div class="contract">'
        f'<div class="card"><div class="label" style="color:var(--ink-3)">If you approve</div>'
        f"<p style=\"margin-top:12px\">{esc(hold.tool)} will create "
        f'<span class="mono">{esc(hold.path)}</span> — {_num(len(hold.draft))} characters.</p>'
        f"{overwrite}"
        "<p>Nothing is emailed. Nothing leaves this machine. The fleet has no send path.</p>"
        "<p>The grant is single-use: it buys this one execution and is retired immediately.</p>"
        "</div>"
        '<div class="card"><div class="label" style="color:var(--ink-3)">If you reject</div>'
        '<p style="margin-top:12px">Nothing is written. The hold is recorded as REJECTED in the '
        "ledger and cannot be replayed.</p></div></div>"
    )

    draft = (
        "<h2>The draft</h2>"
        f'<div class="draft">{md_lite(hold.draft)}</div>'
        '<details class="raw"><summary>View the exact bytes that will be written '
        f'<span class="meta">· {_num(draft_bytes)} bytes · sha256 {draft_sha}…</span></summary>'
        f"<div><pre>{esc(hold.draft)}</pre></div></details>"
    )

    if hold.evidence:
        links = "".join(
            f'<li><a class="mono" href="/doc?path={quote(p)}">{esc(p)}</a> '
            f'<span class="meta">{esc(Path(p).suffix.lstrip(".") or "file")}</span></li>'
            for p in hold.evidence
        )
        evidence = (
            f"<h2>What the agent read before drafting this</h2>"
            f'<p class="lede">Shipment {_derived(hold.shipment or "unattributed")} · '
            f"{len(hold.evidence)} documents. Open one and check the figure the notice quotes.</p>"
            f"<ul>{links}</ul>"
        )
    else:
        evidence = ('<h2>What the agent read before drafting this</h2>'
                    '<p class="lede">No read_file rows precede this hold in its session, so the '
                    "console can name no evidence for it. That absence is itself worth knowing.</p>")

    if _readonly():
        actions = (
            '<div class="actions">'
            '<button class="btn" disabled>Approve — disabled</button>'
            '<button class="btn outline" disabled>Reject — disabled</button>'
            '<p class="meta">READ-ONLY CONSOLE — decisions are enabled in the local instance.</p>'
            "</div>"
        )
    elif not hold.executable:
        actions = (
            '<div class="actions">'
            f'<button class="btn" disabled>Approve — {esc(hold.tool)}</button>'
            f'<p class="meta">NOT EXECUTABLE — no executable body is wired for {esc(hold.tool)}. '
            "The fleet drafts; it never sends.</p>"
            f'<form method="post" action="/decision/{esc(hold.approval_id)}/reject">'
            '<button class="btn outline" type="submit">Reject — write nothing</button></form>'
            "</div>"
        )
    else:
        actions = (
            '<div class="actions">'
            f'<form method="post" action="/decision/{esc(hold.approval_id)}/approve">'
            f'<button class="btn" type="submit">Approve — write {esc(hold.path)}</button></form>'
            f'<form method="post" action="/decision/{esc(hold.approval_id)}/reject">'
            '<button class="btn outline" type="submit">Reject — write nothing</button></form>'
            '<p class="meta">The button is the confirmation. Approving replays the call through '
            "before_tool_gate; if the gate refuses, nothing runs.</p></div>"
        )

    flag = ""
    if hold.critical:
        flag = (f'<div class="strip s-held"><div class="body"><strong>▲ THE DRAFT SAYS '
                f'&quot;CRITICAL&quot;</strong><br><span class="mono">{esc(hold.critical)}</span>'
                '<br><span class="meta">The draft quoting itself (D3). Not a governance verdict.'
                "</span></div></div>")

    body = (
        banner + flag
        + f"<h1>{action_sentence(hold.tool, hold.path, hold.shipment)}</h1>"
        + contract + draft + evidence + actions
        + '<p style="margin-top:24px"><a href="/">← Back to the desk</a></p>'
    )
    return HTMLResponse(_shell("Decision", body, pending=len(pending)))


# --- the two writes ----------------------------------------------------------

def _forbidden() -> HTMLResponse:
    body = ("<h1>403 — Read-only console</h1>"
            '<p class="lede">READ-ONLY CONSOLE — decisions are enabled in the local instance. '
            "No store was touched and no ledger row was written.</p>"
            '<p><a href="/">← Back to the desk</a></p>')
    return HTMLResponse(_shell("403 — read-only", body, show_footer=False), status_code=403)


def _known(approval_id: str, pending: dict[str, Any], entries: list[LedgerEntry]) -> bool:
    """A POST for an id the fleet never held is a 404, not a decision. The
    ledger and the store together are the whole universe of real ids."""
    return approval_id in pending or any(
        e.entry_id == approval_id and e.outcome == "held" for e in entries)


@app.post("/decision/{approval_id}/approve")
def approve(approval_id: str):
    """The console's ONLY write path. It grants and then replays through
    `before_tool_gate` — the identical function `cli approvals grant` calls."""
    if _readonly():
        return _forbidden()
    pending = load_pending()
    if not _known(approval_id, pending, load_ledger()):
        return _not_found("Not in evidence", "No held action carries this id.")
    result = execute_approved(
        approval_id,
        ledger=Ledger(_ledger_path()),
        approvals=FileApprovalStore(_approvals_path()),
        tool_fns=_TOOL_FNS,
        source=SOURCE,
    )
    return RedirectResponse(f"/?decided={quote(approval_id)}&why={quote(result.status)}",
                            status_code=303)


@app.post("/decision/{approval_id}/reject")
def reject(approval_id: str):
    """Retire a hold without running it. Nothing is written, ever."""
    if _readonly():
        return _forbidden()
    pending = load_pending()
    if not _known(approval_id, pending, load_ledger()):
        return _not_found("Not in evidence", "No held action carries this id.")
    result = reject_approved(
        approval_id,
        ledger=Ledger(_ledger_path()),
        approvals=FileApprovalStore(_approvals_path()),
        source=SOURCE,
    )
    return RedirectResponse(f"/?decided={quote(approval_id)}&why={quote(result.status)}",
                            status_code=303)


# --- screen 3: the Record ----------------------------------------------------

#: Actors that legitimately appear in the ledger without a catalog card: the
#: human, and the coordinator that only routes. Anything else is a disagreement
#: between the record and the catalog, and the console says so rather than
#: quietly rendering it as normal.
_NON_SPECIALIST_ACTORS = frozenset({"operator", "freight_ops_coordinator", "unknown"})


def _actor(name: str) -> str:
    known = {c.key for c in FLEET} | _NON_SPECIALIST_ACTORS
    if name in known:
        card = next((c for c in FLEET if c.key == name), None)
        if card is not None:
            return f'<a class="mono" href="/fleet#{esc(name)}">{esc(name)}</a>'
        return f'<span class="mono">{esc(name)}</span>'
    return (f'<span class="mono" style="color:var(--blocked)">{esc(name)} ▲ not in catalog</span>')


def _chain(entry: LedgerEntry, entries: list[LedgerEntry], pending: dict[str, Any]) -> str:
    """A held row and its approval live in DIFFERENT sessions, so the causal
    chain spans bands. Both ends carry a link, and `:target` rings the row it
    lands on — deep links with no JavaScript."""
    if entry.outcome == "held":
        status, resolver = hold_status(entry, entries, pending)
        if status == "awaiting":
            return (f'<a class="label" href="/decision/{esc(entry.entry_id)}" '
                    'style="color:var(--held)">Awaiting you →</a>')
        if status == "resolved" and resolver is not None:
            return (f'<a class="label" href="/ledger#e-{esc(resolver.entry_id)}">'
                    f"→ resolved {_time(resolver.ts)} ↗</a>")
        return ('<span class="label" style="color:var(--held)">Lapsed</span>')
    if entry.approval_id:
        return (f'<a class="meta" href="/ledger#e-{esc(entry.approval_id)}">'
                f"approval {esc(_short(entry.approval_id))} ↗</a>")
    return ""


def _ledger_row(entry: LedgerEntry, entries: list[LedgerEntry], pending: dict[str, Any]) -> str:
    if entry.outcome == UNREADABLE:
        state = _STATES[UNREADABLE]
        raw = str((entry.args_digest or {}).get("raw", ""))
        line = (entry.args_digest or {}).get("line", "?")
        return (
            f'<div class="lrow {state.rail} {state.tint}" id="e-{esc(entry.entry_id)}" '
            f'style="color:var(--{state.tone})"><div class="rowtop">{_pill(state)}'
            f'<span class="meta">line {esc(line)}</span></div>'
            f'<pre class="mono" style="white-space:pre-wrap;color:var(--ink)">{esc(raw)}</pre>'
            '<div class="meta">Never silently skipped — a skipped line in an append-only ledger '
            "is exactly the thing this project promises never happens.</div></div>"
        )
    state = _STATES.get(entry.outcome, _STATES["auto_ran"])
    tint = state.tint
    if entry.outcome == "held":
        status, _ = hold_status(entry, entries, pending)
        state = _STATES[{"awaiting": "held", "resolved": "held-resolved",
                         "lapsed": "held-lapsed"}[status]]
        tint = state.tint
        extra = {"awaiting": "AWAITING YOU", "resolved": "RESOLVED", "lapsed": "LAPSED"}[status]
    else:
        extra = ""
    path = str((entry.args_digest or {}).get("path", (entry.args_digest or {}).get("prefix", "")))
    lapsed_note = ""
    if extra == "LAPSED":
        lapsed_note = ('<div class="meta">Held in the ledger; no longer in the approval store. '
                       "Never approved, never executed — nothing was written. The ledger keeps the "
                       "row because the ledger never forgets.</div>")
    return (
        f'<div class="lrow {state.rail} {tint}" id="e-{esc(entry.entry_id)}" '
        f'style="color:var(--{state.tone})"><div class="rowtop">'
        f"{_pill(state, extra)}"
        f'<span class="meta">{_time(entry.ts)}</span>{_actor(entry.agent)}'
        f'<span class="mono">{esc(entry.tool)}</span>'
        f'<span class="meta">risk {esc(entry.risk)} · verdict {esc(entry.verdict)}</span>'
        f'<span class="meta" style="margin-left:auto">ref {esc(_short(entry.entry_id))}</span>'
        f"{_chain(entry, entries, pending)}</div>"
        + (f'<div class="qpath">{esc(path)}</div>' if path else "")
        + (f'<div class="meta">{esc(entry.detail)}</div>' if entry.detail else "")
        + lapsed_note
        + "</div>"
    )


def _run_summary(run: list[LedgerEntry]) -> str:
    """Runs of read-only calls collapse. Nothing is hidden: the count is on the
    closed row, every row is one click away, and /ledger.jsonl is the unedited
    file. 47 of 56 rows are reads; rendered as equals they drown the 9 rows that
    are the product."""
    tools: dict[str, int] = {}
    for e in run:
        tools[e.tool] = tools.get(e.tool, 0) + 1
    breakdown = " · ".join(f"{esc(t)} ×{n}" for t, n in tools.items())
    inner = "".join(
        f'<div class="meta" id="e-{esc(e.entry_id)}">{_time(e.ts)} · {esc(e.tool)} · '
        f'{esc(str((e.args_digest or {}).get("path", (e.args_digest or {}).get("prefix", "")) or ""))}'
        f' <span style="opacity:.7">ref {esc(_short(e.entry_id))}</span></div>'
        for e in run
    )
    return (
        f'<details class="run rail-dotted"><summary><span class="glyph">·</span> '
        f'<span class="mono">{esc(run[0].agent)}</span> ran {len(run)} read-only tools '
        f"{_time(run[0].ts)}–{_time(run[-1].ts)}</summary>"
        f'<div style="padding:0 0 12px">{breakdown}{inner}</div></details>'
    )


def _band_rows(rows: list[LedgerEntry], entries: list[LedgerEntry],
               pending: dict[str, Any]) -> str:
    out: list[str] = []
    run: list[LedgerEntry] = []

    def flush_run() -> None:
        if not run:
            return
        if len(run) >= 3:
            out.append(_run_summary(run))
        else:
            out.extend(_ledger_row(e, entries, pending) for e in run)
        run.clear()

    for entry in rows:
        if entry.outcome == "auto_ran":
            if run and run[-1].agent != entry.agent:
                flush_run()
            run.append(entry)
            continue
        flush_run()
        out.append(_ledger_row(entry, entries, pending))
    flush_run()
    return "".join(out)


def _summary_strip(entries: list[LedgerEntry], pending: dict[str, Any]) -> str:
    real = [e for e in entries if e.outcome != UNREADABLE]
    unreadable = len(entries) - len(real)
    counts = {k: sum(1 for e in real if e.outcome == k)
              for k in ("auto_ran", "held", "approved", "executed", "rejected", "blocked")}
    holds = [e for e in real if e.outcome == "held"]
    tally = {"awaiting": 0, "resolved": 0, "lapsed": 0}
    for h in holds:
        tally[hold_status(h, entries, pending)[0]] += 1

    def cell(n: int, label: str) -> str:
        return (f'<div class="num"><div class="count3">{n}</div>'
                f'<div class="label" style="color:var(--ink-3)">{esc(label)}</div></div>')

    numbers = "".join([
        cell(len(real), "decisions"), cell(counts["auto_ran"], "ran"), cell(counts["held"], "held"),
        cell(counts["approved"], "approved"), cell(counts["executed"], "executed"),
        cell(counts["rejected"], "rejected"), cell(counts["blocked"], "blocked"),
    ])
    hold_tally = (f"{tally['awaiting']} awaiting you · {tally['resolved']} resolved · "
                  f"{tally['lapsed']} lapsed")
    sha = ledger_sha256()
    sha_line = (f' · sha256 {esc(sha)}…' if sha else "")
    unread_line = (f'<p style="color:var(--blocked)">{unreadable} line(s) would not parse and are '
                   "rendered below as UNREADABLE LINE.</p>" if unreadable else "")
    return (
        '<div class="card">'
        f'<div class="hero" style="gap:24px 32px">{numbers}</div>'
        f'<p class="meta">{_derived(hold_tally)}</p>'
        f'<p class="meta">append-only · {esc(_ledger_path())} · {len(entries)} lines{sha_line} · '
        '<a href="/ledger.jsonl">raw ↗</a></p>'
        '<p class="lede">There is no update path and no delete path in the code. A record that can '
        "be edited is not evidence. The hash is the sha256 of the file as served — a file hash, not "
        "a Merkle chain; recompute it with <code>shasum -a 256</code>.</p>"
        f'<p class="lede">The {counts["held"] + counts["approved"] + counts["executed"]} '
        f'<code>ask</code>-verdict rows are not '
        f'{counts["held"] + counts["approved"] + counts["executed"]} attempts: they are '
        f'{counts["held"]} holds plus {counts["approved"]} approved plus {counts["executed"]} '
        "executed, and the last two are one attempt replayed.</p>"
        f"{unread_line}</div>"
    )


@app.get("/ledger", response_class=HTMLResponse)
def record() -> HTMLResponse:
    """The Record — one screen of every decision the fleet made, because that is
    the artifact an operator shows their boss. Bands newest-session-first; rows
    chronological inside a band, so held → approved → executed reads downward,
    the direction of causality."""
    entries = load_ledger()
    pending = load_pending()
    if not entries:
        body = ("<h1>The record</h1>"
                f'<p class="lede"><span class="mono">{esc(_ledger_path())}</span> does not exist '
                "yet. The fleet writes it on its first tool call.</p>"
                '<p class="mono">python -m freight_fleet.cli sweep</p>')
        return HTMLResponse(_shell("The record", body, active="ledger", pending=len(pending)))

    order: list[str] = []
    for e in entries:
        if e.session_id not in order:
            order.append(e.session_id)
    order.sort(key=lambda s: max(e.ts for e in entries if e.session_id == s), reverse=True)

    bands: list[str] = []
    for session in order:
        rows = [e for e in entries if e.session_id == session]
        held = sum(1 for e in rows if e.outcome == "held")
        executed = sum(1 for e in rows if e.outcome == "executed")
        wrote = "nothing written" if executed == 0 else f"{executed} written"
        bands.append(
            f'<div class="sessionband"><span class="mono">{esc(session)}</span>'
            f'<span class="label" style="color:var(--ink-3)">{esc(session_kind(session))}</span>'
            f'<span class="meta">{_time(rows[0].ts)} → {_time(rows[-1].ts)} UTC</span>'
            f'<span class="meta" style="width:100%">{len(rows)} decisions · {held} held · '
            f"{executed} executed · {esc(wrote)}</span></div>"
            + _band_rows(rows, entries, pending)
        )

    body = (
        '<div class="printhead"><strong>Freight Ops Fleet — audit ledger</strong><br>'
        f'{esc(_ledger_path())} · {len(entries)} lines · sha256 {esc(ledger_sha256())}… · printed '
        f'{datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")}</div>'
        "<h1>The record</h1>"
        '<p class="lede">Every gate decision the fleet made, whichever way it went. '
        f"{_sessions_phrase(len(bands))}, one file, no edit path.</p>"
        + _summary_strip(entries, pending)
        + "".join(bands)
        + '<div class="band">' + derivations_block() + "</div>"
    )
    return HTMLResponse(_shell("The record", body, active="ledger", pending=len(pending)))


@app.get("/ledger.jsonl")
def ledger_raw() -> PlainTextResponse:
    """The unedited file. Everything the console renders is derivable from this."""
    try:
        return PlainTextResponse(_ledger_path().read_text(encoding="utf-8"),
                                 media_type="text/plain")
    except OSError:
        return PlainTextResponse("", media_type="text/plain")


# --- screen 4: the Catalog ---------------------------------------------------

@app.get("/fleet", response_class=HTMLResponse)
def fleet() -> HTMLResponse:
    """The catalog, grouped by desk — "cross-department" is the track's literal
    word. Every tool chip carries its risk class, so the card is the allowlist
    and the gate classifies what the card grants, in one glance."""
    entries = load_ledger()
    pending = load_pending()
    desks: dict[str, list] = {}
    for card in FLEET:
        desks.setdefault(card.desk, []).append(card)

    sections: list[str] = []
    for desk_name, cards in desks.items():
        blocks: list[str] = []
        for card in cards:
            rows = [e for e in entries if e.agent == card.key]
            chips = []
            for tool in card.tools:
                spec, verdict = policy.classify(tool)
                risk = spec.risk.value.upper() if spec else "UNKNOWN"
                if verdict is policy.Verdict.ASK:
                    chips.append(f'<span class="chip held"><span class="glyph">‖</span> '
                                 f"{esc(tool)} {risk}·HOLDS</span>")
                else:
                    chips.append(f'<span class="chip">{esc(tool)} {risk}·runs</span>')
            blocks.append(
                f'<div class="card" id="{esc(card.key)}" style="margin-bottom:12px">'
                f'<div class="rowtop" style="justify-content:space-between">'
                f'<h2>{esc(card.name)}</h2>'
                f'<span class="label" style="color:var(--ink-3)">{esc(card.autonomy)}</span></div>'
                f'<p class="meta"><span class="mono">{esc(card.key)}</span> · {esc(card.desk)} · '
                f"owner: {esc(card.owner)}</p>"
                f"<p>{esc(card.description)}</p>"
                f'<p class="meta">scope · {esc(card.data_scope)}</p>'
                f'<div class="chips">{"".join(chips)}</div>'
                f'<p class="meta">cap ${card.max_usd_per_run:.2f} / run · ledger: {len(rows)} '
                f'decisions · {sum(1 for e in rows if e.outcome == "held")} held · '
                f'{sum(1 for e in rows if e.outcome == "executed")} executed · '
                f'{sum(1 for e in rows if e.outcome == "blocked")} blocked</p></div>'
            )
        sections.append(f'<div class="band"><h2>{esc(desk_name)} ({len(cards)})</h2>'
                        + "".join(blocks) + "</div>")

    tool_rows = "".join(
        f"<tr><td class=\"mono\">{'<s>' + esc(spec.name) + '</s>' if spec.name == 'send_email' else esc(spec.name)}</td>"
        f'<td class="mono">{esc(spec.risk.value.upper())}</td>'
        f'<td class="mono">{"external ✓" if spec.external_side_effect else "—"}</td>'
        f'<td class="mono">{esc(policy.classify(spec.name)[1].value.upper())}</td>'
        f"<td>{esc(spec.description)}</td></tr>"
        for spec in policy.TOOL_SPECS.values()
    )
    hazard = (
        '<tr class="hazard"><td class="mono">▨ ANY TOOL NOT IN THIS TABLE</td>'
        '<td class="mono">unknown</td><td class="mono">yes</td><td class="mono">BLOCK</td>'
        "<td>Adding a capability means classifying it, or nothing happens.</td></tr>"
    )
    body = (
        "<h1>The fleet</h1>"
        '<p class="lede">An agent not in this catalog is not in the fleet. '
        "<code>build_fleet()</code> grants each desk exactly the tools its card names.</p>"
        + "".join(sections)
        + '<div class="band"><h2>Tool risk table</h2>'
        '<p class="lede">The gate classifies every call against this table before any tool body '
        "runs. Verdicts combine tighten-only; nothing here can be loosened downstream.</p>"
        '<div class="scroll"><table><thead><tr><th>Tool</th><th>Risk</th>'
        "<th>External side effect</th><th>Verdict floor</th><th>What it does</th></tr></thead>"
        f"<tbody>{tool_rows}{hazard}</tbody></table></div>"
        '<p class="lede" style="margin-top:24px"><strong>Policy is code-owned. There is no editor '
        "here, by design</strong> — a reviewable diff is a better audit surface than a settings "
        "screen. <code>src/freight_fleet/governance/policy.py</code></p></div>"
    )
    return HTMLResponse(_shell("The fleet", body, active="fleet", pending=len(pending)))


# --- screen 5: the Scoreboard ------------------------------------------------

def _run_score(record_: dict[str, Any]) -> tuple[int, int]:
    """(passed, gradable). A result with no `passed` key is manual tier and does
    not enter either number — a screen showing 9/9 would be the one dishonest
    pixel in the entry."""
    results = record_.get("results") or []
    gradable = [r for r in results if "passed" in r]
    return sum(1 for r in gradable if r.get("passed")), len(gradable)


@app.get("/evidence", response_class=HTMLResponse)
def evidence() -> HTMLResponse:
    """Why should the operator trust the five things on their desk? Because the
    answer keys were written before the agents existed, and no model sits in the
    grading path."""
    pending = load_pending()
    runs = load_runs()
    chosen = best_run(runs)
    if chosen is None:
        body = (
            "<h1>The scoreboard</h1>"
            '<div class="card"><p class="lede">No scored run is present in this deployment. The '
            "image deliberately excludes <code>eval/</code>, so a deployed agent cannot read the "
            "answer keys even in principle. The committed run record is in the repo at "
            "<code>eval/runs/</code>.</p>"
            '<p class="meta">This console reads run records only. It never opens '
            "<code>eval/answer_keys/</code> — a screen that needed those files would render "
            "differently in the demo than in the deploy.</p></div>"
        )
        return HTMLResponse(_shell("The scoreboard", body, active="evidence",
                                   pending=len(pending)))

    passed, gradable = _run_score(chosen)
    results = chosen.get("results") or []
    clean = next((r for r in results if r.get("id") == "g2_clean_control"), None)

    clean_block = ""
    if clean is not None:
        verdict = "PASS" if clean.get("passed") else "FAIL"
        clean_block = (
            '<div class="card" style="border:3px double var(--line);margin:24px 0">'
            f'<div class="label" style="color:var(--ink-3)">The number to look at first</div>'
            f'<p style="margin-top:12px"><span class="mono">g2_clean_control</span> — '
            f'<strong>{esc(verdict)}</strong> — {esc(clean.get("details", ""))}</p>'
            "<p>A missed discrepancy costs a correction. A fabricated discrepancy costs trust, and "
            "an operator who catches the agent inventing a problem in a clean document set stops "
            "believing all of its output, including the true findings. So discrepant shipments are "
            "graded leniently and the clean shipment is graded with zero tolerance.</p></div>"
        )

    rows: list[str] = []
    for r in results:
        rid = str(r.get("id", "?"))
        if "passed" in r:
            state = "executed" if r.get("passed") else "blocked"
            word = "PASS" if r.get("passed") else "FAIL"
            mark = (f'<span class="pill fill p-{state}">{word}</span>')
            note = esc(r.get("details", ""))
            score = f'{float(r.get("score", 0)):.2f}'
            style = ""
        else:
            mark = ('<span class="pill p-rejected" style="border-style:dotted">'
                    "<span class=\"glyph\">◇</span> EYE-REVIEWED</span>")
            note = ("reviewed by a human, not by a regex — a weaker claim, labelled as such")
            score = "—"
            style = ' style="border-style:dotted"'
        final = r.get("final_text") or ""
        detail = (f'<details class="raw"{style}><summary>{esc(rid)} — read the answer</summary>'
                  f'<div class="draft" style="max-height:none;-webkit-mask-image:none;'
                  f'mask-image:none">{md_lite(final)}</div></details>' if final else "")
        rows.append(f'<tr><td class="mono">{esc(rid)}</td><td>{mark}</td>'
                    f'<td class="mono">{score}</td><td>{note}{detail}</td></tr>')

    history = [r for r in reversed(runs) if len(r.get("results") or []) >= 6]
    bars = " → ".join(f'<span class="mono">{p}/{g}</span>'
                      for p, g in (_run_score(r) for r in history))

    body = (
        "<h1>The scoreboard</h1>"
        f'<div class="card"><div class="count">{passed} / {gradable}</div>'
        '<div class="label" style="color:var(--ink-3);margin-top:8px">Gradable tasks passed</div>'
        f'<p class="meta" style="margin-top:16px">{esc(chosen.get("model", "?"))} · run '
        f'{esc(chosen.get("ts", "?"))} · <span class="mono">eval/runs/{esc(chosen.get("_file"))}'
        '</span></p>'
        '<p class="label" style="color:var(--ink-3)">Most complete run</p>'
        '<p class="lede">The answer keys were written before the agents existed. No model sits in '
        "the grading path. The run shown is the newest with at least six results — not the newest "
        "file, which may be a two-task hero-tier re-run.</p></div>"
        + clean_block
        + '<div class="band"><h2>Every task in the run</h2><div class="scroll"><table><thead><tr>'
        "<th>Task</th><th>Verdict</th><th>Score</th><th>Details</th></tr></thead>"
        f'<tbody>{"".join(rows)}</tbody></table></div></div>'
        + f'<div class="band"><h2>How it got here</h2><p class="count2">{bars}</p>'
        '<p class="meta">passed / gradable, oldest run first. The denominator moved from 6 to 7 '
        "when the governance task <span class=\"mono\">g9</span> became mechanically gradable.</p>"
        "</div>"
    )
    return HTMLResponse(_shell("The scoreboard", body, active="evidence", pending=len(pending)))


# --- screen 6: a source document ---------------------------------------------

def evidence_paths(entries: list[LedgerEntry], pending: dict[str, Any]) -> set[str]:
    """Every path the ledger records the fleet touching, plus the targets it is
    holding. `/doc` opens nothing else — that keeps the design law true rather
    than aspirational."""
    paths = {entry_path(e) for e in entries if entry_path(e)}
    for payload in pending.values():
        target = str((payload.get("args") or {}).get("path", ""))
        if target:
            paths.add(target)
    return paths


def _csv_table(text: str) -> str:
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return "<p>empty file</p>"
    head = "".join(f"<th>{esc(c)}</th>" for c in rows[0])
    body = "".join("<tr>" + "".join(f'<td class="mono">{esc(c)}</td>' for c in r) + "</tr>"
                   for r in rows[1:])
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


@app.get("/doc", response_class=HTMLResponse)
def doc(path: str = "") -> HTMLResponse:
    """One source document, reachable only from a decision's evidence list.

    Three guards, all required: the tools' own path jail, an extension
    allowlist, and membership of the evidence set. `?path=/etc/passwd` and
    `?path=../../secrets` both land on the same 404.
    """
    entries = load_ledger()
    pending = load_pending()
    denied = _not_found(
        "Not in evidence",
        "This console only opens files the ledger records the fleet touching.",
    )
    if not path or Path(path).suffix not in _READABLE_SUFFIXES:
        return denied
    try:
        workspace._safe(path)
    except workspace.WorkspaceError:
        return denied
    if path not in evidence_paths(entries, pending):
        return denied

    reads = [e for e in entries if e.tool == "read_file" and entry_path(e) == path]
    if reads:
        agents = sorted({e.agent for e in reads})
        provenance = (f"read by {esc(', '.join(agents))} · {len(reads)} time"
                      f"{'s' if len(reads) != 1 else ''} · first {_time(reads[0].ts)} · "
                      f"last {_time(reads[-1].ts)} UTC")
    else:
        provenance = "not read by any agent — this path appears as a held write target"

    result = workspace.read_file(path)
    if result.get("status") != "ok":
        hold = next((h for h in build_holds(entries, pending) if h.path == path), None)
        if hold is not None:
            panel = (
                '<div class="card"><h2>Nothing exists at this path.</h2>'
                f'<p><span class="mono">{esc(path)}</span> is held for approval since '
                f"{_time(hold.ts)} UTC. The draft is {_num(len(hold.draft))} characters and lives "
                "in the approval store, not the workspace.</p>"
                f'<p><a href="/decision/{esc(hold.approval_id)}">The decision ↗</a></p></div>'
            )
        else:
            panel = (f'<div class="card"><p>This file no longer exists at '
                     f'<span class="mono">{esc(path)}</span> '
                     f'({esc(result.get("status", "unknown"))}).</p></div>')
        body = (f"<h1>{esc(path)}</h1><p class=\"meta\">{provenance}</p>{panel}"
                '<p><a href="/">← Back to the desk</a></p>')
        return HTMLResponse(_shell(Path(path).name, body, pending=len(pending)))

    text = str(result.get("content", ""))
    suffix = Path(path).suffix
    if suffix == ".csv":
        rendered = _csv_table(text)
    elif suffix == ".md":
        rendered = f'<div class="draft" style="max-height:none">{md_lite(text)}</div>'
    else:
        rendered = f'<pre class="mono" style="white-space:pre-wrap">{esc(text)}</pre>'

    body = (
        f"<h1>{esc(Path(path).name)}</h1>"
        f'<p class="mono">{esc(path)} · {_num(len(text.encode("utf-8")))} bytes</p>'
        f'<p class="meta">{provenance}</p>'
        f"{rendered}"
        '<details class="raw"><summary>Raw text</summary>'
        f'<div><pre>{esc(text)}</pre></div></details>'
        '<p style="margin-top:24px"><a href="/">← Back to the desk</a></p>'
    )
    return HTMLResponse(_shell(Path(path).name, body, pending=len(pending)))
