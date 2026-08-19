# Reuse ledger — provenance of everything in this repo

Written so the hackathon submission can state provenance accurately, and so the
building agent knows what is settled versus what is theirs to write.

## 1. Reused content (authored by the team before this hackathon)

These are **content assets** — prose, data, and domain knowledge — carried over
from the team's own prior work. All original authorship; nothing vendored from a
third party; no third-party license attaches.

| Asset | Size | What it is |
|---|---|---|
| `src/freight_fleet/prompts/cross_check.md` | 7.4 KB | The hero procedure: what to compare across documents, severity rubric, notice-drafting rules |
| `src/freight_fleet/prompts/doc_intake.md` | 2.7 KB | Document-type identification by content, shipment grouping, orphan handling |
| `src/freight_fleet/prompts/quote_intake.md` | 2.3 KB | Quote normalization, true all-in computation, the traps |
| `src/freight_fleet/prompts/tracking_triage.md` | 2.4 KB | Exception classification, demurrage clock, options with numbers |
| `src/freight_fleet/prompts/doc_chaser.md` | 2.9 KB | Who owes what, deadlines with consequences, escalation ladder |
| `src/freight_fleet/prompts/glossary.md` | 8.0 KB | 67 freight terms across five sections, incl. all eleven Incoterms 2020 |
| `fixtures/shipments/**` | 24 files | Six synthetic shipments, ocean and air |
| `fixtures/inbox/**`, `fixtures/quotes/**` | 7 files | Unsorted scans; competing rate quotes |
| `eval/answer_keys/*.json` | 6 files | Ground truth per shipment: seeded findings, printed values, grader regexes |

**Fixture authoring laws** (preserved — read before adding `shp-007`):
container numbers carry correct ISO 6346 check digits and AWB numbers correct
mod-7 check digits, computed rather than typed. Dates run forward and transits
are lane-plausible. Net weight is below gross and per-carton weights sum exactly.
C- and D-terms mean freight prepaid; E- and F-terms mean freight collect. One
booking reference ties every document of a shipment together. Every fixture obeys
all of these **except its own seeded discrepancy** — that is what makes the
discrepancy findable.

## 2. Ported in shape, rewritten as code (new work)

The design of these is carried over; the code is new and much smaller.

| Module | Ported concept | What changed |
|---|---|---|
| `governance/policy.py` | Tighten-only verdict precedence, risk→verdict floor, fail-closed unknowns | Reduced from a multi-axis policy engine to one table and one `stricter()` function |
| `governance/gate.py` | The ChangeSet trust boundary (propose → hold → approve → execute → audit) | Re-expressed as an ADK `before_tool_callback`; ~150 lines instead of a subsystem |
| `governance/ledger.py` | Append-only audit log | JSONL on disk instead of a Postgres table with RLS |
| `catalog/registry.py` | Code-owned agent templates | New `AgentCard` shape adding desk / owner / data scope / autonomy for the track's catalog requirement |
| `eval/grader.py` | Answer-key grader with block scoping and the two asymmetries | Ported closely — the block/bullet scoping logic is subtle and was expensive to get right |
| `tools/workspace.py` | Path-jailed workspace file tools | New implementation; resolve-and-contain jail rather than a sandbox provider |

## 3. Entirely new for this hackathon

- All Google ADK integration (`agents/fleet.py`, runner wiring, session services)
- The catalog surface and its `AgentCard` schema
- `eval/golden_tasks.yaml`, `eval/run_eval.py`, `scripts/seed_workspace.py`
- The restructuring that puts answer keys outside `fixtures/` — in the prior work
  they lived beside the documents and were excluded procedurally; here they are
  excluded **structurally**, which is strictly better
- Everything about async operation, durable sessions, and background runs
- All documentation in this repo

## 4. What to say in the submission

Say it plainly:

> The freight domain procedures and the synthetic document fixtures with their
> answer keys were authored by our team before this hackathon and are reused here
> as content. All agent implementation, governance code, catalog, evaluation
> harness and infrastructure are new work built during the submission window.

**Before submitting, check the official rules on pre-existing work.** Some
hackathons require the entire project be new; others allow pre-existing
components with disclosure. If the rules require everything to be new, the
fixtures and prompts are the parts at issue — and the honest options are to
disclose and accept the ruling, or to re-author the fixtures fresh. Do not
misrepresent provenance to fit a rule.
