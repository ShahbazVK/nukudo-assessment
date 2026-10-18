# Travel Disruption Assessor

## What this is

Travel Disruption Assessor is a prototype that answers a simple question for frequent
U.S. business travelers: *is this trip at risk, and what should I do about it?* It gathers
real-time signals from three external sources (NWS weather, Aviationstack flight data,
TomTom traffic incidents), fuses them through an LLM reasoning layer, and returns an
assessment with a risk level, the primary risks, the evidence behind each, and recommended
actions. Every claim is traceable to a persisted raw API payload.

Enter a US route (e.g. *"New York to San Francisco"*) and a date; you get **risk level,
what the risks are, the evidence behind them, and recommended actions**.

Start with [DEMO_SCRIPT.md](DEMO_SCRIPT.md) for the five-minute walkthrough (no setup
needed if this machine already has the records) and
[DEMO_ASSESSMENTS.md](DEMO_ASSESSMENTS.md) for three real assessments this build ran and
stored, including one that failed honestly.

## Where AI is used

**One place: the fusion layer.** `backend/fusion.py` makes one model call per assessment.
Its input is the three normalized signal envelopes (NWS, Aviationstack, TomTom) plus the
route and travel date; its output is parsed into `AssessmentResponse` - risk level,
primary risks with evidence citations, recommended actions, an uncertainty list and a
confidence score. The prompt carries normalized signals, never raw vendor payloads, and it
asks for nothing but that JSON object. Everything before and after this call is
deterministic Python.

**What the model does that rules cannot.** It weighs relevance - *does this closure feed
the terminal, or is it a side street 12 km away* - reasons across signals that disagree (a
clear road under a severe weather alert), distinguishes *checked and clear* from *couldn't
check*, and states its own uncertainty. A rules engine would need a decision tree for
every combination of three source statuses, five severity levels and every route context,
and it would still fail on the combinations nobody enumerated - which is exactly the case
a disruption tool exists for.

**What the model is not responsible for.**

- **Fetching data.** The three source modules do that; fusion only receives their
  envelopes.
- **Choosing or ordering sources.** `backend/assess.py` fans out with `asyncio.gather` and
  assembles the result.
- **Per-signal severity and confidence.** Each source derives those from documented
  thresholds: NWS weights `severity`/`urgency`/`certainty` and applies floors from its own
  forecast phrases; TomTom maps `magnitudeOfDelay` (unknown stays ungraded, never
  *minor*); Aviationstack bands delay minutes at 15 / 60 / 180.
- **The confidence label.** `confidence_label` is banded by the pipeline from the model's
  number (< 0.40 low, 0.40-0.69 medium, >= 0.70 high), so it cannot contradict it.
- **Persistence and evidence.** The record is written to
  `logs/assessments/{assessment_id}.json` and served by `GET /evidence/{id}`; fusion does
  neither.
- **Pipeline-owned fields.** `assessment_id`, `status`, `sources`, `generated_at`,
  `missing_credentials` and the rest are derived from the envelopes and the clock. If the
  model returns them they are discarded, not trusted.

**How the output is validated.** The reply must be a JSON object carrying `risk_level`,
`confidence`, `summary`, `risks`, `recommended_actions` and `uncertainty` - a missing
section is an error, not a partially-filled answer - and it is parsed into
`AssessmentResponse` with Pydantic. It is then checked against the input: `risk_level` and
every risk `severity` must be one of `low`/`moderate`/`high`/`severe`/`unknown`;
`confidence` is bounded to 0.0-1.0; `moderate` or above must cite at least one risk and
`unknown` must cite none; every risk must cite at least one `evidence_id`; and every cited
`evidence_id` must exist in the input *and* belong to the source it is attributed to. A
reply that is unparseable, incomplete, contradictory or cites invented evidence is **not
repaired** - no code-fence stripping, no partial recovery, no silent re-roll - it returns
`status: error` with `fusion_error_code: "invalid_response"`. The only retry is for a
transport error, and a retried generation is treated as a different assessment.

**How the reasoning is preserved.** The exact prompt and the exact raw response body go
to `logs/raw/fusion_<timestamp>_<id>.json` **before** parsing, and the same prompt and
answer are copied into the persisted assessment record with token usage and latency.
`GET /evidence/{id}` returns both, and the *AI reasoning* tab of the demo's evidence
drawer renders them verbatim. If that raw record cannot be written, the assessment is an
error state (`raw_logging_failed`): reasoning that cannot be traced is not reported as a
result.

**How this code itself was written.** The implementation was built with an AI
development assistant (Cline) driving a coding model under the repository's
[`.clinerules`](.clinerules) - typed signatures on every function, an explicit failure
path for every outbound call, no mocked data anywhere, raw responses logged before
parsing. That file is committed, so a reader can check the constraints the assistant was
held to. That is a note about the development process, not the AI capability described
above.

## How it's different from a naive LLM wrapper

- **It does not summarize - it weighs.** The prompt says *"Your value is judgement, not
  summarization"* and *"Do not report a risk because its label is high; report it because
  of what it does to this trip."* The model reasons about relevance - does this closure
  feed the terminal, or is it a side street 12 km away - instead of counting severity
  labels. `null` severity means ungraded, never harmless; `road_class: unclassified`
  means the vendor published no class, never "minor".
- **It distinguishes "checked and clear" from "couldn't check."** A source returning `ok`
  with no alerts is evidence *of calm*; a source returning `unavailable` is *missing
  evidence*. The two produce different confidence, and the assessment says which happened.
- **It refuses to invent data.** If a source failed, the failure is reported; if a risk has
  no supporting signal, it is not flagged; if the model cites an evidence id that does not
  exist, the answer is rejected rather than repaired. `tests/test_assess.py::test_a_malformed_envelope_is_reported_not_crashed`
  injects a defective envelope and asserts an error state instead of a proceed-anyway.
- **It preserves the evidence trail.** Every assessment is persisted with its three source
  envelopes, the exact fusion prompt and the raw model answer, so `GET /evidence/{id}`
  reconstructs the whole story - including which signals were withheld from the model.
- **It discloses its limits.** The *"What this assessment cannot tell you"* section in the
  UI lists the per-source signal cap (3 of 129 TomTom incidents, etc.), the flight sample
  sizes, and the time window each source's data is valid for. A capped source also lists
  every raw record it drew from under a *Raw records* subheading - one monospace line per
  file, nothing truncated - so a reader can open the evidence for the signals the model
  never saw.

## Running it yourself

```bash
cp .env.example .env                    # then fill in the keys
pip install -r requirements.txt
uvicorn backend.main:app --port 8000    # terminal 1
streamlit run frontend/app.py           # terminal 2
```

Open <http://127.0.0.1:8501>. Without the API keys the tool fails loudly and specifically
rather than inventing an assessment: `/health` names each unset credential, a source with
no key returns an `unavailable`/`error` envelope naming the missing environment variable,
fusion refuses to run without its own key (`fusion_error_code: "not_ready"`), and any
assessment it cannot support comes back `error`/`partial` with the reason attached.

Full setup (virtualenv, every environment variable, the curl walkthrough) is in
[Setup](#setup) and [Run](#run) below.

## Status: end-to-end, with an evidence trail

This build runs the whole path: `POST /assess` fans out to NWS, Aviationstack and TomTom
**concurrently**, fuses their normalized signals with one model call, and writes a record
to `logs/assessments/{assessment_id}.json` containing the assessment, the three source
envelopes, the exact fusion prompt, the raw model answer and the token usage. That record
is what `GET /evidence/{assessment_id}` serves. Nothing is mocked: if a source or the
model fails, the response says so and names the reason.

| Area | Status |
| --- | --- |
| Project structure, config, rules | done |
| Shared data-source contract (`data_sources/base.py`) | done |
| Airport coordinate table (`data_sources/iata_lookup.py`) | done (58 US airports) |
| NWS weather source (`data_sources/nws.py`) | done (active alerts + gridpoint forecast, partial routes) |
| Aviationstack flight source (`data_sources/aviationstack.py`) | done (delays/cancellations; free-tier gaps reported, not filled) |
| TomTom ground source (`data_sources/tomtom.py`) | done (road incidents around each airport; real-time only, so future dates are `unavailable`) |
| API layer (`/health`, `/assess`, `/assessment/{id}`, `/evidence/{id}`) | done |
| Orchestrator (`backend/assess.py`) | done (concurrent fan-out, failure isolation, persisted record) |
| AI signal fusion + severity reasoning | done (one OpenAI-compatible model call; strict validation, evidence-id checks, disclosed prompt cap) |
| GDELT | not implemented (optional fourth source) |
| Streamlit UI (`frontend/app.py`) | done (risk badge, per-risk evidence lines, source chips, raw evidence drawer) |

## Shared data-source contract

`data_sources/base.py` is the contract every integration is built on. It defines:

* **The envelope** - `NormalizedSignals`, the single shape every `fetch_signals`
  returns: `status` (`ok` / `partial` / `unavailable` / `error`), an explicit `error`
  + machine-readable `error_code`, normalized `signals`, `evidence` entries, the
  `raw_response_path`(s) on disk, `warnings`, latency and retrieval time.
* **The error-state vocabulary** - `ErrorCode` (timeout, rate limit, plan limit,
  malformed payload, logging failure, …). "No data for this query" maps to
  `unavailable`, so a free-tier gap can never be silently read as "no risk".
* **The HTTP wrapper** - `async get_json()` with an explicit timeout, retries only
  on transport failures and 429/5xx (honouring `Retry-After`), secret redaction in
  every logged URL/header, and the raw response written to `logs/raw/` **before**
  parsing. A logging failure is returned as an error state, never hidden.
* **The evidence reader** - `load_raw_response()`, which backs the planned
  `GET /evidence/{assessment_id}` and refuses paths outside `logs/raw/`.

## Logging is load-bearing

Raw-response logging is the evidence layer, not a debugging convenience. Every
claim in an assessment has to be traceable to the exact payload it came from, so
`base.get_json()` writes the full exchange (URL, status, headers, body, timestamp,
latency, body hash) to `logs/raw/` **before** any parsing, and the returned
envelope carries the resulting `raw_response_path`(s).

That makes `LOG_RAW_RESPONSES` a functional switch, not a log-level setting:

* `LOG_RAW_RESPONSES=true` (default) - the tool works.
* `LOG_RAW_RESPONSES=false` - a source **refuses to return data it cannot
  evidence**. `get_json()` reports `logging_disabled`, the source converts that
  into an explicit error envelope, and the assessment shows the gap. The API still
  answers; it answers *"weather signals unavailable: raw logging disabled"* instead
  of an untraceable number.

So disabling logging disables the tool. That is the intended trade-off: an
untraceable result is not a result, and a failure to persist evidence
(`logging_failed`) is surfaced the same way rather than silently dropped.

## Weather source: NWS (`data_sources/nws.py`)

Keyless (`api.weather.gov`) but requires a descriptive `User-Agent` via
`NWS_USER_AGENT`, otherwise the API answers `403`.

For each airport on the route it makes exactly three requests, one at a time:

1. `GET /points/{lat},{lon}` - resolves the NWS grid and returns the URLs to
   follow (plus the grid's timezone).
2. `GET <properties.forecast>` - the **day/night forecast periods**. The travel
   date is matched against the periods that overlap it (normally the preceding
   night, the day itself, and the following night), and every signal names the
   period it represents. NWS advertises `forecastGridData` (raw time-series grids,
   without periods) in the same payload; the periods resource is the one used
   because that is the shape a travel *date* maps onto.
3. `GET /alerts/active?point={lat},{lon}` - active watches, warnings and
   advisories, normally the highest-signal disruption data.

Honest edges:

* **Coordinates** come from the static table in `iata_lookup.py`. An unsupported
  or non-US code returns `unavailable` / `no_data_for_query`; no coordinate is ever
  guessed and no geocoding API is used.
* **Horizon** - NWS publishes about seven days of periods. A date beyond that (or
  in the past) returns `unavailable` / `no_data_for_query`; nothing is
  extrapolated.
* **`/alerts/active` rejects a `limit` parameter** with `400`, so no pagination
  parameter is ever sent.
* **Followed URLs are allow-listed** to `https` on the `NWS_BASE_URL` host before
  the request is made, so a tampered payload cannot redirect the fetch elsewhere.
* **No event-type filtering and no dropped alerts** - a lower-precision signal
  beats a silently discarded one, so every active alert is mapped onto
  `RISK_LEVELS` by a documented, deterministic severity/urgency/certainty rule and
  reported, flagged when its window does not overlap the travel date.
* **"Nothing found" is a result**: no alerts plus benign periods returns `ok` with
  an explicit "no meaningful weather risk identified" signal, so the fusion layer
  can tell "checked, nothing found" apart from "could not check".
* **A half-covered route is reported as half-covered**: if one airport's data cannot
  be obtained, the airport that answered is still reported - as `partial`, with the
  failing airport's `error_code` recorded alongside. Only when *no* airport can be
  read does the source return an error/unavailable envelope. Raw responses for both
  airports still land in `logs/raw/`.

## Flight source: Aviationstack (`data_sources/aviationstack.py`)

`GET /v1/flights` filtered by the route - and, when it is needed, the travel date - is
turned into flight-status signals. The severity bands are fixed and documented in the
module:

| Observation | Level |
| --- | --- |
| `cancelled` | `severe` (the planned trip cannot happen) |
| `diverted`, `incident` | `high` |
| delay ≥ 180 min | `high` |
| delay 60–179 min | `moderate` |
| delay 15–59 min | `low` |
| delay < 15 min | no signal - below the reporting floor |

The delay used is the **largest of the two legs** (a flight that leaves on time and
lands 150 minutes late is 150 minutes late for the traveller), and a vendor-reported
`delay` always wins over one derived from `scheduled` vs `estimated`/`actual`. The
signal's metrics record `delay_minutes`, `delay_source` (`reported` / `actual` /
`estimated`), both legs' published timestamps, terminal and gate. When flights *are*
returned and none is disruptive, the envelope still carries an explicit "no disruption
identified" summary signal, so the fusion layer can tell "they ran on time" apart from
"we could not check". A flight with neither a status nor a usable delay is counted and
reported as unmeasured rather than silently declared fine.

### The free-tier story, told honestly

The free plan is **100 requests/month, real-time flights only** - no historical flights,
no future schedules (verified against the published plan comparison). That shapes the
source:

* **Today** is queried *without* the `flight_date` filter, because that filter belongs
  to the paid schedule features and today's real-time board is the answer anyway. This
  is the one case a free key can genuinely serve.
* **Any other date** sends `flight_date`. If the plan refuses it, the envelope is
  `unavailable` / `plan_limit`, quoting the vendor's own code and message - the same
  state as the free tier's genuine coverage gap, never a failure and never "no risk".
* **An empty result** is `unavailable` / `no_data_for_query` and says exactly why:
  *"the free-tier API does not return schedules for any date other than today's
  real-time board"*.
* **A result whose flights do not match the route or date** is also `no_data_for_query`,
  never an `ok` with no signals: records are not re-dated, re-routed or extrapolated to
  fit the question.

Rate limits (`429`, `usage_limit_reached`) are retried with backoff and `Retry-After` by
`base.get_json`, then surfaced as `rate_limited`. The API key travels as a query
*parameter* and never in the URL string; the raw record on disk shows it redacted.

The fixtures in `tests/fixtures/` are **real captures** taken on 2026-10-17 with a working
free-tier key: the 401 bodies, the `403 function_access_restricted` refusal for a future
`flight_date`, a zero-row route, and a busy JFK→LAX board (100 of 173 flights). Two things
the capture made concrete, both now pinned by tests:

* the board **mixes travel dates** - 91 of those 100 rows were dated *yesterday*, so a
  "today" assessment is built from the 9 rows that actually belong to today;
* `pagination.total` can exceed the 100-row page, and the resulting coverage gap is stated
  in `warnings` rather than silently dropped.

The replay fixture takes its travel date from the capture itself (the newest `flight_date`
in the board) and pins `base.utc_now()` to noon UTC on that date, so the 9-row "today"
subset is a property of the payload, not of when the tests ran: the suite behaves the same
at 8 a.m., at 8 p.m. (when local and UTC dates disagree) and on the following day.

`aviationstack_flights_bands.json` remains **invented** on purpose: it carries one flight
per delay band plus a cancellation and a below-floor delay, which a real capture cannot be
relied on to contain. `tests/test_aviationstack_live.py` runs the real queries (today, a
future date, and an invalid key) against the live API.

## Ground source: TomTom (`data_sources/tomtom.py`)

`GET https://api.tomtom.com/traffic/services/5/incidentDetails` is queried once per
airport and turned into `traffic_incident` signals. Endpoint and version were verified
against the vendor documentation on 2026-10-16: **v5 is current (v4 is deprecated)**, and
v5 has **no `point`/`radius` parameters** - it takes a `bbox` (max 10,000 km²). The
module therefore sends a box that circumscribes a 15 km circle and then filters the
response by great-circle distance, so "within 15 km" is literally true rather than a
box-corner approximation. 15 km covers the airport access roads, the rental-car return
loop and the immediate approach ring without dragging in city-wide congestion.

Severity is mapped from the two vendor fields, with two documented refusals to take the
obvious shortcut:

| Observation | Level |
| --- | --- |
| `magnitudeOfDelay` 4 - *undefined / indefinite delay* | `severe` |
| `magnitudeOfDelay` 3 - major | `high` |
| `iconCategory` 8 - road closed | floor of `high` (applied with `max()`, never a downgrade) |
| `magnitudeOfDelay` 2 - moderate | `moderate` |
| `magnitudeOfDelay` 1 - minor | `low` |
| `magnitudeOfDelay` 0 - **unknown** | no severity: the incident is reported as ungraded |

**0 means "unknown", not "no delay"**, so it is never treated as a clean road: the
incident is still reported, with `severity: null`, reduced confidence and a summary that
says the severity is ungraded. And **4 means "undefined"**, which is what TomTom uses for
closures and indefinite delays - for ground access that is at least as bad as "major", so
it maps to `severe`.

### The date window, and why it exists

An incident feed describes the road network *now*. It cannot tell you what a jam will look
like in three days, so the travel date decides what is returned:

| Travel date | Behaviour |
| --- | --- |
| today | query, `ok` at full confidence |
| tomorrow | query, but every summary is prefixed `current conditions (indicative for tomorrow):`, confidence × 0.6, warning attached |
| 2+ days out, or in the past | **no request at all**: `unavailable` / `no_data_for_query`, naming the limitation |

TomTom does expose *planned* future incidents (`timeValidityFilter=future`), but they are
mostly roadworks rather than a congestion forecast; the module does not treat them as one
and says so in the envelope. Current incidents are never re-dated to look like a
prediction.

An airport with no incidents inside 15 km gets an explicit `traffic_conditions` signal
(`low`, "checked-and-clear") instead of an empty list - checked-and-clear must be
distinguishable from "could not check". One airport failing while the other answers is
`partial`: the surviving incidents are kept and the failed airport's `error_code` is
recorded.

On the free tier: the pricing page read on 2026-10-16 lists the Traffic APIs at **2.5K
requests/month** (older documentation said 2,500/day), so two requests per assessment fits
either way. Live probing also showed that a missing or invalid key returns **HTTP 401**
with `{"detailedError": {"code": "Unauthorized", ...}}`, even though the documentation
lists 403; a 403 is treated as an entitlement gap (`plan_limit`, `unavailable`) because
invalid keys are already caught as 401.

The incident fixtures in `tests/fixtures/` are **real captures** from 2026-10-17: 86
features around JFK (72 inside the 15 km circle, 14 in the bbox corners) and 110 around
LAX. They settled several things that were only predicted before:

* incident details **are** included on this free-tier key - every probe answered `200`, so
  `plan_limit` for this source is still a predicted state, not an observed one;
* every feature was a `LineString`, and `delay`/`endTime` come back as `null` rather than
  `0` on closures and open-ended events;
* `magnitudeOfDelay: 0` ("Unknown") is common - 18 of the 72 JFK signals - and stays
  ungraded with reduced confidence instead of being called "low";
* no airport probed was incident-free (the quietest was FAI with 4), which is why the
  checked-and-clear fixture is the one document here that is still invented.

`tests/test_tomtom_live.py` runs the real route query for today and tomorrow, the invalid
key path, and the API-model projection.

## Signal context: what fusion will reason over

A signal is an *input to a decision*, not a vote in a tally. The JFK capture makes the
point: 72 incidents inside the radius, 40 of them closures, all `severe`. Counting
severities would make every trip look catastrophic, so before the fusion layer exists,
each source's `SignalEntry.metrics` was audited for the context needed to judge
**relevance to this trip**. `SignalEntry` itself did not change - `metrics` is the open
extension point, so all of this is additive and the envelope contract is untouched.

| Source | Carries | Added this turn (from the audit) |
| --- | --- | --- |
| TomTom | `airport_iata` + `scope` (which end of the route), `distance_km`/`radius_km`, `icon_category`/`icon_label`, `magnitude_of_delay`/`magnitude_label`, `severity_source`, `road_numbers`, `length_meters`, `time_validity`, `start_time`/`end_time` | `place`, `from_place`, `to_place`, `road_class`, `road_class_basis`, `on_numbered_route`, `event_descriptions` |
| NWS | `airports` (alert + summary), `area`, `overlaps_travel_date`, `onset`/`ends`, `severity_score`/`severity_reasons`, period + forecast numbers | `airports` on forecast signals too, so every NWS signal names its airport under one key |
| Aviationstack | `route`, `departure_iata`/`arrival_iata`, per-leg `scheduled`/`estimated`/`actual`/`delay_minutes`/`terminal`/`gate`, `delay_source` (which leg fired), `cancelled`/`diverted` flags, `severity_reasons` | `sample_size`, `returned_records`, `total_available`, `page_coverage`, `sample_is_thin` |

Two findings from the audit are worth stating rather than burying:

* **TomTom publishes no road-class field.** The only class-like fact in `incidentDetails`
  is `roadNumbers` (real values in the captures: `I-495 E`, `CA-1`,
  `Grand Central Pkwy E`, present on 15 of 86 JFK features). So `road_class` is
  `numbered_route` when the vendor published a number and `unclassified` otherwise, and
  `unclassified` means **unknown, not minor** - the JFK capture's unclassified set
  includes `JFK Expy S`, the airport's own expressway. `road_class_basis` records which
  fact produced the value. The street names the vendor does publish (`from`/`to`, e.g.
  `Bay Blvd (Nassau Expy)` → `Seagirt Blvd (Nassau Expy)`) were previously only in the
  prose sentence; they are now in `metrics` too, which is what lets fusion tell a closure
  on a trunk route from one on a side street.
* **Aviationstack's free-tier board is a snapshot, not a schedule.** The JFK→LAX capture
  held 100 rows of 173 reported, and only 9 of those rows were dated today. A cohort claim
  ("none of today's flights is disrupted") therefore carries `sample_size`,
  `total_available` and `page_coverage`, and its confidence is discounted by two
  independent factors - partial page coverage and a thin sample - so 9 of 173 scores well
  below 100 of 100. The same fields ride on the per-flight signals, because when a
  disruption *is* found the summary is never emitted and fusion would otherwise see eight
  delays with no idea what cohort they came from. A delayed flight's own confidence is
  left alone: the flight was delayed whether the sample was 9 or 900.

## Partial results

Some queries have more than one part - a route has two airports, a feed may page - and
a source can fail one part while succeeding at another. Discarding the good half
because the other half hiccuped is honest but wasteful, so `base.partial_signals()`
makes the split explicit. A `partial` envelope carries **both** the real signals and,
in `error_codes`, one code per failed part (`error_code` mirrors the first, and the
combined `error` message names every failure); it can only be built when at least one
signal and at least one recorded failure are present, so it is never a default and
never an empty "sort of worked".

Partial is surfaced as partial. It is never rendered, labelled or logged as `ok`, and
every signal in it still cites evidence that resolves to a raw response on disk - a
partial result keeps full traceability. The fusion layer is expected to treat a
partial source as *weaker* evidence than an `ok` source and *stronger* than an
`unavailable` one, and to reduce its confidence accordingly; when no source can be
reached at all, the assessment says so instead of producing a guess.

A `partial` can have two very different causes, so the envelope names which one:
`fusion_error_code` is set by the fusion layer (never by the model) to a stable token -
`not_ready`, `no_signals`, `no_evidence`, `raw_logging_failed`, `unexpected_error`,
`timeout`, `transport_error`, `rate_limited`, `provider_error`, `invalid_response` - and is
`null` on every `ok` assessment and on a `partial` caused only by a degraded *source*.
That is what lets the UI show "wait 60 seconds and retry" for `rate_limited` without
guessing from prose.

## Airport coverage (`data_sources/iata_lookup.py`)

Coordinates come from a static, audited table of **58 US airports** (Alaska, Hawaii
and Puerto Rico included): latitude/longitude from the OurAirports public dataset,
timezones from the NWS grid covering the same point, every row verified against the
live `/points` endpoint. It is deliberately not a geocoder - an unknown code
returns `None` and the caller must report the gap. A production build would import
and refresh a real airport database (that same dataset) behind the unchanged
`lookup_airport()` / `supported_airports()` signatures.

## Data sources (real data, no mocks)

1. **NWS (National Weather Service)** - active alerts + gridpoint forecast (no key;
   implemented, see below).
2. **Aviationstack** - flight status: delays, cancellations, diversions (API key;
   implemented, with the free-tier gap reported rather than filled).
3. **TomTom Traffic** - road incidents within 15 km of each airport (API key;
   implemented, real-time only, so dates beyond tomorrow are `unavailable`).
4. **GDELT** - news/event context (optional, keyless, disabled by default, planned).

AI fusion runs on an **OpenAI-compatible `/chat/completions` endpoint** (the three demo
assessments in [DEMO_ASSESSMENTS.md](DEMO_ASSESSMENTS.md) used `openai/gpt-oss-120b` on
Groq's free tier; DeepSeek's API works through the same path). Its job is to correlate
signals across sources and reason about severity, rather than summarizing each source
separately.

## Layout

```
.
├── .clinerules            # rules Cline must follow in this repo
├── .clineignore           # what Cline should not read (secrets, caches, logs)
├── .env.example           # copy to .env and fill in keys
├── .gitignore
├── README.md
├── DEMO_SCRIPT.md         # five-minute demo narration, with the facts to quote
├── DEMO_ASSESSMENTS.md    # the three pre-run demo assessments, with their ids
├── requirements.txt
├── config.py              # env-driven settings; single source of truth
├── backend/               # FastAPI API layer
│   ├── main.py            # app + /health + /assess + /assessment/{id} + /evidence/{id}
│   ├── assess.py          # orchestrator: fan-out, fuse, persist the record
│   ├── models.py          # request + response contract (evidence, uncertainty)
│   └── fusion.py          # AI fusion layer (prompt, strict validation, evidence log)
├── data_sources/          # one module per source, one public entry point each
│   ├── base.py            # normalized envelope + HTTP/logging helpers (done)
│   ├── iata_lookup.py     # static coordinates for 58 US airports (done)
│   ├── aviationstack.py   # flights: delays/cancellations (done)
│   ├── nws.py             # weather alerts + gridpoint forecast (done)
│   ├── tomtom.py          # road incidents around each airport (done)
│   └── gdelt.py           # news/events, optional (todo)
├── tests/                 # contract + parsing tests (live tests skip when offline)
│   └── fixtures/          # real captured HTTP payloads (provenance in its README)
└── frontend/              # Streamlit UI (renders state only; no vendor calls)
    ├── app.py             # form, demo buttons, assessment panel, evidence drawer
    ├── demo_assessments.py # ids of the three pre-run assessments (no results)
    └── api_client.py      # typed httpx client: assess(), get_assessment(), get_evidence()
```

## Setup

Requires Python 3.9+ (developed against the local 3.9.6 interpreter).

```bash
cd nukudo

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # then add your API keys
```

### Environment variables

See `.env.example`. Required for a full assessment: `AVIATIONSTACK_API_KEY`,
`TOMTOM_API_KEY`, plus a fusion-model credential. `NWS_USER_AGENT` must identify the app and
a contact address, otherwise `api.weather.gov` rejects requests. Keys are read
from the environment only - never hardcode them.

The fusion layer is **not** tied to DeepSeek's API: it calls an OpenAI-compatible
`/chat/completions` endpoint, so a free hosted model works. Three variables describe it:

| Variable | Default | Meaning |
| --- | --- | --- |
| `FUSION_MODEL_PROVIDER` | `openai_compatible` | `openai_compatible` for any OpenAI-compatible endpoint, `deepseek` for DeepSeek's own API. An unrecognised value falls back to the default and is reported, not swallowed. |
| `FUSION_MODEL_NAME` | *(empty)* | The model id. Left empty on purpose: fusion must refuse to run rather than invent a model. |
| `FUSION_MODEL_BASE_URL` / `FUSION_MODEL_API_KEY` | `DEEPSEEK_BASE_URL` / `DEEPSEEK_API_KEY` | Endpoint and key, falling back to the DeepSeek names so an existing `.env` keeps working. |

`config.fusion_readiness()` reports whether fusion can run and every reason it cannot (no
model name, no key, a key that is obviously a placeholder such as
`skip_for_now_or_fill_later`). It never returns the key itself, and `GET /health` exposes
the verdict as `settings.fusion_ready`.

## Run

```bash
# Terminal 1 - backend (from the project root)
uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000

# Terminal 2 - frontend
streamlit run frontend/app.py
```

Check the API at <http://127.0.0.1:8000/health> (reports which credentials are
unset, whether fusion is `ok` or `unconfigured`, and the active prompt cap) and OpenAPI
docs at <http://127.0.0.1:8000/docs>.

```bash
# One assessment, then read the record it wrote
curl -s -X POST http://127.0.0.1:8000/assess \
  -H 'Content-Type: application/json' \
  -d '{"origin":"JFK","destination":"LAX","travel_date":"2026-10-17"}'
curl -s http://127.0.0.1:8000/assessment/<assessment_id>   # the envelope the UI renders
curl -s http://127.0.0.1:8000/evidence/<assessment_id>     # the whole stored record
```

`GET /assessment/{id}` re-validates the stored envelope against the current contract and
never re-runs collection or fusion, so it can replay a real assessment for free. The three
ids the UI offers as demo buttons are listed in [DEMO_ASSESSMENTS.md](DEMO_ASSESSMENTS.md).

HTTP status is about the *request*, not the verdict: an assessment with
`status="error"` or `"partial"` is still `200`, because the envelope is the answer and it
names what is missing. `422` means the route is unsupported, `404` means no such record,
and `500` means the record could not be written (so no untraceable answer is returned).

## Frontend

<http://127.0.0.1:8501> renders these sections and nothing else:

1. **Demo assessments (pre-run)** - collapsed by default, above the form: three buttons
   naming real assessments this backend already ran (`JFK → LAX`, `SFO → JFK`,
   `DEN → MIA`). A click calls `GET /assessment/{id}` and renders the stored envelope; no
   source or model is called. See [DEMO_ASSESSMENTS.md](DEMO_ASSESSMENTS.md).
2. **Input form** - origin/destination IATA codes (validated against the same 58-airport
   table before any request is spent), travel date, one button.
3. **Assessment panel** - a colour-coded risk pill with numeric confidence and label, the
   summary, one card per risk (severity chip, a traceable `Source · Signal · Value` line
   per evidence reference, and a link to `GET /evidence/{id}`), the recommended actions,
   the uncertainty list, and three source-status chips. `partial` and `error` states are
   rendered as such: an error never shows a risk badge.
4. **Evidence drawer** - collapsed by default: one tab per source with the persisted
   envelope, plus the exact fusion prompt, the raw model answer and token usage.

Each submit is one fresh call - no caching, no auto-retry. The only thing cached is the
*bytes of one evidence record*, so a checkbox click does not re-fetch it.

## Design commitments

- **Traceable evidence** - raw API responses are logged to `logs/raw/` before
  parsing, so every claim can be traced back to its source payload.
- **Explicit uncertainty** - the response envelope carries `uncertainty`,
  per-risk `confidence`, and a per-source status list, so missing signals are
  visible rather than hidden.
- **No mocked data** - failures surface as error states naming the source and
  reason; a missing signal is never filled in.

See `.clinerules` for the full set of coding rules that govern this repo.

Lay of the land:

```
* ``data_sources/base.py`` - provider-agnostic shared contract (envelope builders,
  error vocabulary, async ``get_json`` with retries/timeouts/redaction and raw
  evidence logging). Not a source module: see its docstring.
* ``data_sources/iata_lookup.py`` - the static US airport table a source resolves an
  endpoint against (no geocoding, no guessing).
* ``tests/fixtures/`` - real captured HTTP payloads (see its README).
```

## Decisions and tradeoffs

### What I chose and why

* **Three sources, one job each.** NWS is keyless, authoritative for U.S. weather, and the
  only source that can speak to a *future* date; Aviationstack is the only one that reports
  what actually happened to flights; TomTom is the only one that reports conditions on the
  roads into the airport - the part of a trip a flight-status feed never sees. GDELT was
  left out on purpose: a fourth feed of loosely related news would dilute a reasoning layer
  that is better served by three signals it can weigh.
* **Fan-out first, one fusion call second.** Sources are fetched concurrently and normalized
  before any model sees them, so the model never parses a vendor payload and one broken
  source cannot take down the request. A single call keeps cost and latency predictable and
  keeps the entire input auditable; per-source summarization would multiply tokens and hide
  what each claim was based on.
* **A strict contract around the model.** The answer is validated against a JSON schema,
  every risk must cite evidence ids that exist in the input, severities must come from a
  fixed vocabulary, and a `severe` verdict with zero risks is rejected. A rejected answer is
  reported (`invalid_response`), never repaired - a repaired answer has no evidence trail.
* **A prompt about relevance, not severity.** The model receives the audited *metrics*
  (distance, scope, road class, event descriptions, sample size) rather than a severity
  score, and is told that `null` is ungraded and `unclassified` is unknown, and that it may
  not make claims about the signals withheld by the cap.

### What I would change

* **Paid tiers, or provider swaps.** Groq's free tier meters roughly 8,000 tokens per minute
  for `gpt-oss-120b` - one assessment is ~6,000 - Aviationstack's free plan serves today's
  board only, and TomTom meters requests. Those are product limits, not bugs, and upgrading
  changes no interface: sources and fusion sit behind stable seams.
* **An evaluation set.** Nothing here has measured whether the assessments are *right*. The
  fusion prompt has not been evaluated against a labeled disrupted/undisrupted set; in
  production I'd build that evaluation set and tune the prompt against it, with explicit
  thresholds per risk type - precision and recall scored per risk type, and the
  `severe`/`high` boundary tuned against labels instead of intuition.
* **A real airport database.** The 58-airport table is auditable and honest about its
  coverage; production would import and refresh the full OurAirports dataset behind the
  unchanged `lookup_airport()` / `supported_airports()` signatures.
* **A larger signal budget.** Raise `FUSION_MAX_SIGNALS_PER_SOURCE` on a paid tier, or rank
  signals with a cheap classifier first and hand the fusion model the top N.

### What surprised me

* **Free tiers shape the product more than the code does.** Aviationstack returns
  `403 function_access_restricted` with `error_code: "plan_limit"` for any date other than
  today - the SFO → JFK demo record is that refusal, rendered honestly. TomTom's
  `incidentDetails` endpoint, by contrast, *is* included on the free key.
* **The flight feed is a snapshot, not a schedule.** One capture held 100 rows of 173
  reported, and only 9 of those rows were dated today (91 vs 9). Reading those 9 rows as
  "no disruption" would have been the most misleading thing this tool could do, which is why
  cohort confidence carries `sample_size`, `total_available` and `page_coverage`.
* **"Checked and clear" is the rare case.** No airport probed for the traffic source was
  incident-free (the quietest had 4 incidents), so the interesting work is not detecting
  problems but deciding which of 129 incidents matter for one trip.

## Next steps

1. Optional fourth source: GDELT.
2. An evaluation set and threshold tuning for `severe` vs `high` (see above).
3. Larger signal budget via a paid tier or a rank-then-fuse pipeline; caching identical
   route/date queries is the obvious companion step.
4. Full airport database behind the unchanged lookup signatures.
