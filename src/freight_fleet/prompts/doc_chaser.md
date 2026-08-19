## Purpose
Find which shipment documents are missing, who owes them, and draft the chaser emails -- each held for approval -- with deadlines tied to the shipment's real clock, escalating as arrival approaches.

## Quick start
Operator: "chase the missing docs" -> inventory each shipment's documents vs what it needs -> identify the issuer of each missing document -> one chaser per issuer -> save under outbox/ (held for approval) -> diarize the next chase.

## Find the gaps
Inventory the documents (files:list / workspace:list_files; read with files:read / workspace:read_file). The working set for a customs entry: commercial invoice, packing list, and the transport document (B/L or air waybill) -- plus certificate of origin where preferential duty is claimed, and any licence or certificate the goods require. A missing document blocks the entry: it is a blocker, not a nice-to-have.

## Who owes what
- Commercial invoice, packing list: the shipper or supplier.
- B/L or sea waybill: the carrier or forwarder (drafts to verify; originals or a telex release to secure before release at destination).
- Certificate of origin: the shipper via their chamber of commerce -- the slowest document; chase it earliest.
- Arrival notice, delivery order: the carrier's destination agent.

## Deadlines that mean something
Anchor every ask to the shipment's own clock: documents reach the broker with a working margin before arrival (ask the operator for the broker's lead time if it is not on record). A deadline with no consequence attached is a wish -- state the consequence factually: "the entry cannot be filed without it; storage and demurrage accrue from <date>".

## The chaser email
One chaser per issuer, covering every affected shipment (numbered list if several). Subject: references first, then the ask: "[BK4471 / MERU26071234] Commercial invoice needed by Thu 17:00". Body: what is missing, for which shipment, the deadline, the consequence -- factual, brief, no blame, no threats. Escalate across rounds: first ask (polite, full context) -> second (deadline restated, consequence explicit, suggest copying a manager) -> final (consequences now dated and unavoidable -- and flag to the operator that a phone call beats a third email). Save each draft with workspace:write_file under outbox/ (for example outbox/BK4471-chase-invoice.md). One approval covers one draft; an edit restarts the round; nothing is transmitted by this procedure -- the operator sends approved drafts from their own mail.

## Keep it moving
Diarize the next chase with schedules:create rather than trusting anyone's memory; track a multi-document chase as a case with the missing documents as its acceptance criteria; and when a document arrives, confirm it is what it claims to be (right shipment, right type -- a proforma invoice is not a commercial invoice) before closing the gap.
