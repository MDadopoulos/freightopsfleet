## Purpose
Cross-check the documents of ONE shipment against each other -- waybill or bill of lading vs packing list vs commercial invoice (plus the rate quote or booking confirmation when provided) -- flag every discrepancy with a severity and a recommended fix, and draft the notice email, always held for the operator's approval.

## Quick start
Operator: "check these shipment docs" -> read each document -> extract the comparison fields -> run the checks -> findings table (severity-ranked) -> skipped-checks list -> draft the discrepancy notice -> save it under outbox/, where approval holds it. Nothing is ever sent by this procedure.

## Ground rules
- Work ONE shipment at a time. If the pile spans several shipments, split it first (tie documents together by shared references: booking number, B/L or AWB number, container number).
- Quote, never characterize. Every finding states the field, each document's exact value, and where it was read -- copied verbatim, numbers and units unchanged.
- A field a document does not show is "not shown -- cannot verify". Never fill a gap by inference, and never let a missing document pass silently: name every check that was skipped and why. A required document absent from the set entirely -- no transport document, no packing list, no commercial invoice -- is itself a HIGH finding, listed in the discrepancy list with the document named and the operational consequence stated (customs entry, payment, release), in addition to the checks its absence forces you to skip.
- House vs master: these checks assume house-level documents (shipper = the actual exporter). On a MASTER bill the parties are the forwarder and their agent, so shipper or consignee "mismatches" against the invoice are expected -- say the document looks like a master bill and compare parties only where it is meaningful.
- Ocean vs air: an air waybill never carries container or seal numbers, and its consignee is always a named party. Skip container and seal checks on air shipments instead of reporting them missing.

## Reading the documents
Read attachments with files:read (files:list shows what is attached) and workspace files with workspace:read_file (workspace:glob and workspace:grep to find them). If a document cannot be read -- a scan with no text layer, an unreadable format -- say exactly that, name the document, and list the checks skipped as a result. Never reconstruct its contents from the other documents.

## The checks
Run EVERY check below, every time, and report every failure -- a check is not satisfied by finding other discrepancies first. Compare each field across every document that shows it:
- Shipper / consignee / notify party: same legal entity everywhere (abbreviations and punctuation may differ). A "TO ORDER" consignee is acceptable on an ocean B/L only when the notify party is the buyer or their broker. A different legal entity is a finding.
- Container number(s): exact match, and the ISO 6346 check digit must verify (4 letters + 6 digits + check digit). A failed check digit means a typo somewhere -- a finding even when the documents agree with each other.
- Seal number(s): exact match. An FCL ocean B/L with no seal number is a finding.
- Package count and kind: exact match at the same packaging level. "X pallets STC Y cartons" is the only legitimate dual count.
- Gross weight: packing-list total vs B/L gross within the house tolerance -- default 3-5% or 50 kg, whichever is larger; the operator's own tolerance on record wins. Net must be less than gross.
- Goods description: consistent and non-contradictory. A description revealing dangerous goods (batteries, chemicals, aerosols) with no DG declaration anywhere is the most serious finding there is.
- HS code: same 6-digit root across documents and plausible for the description. Flag inconsistencies for the customs broker -- never assign or correct a code yourself; the broker owns that call.
- Incoterm coherence: the invoice term must match the quoted or booked term. CFR, CIF, CPT, CIP, DAP, DPU and DDP imply freight PREPAID; EXW, FCA, FAS and FOB imply freight COLLECT. A term contradicting the freight clause is a finding.
- Currency and arithmetic: invoice currency = quote or contract currency. Unit price x quantity = line total; line totals sum to the invoice total. Report the exact arithmetic. Never silently convert currencies.
- Charges vs quote: when a rate quote is provided, invoiced freight and each surcharge line must match it -- zero tolerance on fixed quoted lines, and any charge the quote never listed is a finding.
- References and dates: B/L, AWB and booking references should tie the set together. Dates must run in order -- quote <= booking <= cargo ready <= cut-offs <= departure <= arrival -- with a plausible transit for the lane. An impossible date order is a finding.

## Severity
- CRITICAL -- stop and escalate today: possible undeclared dangerous goods; a seal number differing from the seal recorded at arrival (write "seal discrepancy noted", never "tampering").
- HIGH -- fix before cut-off or arrival: a required document missing from the set, quantity or value mismatch, consignee mismatch, container number typo or failed check digit, a charge the quote never listed.
- MEDIUM -- correct in normal course: weight outside tolerance, incoterm or currency conflict, HS-code inconsistency, impossible date order, invoice arithmetic that does not add up.
- LOW -- note it: formatting or spelling that changes no legal entity, number, or amount.
When in doubt between two severities, take the higher one.

## What to deliver
1. One-line verdict: "N discrepancies found (X critical / Y high)" -- or "No discrepancies found on the checks performed."
2. Findings table: Field | value in each document (named) | Severity | Recommended action. Numbers lead, words follow.
3. Skipped checks: every check not performed, and why (document missing, unreadable, not applicable to the mode).
4. Then the draft notice (below). Table first, draft second, always.
A shipment is "clean" only on the checks actually performed -- say so in those words when anything was skipped.

## The discrepancy notice (always held for approval)
Draft the email asking the responsible party to correct the document:
- Subject: references first -- booking / B/L / container -- then the issue: "[BK4471 / MERU26071234] Gross weight - packing list vs B/L".
- Body: one line of shipment context (lane, vessel or flight, ETA); the factual side-by-side ("Packing list dated 02 Jul shows 18,412 kg; draft B/L shows 19,650 kg"); the ask with a deadline tied to a real clock ("please confirm the corrected figure by Thursday 17:00 -- SI cut-off is Friday"); the attachments named.
- Never state or imply fault, never speculate about the cause, never use claims language ("we hold you responsible" belongs in a formal claims letter, not routine ops mail), never promise payment or waiver.
Save the draft with workspace:write_file under outbox/ (for example outbox/BK4471-discrepancy-notice.md). A consequential write is held for approval -- that pause is the point: the operator reviews the findings and the draft together, then approves or edits. One approval covers one draft; if the findings or the draft change afterwards, the revised draft goes back for approval. This procedure never transmits anything -- an approved draft sits in outbox/ for the operator to send from their own mail.

## When the operator corrects you
A correction is a rule waiting to be written. When the operator overrules a finding ("that consignee difference is fine -- it is their customs broker") or sets a tolerance ("we accept up to 200 kg on bulk lanes"), confirm it back in one line and store it with memory:store so later checks apply it; when it is durable house policy, fold it into this procedure with skill:update. Applied corrections are what make the second check better than the first.
