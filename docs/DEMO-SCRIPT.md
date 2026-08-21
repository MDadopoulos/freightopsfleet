# Demo run-of-show — 3 minutes

The video is the submission. Most entries show an agent doing one thing well.
Yours shows an agent doing one thing well **and proves it**, then shows it
stopping when it shouldn't act alone.

Rehearse it twice. Record the third take.

---

## 0:00–0:20 — The problem, in an operator's words

Show the three documents side by side on screen — waybill, packing list,
commercial invoice for `shp-002-hero`.

> "Every import shipment arrives with paperwork that's supposed to agree. This
> one doesn't. The bill of lading says 6,098 kilos. The packing list adds up to
> 5,384. That's a 714-kilo gap, and somebody reconciles this by hand today."

Don't explain AI. Explain the gap.

## 0:20–1:10 — The hero run

```bash
python -m freight_fleet.cli chat --session demo \
  "Cross-check the documents in shipments/shp-002-hero"
```

Let it run — do not cut away.

Point at the routing when the coordinator hands off to `cross_check`: five
desks, and the request found the right one without being told.

When the report lands, read the verdict line out loud. Four discrepancies, with
severities. Note that it also lists the checks it *couldn't* perform — that
honesty is the feature, not filler.

## 1:10–1:40 — The gate, in the operator console (the moment that wins the track)

Keep the terminal for the sweep and the scoreboard. Switch to the browser for
the *decision* — the contrast between the terminal doing the work and the
console holding the decisions is itself the argument.

```bash
python -m freight_fleet.cli console      # no credentials, no model, zero JS
```

Record at **390×844** so it reads on a phone. The path is deterministic and
offline: no model call, so it cannot fail live on camera.

| t | Screen | What the viewer sees | Line to say |
|---|---|---|---|
| **0:00** | `/` | A huge **5 · DECISIONS WAITING**, beside it **0 IN OUTBOX** and **0 TRANSMITTED**, and the sentence: *the sweep ran 08:18–08:21, read 19 documents across 6 shipments, made 39 gate decisions, wrote nothing and sent nothing.* | "It worked all night and left me five decisions. Nobody was awake for any of it." |
| **0:03** | `/` | One scroll notch. Top card: `▲ THE DRAFT SAYS "CRITICAL"` — undeclared lithium batteries on an air shipment. The other four calm. Below them: *CLEARED — 1 shipment checked, nothing to report.* | "It ranked one first, because the notice itself says critical. And one shipment came back clean — that one's the control." |
| **0:06** | tap the card | `/decision/061a64c3…`. Amber **HELD — THIS HAS NOT RUN**, `write_file · risk HIGH · verdict ASK`. Then *IF YOU APPROVE: write_file will create outbox/…notice.md — 2,569 characters. The file does not exist yet. Nothing is emailed.* Then the notice. Then the three documents it read. | "Here's what it wants to do, what happens if I say yes, and the three documents it read to write it." |
| **0:12** | tap **APPROVE** | Back on the Desk. Green strip: *APPROVED — write_file executed, bytes written, grant retired (single-use), two ledger rows written.* **The numeral is 4. IN OUTBOX is 1.** Nav badge is 4. | "One click. It goes back through the same gate the agent hit, and the grant is retired — that id can never run again." |
| **0:16** | tap **Ledger** | `56 DECISIONS · 47 RAN · 7 HELD · 1 APPROVED · 1 EXECUTED`, then `append-only · sha256 …`. A new `approval-console` band at the top: **HELD → APPROVED → EXECUTED**, seconds apart, joined by one approval id. | "And here's the record — who, what, when, and under whose authority." |

**The single strongest frame is at 0:12: `0 → 1 IN OUTBOX` changing because a
human clicked**, with `5 → 4` beside it and the audit chain one tap away. That is
the whole submission in two numbers.

Reset before the next take:

```bash
git checkout data/approvals.json 2>/dev/null || cp backup/approvals.json data/
rm -f workspace/outbox/*
```

(`audit/`, `data/` and `workspace/` are gitignored — keep a copy of
`audit/ledger.jsonl` and `data/approvals.json` before the first take.)

## 1:40–2:10 — The fleet and the catalog

Show the console's `/fleet` (or `catalog()` in the terminal): five agents, three
departments, each with an accountable owner, a data scope, and an autonomy level.
Point at one chip — `write_file HIGH·HOLDS` in amber — and at the last row of the
tool table: `ANY TOOL NOT IN THIS TABLE → BLOCK`.

> "Import ops, procurement, customer service. Each declares what it may touch and
> what it's allowed to do on its own. An agent that isn't in the catalog isn't in
> the fleet."

Then the async story — both halves are built, show them for real:

```bash
# a NEW process, same session id - it remembers without re-reading:
python -m freight_fleet.cli chat --session demo \
  "Without re-reading anything: what did we find on that shipment?"

# the unattended sweep - every open shipment, nobody watching:
python -m freight_fleet.cli sweep --date demo-day
ls workspace/outbox   # still empty; every notice HELD, none written
```

Read the sweep's closing line out loud: "N drafts held for approval; nothing
sent, nothing written." That sentence is the track requirement.

## 2:10–2:50 — The evidence

Run the gate on camera.

```bash
python eval/run_eval.py
```

> "Six shipments with answer keys we wrote before we built any of this. No model
> in the grading path — every check is a regex against ground truth."

Then land the clean control:

> "This shipment has nothing wrong with it. The only passing answer is 'nothing
> wrong here.' An agent that invents problems in a clean document set is worse
> than one that misses a real one — so we grade that one strictly, and it's the
> number I'd look at first."

## 2:50–3:00 — Close

> "Five agents, one approval gate, one audit trail, and a score we can defend.
> The agents do the paperwork. The operator keeps the controls."

---

## Rules for the recording

- **Never fake a run.** If a task fails, show it failing and say why. A real 5/6
  is more persuasive than a suspicious 6/6 — and judges have seen many of the latter.
- **Don't narrate the architecture.** The diagram is in the writeup. The video
  shows behavior.
- **Show the empty outbox before approving.** That beat is the entire governance
  argument and it takes four seconds.
- Keep the terminal font large enough to read on a phone.
