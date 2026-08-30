# Demo run-of-show — under 3 minutes

The video is the submission. Most entries show an agent doing one thing well.
Yours shows an agent doing one thing well, **stopping** when it shouldn't act
alone, and a human closing the loop — on one URL, in one take.

It is all one browser now. No terminal, no service-switching, no "and here's the
other deployment": open the homepage, sign in, and every beat below is a click
away on the same nav.

Rehearse it twice. Record the third take.

**Before you roll:** sign in once as `judge1`, run the sweep (or wait for the
06:00 one) so the desk has a queue, then sign out. The queue is the first frame;
it must not be empty. Record at a width where the nav does not wrap.

---

## 0:00–0:15 — The homepage

Open **https://freight-ops-fleet-d5eomsog5a-ew.a.run.app/** cold. Let the
headline sit on screen for a beat before you talk over it:

> "Every import shipment arrives with paperwork that's supposed to agree — a
> bill of lading, a packing list, an invoice. Often it doesn't. These are five
> agents that reconcile it. The headline is the whole claim: they cannot act
> alone."

Don't explain AI. Explain the gap, then press **Sign in**.

## 0:15–0:30 — One door, two ways through it

The login page, both panels visible in one frame.

> "One service, one login. Judges can type a username and password, or sign in
> with Google and an invite code — our own OAuth client, so it's one page
> either way. Whichever you pick, you get a name, and that name follows you:
> it's pinned into every conversation and stamped on every decision you make."

Type `judge1` and its password. Land on the desk.

## 0:30–0:50 — The desk: what happened while nobody was watching

The desk's first frame does the work — the big waiting count, then the sentence
under it.

> "It ran at six this morning on a schedule. Nobody was awake. It read the
> documents across every open shipment, made its gate decisions, and wrote
> nothing and sent nothing — it left me a queue of drafted emails instead."

Scroll one notch so the queue cards are on screen. Point at the one ranked
first — the draft that calls itself CRITICAL — and at the cleared strip below.

> "It ranked that one first because the notice itself says critical. And one
> shipment came back clean. That one's the control, and I'll come back to it."

## 0:50–1:20 — One decision, opened

Tap the top card. This is the frame the whole submission rests on; do not rush
it. Read the page top to bottom on camera.

1. The amber **HELD — THIS HAS NOT RUN** strip: `send_email · risk CRITICAL ·
   verdict ASK`, and who held it in which session.
2. **The email** — To, Subject, the body the fleet drafted.
3. **If you approve.** Read this one out loud, because it is the argument:

> "The fleet addressed it to the carrier's ops desk. It will *not* go there —
> the model never chooses a real recipient. It goes to the operator's demo
> mailbox and to me, with the drafted address recorded as intended. This is the
> one action in the fleet that leaves the building and can't be undone, which
> is exactly why it's the one I'm being asked about."

4. **The documents it read** — the waybill, the packing list, the invoice, each
   one a link. Open one for two seconds so it's clear they're real files, not a
   citation string.

## 1:20–1:35 — Approve, and what actually left

Press **APPROVE**. You land back on the desk with the green strip: executed,
grant retired single-use, ledger rows written, the waiting count one lower.

> "One click. It goes back through the same gate the agent hit, and the grant
> is retired — that approval id can never run again."

Click **Sent** in the nav.

> "And here's what actually left: the subject, the address it was drafted for,
> where it really went, and who approved it. Me."

**Optional, +10 s, only if SMTP is switched on (DEPLOY.md §4.5):** switch to a
browser tab already open on the **freightops.demo@gmail.com inbox** and refresh.
The same message is sitting there — subject prefixed `[Freight Ops demo]`, and
the first line of the body naming the address it was drafted for and *not*
delivered to. Say one sentence:

> "It really left — to the operator's mailbox, and only there."

If you signed in with Google, your own inbox has the copy; showing that instead
is fine, but the demo mailbox is the safer shot (nothing personal on screen).

## 1:35–2:05 — Ask the fleet

Click **Ask the fleet**. Click the starter *"Which shipment is missing a
commercial invoice, and draft the chaser"* — don't type on camera.

Let it stream. While it runs, point at three things as they appear:

- the **route** card — the coordinator handing the work to `doc_chaser`, without
  being told which desk;
- the **Trace** under the answer — every tool call in order, with its arguments
  and what came back;
- the amber **HELD** card at the end.

> "It found the gap, drafted the chaser — and stopped. Nothing was written and
> nothing was sent."

## 2:05–2:15 — The loop closes

Click **Desk** in the nav. The hold you just raised in chat is sitting in the
queue with the rest.

> "That's the same approval store and the same ledger the overnight sweep writes
> to. A hold raised in the chat isn't a demo of a gate — it's a decision waiting
> for me, on the same desk."

## 2:15–2:30 — A document it has never seen

Back in the chat, use **Upload a document** in the sidebar on a scanned PDF —
one of `fixtures/raw/`'s scan-like pages is ideal, because it arrives as pixels.

> "Uploaded, and transcribed by Gemini through the same ingest step the operator
> runs — with a marker on line one saying a model read it, so a transcription is
> never mistaken for a hand-written record."

Click **Ask the fleet about it →** and let the first line of the answer land.

## 2:30–2:40 — Run it again, on demand

Back on the desk, press **Run the sweep now**.

> "Same unattended job Cloud Scheduler runs every weekday morning — a judge can
> just start it. A few minutes later its holds appear right here."

(It won't finish inside the video, and it shouldn't: say what it is and move on.
A second press inside ten minutes is refused, by design.)

## 2:40–2:55 — The record, and close

Click **Ledger**. Land on the `approval-console` band: **HELD → APPROVED →
EXECUTED**, seconds apart, joined by one approval id — and the row reading
`… by judge1`.

> "Five agents, one gate, one audit trail, and a score we can defend: seven of
> seven graded tasks, three runs out of three, with no model in the grading
> path. The agents do the paperwork. The operator keeps the controls — and the
> record says which human."

---

## Rules for the recording

- **Never fake a run.** If a turn fails, show it failing and say why. A real
  stumble is more persuasive than a suspicious clean sweep — judges have seen
  many of the latter.
- **Don't narrate the architecture.** The diagram is in the writeup. The video
  shows behavior.
- **The strongest frame is 1:20–1:35** — "it will NOT go to the drafted address"
  on screen, then the Sent page proving it. If you cut anything, don't cut that.
- **Show the queue before you approve, and the count after.** That subtraction
  is the governance argument and it costs four seconds.
- Keep the font large enough to read on a phone; the desk was built for that.
