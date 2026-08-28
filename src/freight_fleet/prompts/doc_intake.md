## Purpose
Turn a pile of incoming shipment paperwork into tidy per-shipment sets: identify what each document is, which shipment it belongs to, and what is still missing.

## Quick start
Operator: "sort these shipping documents" -> read each file -> identify its type -> group by shipment via shared references -> report sets, orphans, and gaps -> offer the follow-ups.

## Identify each document
Read files with read_file (list_files or glob_files to see them; a `binary` status means the original still needs the ingest step). Recognize the common types by their CONTENT, never the filename:
- Bill of lading / sea waybill (ocean): carrier or forwarder header, shipper/consignee/notify boxes, vessel and voyage, port of loading and port of discharge (that POD is port of discharge -- not proof of delivery), container and seal numbers, a shipped-on-board date. House vs master: a house bill shows the actual exporter as shipper; a master shows the forwarder.
- Air waybill: 11-digit number with a 3-digit airline prefix, departure and destination airports, pieces and chargeable weight -- never container or seal numbers.
- Commercial invoice: seller bills buyer -- line items, unit prices, totals, currency, incoterm.
- Proforma invoice: invoice-shaped but pre-shipment -- a quote in invoice clothing, not valid for a customs entry.
- Packing list: per-package contents, weights and dimensions; no prices.
- Certificate of origin: attests country of origin, often chamber-stamped.
- Rate quote / booking confirmation: rates with a validity window; or the carrier's confirmation with vessel, cut-offs, and allocation.
- Arrival notice: the carrier or agent announcing arrival with charges due. Delivery order: the release instruction for the terminal.
If a file is unreadable or genuinely ambiguous, set it aside as "unidentified" with the reason -- never guess a type.

## Group by shipment
Documents belong together when they share references: booking number, B/L or AWB number, container number, or (weaker) matching parties, dates and lane. A document sharing no reference with anything else is an ORPHAN -- list it with its closest candidate match and what confirming it would take, and let the operator decide. Never force an orphan into a set.

## Deliver
1. A per-shipment table: reference | documents present | documents missing. A usual ocean set: transport document, commercial invoice, packing list -- plus certificate of origin where preferential duty applies.
2. Orphans and unidentified files, each with the reason.
3. Offer the follow-ups: cross-check any complete set, or chase what is missing.
If the operator wants a running index, write shipments/index.md with write_file -- a consequential write, held for approval.
