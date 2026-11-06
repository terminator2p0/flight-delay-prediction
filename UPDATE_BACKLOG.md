# Delay Aware update backlog

Last updated: October 23, 2026.

This is the saved review list requested by the user. Keep it current as decisions are made, and review the deferred updates together once the application is ready. Items described as proposals or research are candidates for discussion, not approved commitments. Priorities and the readiness milestone have not yet been agreed.

The [project brief](PROJECT_BRIEF.md) records the requirements and implementation history. This backlog tracks outstanding work without repeating features already delivered.

## Current expansion: implemented

| ID | Update | Status |
| --- | --- | --- |
| C01 | Retain nonstop flights and add US domestic journeys with exactly one connection (two flight legs). | Confirmed |
| C02 | Include only connecting flights sold together as one ticket; exclude self-transfers and separate-ticket combinations. | Confirmed; verify how the search source identifies eligible offers |
| C03 | Require the first leg's arrival airport to match the second leg's departure airport; exclude airport changes. | Confirmed; resolves Q01 |
| C04 | Show all eligible layover durations returned by the search source, including long and overnight connections, without an extra application minimum or maximum. Display duration and date changes; explain supported concerns about short connections. | Confirmed; resolves Q02. Existing scope and budget still apply; source coverage and minimum-connection-rule verification remain explicit limitations |
| C05 | Show delay history for both flight legs separately, with each leg's dates, sample size, and missing-data state. | Confirmed; history requirement in Q04 resolved. Do not pool rates or invent a missed-connection probability |
| C06 | Offer All, Nonstop only, and One connection only filters; default to All so both itinerary types appear together. | Confirmed; resolves Q03 |
| C07 | Show all eligible alternatives retrieved, including nonstop and one-connection journeys regardless of the main search filter, so the traveler can decide. | Confirmed; resolves Q05. Preserve the budget, date/airport scope, and connection requirements; disclose partial provider coverage |
| C08 | Display both flights' delay histories together in one journey report, with each leg clearly labeled; retain compact journey and connection summaries. | Confirmed; resolves Q04. Source/calculation details may remain expandable |
| C09 | Label connecting candidates "Ticket type not yet checked" and check booking details on selection; exclude identified separate-ticket or over-budget offers, keeping unavailable evidence explicit. | Confirmed; resolves Q06. Provider sold-together evidence must match both exact flights; this is not an issued-ticket guarantee |

C01–C09 are implemented. Validation: 117 offline checks pass; live browser checks verified the filters, on-selection sold-together ticket evidence, both flight histories in one report, all returned alternatives through pagination, and selecting an alternative. The two-leg assistant assessment was also verified.

Existing one-way searches, the 30-day departure window, optional fare cap, and local operation are preserved. Deferred items below remain open for later review.

## Current expansion: requirements discussion

Q01–Q06 are resolved by the confirmed items above. Continue asking one question at a time if implementation reveals a substantive product decision.

## Saved for later review: scope extensions

| ID | Update | Current decision / review condition |
| --- | --- | --- |
| L01 | Journeys with two or more connections. | Deferred; the current expansion allows one connection only |
| L02 | Self-transfers and separate-ticket journeys. | Excluded from the current expansion; revisit only if requested |
| L03 | Round-trip searches. | Explicitly deferred in the original requirements |
| L04 | Continuous live flight monitoring. | Outside current scope; review after the application is ready |
| L05 | Public hosting or access beyond the local Mac. | Outside current scope; review before changing the local-only setup |
| L13 | Connections requiring a transfer between airports. | Excluded from the current expansion; saved for later review |

## Saved for later review: unfinished improvements

These were already documented as pending or desired. Their order relative to the connection expansion has not been agreed.

| ID | Update | What remains |
| --- | --- | --- |
| L06 | Rank alternatives by reliability and price together. | Choose and validate a comparison method; show evidence and tradeoffs and continue enforcing the budget. Current alternatives sort by quoted fare |
| L07 | Weather- and season-informed delay probabilities. | Build and validate an estimator before displaying probabilities; keep measured history, recent observations, and weather evidence distinct |
| L08 | Better coverage for unmatched flights and comparable services. | Investigate remaining identity gaps and service/departure-time comparability without treating uncertain matches as verified history |
| L09 | Automatic historical-data refresh. | Decide whether and when to schedule refreshes; the existing refresh process is manual |
| L10 | Broader flight-search coverage checks. | Evaluate more routes and airlines before claiming comprehensive coverage |
| L11 | Systematic comparison of the two local assistant models. | Compare tool correctness, evidence quality, and response time on representative tasks before choosing permanent roles |
| L12 | Departure-time preferences for alternative flights. | Decide acceptable departure times; not yet specified |

## Saved for later review: proposals and research

These are documented ideas, not promised features. Confirm usefulness and data access before adding them.

| ID | Proposal | What to review |
| --- | --- | --- |
| R01 | More detail on severe delays and the tail of the delay distribution. | Identify what would add value beyond existing signed averages, medians, and timing buckets |
| R02 | Confidence and uncertainty information. | Add defensible measures where appropriate; sample sizes, dates, source timestamps, and missing-data disclosures already exist |
| R03 | Clearer explanations of alternative-flight tradeoffs. | Develop alongside evidence-based ranking; show fare, date, airport, and reliability differences |
| R04 | FAA airport and airspace disruption information. | Validate a usable integration and relevance to the travel time |
| R05 | Search-provider frequent-delay indicators. | Verify and retain the documented optional flag as separately attributed evidence; a missing flag must not imply reliability |
| R06 | Additional supplemental data sources, including AeroDataBox and AirLabs history. | Verify current free access, useful fields, identity matching, and incremental value before selecting a source |
| R07 | Further cancellation and diversion presentation. | Existing history already counts these outcomes and the traveler summary includes cancellations; review whether more detail is useful |

## Maintenance

- Record each new confirmed requirement in the project brief.
- Add deferred ideas here with a stable ID, reason, and status. Keep unresolved current requirements in the decision list.
- Move an item into active work only when it is selected for implementation; record completion and validation when delivered.
- Review this list with the user after the agreed readiness milestone.
