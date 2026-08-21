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

## 1:10–1:40 — The gate (the moment that wins the track)

```bash
python -m freight_fleet.cli chat --session demo \
  "Draft the discrepancy notice and save it to outbox/shp-002-notice.md"
```

It drafts. Then it stops.

> "This is where it holds. Writing that notice is consequential, so it never
> happens unattended — the agent tells me what's waiting and gives me an
> approval id. Nothing is in outbox yet."

Show the empty `outbox/`. Then, on camera:

```bash
python -m freight_fleet.cli approvals list     # the draft, previewed
ls workspace/outbox                            # empty
python -m freight_fleet.cli approvals grant <id>
ls workspace/outbox                            # the notice exists
python -m freight_fleet.cli ledger             # held -> approved -> executed
```

> "That's what the ops lead shows their boss."

## 1:40–2:10 — The fleet and the catalog

Show `catalog()`: five agents, three departments, each with an accountable owner,
a data scope, and an autonomy level.

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
