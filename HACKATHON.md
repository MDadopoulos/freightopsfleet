# Track mapping — The Fortified Enterprise Fleet

**Hackathon:** [All Things Agentic](https://allthingsagentichackathon.devpost.com/) ·
submissions 2026-08-03 → 2026-08-31 · $180,000 in prizes, cash and Google Cloud credits.

> ⚠️ **Verify the official rules yourself**, particularly the pre-existing-project
> clause and any Antigravity bonus weighting. The summary below was assembled
> from secondary sources; the rules page is the authority.

---

## Why this track

The track brief asks for three things by name. This project was chosen because it
answers all three with real mechanisms rather than claims. The three rows after
those are not in the brief; they are the ones a judge asks about anyway — where
the documents came from, whether they can drive it themselves, and whether the
scoreboard is reproducible or a screenshot.

| Track requirement | What answers it here | Where |
|---|---|---|
| **Agents cataloged for cross-department use** | A code-owned catalog where every agent declares its desk, accountable owner, tool surface, data scope, autonomy level and per-run cost cap. Five agents across three departments — import ops, procurement, customer service. An agent not in the catalog is not in the fleet. | `src/freight_fleet/catalog/registry.py` |
| **Context maintained safely across weeks of async operation** | A shipment is genuinely a multi-week object: booked in week 1, sails in week 2, arrives in week 5, and discrepancies surface at any point. Sessions are durable (`cli chat` over `DatabaseSessionService`): kill the process, start another, and the fleet still knows what it found. The unattended sweep (`cli sweep`) re-checks every open shipment with nobody watching and stops at the gate — drafts held, outbox untouched. | `src/freight_fleet/cli.py` (`chat`, `sweep`) |
| **Interacting with production data without violating compliance or security** | One gate seam classifies every tool call: read-only runs, consequential holds for a human, unknown fails closed. Nothing leaves the building — the fleet drafts, the operator sends. Every decision lands in an append-only ledger the operator can show their boss. | `src/freight_fleet/governance/` |
| **Production-shaped inputs, not a curated text corpus** | The documents arrive as 26 PDFs and scans under `fixtures/raw/` — rendered *from* the canonical text so the two can never disagree, committed and byte-sealed by `--check`. `read_file` refuses a binary rather than guessing at it, and an operator command transcribes the pile into the inbox with a marker line saying a model read it. Realistic intake, without making the graded path stochastic. | `scripts/render_documents.py`, `src/freight_fleet/ingest.py`, `docs/DEPLOY.md` §1a |
| **A surface a judge can interrogate, safely** | ADK's dev UI deployed behind IAP: signed-in access only, and a middleware that overwrites `user_id` in the path, the body and the query string with the verified JWT identity, so per-user sessions are enforced server-side rather than asserted by the browser. Sessions live in Cloud SQL through the same `DatabaseSessionService` the CLI uses — one durable store, three entry points, no new code. | `src/freight_fleet/devui.py`, `docs/DEPLOY.md` §4d |
| **The scoreboard is a build artifact, not a screenshot** | Lint, tests and the fixture seals run on every push with no credentials; the paid eval is a separate workflow reachable only by `workflow_dispatch` and `schedule`, authenticating through Workload Identity Federation with no JSON key anywhere and a service account holding `roles/aiplatform.user` and nothing else. | `.github/workflows/ci.yml`, `.github/workflows/eval.yml`, `docs/DEPLOY.md` §11 |

---

## Judging axes

**Innovation.** The interesting claim is not "agents read documents" — it is that
*the approval boundary and the audit trail are the product*, and the agent is the
engine. Freight operators don't fear AI being wrong; they fear it being
unsupervised. Governance is the feature.

**Architectural discipline.** One seam. Every tool call from every agent passes
through it. Verdicts combine tighten-only through a single function. Adding an
agent cannot add a bypass, because agents carry no policy — the catalog card
declares tools and the gate classifies them. This is checkable in about five
minutes of reading, which is the point.

**Demo / production readiness.** A scored regression gate with human-written
answer keys, including a strictly-graded false-positive control. Committed run
records. This is the axis where most entries have nothing to show, and it is
where this repo is strongest.

**Bonus.** Judges award points for published content. Budget an hour on day 12
for a write-up — the most interesting post is *"why we graded the clean shipment
strictly"*, which is a real engineering argument, not marketing.

---

## The 90-second version, for the writeup

> Freight operators reconcile shipment paperwork by hand: a waybill, a packing
> list and a commercial invoice that are supposed to agree, and often don't. A
>714 kg weight gap is a customs problem; a wrong container check digit is a
> delivery problem; an incoterm that contradicts the freight clause is a billing
> argument three weeks later.
>
> Freight Ops Fleet is five ADK agents across three departments that do this
> reconciliation and draft the correction notice. Everything consequential stops
> at a human approval gate. Everything decided lands in an append-only ledger.
>
> We can tell you exactly how good it is, because we wrote the answer keys first:
> six shipments with known discrepancies, scored with no model in the grading
> path — and one deliberately clean shipment where the only passing answer is
> "nothing wrong here."

---

## Honest scope statement

Include something like this in the submission. Judges reward it, and it is true.

- The fleet **drafts**; it does not transmit. `send_email` is risk-classified but
  deliberately unwired.
- Documents are **synthetic**. Every trading party is fictional; ports, UN/LOCODEs
  and HS codes are real public facts. Check digits are computed, not typed.
- The **domain procedures and document fixtures were authored by the team before
  this hackathon** and are reused here as content; all ADK integration,
  governance code, catalog, grader and evaluation harness are new work built
  during the submission window. See `docs/REUSE-LEDGER.md`.
- Sessions genuinely survive process death (kill-restart-resume is demonstrated
  live), and the sweep genuinely runs unattended — but the **multi-week timescale
  is compressed**: the fleet has not literally run for three weeks.
- The scoreboard's manual tier (`g7`, `g8`) is reviewed by eye, and that review
  found both correct — including `g8` catching the seeded destination-port trap
  (Hamburg requested, both quotes to Rotterdam). Eye-review is still not a regex.
- The deployed **chat surface is sandbox-class for governance**: a hold raised
  there lands in that container's own disposable ledger and vanishes with the
  instance. It demonstrates the gate; it is not the audit record. The governed
  ledger has exactly the writers it has always had — the sweep and the operator
  console — and the submission says which surface is which rather than letting a
  judge assume.
- Document **ingestion is a paid, best-effort step, and the eval does not use
  it**. Transcription is pinned to a seed and low thinking, which is stable in
  practice and not guaranteed; the scoreboard grades the canonical markdown, so
  a stochastic front door cannot quietly move a graded number.
