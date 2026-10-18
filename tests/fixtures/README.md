# Test fixtures

Two kinds of file live here, and the difference is deliberate and stated:

1. **Real captures** - the payload was received from the live API and stored verbatim.
   Every `nws_*` file is one of these. So are all the `aviationstack_*` files except
   `aviationstack_flights_bands`, both real TomTom incident captures, the
   `tomtom_401_unauthorized` body and the live `aviationstack_403_plan_restricted` and
   `aviationstack_flights_empty` answers.
2. **Invented unit fixtures** - written by hand, marked **[INVENTED]** below, and kept only
   for shapes a real capture cannot be relied on to contain on any given day (one record
   per severity band, and an incident feed with nothing in it). They use obviously
   synthetic values (`Example Airways`, `XX100`, `TT-EXCL-*`) so nobody can mistake them
   for real data.

## NWS (`api.weather.gov`)

Real HTTP payloads captured from `api.weather.gov` with the project's own
`NWS_USER_AGENT` on **2026-10-16/17 UTC**, used verbatim by the parsing tests in
`tests/test_nws.py`. Nothing here is hand-written or reshaped: the parsing tests
exercise the same document shapes the live tests do, and `tests/test_nws_live.py`
re-checks the same contracts against the live API.

| File | Endpoint | Notes |
| --- | --- | --- |
| `nws_points_JFK.json` | `GET /points/40.6413,-73.7781` | whole document, unmodified |
| `nws_forecast_JFK.json` | `GET /gridpoints/OKX/42,40/forecast` (the URL that `points` returned) | whole document; 14 day/night periods, 2026-10-16 20:00 → 2026-10-23 18:00 local |
| `nws_points_SFO.json` | `GET /points/37.619806,-122.374821` | whole document, unmodified (grid `MTR/85,98`, `America/Los_Angeles`) |
| `nws_forecast_SFO.json` | `GET /gridpoints/MTR/85,98/forecast` | whole document; 14 periods, 2026-10-16 14:00 → 2026-10-23 06:00 local |
| `nws_alerts_empty.json` | `GET /alerts/active?point=40.6413,-73.7781` | a real "no active alerts" FeatureCollection (empty `features`) |
| `nws_alerts_SFO.json` | `GET /alerts/active?point=37.619806,-122.374821` | the same empty shape, for the second airport, with its own point in the title |
| `nws_alerts_tx_sample.json` | `GET /alerts/active?area=TX` | six real alert features - one per distinct (severity, certainty, urgency) combination in that response. Only the `features` array was **trimmed**, from 32 entries to those six; everything else is untouched |
| `nws_alerts_bad_request.json` | `GET /alerts/active?limit=50` | the real `400` error document NWS returns for an unsupported `limit` parameter, used to prove a non-FeatureCollection body is reported as a payload-shape error instead of being parsed |

Details the point fixture records: `gridId OKX`, `gridX 42`, `gridY 40`,
`timeZone America/New_York`, `relativeLocation New York, NY`. The forecast document
reports `units: us` and `generatedAt 2026-10-17T00:46:45+00:00`.

**Refreshing a fixture:** re-issue the same request and replace the file. Most
assertions are contract properties (evidence ids resolve, severities are in
`RISK_LEVELS`, the selected periods are the ones the document's own timestamps
imply) and survive a new capture. Two things are deliberately tied to *this*
capture and must be updated with it:

* the pinned period names for `2026-10-17` in
  `tests/test_nws.py::test_travel_date_selects_the_overlapping_day_and_night_periods`,
* the "beyond the published window" date used by
  `tests/test_nws.py::test_date_beyond_the_forecast_horizon_is_unavailable` and by
  the live horizon test, which must stay outside the new document's range.

## Aviationstack (`api.aviationstack.com/v1`)

A free-tier key was available for the captures below on **2026-10-17**, so every document
that needs a key is a real answer from the live API.

| File | Provenance | Notes |
| --- | --- | --- |
| `aviationstack_401_missing_key.json` | **real capture**, `2026-10-17T04:24:16Z` | `GET /v1/flights` with no `access_key`; HTTP `401`. Body stored verbatim |
| `aviationstack_401_invalid_key.json` | **real capture**, `2026-10-17T04:24:16Z` | `GET /v1/flights?access_key=<invalid sentinel>`; HTTP `401`. Body stored verbatim |
| `aviationstack_403_plan_restricted.json` | **real capture**, `2026-10-17T05:09:22Z` | `GET /v1/flights?dep_iata=JFK&arr_iata=LAX&limit=100&flight_date=2026-10-25` → HTTP `403`, `{"error":{"code":"function_access_restricted","message":"Your current subscription plan does not support this API function."}}`. Stored verbatim: the live message carries **no** `[Technical Support: support@apilayer.com]` suffix, which the published example shows |
| `aviationstack_flights_empty.json` | **real capture**, `2026-10-17T05:09:23Z` | `GET /v1/flights?dep_iata=JFK&arr_iata=MHT&limit=100` (today, no `flight_date`) → HTTP `200`, `{"pagination":{"limit":100,"offset":0,"count":0,"total":0},"data":[]}`. A genuinely flightless route, not a fabricated empty page |
| `aviationstack_flights_JFK_LAX.json` | **real capture**, `2026-10-17T05:09:56Z` | `GET /v1/flights?dep_iata=JFK&arr_iata=LAX&limit=100` (today, no `flight_date`) → HTTP `200`, `pagination.total` **173**, 100 rows. 91 rows are dated `2026-10-16` and 9 are dated `2026-10-17` - see below |
| `aviationstack_flights_bands.json` | **[INVENTED]** | four flight records covering the documented field names: an `active` flight with a reported 45-minute delay, a `cancelled` flight with no timestamps, a `landed` flight 12 minutes late (below the reporting floor), and a `scheduled` flight whose 95-minute delay is only derivable from `scheduled` vs `estimated`. Carriers, flight numbers, airports and aircraft are **invented placeholders**. Kept because a real capture cannot be relied on to contain a cancellation, a below-floor delay *and* a derive-only delay on any given day; the band table is pinned separately by `test_delay_bands` |

### What the live free tier actually does (measured, not assumed)

* **`flight_date` is a paid feature.** A future date answers `403
  function_access_restricted`; today's board without the parameter answers `200`. That is
  exactly the `plan_limit` path the module predicts, and
  `test_live_future_date_reports_the_free_tier_gap_honestly` now exercises it for real.
* **The real-time board mixes travel dates.** `dep_iata=JFK&arr_iata=LAX` returned 100 rows
  dated *yesterday* 91 times and *today* 9 times: in-progress and recently-landed flights
  are still on the board. The module matches on `flight_date`, so a "today" assessment is
  built from the 9 rows that belong to today -
  `test_the_real_board_mixes_travel_dates_and_only_the_travel_date_is_used` pins this
  against the capture.
* **Pages are capped at 100 rows and `total` reports the full count** (173 here), so the
  module emits its pagination-gap warning instead of implying it saw the whole route.
* **A quiet route answers `200` with `count: 0`** - an empty page is real data, not a plan
  gap, and the module reports it as `unavailable` with `no_data_for_query`.

### Refreshing a fixture

```bash
# 1. a route with traffic (today: the free tier is real-time only)
curl -sS "https://api.aviationstack.com/v1/flights?access_key=$AVIATIONSTACK_API_KEY&dep_iata=JFK&arr_iata=LAX&limit=100" \
  > tests/fixtures/aviationstack_flights_JFK_LAX.json

# 2. any future date, which is where the free-tier plan restriction shows up
curl -sS "https://api.aviationstack.com/v1/flights?access_key=$AVIATIONSTACK_API_KEY&dep_iata=JFK&arr_iata=LAX&flight_date=2027-01-15" \
  > tests/fixtures/aviationstack_403_plan_restricted.json

# 3. a route with no flights at all
curl -sS "https://api.aviationstack.com/v1/flights?access_key=$AVIATIONSTACK_API_KEY&dep_iata=JFK&arr_iata=MHT&limit=100" \
  > tests/fixtures/aviationstack_flights_empty.json
```

Better still, take the body from the evidence record the module itself wrote for the call
(`logs/raw/aviationstack_*.json`, field `response.body_text`): that is exactly what the
parser saw. Two assertions name *this* capture's shape and must move with a new one -
`REAL_CAPTURE_DATE` plus the date histogram and the 9-evidence/8-signal counts in
`tests/test_aviationstack.py`.

## TomTom (`api.tomtom.com/traffic/services/5`)

| File | Provenance | Notes |
| --- | --- | --- |
| `tomtom_401_unauthorized.json` | **real capture**, `2026-10-16T23:52Z` | `GET /traffic/services/5/incidentDetails` with **no key** *and* with an invalid key; both answered HTTP **401** with this body, although the documentation lists 403. Stored verbatim |
| `tomtom_incidents_JFK.json` | **real capture**, `2026-10-17T05:09:56Z` | `GET .../5/incidentDetails?bbox=-73.957268,40.504414,-73.601366,40.774480&timeValidityFilter=present&language=en-GB` plus the module's `fields` → HTTP `200`, **86** features: 72 inside the 15 km circle, 14 in the bbox corners, 0 unplaceable. 40 closures (`iconCategory` 8, `magnitudeOfDelay` 4), 18 with magnitude 0 ("Unknown"), `delay: null` on 58 and `endTime: null` on 42 |
| `tomtom_incidents_LAX.json` | **real capture**, `2026-10-17T05:09:56Z` | the same call for `bbox=-118.570767,33.807468,-118.245227,34.077534` → HTTP `200`, **110** features, 75 inside 15 km. Captured specifically to test the "checked and clear" path - and LAX was **not** clear |
| `tomtom_incidents_bands.json` | **[INVENTED]** | six v5 features: one per `magnitudeOfDelay` band (1–4), a road closure (`iconCategory` 8), an incident with magnitude 0 (documented as "unknown"), one inside the bbox but outside the 15 km radius, and one with empty geometry. Incident ids `TT-EXCL-*` and every description are invented. Kept because no single real capture contains every band at once |
| `tomtom_incidents_empty.json` | **[INVENTED]** | the empty result shape, `{"incidents": []}`, used for the checked-and-clear path. **No real zero-incident answer could be captured**: fourteen airport queries on 2026-10-17 all returned incidents inside their bbox - FAI 4, OGG 5, MSY 19, ANC 20, SJU 27, PVD 37, ABQ 38, TUS 41, HNL 48, CLE 66, SAT 82, JFK 86, LAX 110, MKE 169. The `incidents` container itself is confirmed by every real capture above |
| `tomtom_403_not_entitled.json` | **[INVENTED]** | the documented `403` body ("The supplied API Key is not valid for this request"). It **could not be provoked**: this key answered `200` on every airport probed, so incident details *are* included on the free tier, and an old `/4/incidentDetails` path answers `404 {"type":"about:blank","title":"Not found"}`, not a 403. Retained so the 403→`plan_limit` mapping stays pinned if the plan ever changes |

### What the live API actually does (measured, not assumed)

* **Incident Details is included on this free-tier key** (2.5K requests/month is the current
  listing). No entitlement error occurred, so `plan_limit` remains a *predicted* state for
  this source rather than an observed one.
* **Every feature is a `LineString`** - none of the 196 captured features was a `Point` and
  none lacked coordinates.
* **`delay` and `endTime` are `null`, not `0`**, on closures and open-ended events;
  `numberOfReports` and `lastReportTime` are often `null` too. The parser reads these as
  absent.
* **`magnitudeOfDelay: 0` is common** (18 of the 72 JFK signals inside the radius, 29 of the
  146 live JFK→LAX signals at capture time) - the "Unknown" magnitude the module refuses to
  grade as `low`.
* **The feed is dominated by road closures**: 40 of the 72 JFK signals are `iconCategory` 8
  at magnitude 4, which the closure floor lifts to `severe`.

### Refreshing a fixture

```bash
# incidents within 15 km of JFK (bbox = 15 km around the table's JFK coordinates)
curl -sS -G 'https://api.tomtom.com/traffic/services/5/incidentDetails' \
  --data-urlencode "key=$TOMTOM_API_KEY" \
  --data-urlencode 'bbox=-73.957268,40.504414,-73.601366,40.774480' \
  --data-urlencode 'fields={incidents{type,geometry{type,coordinates},properties{id,iconCategory,magnitudeOfDelay,events{description,code,iconCategory},startTime,endTime,from,to,length,delay,roadNumbers,timeValidity,probabilityOfOccurrence,numberOfReports,lastReportTime}}}' \
  --data-urlencode 'language=en-GB' \
  --data-urlencode 'timeValidityFilter=present' \
  > tests/fixtures/tomtom_incidents_JFK.json
```

The same call with LAX's bbox (`-118.570767,33.807468,-118.245227,34.077534`) produces
`tomtom_incidents_LAX.json`. The parser tests derive their counts from the document itself
(`vendor returned N`, signals equal the inside-radius count), so a newer capture keeps
passing as long as the mapping still holds; the band table is pinned by
`tests/test_tomtom.py::test_severity_mapping_table`, which needs no payload at all.
