# Freight Ops Fleet — submission writeup (draft)

> **Track:** The Fortified Enterprise Fleet · All Things Agentic
> **Stack:** Google Agent Development Kit 2.7.1 + Gemini 3.7 Flash
> **Repo:** https://github.com/MDadopoulos/freightopsfleet
>
> *Draft for review. Bracketed items need your input before submitting.*

---

## The one-sentence pitch

**The agents do the paperwork; the operator keeps the controls, and can prove it
afterwards.**

---

## The problem, in an operator's words

Every import shipment arrives with paperwork that is supposed to agree: a bill of
lading, a packing list, a commercial invoice. Often they don't.

Take one real-shaped example. The B/L says **6,098.0 kg**. The packing list adds
up to **5,384.0 kg**. That 714-kilo gap is a customs problem. On the same
shipment the invoice bills 740 cartons while the packing list details 720; the
invoice's second line multiplies 6,000 × 7.10 and prints 43,620.00 instead of
42,600.00; and the invoice says FOB Shanghai — freight collect — while the B/L
is stamped FREIGHT PREPAID.

Four discrepancies, four different downstream departments, and today a human
finds them by putting three documents side by side and reading carefully. Miss
one and it surfaces three weeks later as a customs hold or a billing argument.

This is high-volume, high-attention, low-glamour work. It is exactly what agents
should do — and exactly the kind of work where an agent acting *unsupervised*
would be unacceptable.

---

## What we built

Five ADK agents across three departments, coordinated by a routing agent:

| Agent | Desk | What it does |
|---|---|---|
| `cross_check` | Import operations | Cross-checks one shipment's documents; flags every discrepancy with a severity; drafts the correction notice |
| `doc_intake` | Import operations | Sorts a pile of scans into per-shipment sets; reports orphans and gaps |
| `quote_intake` | Procurement | Normalizes incomparable freight quotes into one true all-in comparison |
| `tracking_triage` | Customer service | Triages rollovers, holds and delays into facts, demurrage exposure, options |
| `doc_chaser` | Import operations | Finds missing documents, identifies who owes each, drafts escalating chasers |

Every consequential action stops at a human approval gate. Every decision — run,
held, approved, executed, blocked — lands in an append-only ledger.

---

## The three things the track asked for

### 1. Agents cataloged for cross-department use

The catalog is code-owned (`catalog/registry.py`). Each agent declares its desk,
its **accountable human owner**, its tool surface, its data scope, its autonomy
level, and a per-run cost cap:

```
  Shipment document cross-check
    key        cross_check
    desk       Import operations  (owner: Ops lead)
    autonomy   drafts-for-approval
    data scope shipments/** (read), outbox/** (draft)
    tools      read_file, list_files, glob_files, grep_files, write_file
    cap        $0.50/run
```

An agent not in the catalog is not in the fleet. The card is the allowlist:
`build_fleet()` grants each specialist exactly the tools its card names.

Routing works without the operator naming a desk. "Sort the documents in inbox/"
reaches `doc_intake`; "compare these quotes" reaches `quote_intake` — verified
from the ledger, which records which desk actually ran the tools, not what the
coordinator said it would do.

### 2. Context maintained safely across weeks of async operation

A shipment is genuinely a multi-week object — booked in week 1, sails in week 2,
arrives in week 5 — and discrepancies surface at any point in that window. Two
mechanisms, both real:

**Sessions survive process death.** The CLI runs one turn per process over ADK's
`DatabaseSessionService`. Kill it, start a new process, resume the same session
id, and the fleet still knows what it found:

```
$ python -m freight_fleet.cli chat --session ops-shp002 "Cross-check shp-002-hero"
  session ops-shp002 (new)
  4 discrepancies found …                     [process exits]

$ python -m freight_fleet.cli chat --session ops-shp002 \
    "Without re-reading anything: what did we check and what was worst?"
  session ops-shp002 (resumed, 4 prior events)
  We checked shipments/shp-002-hero (B/L MFSB-26071842). Worst finding (HIGH):
  packing list 720 cartons / 17,520 pcs vs invoice and B/L 740 / 18,000.
```

**Unattended operation stops at the gate.** A scheduled sweep re-checks every
open shipment with nobody watching:

```
  SWEEP sweep-2026-08-21 — 6 open shipment(s)
  shp-001-pristine         0 discrepancies
  shp-002-hero             4 discrepancies
  shp-003-container-refs   3 discrepancies
  shp-004-quote-invoice    3 discrepancies
  shp-005-air-dg           2 discrepancies
  shp-006-missing-doc      1 discrepancies

  5 draft(s) held for approval; nothing sent, nothing written.
```

That last line is the requirement. It ran, nobody was watching, it found real
problems, and it did **not** act.

### 3. Production data without violating compliance or security

**One seam.** Every tool call from every agent passes `before_tool_gate`, which
classifies it and either runs it, holds it for a human, or blocks it. Verdicts
combine tighten-only. A tool absent from the risk table is `BLOCK`, not `AUTO` —
adding a capability means classifying it, or nothing happens.

Agents carry no policy. That is what makes it an architecture rather than a
convention: **adding an agent cannot add a bypass**, because the catalog card
declares tools and the gate classifies them, and neither the agent nor the tool
gets a vote.

The approval flow, end to end, from the real ledger:

```
HELD      write_file  outbox/shp-002-notice.md  consequential action held for operator approval
APPROVED  write_file  outbox/shp-002-notice.md  human-approved replay
EXECUTED  write_file  outbox/shp-002-notice.md  replayed after approval; result status=ok
```

`outbox/` is empty until the middle line. Grants are **single-use** — the gate
retires a grant the moment it lets the replay through, so an approval id that
leaks cannot be replayed forever.

And the fleet **drafts; it never sends**. `send_email` is risk-classified as
CRITICAL and deliberately unwired. The approval CLI refuses to execute it by
design.

---

## Why you should believe any of this: the scoreboard

Most agent demos show one happy path. We can tell you exactly how good this one
is, because **the answer keys were written before the agents existed**.

Six synthetic shipments carry human-authored ground truth: which discrepancies
are seeded, what the printed values are, and which regexes a correct report must
contain. **No model sits in the grading path** — every check is a regex against
that truth.

```
  model: gemini-3.7-flash
  g1_hero_crosscheck       PASS  1.00  all 4 seeded discrepancies reported (N=5)
  g2_clean_control         PASS  1.00  clean control correctly reported DISCREPANCIES FOUND: 0
  g3_container_refs        PASS  1.00  all 3 seeded discrepancies reported (N=3)
  g4_quote_vs_invoice      PASS  1.00  all 3 seeded discrepancies reported (N=3)
  g5_air_dangerous_goods   PASS  1.00  all 2 seeded discrepancies reported (N=2)
  g6_missing_document      PASS  1.00  all 1 seeded discrepancies reported (N=1)
  g9_approval_gate         PASS  1.00  draft held for approval; nothing executed
  7/7 gradable tasks passed
```

The run record is committed in `eval/runs/`.

### The clean control is the number to look at first

One of those six shipments has **nothing wrong with it**. The only passing answer
is "nothing wrong here" — zero findings, no bullets, graded strictly.

This asymmetry is deliberate and it is the most opinionated thing in the repo. A
missed discrepancy costs a correction. **A fabricated discrepancy costs trust**,
and an operator who catches the agent inventing a problem in a clean document set
stops believing all of its output, including the true findings. So discrepant
shipments are graded leniently — find everything seeded, extras tolerated — and
the clean shipment is graded with zero tolerance.

### How it got to 7/7 — and why we're showing you

The first full run scored **4/6**. The commit history shows the path: 4/6 → 5/6 →
6/6 → 7/7. Two failures, two fixes, both in the **agent's procedure**, never the
grader:

- **`g6` (missing document):** the agent listed the absent commercial invoice
  under "checks I could not perform" but not as a finding. The fix is a rule a
  freight operator would recognize: *a required document missing from the set is
  itself a HIGH finding*, because customs entry fails without it.
- **`g4` (HS code):** the agent reported the code mismatch but named neither the
  goods. The fix: *an HS finding names the goods alongside the codes* — a code
  mismatch means nothing to a customs broker without knowing what it
  misclassifies.

We diagnosed both from the per-task ledgers, which showed every available
document had been read — so these were procedure gaps, not context failures.

**The grader and answer keys were never touched.** That rule is written into the
repo's operating instructions, because a scoreboard you can adjust is a
scoreboard that means nothing.

---

## Architectural discipline, checkable in five minutes

```
  operator → coordinator (LlmAgent)
                ├── cross_check · doc_intake · quote_intake
                │   tracking_triage · doc_chaser        (AgentTool)
                │
                │   EVERY tool call — no exceptions, no second path
                ▼
         before_tool_gate    classify(tool) → auto | ask | block
                             tighten-only · unknown fails closed
             auto │  │ ask
                  ▼  ▼
            tool body  held → ApprovalStore → operator grants → replay
                  └────┬────┘
                       ▼
            append-only ledger (JSONL)
```

Three properties, each sealed by a test that fails loudly if broken:

1. **Tighten-only.** No code path turns `ask` into `auto`. Verdicts combine
   through one function.
2. **Unknown fails closed.** An unclassified tool is `BLOCK`. When we added the
   five desks as delegation targets, routing broke *correctly* until each desk
   was classified — the failure mode worked.
3. **One seam.** The approval CLI's replay goes back through the same
   `before_tool_gate` rather than calling the tool directly.

We also verified the framework contract the whole boundary rests on. ADK's
`before_tool_callback` is documented to short-circuit a tool when it returns a
dict — and the gate's safety depends entirely on that being true. So we proved
it, with the tool body's own execution counter as the witness (a body that did
not run cannot have left a record), plus a control run to rule out a vacuous
pass. It is sealed as a test, so an ADK upgrade that broke it goes red in the
suite instead of silently executing held actions.

That check found two sharp edges in ADK's dispatch loop worth knowing: the
short-circuit tests `is None` rather than truthiness, and with a *list* of
callbacks a later `None` overwrites an earlier hold. Neither bites the current
wiring; both are documented where the next person will look.

---

## Honest scope

- The fleet **drafts**; it does not transmit. `send_email` is classified but
  deliberately unwired.
- Documents are **synthetic**. Every trading party is fictional; ports,
  UN/LOCODEs and HS codes are real public facts. Container check digits (ISO
  6346) and AWB check digits are computed, not typed.
- **Sessions genuinely survive process death** and the sweep genuinely runs
  unattended — but the multi-week *timescale is compressed*. The fleet has not
  literally run for three weeks.
- Two of the nine golden tasks (`doc_intake`, `quote_intake`) are **reviewed by
  eye**, not regex-graded. Both were correct on the final run — including the
  quote task catching a seeded trap where both forwarders quoted Rotterdam for a
  Hamburg request — but eye-review is a weaker claim than the answer keys, and we
  label it as such.
- **Provenance:** the freight domain procedures and the synthetic document
  fixtures with their answer keys were authored by our team **before** this
  hackathon and are reused here as content. All agent implementation, governance
  code, catalog, evaluation harness and infrastructure are new work built during
  the submission window. Full detail in `docs/REUSE-LEDGER.md`.

---

## Try it in 60 seconds

```bash
git clone https://github.com/MDadopoulos/freightopsfleet && cd freightopsfleet
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
export GOOGLE_API_KEY=...            # from aistudio.google.com
python scripts/seed_workspace.py --all
export FREIGHT_WORKSPACE_ROOT=$PWD/workspace

python eval/run_eval.py              # the scoreboard, on your own key
python -m freight_fleet.cli sweep    # the unattended run
python -m freight_fleet.cli approvals list   # what it held for you
```

The trust boundary can be verified without credentials at all:

```bash
python scripts/adk_spike.py          # proves the gate short-circuits the tool body
python -m pytest tests/ -q           # 42 tests
```

---

## Links

- **Repo:** https://github.com/MDadopoulos/freightopsfleet
- **Demo video:** [ADD LINK]
- **Live URL:** [ADD IF DEPLOYED — see `docs/DEPLOY.md`]
- **Architecture:** `docs/ARCHITECTURE.md`
- **Track mapping:** `HACKATHON.md`
- **Provenance:** `docs/REUSE-LEDGER.md`

---

## [TO FILL IN BEFORE SUBMITTING]

1. **Team names / roles.**
2. **Demo video link** (script ready in `docs/DEMO-SCRIPT.md`).
3. **Live URL**, if you deploy.
4. **Check the official rules on pre-existing work.** The reused fixtures and
   prompts are the parts at issue. If the rules require everything to be new, the
   honest options are to disclose and accept the ruling, or re-author the
   fixtures fresh — not to misrepresent provenance.
5. **Bonus post.** Judges award points for published content. The strongest post
   is *"why we grade the clean shipment strictly"* — it is a real engineering
   argument about false-positive cost, not marketing, and it is the part of this
   project most likely to be useful to someone else.
