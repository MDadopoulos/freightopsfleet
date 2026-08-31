# Freight Ops Fleet

**A governed fleet of freight back-office agents, built on Google ADK + Gemini.**

They read a shipment's actual documents — waybill, packing list, commercial
invoice — find the discrepancies between them, and draft the correction notice.
Every consequential action stops at a human approval gate. Every decision lands
in an append-only audit ledger.

> Built for the [All Things Agentic Hackathon](https://allthingsagentichackathon.devpost.com/),
> track: **The Fortified Enterprise Fleet**.

## Live — one door

**<https://freight-ops-fleet-d5eomsog5a-ew.a.run.app/>**

That is the homepage. Sign in at
[`/access`](https://freight-ops-fleet-d5eomsog5a-ew.a.run.app/access) — either
the **demo login** (username and password from the submission) or **Google
sign-in** (the invite code, then your Google account). Either way you get a
name, and that name is pinned into every conversation and stamped on every
decision you make. Everything below sits behind that one login, on one nav.

| Behind the login | What you can do there |
|---|---|
| **Desk** — `/desk` | The approval queue. Open a held email: the draft, the contract for what approving it does, and the documents the agent read to write it — with every figure the draft cites verified byte-for-byte against its source and pre-marked in the viewer. **Approve** or **Reject** — the buttons are live, and the ledger row carries your name. |
| **Ask the fleet** — `/chat` | Ask the agents something, upload a scan, watch the routing and every tool call in the trace. A hold raised here lands on the Desk. |
| **Sent** — `/sent` | What actually left: the subject, the address the fleet drafted for, where it was really delivered, and who approved it. |
| **Ledger · Fleet · Scoreboard** | The append-only record; the catalog, where each agent declares its owner, data scope and tool allow-list — read-only by design; and the graded run (7/7) with the documents behind it. |

The buttons work for everyone signed in — this is the same unrestricted system
the operator uses, not a demo mode of it. What makes that safe is the data:
every shipment, party and figure is fictional, and the one action that leaves
the building cannot reach a real address (see below).

---

## Why this is not another chatbot with tools

**It is graded against ground truth.** Six synthetic shipments carry
human-written answer keys: exactly which discrepancies are seeded into the
documents, what the printed values are, and what a correct report must say. The
regression gate scores the fleet against them with no LLM in the grading path.

The seventh shipment has no discrepancies at all. That one is the point — it is
the false-positive control, and it is graded strictly. An agent that invents
problems in a consistent document set is worse than useless to an operator.

**Its trust boundary is one seam, not a confirmation dialog.** Every tool call
from every agent passes `before_tool_gate`, which classifies it and either runs
it, holds it for a human, or blocks it. Unknown tools fail closed. No layer may
ever loosen a verdict — only tighten it.

**And the gate is what makes unattended work allowed at all.** The workspace is
not scratch space — it is the shipment record, the thing a customs entry and an
invoice dispute are argued from. The morning sweep runs at 06:00 with nobody
watching, which is the whole point and also the reason a human has to stand
between it and the record. So the fleet drafts and stops. The one action that
cannot be taken back — `send_email` — is the one the gate holds on every path,
without exception, and even after a human approves it the message does not go to
the address the model drafted: delivery is to the operator's own demo mailbox
and to the approver. The model can propose a recipient; it can never reach one.

---

## The fleet

| Agent | Desk | What it does |
|---|---|---|
| `cross_check` | Import operations | Cross-checks one shipment's documents; flags every discrepancy with a severity; drafts the correction notice |
| `doc_intake` | Import operations | Sorts a pile of paperwork into per-shipment sets; reports orphans and gaps |
| `quote_intake` | Procurement | Normalizes incomparable freight quotes into one true all-in comparison |
| `tracking_triage` | Customer service | Triages rollovers, holds and delays into facts, demurrage exposure, options |
| `doc_chaser` | Import operations | Finds missing documents, identifies who owes each, drafts escalating chasers |

Each declares its owner, data scope, tool surface and autonomy level in the
catalog (`src/freight_fleet/catalog/registry.py`). An agent not in the catalog is
not in the fleet.

**There is no agent editor in the UI, on purpose.** The Fleet page renders the
catalog; it cannot change it. Which agents exist, what each may touch and what
stops it is a reviewable diff with tests behind it (an unknown tool blocks;
verdicts only tighten), not a settings screen anyone signed in can flip. Adding
a desk is a pull request.

---

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # add your Gemini / Vertex credentials

python scripts/seed_workspace.py --all
export FREIGHT_WORKSPACE_ROOT=$PWD/workspace

adk web                          # or: python -m freight_fleet.cli chat
```

The venv is not politeness. On Debian-based images `pip install -e .` aborts
trying to uninstall the system PyYAML ("RECORD file not found"), and the ADK
never lands.

Try:

> Cross-check the shipment documents in `shipments/shp-002-hero` — waybill vs
> packing list vs commercial invoice. Give me a discrepancy report with
> severities.

There are four discrepancies seeded into that shipment: a 714 kg weight gap, a
20-carton count mismatch, an incoterm that contradicts the freight clause, and an
invoice line that doesn't multiply correctly.

Then ask it to draft the notice — and watch it stop for approval.

## Run the gate

```bash
python eval/run_eval.py --tier hero   # the two that matter
python eval/run_eval.py               # all nine golden tasks
```

Current standing, on `gemini-3.7-flash` (committed run record in `eval/runs/`):
**7/7 gradable tasks pass, three runs out of three** — all seeded discrepancies
on five discrepant shipments, exactly zero on the strict clean control, and the
governance task green mechanically (draft held, nothing executed). Run
`eval/run_eval.py --repeat 3` to reproduce it; a task counts as passed only if
every attempt passed, so the number cannot round up a flaky one. The two
manual-tier tasks are reviewed by eye. Try the async story too:

```bash
python -m freight_fleet.cli sweep               # every open shipment, unattended
python -m freight_fleet.cli approvals list      # the emails it drafted and held for you
python -m freight_fleet.cli approvals reconcile # does the record match the queue?
python -m freight_fleet.cli console             # the operator console at http://localhost:8080
```

`console` needs **no credentials** — it renders five artifacts the fleet already
produced (the ledger, the approval store, the catalog, the committed runs, the
mail spool), never
calls a model, and ships zero JavaScript. It offers exactly two buttons —
**approve**, which replays the held call through the same `before_tool_gate` the
agent hit, and **reject**, which records the decision and writes nothing. Set
`FREIGHT_CONSOLE_READONLY=1` to serve it with both disabled.

## Verify the trust boundary

The gate holds a consequential call by returning a dict from ADK's
`before_tool_callback` and trusting the framework to skip the tool body. That is
an unversioned framework contract, so it is checked rather than assumed — and the
witness is the tool body's own side effect, not the framework's word for it:

```bash
python scripts/adk_spike.py     # no credentials needed
```

Verified on google-adk 2.7.1. `tests/test_adk_contract.py` runs the same probes
under `pytest`, so an ADK upgrade that broke the contract goes red in the test
suite rather than quietly letting held actions execute.

---

## Layout

```
src/freight_fleet/
  prompts/       the freight procedures — the domain IP
  agents/        ADK fleet assembly
  governance/    policy · gate · ledger   ← the trust boundary
  catalog/       agent cards
  tools/         the workspace jail's file tools, and send_email
fixtures/        6 shipments, an inbox, competing quotes — agent-readable
eval/
  answer_keys/   ground truth — NEVER seeded into a workspace
  grader.py      answer-key grader, no LLM judge
  golden_tasks.yaml
```

**`eval/answer_keys/` is deliberately outside `fixtures/`.** The seeding script
cannot leak them because they are not in the tree it copies. An agent that can
read the answer key is reciting, not working, and every run after that is worthless.

---

## Documents

Every party in the fixtures is fictional. Ports, UN/LOCODEs and HS codes are real
public facts; the carrier, forwarder, airline, container prefix and all trading
parties are invented. Nothing is derived from a real customer document.

Container numbers carry correct ISO 6346 check digits and air waybill numbers
carry correct mod-7 check digits — computed, not typed, so a typo is a red test
rather than a rotten fixture.

**A freight desk does not receive markdown.** `fixtures/raw/` is the arrival
surface: 26 originals — 23 PDFs and three scan-like PNGs, the last with seeded
noise, a slight skew and a blur, so at least one document arrives as pixels
rather than as extractable text. They are *rendered from* the canonical markdown
by `scripts/render_documents.py`, so the two can never disagree about a figure,
and they are committed rather than built — `python scripts/render_documents.py --check`
is the seal, and CI runs it on every push. `read_file` refuses all of them with
`{"status": "binary"}`, which is the honest answer for a tool that reads text.

Turning them into something the fleet can read is an operator step, not an agent
decision:

```bash
python -m freight_fleet.cli ingest --dry-run    # the plan; no credentials needed
python -m freight_fleet.cli ingest              # transcribe raw/ into inbox/
```

Each transcription lands in `inbox/` with a
`<!-- transcribed from raw/... by MODEL -->` marker on line one, so a model's
reading of a scan is never mistaken for a hand-written fixture. The eval never
runs it — the scoreboard grades the canonical markdown, which does not move.
`docs/DEPLOY.md` §1a has the cost, the naming rule and the reseed step.

---

## Ask the fleet

The chat surface is one page, `/chat`, in front of ADK's API — a judge can ask
the fleet something rather than only reading what it already did, watch it hand
work to a desk, and see a draft stop at the gate.

**Live:** <https://freight-ops-fleet-d5eomsog5a-ew.a.run.app/chat> — behind the
same login as everything else.

**One login, two ways in.** `/access` puts both panels side by side: type a
username and password from the submission, or type the invite code and continue
with Google. The demo login involves no Google account at all; Google sign-in
runs on our own OAuth client, and the code is what makes it a door rather than a
public spend button — any Google account on earth can sign in, only an invited
one is let through. Neither credential lives in this repo; both are in the
submission form, and both are retired after judging.

**Your name follows you.** Whichever door you came through, the username or the
verified email is pinned into every ADK session server-side and stamped on every
ledger row you decide (“… by judge2”). Conversations are durable and per
identity — sign out, sign back in, and your own history is still there; nobody
else's ever is.

**The sidebar does three things.** It lists your conversations — Cloud SQL
behind the container, not a browser tab holding state. It takes an **upload**:
a PDF, PNG or JPEG up to 6 MB, which goes into the fleet's `inbox/` through the
same `ingest` step the operator runs, transcribed by Gemini with the same
provenance marker every ingested page carries, and then offers to ask the fleet
about it. And it counts **this visit** — turns, tokens in and out, and a dollar
estimate when the operator has set `FREIGHT_PRICE_IN_PER_M` /
`FREIGHT_PRICE_OUT_PER_M`. (The catalog declares a $0.50 cap per run per desk;
the panel is what makes the spend visible.)

**Every turn shows its work.** Folded under each answer is a **Trace** — who
did what, in order: every tool call with a digest of its arguments, what came
back, what was held, what errored — alongside route, HELD and BLOCKED cards as
they happen. And **every answer ends with its evidence**: the documents the
fleet actually read to produce it, each linked to the console's document viewer
— or, when nothing was read, a line saying so, so a recall or a routing reply is
never mistaken for a finding. When a notice cites its sources
(`- "Gross weight: 6,098.0 kg" — waybill.md`), the console verifies each quote
**byte-for-byte against the file** and the viewer opens with the passage
already marked — a mechanical string match, never the model's own claim; a
quote the file does not carry is flagged on the decision, not highlighted.

Ask about a *named* shipment or folder, as the table does. A question that would
need every shipment cross-checked ("which one has the most discrepancies?") is
one the coordinator declines rather than guesses at.

Eight questions worth asking, in the order that makes the argument:

| Ask | Desk it exercises | What to look for |
|---|---|---|
| *Cross-check shipment shp-002-hero and list every discrepancy with the document each figure comes from* | `cross_check` | **Four** discrepancies — a 714 kg weight gap, a 20-carton count mismatch, an incoterm that contradicts the freight clause, and an invoice line that doesn't multiply. Each figure should be attributed to the document it was read from. |
| *Is shp-001-pristine clean?* | `cross_check`, the false-positive control | Nothing. This shipment has no discrepancies, and the only correct answer says so. An invented finding here is worse than a missed one. |
| *Sort the documents in inbox/ into shipment sets and tell me what is missing* | `doc_intake` | Loose paperwork grouped by what it says, not by its filename — plus the gaps named. |
| *Compare the two quotes in quotes/ against the freight invoice for shp-004-quote-invoice* | `quote_intake` | Two incomparable quotes normalised to one all-in figure, and the seeded trap caught: Hamburg was requested, both quotes route to Rotterdam. |
| *Which shipment is missing a commercial invoice, and draft the chaser* | `doc_chaser` | It finds the gap, drafts the chaser — and **stops**. The draft is HELD, nothing is written and nothing is sent. See the caveat below. |
| *Read raw/inbox/scan_001.pdf* | `workspace:read_file` | A refusal: `{"status": "binary"}` plus the hint to run `ingest`. The fleet reads documents, and it will not pretend to have read one it cannot. |
| *Check the container numbers on shp-003-container-refs* | `cross_check` | ISO 6346 check-digit arithmetic, done rather than assumed. |
| *What did you tell me last time about shp-002-hero?* — after signing out and back in | durable sessions | It remembers. That is Cloud SQL behind the container, not a browser tab holding state. |

**One caveat, and it is now the good news.** A hold raised in this chat is not
a demonstration in a throwaway ledger. It goes into the **same approval store
and the same append-only record the Desk reads** — one service, one state. Ask
for the chaser, watch it stop, then click **Desk** in the nav: it is waiting
there, with the email, the contract and the documents it was drafted from, and
your name on the row once you decide. Chat, gate, desk and ledger are one loop.

---

## Start here

- **Building it?** `BUILD-PLAN.md`, then `AGENTS.md`.
- **Judging it?** `HACKATHON.md` maps the track requirements to the code.
- **Curious about provenance?** `docs/REUSE-LEDGER.md`.
