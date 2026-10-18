# Pre-run demo assessments

Three real assessments, run once against the live sources and the live fusion model on
**2026-10-17** between 11:47 and 11:52 UTC, and persisted by the backend. The Streamlit
UI offers them as buttons behind **Demo assessments (pre-run)**; each button calls
`GET /assessment/{id}` and renders the stored envelope.

Nothing here is a mock, a sample, or a replay of a fabricated payload. The summaries,
risks and actions below were produced by the model from the source signals at the time
of the run; every claim in them cites an evidence id that resolves in the stored record.
If a source was down, the assessment says so instead of filling the gap - see the
SFO → JFK entry.

| # | Route | Travel date | Status | Risk | Confidence | Assessment ID |
|---|-------|-------------|--------|------|------------|---------------|
| 1 | JFK → LAX | 2026-10-17 | `ok` | high | 0.66 (medium) | `4044a5aa-71e9-4f50-8014-64173c85bf08` |
| 2 | SFO → JFK | 2026-10-18 | `partial` | severe | 0.55 (medium) | `147c9cbf-a85c-48b9-9389-d61d9ae5be9e` |
| 3 | DEN → MIA | 2026-10-17 | `ok` | severe | 0.78 (high) | `dc3004c6-5997-413c-970f-6eaef5a80319` |

All three fused with `openai/gpt-oss-120b`, HTTP 200, one attempt, no fusion error code.

---

## 1. JFK → LAX - today · **high** risk, medium confidence

**ID** `4044a5aa-71e9-4f50-8014-64173c85bf08` · **route** JFK → LAX · **date**
2026-10-17 · **status** `ok` (all three sources `ok`) · **fused** 2026-10-17T11:47:32Z

**One line:** severe road closures within ~1 km of both airports plus a heat advisory at
LAX, with a 24-minute sample delay on the route.

> Both airports have notable issues today: a heat advisory at LAX (moderate) and severe
> road closures within 1 km of JFK and LAX that could impede ground travel. Reported
> flights on this route show a typical 24-minute delay (low). All three sources returned
> OK data, giving medium confidence in the assessment.

| Severity | Risk | Cited evidence |
|----------|------|----------------|
| severe | Road closure ~1 km from JFK may delay ground transport to the origin airport | `tomtom:event_descriptions=Closed` |
| severe | Road closures ~1 km from LAX may delay ground transport to the destination airport | `tomtom:event_descriptions=Bridge closed`, `Closed` |
| moderate | Heat Advisory at LAX may affect airport operations | `nws:event=Heat Advisory` |
| low | Potential 24-minute departure delay for flights JFK→LAX | `aviationstack:delay_minutes=24` |

Actions the model recommended: leave for JFK early and check live traffic; monitor the
flight for a 20–30 minute delay and check in early; carry water for the LAX heat
advisory; allow extra time after landing at LAX.

Signals collected: aviationstack 13, NWS 6, TomTom 129 records. The prompt carried 3
signals per source (the rest were withheld for prompt size and are listed in
`uncertainty` with their raw-log paths). Prompt 4 783 tokens, completion 1 233, total
6 016; model call 3 011 ms (3 038 ms end-to-end through fusion), full request 4 810 ms.

---

## 2. SFO → JFK - tomorrow · **severe** risk, medium confidence · `partial`

**ID** `147c9cbf-a85c-48b9-9389-d61d9ae5be9e` · **route** SFO → JFK · **date**
2026-10-18 · **status** `partial` (aviationstack `unavailable`, NWS `ok`, TomTom `ok`) ·
**fused** 2026-10-17T11:50:44Z (the stored record is the second run; its own fusion call
took one attempt)

**One line:** severe road closures near both airports, assessed *without* flight data
because the aviationstack plan does not cover a future date.

> Weather at SFO is clear with low risk. Flight status could not be checked because the
> aviationstack source was unavailable, so flight delays are unknown. TomTom reports
> current severe road closures within 1 km of JFK and about 3.5 km of SFO, which may
> impede airport access; these incidents are indicative for tomorrow and carry moderate
> confidence. Overall confidence is medium due to missing flight data and the predictive
> nature of the traffic feed.

| Severity | Risk | Cited evidence |
|----------|------|----------------|
| severe | Road closure 0.96 km from JFK may delay arrival | `tomtom:summary=Road closed 1.0 km from JFK (destination airport)` |
| severe | Road closure 3.59 km from SFO may delay departure | `tomtom:summary=Road closed 3.6 km from SFO (origin airport)` |

Actions: allow 30–60 minutes extra to reach both airports and monitor traffic; check
real-time flight status with the airline or airport app; keep alternative transport
ready; reconfirm road conditions on the morning of travel.

**Why this one is `partial`.** The aviationstack free tier serves only today's real-time
board, so the request for 2026-10-18 was refused verbatim:

> Aviationstack refused this request as a plan restriction (HTTP 403, code
> `function_access_restricted`, message "Your current subscription plan does not support
> this API function."). This key's plan does not include flight schedules for
> 2026-10-18, so no flight status could be read (free-tier keys serve only today's
> real-time board). Free-tier keys return real-time flights only (100 requests/month) and
> no historical or future schedules, and no schedule was inferred to fill the gap.

The source is recorded as `error_code: "plan_limit"` with zero records - not as "no
disruption". This is the case the project rules care about: a missing signal is reported
as missing, and the assessment explicitly says flight delays are unknown. A retry 90
seconds later hit the same deterministic limit, so the second attempt is the stored one.

Prompt 3 734 tokens, completion 1 644, total 5 378; model call 3 848 ms (3 868 ms
end-to-end through fusion), full request 5 332 ms. 2 risks, 4 actions - fewer risks than
the other two runs, entirely because the flight dimension is absent.

---

## 3. DEN → MIA - today · **severe** risk, high confidence

**ID** `dc3004c6-5997-413c-970f-6eaef5a80319` · **route** DEN → MIA · **date**
2026-10-17 · **status** `ok` (all three sources `ok`) · **fused** 2026-10-17T11:52:20Z

**One line:** three road closures within 3 km of MIA and a 92% chance of storms, on top
of a reported 21-minute delay on UA1418.

> A United flight on this route shows a reported 21-minute departure delay (low risk).
> Weather forecasts indicate a 92% chance of rain showers and thunderstorms at MIA today
> and an 84% chance tonight, posing moderate ground-operation risk. TomTom reports three
> severe road closures within 3 km of MIA airport, which could significantly disrupt
> ground travel. Confidence is high (0.78) because all sources are online, though
> TomTom's real-time feed may change and the aviationstack sample is thin.

| Severity | Risk | Cited evidence |
|----------|------|----------------|
| severe | Multiple road closures within ~3 km of MIA may impede ground transport | `tomtom:distance_km=2.27`, `2.99`, `3.44` |
| moderate | High probability of rain and thunderstorms at MIA during the arrival window | `nws:precipitation_probability=92`, `84` |
| low | United flight UA1418 departure delayed ~21 minutes | `aviationstack:delay_minutes=21` |

Actions: arrive early for the reported delay; pack rain gear and allow time for
weather-related delays; check alternative routes to MIA given the closures; monitor
flight status and traffic up to departure.

Signals collected: aviationstack 1, NWS 5, TomTom 159 records. Prompt 3 957 tokens,
completion 1 634, total 5 591; model call 3 820 ms (3 859 ms end-to-end through fusion),
full request 5 940 ms. Only one
flight record matched the route, which the model itself flags as a thin sample.

---

## How the UI uses these

* `frontend/demo_assessments.py` stores **ids, routes and dates only** - no summaries, no
  risk levels, no envelopes. `test_demo_entries_are_identifiers_only` fails if a result
  field is ever added there.
* Clicking a button in **Demo assessments (pre-run)** issues `GET /assessment/{id}` and
  renders whatever comes back, then fetches `GET /evidence/{id}` for the drawer. No
  source is called, no model is called, no quota is spent.
* The button label shows `today` / `tomorrow` only when that is true relative to the
  current date; otherwise it shows the ISO date. A record is never mislabelled "today"
  a week later.
* The assessment currently on screen has its button marked `✓` and drawn as the primary
  button.

**Prerequisite.** The records live in `logs/assessments/` on the machine that ran them,
and `logs/` is git-ignored. On a checkout without those files the endpoint answers
`404 No assessment record for id …` and the UI displays that message verbatim; it does
not fall back to a built-in copy of the assessment.

**Re-running.** Each run mints a new id, so re-running one of these routes does not
update the entry above - it creates a different assessment. If a demo record is
regenerated deliberately, update `frontend/demo_assessments.py` and this file together.

## Reproducing a check

```bash
.venv/bin/python -m uvicorn backend.main:app --port 8000 &
curl -s http://127.0.0.1:8000/assessment/4044a5aa-71e9-4f50-8014-64173c85bf08 | head -c 400
curl -s http://127.0.0.1:8000/evidence/147c9cbf-a85c-48b9-9389-d61d9ae5be9e | head -c 400
```

`/assessment/{id}` returns the envelope the UI renders; `/evidence/{id}` returns the
whole stored record - source envelopes, the fusion prompt, the raw model body and the
token usage - so any statement above can be traced to its origin.


