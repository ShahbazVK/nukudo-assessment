# Five-minute demo script

Everything below was checked against the three records in `logs/assessments/` on
2026-10-17. Quoted UI strings are literal - if the screen shows something else, the
record has changed and [DEMO_ASSESSMENTS.md](DEMO_ASSESSMENTS.md) is the authority.

Open two browser tabs before you start: the app at <http://127.0.0.1:8501> and
<http://127.0.0.1:8000/health>. Have `DEMO_ASSESSMENTS.md` open in a third if you want a
fallback for numbers you forget.

---

## 1. Opening - 30 seconds

> "This is a travel disruption assessor. It answers one question for a business traveler:
> is this trip at risk, and what should I do about it? You give it a U.S. route and a date;
> it pulls live signals from three sources - National Weather Service alerts and forecasts,
> Aviationstack flight status, and TomTom road incidents around both airports - and then a
> single LLM call fuses them. It doesn't summarize each source; it weighs whether this
> closure, this delay, this storm actually affects *this* route on *this* date. You get a
> risk level, the specific risks, the evidence line behind each one, and concrete actions.
> And every claim traces back to a raw API payload saved on disk."

---

## 2. The walkthrough - 3 minutes

### 2.1 Show the blank form

**Do:** point at the form, then at the caption under it.

**Say:**

> "This is what an Operations team member sees: origin, destination, date. Nothing else.
> The caption is deliberate - *'Nothing is fetched until you press Assess trip'* - because
> every submission costs three API calls and one model call. The tool never spends quota
> just because a page loaded."

### 2.2 Open the demo assessments

**Do:** expand **Demo assessments (pre-run)** - collapsed, directly above the form.

**Say:**

> "I've pre-run three assessments and stored them, because the free-tier model is limited
> to roughly one call per minute and I don't want the demo to be a 60-second silence.
> These are not screenshots or fixtures: each button reads a real record back from the
> backend with `GET /assessment/{id}`. No source is called, no model is called."

**Do:** click **JFK → LAX · today** (`4044a5aa-71e9-4f50-8014-64173c85bf08`). The button
gains a `✓` and turns primary; the panel appears below.

> Note: the suffix is computed from the current date (`today` / `tomorrow` / ISO). If the
> demo is run after 2026-10-17 the buttons read *JFK → LAX · 2026-10-17* instead - say the
> date, and keep every number identical: the records do not change.

### 2.3 Walk the assessment panel

**Say (pill):**

> "The model concluded this trip is **high risk**, confidence **0.66** - medium. Two of the
> four individual risks are *severe* - a road closure near JFK and closures near LAX - so
> why is the overall level high and not severe? My reading: there's no weather threat at the
> origin, the only flight signal is a routine 24-minute delay, and the model can't see your
> specific flight - the uncertainty list says so. That's the difference between weighing and
> counting labels."

**Do:** read the summary line, then scroll to **Primary risks**. Note the order is the
model's, not severity-sorted: low, moderate, severe, severe.

**Say (the first severe card):**

> "Closure roughly a kilometre from JFK - severe. The evidence line under it reads
> `Source: TomTom · Signal: event_descriptions · Value: Closed`. That is the actual field
> the incident carries; the underlying signal is *'Road closed 1.0 km from JFK (origin
> airport), near JFK Expy S to exit [A] … affects 600 m of road'*. The line names the field
> that proves the claim, not a paraphrase of it."

**Say (actions):**

> "Four recommended actions, and they're operational: *'Leave for JFK early and check
> real-time traffic before departing'*. Not 'be careful' - reroute, add buffer, carry water
> for the LAX heat advisory."

**Say (uncertainty):**

> "And here's what the tool says it *cannot* tell you. Six statements. Three are the model
> naming what it doesn't know - your exact flight, that traffic is current conditions not a
> forecast, that the weather forecast covers one day. Three are the pipeline admitting it
> showed the model only **3 of its 129** TomTom incidents, 3 of its 13 flights and 3 of its
> 6 weather signals - and each of those now carries its own *Raw records* list, one
> monospace line per file, nothing truncated, so you could open the evidence for the
> signals the model never saw. That list is the point of the project."

### 2.4 Open the evidence drawer

**Do:** expand **Show raw evidence and AI reasoning**.

**Say:**

> "Four tabs: Aviationstack, NWS, TomTom, and AI reasoning. These are the persisted
> envelopes - the raw payloads were written to disk *before* parsing - plus the exact prompt
> the model received and the exact JSON it returned, with token usage. So if you doubt any
> sentence in that assessment, you can walk it back to the file it came from."

### 2.5 Switch to SFO → JFK - the honest failure

**Do:** click **SFO → JFK · tomorrow** (`147c9cbf-a85c-48b9-9389-d61d9ae5be9e`).

**Say (banner and chips first):**

> "This one is more interesting. It's an assessment for *tomorrow*, and the aviationstack
> free tier only serves today's board - the request was refused with HTTP 403,
> `function_access_restricted`, `error_code: plan_limit`. Look at what the tool did: the
> banner says some sources were unavailable, the Aviationstack chip reads **unavailable**
> with `code: plan_limit` and **0 record(s)**, and the first line of the uncertainty list is
> *'Flight status unavailable (aviationstack plan limit)'*."

**Say (the verdict):**

> "It still assessed the trip - **severe**, confidence 0.55 - from the two sources that did
> answer. The top card is a closure 0.96 km from JFK, evidence line
> `Source: TomTom · Signal: summary · Value: Road closed 1.0 km from JFK (destination
> airport)`. What it did *not* do is the part that matters: it didn't pretend the flights
> were fine, it didn't invent a schedule, it didn't quietly fall back to yesterday's data.
> The status pill says `partial` and the summary says flight delays are **unknown**. That's
> what 'no mocked data' means when it costs you a result."

**Say (if asked why the banner isn't the rate-limit one):**

> "Because the failure was a *source*, not the model. `fusion_error_code` is null here;
> it's only set when fusion itself failed - `rate_limited`, `timeout`, `provider_error` -
> and that's what switches the banner to 'wait 60 seconds and retry'."

### 2.6 Optional - run a fresh one

**Only if there's time and they ask.** Say:

> "If you want to see a live one, we can run it now - about five seconds - and then we wait
> a minute before the next, because that's the free-tier budget."

If the model refuses, narrate it rather than hiding it: the banner explains the rate limit,
the three source chips stay `ok`, and the drawer still holds what the sources found.
Otherwise skip and move to tradeoffs.

---

## 3. The tradeoffs - 1 minute

> "What would I change before deploying? Four things.
>
> **One: the free tiers are real constraints, not bugs.** TomTom's incident-details endpoint
> is included on the free key, but aviationstack's future-date schedules are not, and Groq's
> free tier meters about 8,000 tokens a minute - one of these assessments is ~6,000, so it's
> effectively one call per minute. Production means paid tiers or different providers, and
> the architecture already handles either: the source contract and the fusion interface
> don't change.
>
> **Two: the airport table is 58 airports.** That's auditable and honest about its coverage,
> but production would use a full airport database - OpenFlights or the OurAirports dataset
> - behind the same lookup signature. It's a scope decision, not a finished feature.
>
> **Three: the fusion prompt is one prompt.** It has never been evaluated against a labeled
> set of 'this trip was disrupted' versus 'this trip was not'. In production I'd build that
> set, measure precision and recall per risk type, and tune the prompt - with explicit
> thresholds for what counts as severe versus high for closures, weather and delays.
>
> **Four: three signals per source is a token-budget compromise.** The model sees the three
> most relevant signals per source, not all 129. The cap is disclosed in the assessment, but
> production would either raise it on a paid tier or use a two-stage pipeline: a cheap
> classifier ranks signals, then the fusion model sees the top N."

---

## 4. Closing - 30 seconds

> "What I'm proudest of isn't the happy path - it's that the tool is honest when it's
> incomplete. The SFO → JFK record exists because a real free-tier limit hit a real query,
> and the system reported the gap, kept the evidence it *did* have, and dropped its
> confidence instead of filling the hole. Every claim in every assessment walks back to a
> raw payload, a prompt and a response that are still on disk. That's the judgment I'd want
> reviewed: what the system refuses to do."

---

## Numbers you can quote

| | JFK → LAX | SFO → JFK | DEN → MIA |
|---|---|---|---|
| Status | `ok` | `partial` (aviationstack `plan_limit`) | `ok` |
| Risk / confidence | high · 0.66 | severe · 0.55 | severe · 0.78 |
| Risks / actions / caveats | 4 / 4 / 6 | 2 / 4 / 6 | 3 / 4 / 5 |
| Signals collected | 13 / 6 / 129 | 0 / 7 / 112 | 1 / 5 / 159 |
| Tokens (prompt · total) | 4,783 · 6,016 | 3,734 · 5,378 | 3,957 · 5,591 |
| Model call ms | 3,011 | 3,848 | 3,820 |
| Assessment id | `4044a5aa…85bf08` | `147c9cbf…be9e` | `dc3004c6…80319` |

Signal counts are aviationstack / NWS / TomTom. Full ids are in the buttons' hover text and
in [DEMO_ASSESSMENTS.md](DEMO_ASSESSMENTS.md).

## If something breaks

* **No demo buttons, or a red error on click** - the backend isn't running:
  `uvicorn backend.main:app --port 8000` from the project root.
* **404 `No assessment record for id …`** - this machine doesn't have the stored records
  (`logs/` is git-ignored). Say so: the UI shows the backend's own 404 verbatim rather than
  substituting a copy, which is the same honesty rule as the `partial` case. Fall back to
  [DEMO_ASSESSMENTS.md](DEMO_ASSESSMENTS.md) and one live run.
* **The rate-limit banner** - real and expected: wait ~60 s and click **Assess trip** again.

