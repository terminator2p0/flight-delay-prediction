# Delay Aware

A local US domestic flight assistant supporting one-way nonstop and one-connection searches within the next 30 days, with a search form and an assistant beside it. Reliability reports use up to 24 months of published flight-level history and show both legs of a connecting journey together.

The local browser application supports real flight search, selectable flight reports, an Ollama assistant with flight-data tools, and alternative searches across nearby dates and airports. Historical reports combine BTS Marketing Carrier and Reporting Carrier records using an operation-level key. Marketing records identify historical operating flights. FlightAware AeroAPI adds exact selected-departure matching, recent measured gate-arrival history, and available current estimates; long-term and recent statistics remain separate. Alternative results currently sort by quoted fare; balanced reliability ranking remains pending. The existing notebooks are research references; their trained models are not used by this application.

## Run locally

From the repository root, use the commands below. Python 3.10 or newer is sufficient. No third-party Python dependencies are required. With the existing `.env` and airport reference file configured, start Ollama, then run:

```sh
python3 -m delay_aware serve
```

Open **http://127.0.0.1:8765** in your browser. The server binds only to this Mac. Stop it with Ctrl+C. Use `--port 8766` if the default port is already occupied.

For a first-time setup, copy `.env.example` to `.env` only if the real file does not already exist, add the key, and initialize the airport data:

```sh
python3 -m delay_aware status
python3 -m delay_aware sync-airports
```

Create a free [SerpApi account](https://serpapi.com/), then edit the local `.env` file and set `SERPAPI_API_KEY`. The real file is ignored by Git. Environment variables take precedence over `.env` settings. `OLLAMA_MODEL` can be `gpt-oss:20b` or `devstral:24b`; both are selectable in the application. The assistant runs locally, while flight and weather services require internet access.

## Browser workflow

1. Enter the departure airport, arrival airport, date, and optional maximum quoted fare. Choose All (the default), Nonstop only, or One connection only. All includes both journey types.
2. Search and select a journey to open its available history and establish the assistant's context. Connecting options start as “Ticket type not yet checked.” Selecting one checks booking details and shows both flight histories, with separate historical and recent evidence for each leg. Identified separate-ticket journeys and updated fares above the budget are excluded; unavailable ticket evidence stays unverified.
3. Ask the assistant to check history, weather, nearby airports, or alternatives. A tool trace identifies the evidence it checked. Switching models selects one of the existing local models, rather than a hosted AI service.
4. Use “Compare nearby dates & airports” for an explicit alternative search. It checks the original date and one day either side, using groups of airports within 50 straight-line miles. It makes at most three discovery requests, fewer when a date is outside the supported window or results are cached. Alternatives include both nonstop and one-connection journeys regardless of the main search filter. All returned matching alternatives are available through “Show more”; selecting an alternative opens its own report and may use one additional ticket-details request. No fare cap is relaxed.

On wider screens the assistant sits beside the results. On narrow screens, “Ask assistant” opens its panel. Reports show observed coverage, historical marketing-to-operating relationships, the number of operations matched across both datasets, and separately identified Marketing Carrier-only operations. Numeric-only fallback matches remain labeled as candidates. No numeric weather-adjusted delay prediction is produced.

The agent can use only five selected-flight tools: combined BTS/AeroAPI analysis, BTS flight history, date-specific weather, nearby airports, and alternative discovery. It cannot execute shell commands, open unrelated files, issue tickets, or make payments. A turn has bounded tool calls and uses a limited recent conversation context. Sessions are kept in memory for two hours and disappear when the server restarts. API keys stay on the server. The browser serves only the three application assets; project files and `.env` are not web-accessible.

## Data commands

Search with a departure date from today through 30 days ahead:

```sh
python3 -m delay_aware search BOS ORD YYYY-MM-DD
python3 -m delay_aware search BOS ORD YYYY-MM-DD --max-price 250
python3 -m delay_aware search BOS ORD YYYY-MM-DD --itinerary-type nonstop
python3 -m delay_aware search BOS ORD YYYY-MM-DD --itinerary-type one_connection
```

The initial search defaults to one adult in economy. Only supported US airports (including US territories) with scheduled service are accepted. Calendar validation uses `APP_TIMEZONE`, initially America/New_York; returned departure and arrival times are each local to their airport.

Search results are filtered to the requested route and first-leg departure date and contain one or two flight legs. Both legs and the connection airport must be in the supported US airport reference. Airport changes, identified self-transfers, separate tickets, and journeys with more than one connection are excluded. All positive provider-reported layover durations are accepted, including short, long, and overnight layovers; date changes are explicit. Minimum connection rules and gate-transfer times are not independently verified. There is no combined itinerary delay rate or validated missed-connection probability.

Each result retains its individual legs, whole-journey fare, total duration, and layover details. Journey identity includes both legs and all scheduled local times, so two journeys sharing a first leg remain distinct. Historical and recent evidence is calculated per leg; weather checks cover the connection airport as well as the endpoints, including both dates of an overnight connection. Search coverage may be incomplete. Prices are observations, not guaranteed final fares or ticket availability; missing prices remain missing. The application never purchases, reserves, or issues tickets.

### Ticket details on selection

A connecting search result alone does not establish single-ticket travel. Selection uses the provider's opaque booking token for one additional SerpApi request. The response must match both exact legs, routes, and scheduled local times. A qualifying sold-together offer must identify both flight numbers, provide a usable USD fare, and have no separate-ticket or split-booking evidence. A missing separate-ticket flag alone, a shared airline, or an ordinary result grouping is insufficient. The report labels this as a provider sold-together offer, subject to seller terms; it is not an issued-ticket guarantee. New fares are checked against the original search budget.

Ticket lookups share the existing 50-attempt hourly and rolling-31-day local SerpApi caps. Successful normalized checks cache for 15 minutes; unavailable checks cache for one minute. Raw booking actions, purchase URLs, credentials, and raw provider responses are not persisted. Unknown ticket structure remains unknown and is not described as protected travel. The command-line search only discovers candidates; the browser performs the on-selection ticket check. Schema-versioned caches separate the new journey results, filters, budgets, and ticket evidence from older nonstop results.

The saved feature review list is [UPDATE_BACKLOG.md](UPDATE_BACKLOG.md), alongside the [project brief](PROJECT_BRIEF.md). Deferred proposals remain available for review after the application is ready.

An optional maximum displayed fare can be supplied in whole US dollars. The cap is sent to the provider and independently enforced on returned results. Over-budget flights and flights without a usable quoted price are excluded when a cap is active. Without a cap, unpriced results may appear with a missing price. Each budget has separate cached results. The cap does not include unquoted baggage fees, other charges, or ground transport costs. When alternative recommendation ranking is added, it must respect the same budget and balance historical reliability against ticket price.

## Nearby airports

Alternative-flight discovery may consider airports within **50 straight-line miles** of the selected departure or arrival airport, on the selected date and one day either side. Dates must remain inside the 30-day search window.

```sh
python3 -m delay_aware nearby-airports BOS
```

The local lookup returns supported US airport candidates, excluding the reference airport and sorted by distance. It does not consume SerpApi requests. Reference data indicating scheduled service does not establish that a flight operates on a particular route/date; flight searches confirm that separately. Distances are not driving distances or travel times. The browser and assistant can search these candidate groups; reliability ranking still requires imported history and verified matches.

## Free data sources

| Source | Purpose | Access |
| --- | --- | --- |
| [SerpApi Google Flights results](https://serpapi.com/google-flights-api) | Future flight search and observed prices | Account and key; selected free plan lists 250 searches/month and 50/hour |
| [BTS Reporting Carrier On-Time Performance](https://transtats.bts.gov/Fields.asp?gnoyr_VQ=FGJ) | Flight-level arrival delay and cancellation history | Public monthly CSV/ZIP downloads; published with a reporting lag |
| [BTS Marketing Carrier On-Time Performance](https://transtats.bts.gov/Fields.asp?gnoyr_VQ=FGK) | Historical marketing-to-operating flight relationships and supplemental outcomes | Public monthly CSV/ZIP downloads from January 2018; published with a reporting lag |
| [FlightAware AeroAPI](https://www.flightaware.com/commercial/aeroapi/) | Exact selected-flight identity, recent operations, and current estimates | Account/key; standard flights lookup only, one page per request; long-term history/Foresight endpoints are not called |
| [Aviation Weather Center](https://aviationweather.gov/data/api/) | Current METAR observations and terminal forecasts | Public endpoints, with provider rate limits |
| [National Weather Service](https://www.weather.gov/documentation/services-web-api) | Date-specific hourly forecasts | Public API; standard forecast horizon is seven days |
| [OurAirports](https://ourairports.com/data/) | Airport names, codes, and coordinates | Public-domain downloads |

Provider terms and quotas were reviewed October 23, 2026. SerpApi retrieves Google Flights results as a third-party service; it is not an official Google API. A live search for BOS–ORD on November 6, 2026 returned 25 nonstop one-way options on October 23; repeating it used the local cache without another provider attempt. This verifies connectivity and one sample route, not comprehensive route coverage. Historical matches for those options are not yet verified.

The application caches normalized search results for 15 minutes and stores the retrieval time. It limits new provider attempts to 250 in a rolling 31-day window and 50 in a rolling hour. Failed attempts count conservatively. A local limit may be reduced through `SERPAPI_SEARCH_LIMIT`, but cannot exceed 250. This protection only covers this local database: other applications using the same key, other installations, or deleting the usage database can change actual account usage. Monitor the provider dashboard as well. Credentials and raw echoed search parameters are not cached.

## FlightAware + BTS analysis

Add `AEROAPI_API_KEY` to the existing local `.env` file and restart the server. Keep the key out of chat and source control. Selecting a flight automatically requests a combined assessment; the assistant can retrieve the same evidence with its combined-analysis tool. The main report is a single traveler summary: status, expected local times, historical and recent timing panels, cancellations, and the five latest measured arrivals. Each timing panel shows a signed average, median, early arrivals, arrivals on time to under 15 minutes late, and 15+ minute delays, with its own dates and sample size. Recent coverage is the returned sample within the last nine days; missing recent evidence is shown explicitly. Source names and matching details are collapsed under “Sources & calculation details”; assistant replies default to a few traveler-focused sentences. Separate underlying periods and denominators are preserved. BTS reports remain usable if FlightAware is unconfigured, unavailable, rate-limited, or has no matching departure.

```sh
python3 -m delay_aware analyze-flight "DL 2351" DFW BOS 2026-10-24T07:05:00
```

Use the actual flight designator, route, and scheduled departure time local to the origin airport. The adapter calls only `GET /flights/{ident}` with `ident_type=designator`, nine days of recent data and approximately two days ahead, and `max_pages=1`. It never follows pagination links or calls long-term history, Foresight, or paid-plan-only endpoints. API responses are filtered independently by an explicit IATA flight/codeshare identity and route. Exact departure verification also requires scheduled **gate** departure time converted using the origin's timezone; runway time is not substituted. Conflicting identity fields, conflicting duplicate records, and ambiguous exact matches remain unverified. The API key is sent in the `x-apikey` header over HTTPS, and authenticated redirects are blocked.

A unique selected-departure match identifies its operating flight. The assessment then uses that operator's explicit BTS marketing-history group, or separately labeled exact operating-flight Reporting Carrier history. A new operator never inherits another operator's rate. Current API identity cannot override conflicts in historical BTS records.

Recent API rates use measured **actual gate arrival minus scheduled gate arrival**, excluding diverted flights and flights flagged cancelled/no longer tracked. Estimates, runway arrivals, and the provider's mixed actual/estimated delay fields do not enter the measured rate. FlightAware's cancellation flag can also mean tracking has stopped, so it is not presented as a confirmed airline cancellation count. Recent observations are grouped by operating flight and labeled as a small returned sample, with pagination limits disclosed. They are not averaged or pooled with BTS history and do not create a calibrated delay forecast. A missing future match is not evidence of cancellation; longer-range searches can still show BTS and available recent same-flight/route observations.

Successful lookups share a 15-minute in-memory cache across the same flight designator. Failures are cached for one minute to avoid repeated provider attempts. No FlightAware payloads are written by the application to the persistent search cache. The separate usage ledger stores request timestamps only. `AEROAPI_REQUEST_LIMIT` defaults to **100 attempts per rolling 31 days** and can be reduced to 1–100; failed attempts count. The app also caps attempts at six per minute. These are local request protections, not a guarantee about account-wide charges: other applications, installations, existing usage, and provider pricing affect the account allowance. Check the FlightAware account dashboard. This integration does not change subscriptions or billing settings.

Live verification on October 23, 2026 matched DL 2351 on DFW–BOS for October 24 at 07:05 local, and mapped marketed DL 5652 on SDF–LGA to operating YX 5652 for October 24 at 08:40 local. Both selected-operator BTS baselines and nine recent measured API arrivals were returned. Repeating each adapter lookup reused its in-memory cache. Verification snapshots are stored under ignored `data/aeroapi_verification.json` and `data/aeroapi_regional_verification.json`. These samples establish connectivity and matching for two flights, not complete provider coverage.

## Weather

```sh
python3 -m delay_aware aviation-weather KBOS
python3 -m delay_aware weather BOS YYYY-MM-DD
```

Date-specific weather returns only forecast periods covering the requested local calendar date. Dates outside the supported horizon return an explicit unavailable result without a forecast request. Current observations and terminal forecasts are timestamped separately and must not be treated as conditions on a later travel date. No weather-adjusted probability is generated.

## Flight history

Both local datasets cover September 2024–August 2026: 14,094,455 Reporting Carrier operations and 15,488,967 Marketing Carrier records. All 24 months were reconciled: 14,094,014 exact matches, 1,394,953 Marketing Carrier-only operations, 441 Reporting Carrier-only operations, and zero conflicting outcomes among matched records. Unmatched records remain separate. The ignored `data/marketing_join_verification.json` file contains the monthly audit. Refresh the corresponding **Marketing Carrier** records with:

```sh
python3 -m delay_aware sync-marketing-history --months 24
python3 -m delay_aware history-status
python3 -m delay_aware audit-history-join 2026-08
```

Marketing sync anchors its window to the latest imported Reporting Carrier month, keeps its own import ledger and progress file, and resumes committed months without downloading again. Imports commit atomically one month at a time; both sync commands share a lock. Allow several additional GB for archives and indexed records. Refresh both sources manually by running `sync-history` followed by `sync-marketing-history`.

The common key is **flight date + operating airline + operating flight number + origin + destination + scheduled local departure time**. Marketing records supply both the marketed and operating identities for each historical operation. The reporting primary key makes each join one-to-one; the same physical operation is never counted twice in a selected flight report. Marketing Carrier-only outcomes are included with explicit provenance. Disagreements in measured arrival delay, cancellation, or diversion flags are excluded, as are ambiguous operating assignments for one marketed departure. BTS records flagged as duplicate/swapped (`Duplicate=Y`) are excluded during marketing import and counted in its ledger.

Reports first match the requested marketing airline, flight number, and route. They show the observed operating flight numbers, carrier changes, observation dates, delay denominator, and join coverage. They include all historical departure times unless the command-line hour filter is supplied. A historical relationship is not proof of the operating airline on the selected future date. AeroAPI can verify that selected departure separately when a unique flight or explicit codeshare, route, and scheduled local departure minute match. If no explicit marketing relationship is found, the earlier numeric-number/route candidate search remains available with a clear unverified label. Candidate fallback records are never mixed into a verified historical marketing report. Airline identifiers are displayed as BTS codes; they are not guessed from a numeric flight number.

To import an individual Marketing Carrier archive:

```sh
python3 -m delay_aware import-history data/bts_marketing/BTS_MONTH.zip --dataset marketing
```

Download and import the latest 24 contiguous published months of **Reporting Carrier On-Time Performance** from the official [BTS archive directory](https://transtats.bts.gov/PREZIP/):

```sh
python3 -m delay_aware sync-history --months 24
```

Leave that terminal running until it finishes. In another terminal, check progress with `python3 -m delay_aware history-status`. The app can remain open during imports; select a flight again to refresh its report. Downloads use four connections and imports commit one month at a time. Allow several GB of disk space. Rerunning the command skips committed months and reuses complete ZIPs. Interrupted months roll back and can be retried. It discovers the latest publication each time it runs; there is no scheduled background refresh yet.

The app shows completed months, actual coverage dates, and separate explanations for an empty database and a flight with no matching records. Partial coverage is never described as a complete 24-month history.

Required fields: `FlightDate`, `Reporting_Airline`, `Flight_Number_Reporting_Airline`, `Origin`, `Dest`, `CRSDepTime`, `ArrDelay`, `Cancelled`, and `Diverted`. Equivalent common uppercase BTS column names are also supported. Put downloads under the ignored `data/` folder.

```sh
python3 -m delay_aware import-history data/BTS_MONTH.zip
python3 -m delay_aware history UA 123 BOS ORD --as-of YYYY-MM-DD
```

Use the selected trip's departure date for `--as-of`. The numeric flight number is separate from the reporting airline code. Add `--departure-hour 8` to restrict observations to flights scheduled from 08:00 through 08:59 local time. Reimporting the same operations does not double-count them. Monthly `Airline_Delay_Cause.csv` summaries are explicitly rejected.

When marketing history is unavailable for a requested identity, reports search the numeric flight number and route, with an optional departure hour. For example, a fallback for `DL 5652` looks up number `5652` without filtering by `DL`. If several reporting airlines share the number and route, their candidate histories remain separate and no combined delay rate is produced. The Python report method also supports `match_carrier=True` for exact reporting-carrier checks that bypass marketing resolution.

Reports use a 24-calendar-month window ending at the latest imported month before the trip date, and expose the actual observation dates and months present. A sparse import is not a complete 24-month history. Marketed flight numbers may differ from operating flight numbers; explicit Marketing Carrier relationships resolve that difference without guessing.

The observed delay rate is the fraction of non-cancelled, non-diverted flights with a known arrival delay that arrived **15 minutes or more late**. Cancelled flights, diverted flights, and completed flights without measured arrival delay are reported separately. The traveler timing comparison preserves signed arrival differences: negative minutes mean early. Average and median arrival timing include early arrivals; the older `mean_arrival_lateness_minutes` field remains available for compatibility and uses zero for early arrivals. Same-calendar-month statistics provide a seasonal comparison; they are not a validated forecast. Missing history produces an unavailable result, never a generated score.

## Verify

```sh
python3 -m unittest discover -s tests -v
```

These offline tests use synthetic fixtures and never contact providers or consume an API quota. They cover search scope, filtering and caching, credential handling, request limits, weather coverage, history matching, deduplication, and delay-rate denominators.
