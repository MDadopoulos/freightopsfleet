# BUILD PLAN — Freight Ops Fleet

**For:** the coding agent that will build this repo.
**Deadline:** 2026-08-31 (hackathon submission). Today is 2026-08-19 — **12 days**.
**Track:** All Things Agentic → *The Fortified Enterprise Fleet*.

Read this file completely before writing code. Then read `AGENTS.md` for the
invariants you must not break, and `docs/REUSE-LEDGER.md` for what is already
done and must not be rewritten.

---

## 0. What you are building, in one paragraph

A fleet of five freight back-office agents — built on Google's Agent Development
Kit with Gemini — that read a shipment's actual documents, find the discrepancies
between them, and draft the correction notice. Every consequential action stops
at a human approval gate, and every decision the fleet makes lands in an
append-only audit ledger. The demo is a real one: six synthetic shipments with
answer keys written by a freight operator, and a scored regression gate that says
out loud how many discrepancies the fleet actually caught.

**The one-sentence pitch:** *the agents do the paperwork; the operator keeps the
controls, and can prove it afterwards.*

---

## 1. Why this wins, and what would make it lose

The judging axes are **innovation**, **architectural discipline**, and
**demo/production readiness**. Play to the second and third — most entries will
be a chatbot with tools and a happy-path video.

**Your three unfair advantages, in the order they matter:**

1. **A scored gate with human-written answer keys.** `eval/answer_keys/` holds
   six shipments' ground truth: exactly which discrepancies are seeded, what the
   printed values are, and which regexes must appear in a correct report. Almost
   nobody else will be able to say "the fleet catches 4 of 4 seeded discrepancies
   on the hero shipment, and correctly reports zero on the clean control."
   **The clean control is the part judges will remember** — it proves the fleet
   doesn't hallucinate problems, which is the failure mode everyone actually
   fears.
2. **A real trust boundary, not a confirmation dialog.** The gate is one seam
   (`before_tool_callback`) that classifies every tool call and holds the
   consequential ones. It is already written and tested. Show the ledger.
3. **Genuine domain content.** ~26 KB of freight-operations procedure in
   `src/freight_fleet/prompts/` — ISO 6346 check digits, incoterm-vs-freight-clause
   coherence, demurrage clocks, house-vs-master bills. This is the part that
   cannot be produced in a weekend, and it is already in the repo.

**What would make it lose:**

- **Building a platform.** You have 12 days. Five agents, one gate, one ledger,
  one eval. If you find yourself writing a settings UI, a permission matrix
  editor, or a multi-tenant data model, stop — that is the wrong repo's instinct.
- **A demo with no evidence.** A video of the agent doing one thing correctly is
  what everyone submits. Yours must show the scoreboard.
- **Faking the async story.** The Fleet track explicitly asks about context
  maintained across *weeks* of asynchronous operation. A `sleep(2)` is not that.
  Do step 6 properly or cut it and say so.

---

## 2. What already exists (do not rebuild)

Everything below is written, runnable, and verified. Read `docs/REUSE-LEDGER.md`
for provenance.

| Path | State | Notes |
|---|---|---|
| `src/freight_fleet/prompts/*.md` | **Done** | 5 agent procedures + glossary, ~26 KB. Ported prose; edit only to fit ADK phrasing. |
| `src/freight_fleet/governance/policy.py` | **Done, tested** | Risk table, `Verdict`, `stricter()`, fail-closed `classify()`. |
| `src/freight_fleet/governance/gate.py` | **Done, tested** | `before_tool_callback` factory, `ApprovalStore`, arg digesting. |
| `src/freight_fleet/governance/ledger.py` | **Done, tested** | Append-only JSONL audit ledger. |
| `src/freight_fleet/catalog/registry.py` | **Done, tested** | Five `AgentCard`s; verified consistent with the policy table. |
| `src/freight_fleet/tools/workspace.py` | **Done** | read/list/glob/grep/write, path-jailed by resolve-and-contain. |
| `fixtures/` | **Done** | 6 shipments (26 documents), `inbox/` (5 scans), `quotes/` (2 quotes). |
| `eval/answer_keys/*.json` | **Done** | Ground truth for all six shipments. |
| `eval/grader.py` | **Done, tested** | Answer-key grader, no LLM judge. Verified on pass/fail/false-positive cases. |
| `eval/golden_tasks.yaml` | **Done** | 9 tasks: 2 hero, 4 core, 2 breadth, 1 governance. |
| `scripts/seed_workspace.py` | **Done, verified** | Seeds the workspace; proven not to leak answer keys. |
| `src/freight_fleet/agents/fleet.py` | **Skeleton** | Structure is right; ADK API calls need verifying. **Your step 1.** |
| `eval/run_eval.py` | **Skeleton** | Everything but `run_task()`. **Your step 4.** Raises today so green can't lie. |

---

## 3. The 12-day plan

Each step has a **done test** — a command whose output proves the step landed.
Do not move on without it.

### Step 1 (Day 1) — ADK spike: make ONE agent read ONE document

Before anything else, verify the three framework assumptions in
`src/freight_fleet/agents/fleet.py`'s docstring. They are the only
framework-dependent things in the repo, and everything hangs off them.

```bash
pip install -e ".[dev]"
adk --version                       # pin this version in pyproject.toml
python scripts/seed_workspace.py    # hero + clean control
export FREIGHT_WORKSPACE_ROOT=$PWD/workspace
```

Build the smallest possible thing: an `LlmAgent` with only `read_file`, and ask
it to read `shipments/shp-002-hero/waybill.md`.

Then verify the load-bearing contract **explicitly**: attach a
`before_tool_callback` that returns `{"status": "test"}` and confirm the tool
body never runs. If returning a dict does *not* short-circuit in your ADK
version, find the mechanism that does (long-running tools, or a tool wrapper)
and adapt `gate.py`'s caller — **do not weaken the gate to fit the framework.**

> **Done test:** the agent quotes a real figure from the waybill, and the
> short-circuit probe proves the tool body was skipped.

### Step 2 (Days 2–3) — The hero agent, end to end

Wire `cross_check` only: its prompt, its five tools, the real gate, the real
ledger. Run golden task `g1_hero_crosscheck` by hand and read the output like an
operator would.

Expect to iterate on the model and the prompt here. Two things to try before
blaming the prompt: use a Gemini model strong enough for multi-document
reasoning (start at `gemini-2.5-pro` for the hero, drop to flash only if it holds
the score), and make sure all three documents are actually reaching the context —
the most common failure is the agent reading one file and inferring the rest.

> **Done test:** the hero report contains a `DISCREPANCIES FOUND: 4` block and
> `grade_discrepant()` returns `passed=True`.

### Step 3 (Day 4) — The clean control

Run `g2_clean_control`. This is the false-positive guard and it is graded
strictly: exactly zero, no bullets.

If it invents findings — the usual failure — the fix is in the prompt's
"Ground rules" section, not the grader. **Never tune the grader to the model's
behavior.** The whole value of the gate is that it was written before you knew
what the model would say.

> **Done test:** `python eval/run_eval.py --tier hero` → 2/2.

### Step 4 (Day 5) — Wire the eval runner

Implement `run_task()` in `eval/run_eval.py` against the ADK `Runner` (the
docstring names the imports). Then run all six answer-key-graded tasks.

Do **not** chase 6/6. Hero-first ordering means g1 and g2 must be green; the
other four are score, not gate. Record whatever you get — an honest 5/6 with a
named failure is better evidence than a suspicious 6/6.

> **Done test:** `python eval/run_eval.py` prints a scoreboard and writes
> `eval/runs/<timestamp>.json`.

### Step 5 (Days 6–7) — The fleet: coordinator + four more desks

Now build the full `build_fleet()`. Add the remaining four specialists as
`AgentTool`s under the coordinator, and check the routing: an ambiguous request
should reach the right desk without the operator naming it.

Add the catalog surface — a `/fleet` endpoint or a CLI command that prints
`catalog()`. **This is a track requirement, not decoration:** the judges asked
how agents are cataloged for cross-department use. Make it visible.

> **Done test:** "sort the documents in inbox/" reaches `doc_intake`; "compare
> these quotes" reaches `quote_intake`; the catalog lists five agents with their
> desks, owners, data scopes, and autonomy levels.

### Step 6 (Days 8–9) — Async + long-horizon context (the track's core ask)

This is what separates a Fleet entry from a Taskmaster entry. Two pieces:

**(a) Sessions that survive.** Replace `InMemorySessionService` with
`VertexAiSessionService` (or Memory Bank). A shipment lives for weeks — the
demo must show an agent picking up a shipment it last touched "three weeks ago"
and knowing what it already found. Seed a session with prior history to make
this real rather than asserted.

**(b) Background operation.** One agent runs unattended on a schedule — a
morning sweep over open shipments that flags anything new and *stops at the
approval gate*. Cloud Run job + Cloud Scheduler is the least-effort honest
version. The demo moment is: it ran at 06:00, nobody was watching, it found a
discrepancy, and it did **not** send anything.

> **Done test:** kill the process, restart it, resume the same session, and the
> agent still knows the shipment's history. The scheduled run leaves a `held`
> row in the ledger with nothing in `outbox/`.

### Step 7 (Day 10) — The approval surface

The gate already holds actions. Now make approving one visible: list pending
approvals, show the exact diff/draft awaiting a decision, approve or reject, and
show the resulting ledger rows.

A CLI is enough (`fleet approvals list` / `fleet approvals grant <id>`). If you
have time for a thin web page, the screen worth building is the **ledger view** —
one table of every decision the fleet made, because that is the artifact an
operator shows their boss and no other entry will have one.

> **Done test:** golden task `g9_approval_gate` — the draft is held, `outbox/` is
> empty until approval, and the ledger shows `held` → `approved` → `executed`.

### Step 8 (Day 11) — Deploy + record

Deploy to Cloud Run or Agent Engine (a live URL helps on "production readiness").
Then record the demo — see `docs/DEMO-SCRIPT.md` for the run-of-show.

### Step 9 (Day 12) — Submit

Writeup, architecture diagram, repo tidy, submission form. Do not build on day 12.

**Buffer honesty:** there is none. If you are behind on day 8, cut step 6(b) —
scheduled background running — and keep 6(a), sessions. Sessions are the track's
explicit ask; the scheduler is the flourish.

---

## 4. Architecture in one diagram

```
  operator
     │
     ▼
  coordinator (LlmAgent, Gemini)
     │  routes by desk — never does the specialist work itself
     ├── cross_check      ← the hero
     ├── doc_intake
     ├── quote_intake
     ├── tracking_triage
     └── doc_chaser
              │
              │  every tool call, no exceptions
              ▼
     ┌────────────────────────┐
     │  before_tool_gate      │  classify → auto | ask | block
     │  (governance/gate.py)  │  tighten-only; unknown tools fail closed
     └────────────────────────┘
          │            │
     auto │            │ ask
          ▼            ▼
    tool runs     held → ApprovalStore → operator grants → replay
          │            │
          └──────┬─────┘
                 ▼
        append-only ledger (JSONL)
```

**The invariant that makes this an architecture and not a demo:** there is
exactly one seam. Every tool call from every agent passes through it. Adding an
agent cannot add a bypass, because agents don't carry policy — the catalog card
declares the tools and the gate classifies them.

---

## 5. Decisions already made (don't relitigate)

| Decision | Why |
|---|---|
| **AgentTool, not `sub_agents` transfer** | The coordinator keeps the thread and can run two desks on one shipment. Transfer hands the conversation away. |
| **Answer keys live in `eval/`, not `fixtures/`** | Structural, not procedural. The seeding script *cannot* leak them because they are not in the tree it copies. |
| **No LLM judge in the grader** | A model grading a model makes the verdict a second opinion. Every check is a regex against human-written truth. |
| **Clean control graded strictly, discrepant leniently** | A miss is a document that goes out wrong. A false positive on a clean set destroys operator trust faster. Asymmetric costs, asymmetric grading. |
| **`send_email` classified but NOT wired** | The fleet drafts; the operator sends. This is a product decision that also happens to remove the scariest demo risk. Keep the classification — it proves the risk model handles the case. |
| **Markdown/CSV fixtures, not PDFs** | They work with zero infrastructure and are reviewable in a diff. If you want the multimodal flourish, render PDFs *in addition* — never replace the canonical text. |

---

## 6. Where the risk actually is

1. **ADK API drift** (Day 1). Highest-probability blocker. Mitigated by making
   the spike step 1 — if the callback contract differs you find out on day 1, not
   day 9.
2. **The model under-reads.** Multi-document reasoning across three files is the
   real task. If the hero score is stuck, check context assembly before the
   prompt: agents that infer document contents instead of reading them will fail
   in a way that looks like a reasoning problem.
3. **Scope creep toward a platform.** Named because it is the likeliest failure
   here. Re-read §1's losing conditions on day 6.
4. **Day-12 crunch.** The writeup and video take longer than anyone plans. Step 8
   is a whole day for a reason.

---

## 7. Submission checklist

- [ ] Public repo, MIT or Apache-2.0 licensed
- [ ] `README.md` with a 60-second quickstart that actually works from clean
- [ ] Demo video (see `docs/DEMO-SCRIPT.md`) — **shows the scoreboard**
- [ ] Architecture diagram (§4 is a starting point)
- [ ] Writeup naming the track and mapping each of its three requirements to a
      concrete thing in the repo (see `HACKATHON.md`)
- [ ] `eval/runs/` contains a real scored run, committed
- [ ] Deployed URL, if step 8 landed
- [ ] Bonus: one social post / blog write-up (judges award points for it)
- [ ] Verify the pre-existing-project rules on the official rules page and
      disclose provenance honestly (see `docs/REUSE-LEDGER.md` §4)
