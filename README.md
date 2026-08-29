# Freight Ops Fleet

**A governed fleet of freight back-office agents, built on Google ADK + Gemini.**

They read a shipment's actual documents — waybill, packing list, commercial
invoice — find the discrepancies between them, and draft the correction notice.
Every consequential action stops at a human approval gate. Every decision lands
in an append-only audit ledger.

> Built for the [All Things Agentic Hackathon](https://allthingsagentichackathon.devpost.com/),
> track: **The Fortified Enterprise Fleet**.

## Live — four doors, one image

| Door | URL | What you can do there |
|---|---|---|
| **Public desk** | <https://freight-ops-fleet-d5eomsog5a-ew.a.run.app/> | Read everything the fleet produced overnight: the held drafts, the evidence it read, the ledger. Press *Approve* and watch the surface refuse with a 403 — that refusal is the point. |
| **Sandbox** | <https://freight-ops-sandbox-819664522984.europe-west1.run.app/> | The same console on a disposable copy of the record, with the buttons live. Approve a draft, see the file appear and the ledger row land. |
| **Ask the fleet** | <https://freight-ops-chat-819664522984.europe-west1.run.app/chat> | Chat with the fleet. Google sign-in, then the access code from the submission. |
| **Ask the fleet — demo login** | <https://freight-ops-chat-demo-819664522984.europe-west1.run.app/chat> | The same chat, no Google account: username and password from the submission. |

Every shipment, party and figure on these pages is fictional. The operator's own
console — where decisions are real — is a fifth deployment behind IAM and is not
linked; `docs/DEPLOY.md` explains why the public one structurally cannot decide.

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
python -m freight_fleet.cli approvals list      # what the sweep held for you
python -m freight_fleet.cli approvals reconcile # does the record match the queue?
python -m freight_fleet.cli console             # the operator console at http://localhost:8080
```

`console` needs **no credentials** — it renders four artifacts the fleet already
produced (the ledger, the approval store, the catalog, the committed runs), never
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
  tools/         path-jailed workspace file tools
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
the fleet something rather than only reading what it already did, watch it
hand work to a desk, and see a draft stop at the gate. ADK's own developer UI
stays reachable at `/dev-ui/` as a trace view.

**Live URL:** `https://freight-ops-chat-819664522984.europe-west1.run.app/chat`

**You will be asked to sign in with a Google account.** That is not
bureaucracy: it is the only surface where a visitor's click spends model
tokens, and the signed identity is what scopes the conversation. Sessions are
durable and **per Google account** — sign out, sign back in, and your own
history is still there; nobody else's ever is. (`docs/DEPLOY.md` §4d explains
how the server pins `user_id` to the verified JWT, and why the UI's "Edit user
ID" control is cosmetic.)

**Then it asks for an access code** — the one in the submission form. Google
sign-in says who you are; the code says you were invited. It is the only thing
standing between any Google account on earth and the fleet's model budget, so it
is rotated after judging and never appears in this repo.

**Would rather not sign in with a Google account?** The same chat runs a second
time as a **demo login** — username and password from the submission, no Google
involved. Each username is its own identity with its own durable history, and the
credentials are retired after judging.

**Demo login URL:** `https://freight-ops-chat-demo-819664522984.europe-west1.run.app/chat`

Either way, ask about a *named* shipment or folder, as the table does. A question
that would need every shipment cross-checked ("which one has the most
discrepancies?") is one the coordinator declines rather than guesses at.

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

**One caveat, and it matters.** A hold raised in this chat lands in **that
container's own disposable ledger** — you will see it in the reply, and it is
**not** the ops console's governed record. It is a demonstration of the gate,
not an entry in the audit trail. To click **approve** on a real held action, use
the public sandbox console (`docs/DEPLOY.md` §4c), where the buttons work
against a durable store that is disposable on purpose.

---

## Start here

- **Building it?** `BUILD-PLAN.md`, then `AGENTS.md`.
- **Judging it?** `HACKATHON.md` maps the track requirements to the code.
- **Curious about provenance?** `docs/REUSE-LEDGER.md`.
