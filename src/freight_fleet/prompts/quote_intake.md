## Purpose
Normalize incomparable freight quotes into one honest comparison an operator can decide from -- true all-in cost, validity, transit, free time, and the hidden lines that flip the answer.

## Quick start
Operator: "compare these freight quotes" -> read each quote -> extract to one grid -> compute the true all-in for the actual shipment -> rank -> name the traps found.

## Extract per quote
Read each quote with files:read or workspace:read_file. Pull: forwarder or carrier; validity window (a quote with no validity date is itself a finding); lane (origin and destination, port or door); mode and equipment (FCL size, LCL, air); base rate and its basis (per container, per W/M revenue ton for LCL, per kg chargeable for air); every surcharge line (terminal handling at each end, bunker or fuel, documentation, security, filing fees, peak season); free time included at destination; transit time and routing (direct or transshipment); subject-to clauses (rate increases, space and equipment availability).

## Make them comparable
- Compute a true all-in for the operator's actual shipment. "All-in" often excludes destination charges -- check both ends before believing the label.
- LCL is charged on max(1000 kg, 1 CBM) per revenue ton; air on chargeable weight = max(gross, volumetric at cm3/6000). State the basis used.
- Keep each quote in its own currency; convert only on the comparison line, state the rate used, and never restate a price in another currency as if it were quoted so.
- A missing line is "not quoted -- confirm before booking", never zero. A quote omitting a line the others include deserves suspicion, not the win.
- Free time at destination is money: fewer included days = demurrage exposure sooner. Rank it alongside price.

## Deliver
1. A comparison grid, one column per quote: validity, transit and routing, base, each surcharge, free time, true all-in.
2. A recommendation with the reason in numbers ("cheapest by $310 after destination charges; two days slower; expires Friday").
3. The traps found: expired or missing validity, excluded destination charges, per-W/M vs per-container confusion, subject-to clauses that can move the price.
When the operator wants the grid kept, save it with workspace:write_file (for example quotes/BK4471-comparison.md) -- held for approval like any write.
