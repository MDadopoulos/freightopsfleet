# Operating instructions for the coding agent

Read `BUILD-PLAN.md` first. This file is the shorter, harsher one: the rules that
hold while you work.

## Invariants — breaking one is a bug, not a tradeoff

1. **The gate is tighten-only.** No code path may ever turn `ask` into `auto` or
   `block` into anything. Verdicts combine through `stricter()` and only through
   `stricter()`. If a change would let an unattended agent do something a human
   previously approved, it is wrong however convenient.

2. **Unknown tools fail closed.** A tool absent from `TOOL_SPECS` is `BLOCK`, not
   `AUTO`. Adding a tool means classifying it. This is the whole reason the table
   exists.

3. **One gate seam.** Every tool call from every agent passes `before_tool_gate`.
   Do not add a second policy check inside a tool body, and do not let any agent
   call a tool that bypasses the callback. Policy in two places is policy in
   neither.

4. **Answer keys never enter an agent-readable path.** They live in
   `eval/answer_keys/`. Nothing under `fixtures/` or `workspace/` may contain
   one. If you add a fixture, its key goes in `eval/`.

5. **Never tune the grader to the model's output.** If a task fails, fix the
   prompt or the agent. Moving the goalposts produces a green scoreboard that
   means nothing — and the scoreboard is the entire submission strategy.

6. **No LLM judge in the grading path.** Every check is deterministic.

7. **The fleet drafts; it never sends unattended.** `send_email` is CRITICAL
   with an external side effect, so it is held on every path and runs only as
   a human-approved replay. Its body (`tools/mail.py`) never delivers to the
   address the model drafted — only to the operator's configured mailbox and
   the approving human. Do not add a recipient the model can choose.

8. **Fixtures are canonical.** Do not edit a document to make a test pass. The
   answer keys assert that the printed values really appear in the files — change
   a figure and the seals go red, correctly.

## Conventions

- Python 3.11+, type hints on public functions, dataclasses over dicts for
  anything with a shape.
- Docstrings explain **why**, not what. The existing modules set the register —
  match them.
- Prefer editing an existing file over adding one.
- Commit prefixes: `feat|fix|docs|test|chore` + scope, e.g. `feat(gate): ...`.

## What "done" means for a step

Every step in `BUILD-PLAN.md` has a **done test** — a command with observable
output. A step is done when its command produces the stated output, not when the
code looks finished. Do not proceed on a step whose done test you have not run.

## When you are stuck

- **ADK behaves differently than the skeleton assumes** → adapt the caller in
  `agents/fleet.py`. Never weaken `governance/` to fit the framework.
- **The hero task won't score** → check that all three documents actually reach
  the context before touching the prompt. Under-reading looks like bad reasoning.
- **You're behind schedule** → cut step 6(b) (scheduled background runs). Keep
  6(a) (durable sessions) — that one is an explicit track requirement.
- **You want to add a feature** → re-read §1 of `BUILD-PLAN.md`. You have 12 days.
