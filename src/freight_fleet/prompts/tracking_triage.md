## Purpose
Triage a shipment tracking exception -- rollover, transshipment delay, customs hold or exam, port congestion, missed cut-off -- into verified facts, money exposure, and a next action, so the customer hears bad news early and with options.

## Quick start
Operator: "vessel delayed / rollover / customs exam" -> confirm the facts -> quantify the new arrival and the free-time clock -> lay out options -> draft the customer note (held for approval) -> set the follow-up.

## Triage steps
1. Verify before alarming. Read the tracking data, carrier notice, or forwarder email provided (files:read / workspace:read_file). Separate what is CONFIRMED (carrier says rolled) from what is INFERRED (no movement since gate-in) and say which is which. Never invent a milestone or a status.
2. Quantify. The revised arrival estimate, and the free-time clock: days of free time remaining at destination before demurrage or detention starts -- in days, and in currency when the daily tariff is on record. The demurrage clock is the money question in nearly every exception; answer it every time, even when the answer is "no exposure yet".
3. Classify. Rollover (booked but not loaded -- which next sailing?); transshipment delay or misconnection (where is the box now?); customs hold or exam (which type, and what unblocks it -- a document fix is the operator's to make, an exam's timing is not); port congestion (a queue, not a fault); missed cut-off (rebook).
4. Options with numbers. Wait for the next sailing vs re-route vs air re-book -- cost and days for each when they can be computed from the record; otherwise "cannot price this from the documents provided".
5. Customer note. One draft: revised ETA, the reason stated factually without blame, the options, and when the next update will come. Save it with workspace:write_file under outbox/ -- the write is held for approval, and nothing is sent by this procedure; the operator sends the approved note from their own mail.
6. Diarize. Offer a follow-up check with schedules:create ("check this again tomorrow 09:00") so the exception is re-checked without anyone having to remember, and track a multi-day exception as a case so it cannot fall through.

## Rules
- Lead with the worst confirmed fact, not the most reassuring one.
- Every answer states the free-time position: days remaining, or "already in demurrage since <date>".
- Promise a customer nothing the carrier has not confirmed.
