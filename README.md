# Freight Ops Fleet

**A governed fleet of freight back-office agents, built on Google ADK + Gemini.**

They read a shipment's actual documents — waybill, packing list, commercial
invoice — find the discrepancies between them, and draft the correction notice.
Every consequential action stops at a human approval gate. Every decision lands
in an append-only audit ledger.

> Built for the [All Things Agentic Hackathon](https://allthingsagentichackathon.devpost.com/),
> track: **The Fortified Enterprise Fleet**.

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
**7/7 gradable tasks pass** — all seeded discrepancies on five discrepant
shipments, exactly zero on the strict clean control, and the governance task
green mechanically (draft held, nothing executed). The two manual-tier tasks
are reviewed by eye. Try the async story too:

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

---

## Start here

- **Building it?** `BUILD-PLAN.md`, then `AGENTS.md`.
- **Judging it?** `HACKATHON.md` maps the track requirements to the code.
- **Curious about provenance?** `docs/REUSE-LEDGER.md`.
