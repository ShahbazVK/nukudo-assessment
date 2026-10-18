"""Aviationstack flight-status source: delays, cancellations and diversions.

What this source is for
-----------------------
Aviationstack's ``/flights`` endpoint reports the operational status of a flight:
whether it ran, when it was scheduled to leave and arrive, when it is now expected
(or actually did), and by how many minutes it slipped. That is a direct disruption
signal for a route, so it maps onto our own ``config.RISK_LEVELS`` vocabulary rather
than carrying the vendor's words through.

The honest free-tier story
--------------------------
The free plan is **100 requests per month, real-time flights only**. It does *not*
include historical flights, airline routes, autocomplete, or **flight schedules /
future flights** (verified against the published plan comparison). Two consequences
shape this module:

* The ``flight_date`` filter belongs to the paid schedule features, so it is only
  sent when it is actually needed - that is, when the travel date is not today. For
  today the real-time board *is* the answer, so no filter is sent and a free key has
  the best chance of returning real flights.
* For any other date the free tier will usually answer with a plan restriction or an
  empty list. Both are reported as ``unavailable`` with the reason and the limitation
  named - a plan gap is never presented as "no disruption". When the vendor says the
  plan does not cover the request the code is ``plan_limit``; when it simply returns
  no rows the code is ``no_data_for_query``. Nothing is ever extrapolated into a
  schedule.

For today's date, this module queries the real-time board without a date filter and
matches by route; for future dates, it sends ``flight_date`` and expects
``plan_limit`` or ``no_data_for_query`` from the free tier. A paid plan would remove
this asymmetry.

Severity bands (deliberate, deterministic, documented)
------------------------------------------------------
The delay used is the **largest** delay the vendor reports for the flight (departure
or arrival), because a late arrival is what disrupts the trip even when the aircraft
left on time. Reported ``delay`` fields win over delays computed from ``scheduled``
versus ``estimated``/``actual`` timestamps; the source of the number is recorded in
the signal's metrics.

===========================  ==========================================
Observation                  Level
===========================  ==========================================
``cancelled``                ``severe`` - the planned trip cannot happen
``diverted`` / ``incident``  ``high``
delay >= 180 min             ``high``
60 <= delay < 180 min        ``moderate``
15 <= delay < 60 min         ``low``
delay < 15 min               no signal (below the reporting floor)
===========================  ==========================================

A flight that is returned and is *not* disruptive is still evidence: it is cited by
an explicit "checked, no disruption identified" summary signal, so the fusion layer
can tell "flights ran on time" apart from "we could not check". A query that returns
no matching flight is ``unavailable`` - never an ``ok`` envelope with no signals.

Sample size, and why a clear reading can be weak
------------------------------------------------
The real-time board is a **snapshot, not a schedule**: the JFK->LAX capture returned
100 rows for 173 reported flights, and only 9 of those rows were dated today. Every
signal therefore carries ``sample_size``, ``returned_records``, ``total_available``,
``page_coverage`` and ``sample_is_thin``, and the cohort-level summary signal discounts
its confidence by page coverage *and* by how thin the sample is, so 9 of 173 scores well
below 100 of 100. Per-flight signals carry the same metadata but keep their own
confidence: a cancellation is a fact whether the sample was 9 flights or 900 - what the
sample size changes is how much a *conclusion about the route* is worth, which is the
fusion layer's call to make with the fields in hand.

Rate limits and retries are handled by :func:`data_sources.base.get_json`: 429/5xx
are retried with backoff and ``Retry-After`` honoured, and every attempt's raw
response is written to ``logs/raw/`` before it is parsed.
"""

from __future__ import annotations

import datetime
import json
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import config

from . import base

# --------------------------------------------------------------------------- #
# Normalized categories and thresholds
# --------------------------------------------------------------------------- #
_SIGNAL_KIND_DELAY: str = "flight_delay"
_SIGNAL_KIND_CANCELLATION: str = "flight_cancellation"
_SIGNAL_KIND_SUMMARY: str = "flight_status"

# The bands documented in the module docstring, in one place.
_DELAY_FLOOR_MINUTES: int = 15
_DELAY_MODERATE_MINUTES: int = 60
_DELAY_HIGH_MINUTES: int = 180

# Confidence is a fixed, declared value per evidence strength: a vendor-reported
# delay on a flight that has not finished is solid but not final; a delay derived
# from actual timestamps is the strongest reading this source can give; a "nothing
# disruptive" summary is weaker because absence of a report is weaker evidence.
_CONFIDENCE_ESTIMATED: float = 0.75
_CONFIDENCE_REPORTED: float = 0.85
_CONFIDENCE_ACTUAL: float = 0.95
_CONFIDENCE_SUMMARY: float = 0.70
# A summary signal is an inference over a cohort ("none of the N flights dated today is
# disruptive"), so it is only as strong as that cohort. The free-tier real-time board is
# a snapshot, not a schedule: the JFK->LAX capture held 100 rows for 173 reported
# flights, and only 9 of those rows were dated today. Two independent discounts are
# applied - how much of the vendor's matching set we actually received (`page_coverage`)
# and how thin the assessed sample is in absolute terms (`sample_size` against this
# target). Below the target, confidence falls; at or above it with full coverage, it is
# `_CONFIDENCE_SUMMARY` exactly.
_FULL_SAMPLE_FLIGHTS: int = 20

# The free tier pages 100 flights at a time and each page costs a request, so one
# request is made and any gap is reported rather than silently paginating.
_FREE_TIER_LIMIT: int = 100

_DISRUPTIVE_STATUSES: Mapping[str, str] = {
    "cancelled": "severe",
    "diverted": "high",
    "incident": "high",
}

# Vendor codes that mean "your account/plan may not do this", as opposed to a
# transport problem. Only ``missing_access_key`` and ``invalid_access_key`` were
# captured live (see tests/fixtures/README.md); the rest are recognized by code and,
# as a fallback, by the message text, so an unlisted plan code is still classified
# instead of being reported as an outage.
_QUOTA_CODES = frozenset(
    {"usage_limit_reached", "ratelimit_exceeded", "rate_limit_reached"}
)
_PLAN_CODES = frozenset(
    {"function_access_restricted", "https_access_restricted", "inactive_user"}
)
_CREDENTIAL_CODES = frozenset({"missing_access_key", "invalid_access_key"})
_PLAN_MESSAGE_HINTS: Tuple[str, ...] = (
    "plan",
    "subscri",
    "upgrade",
    "not included",
    "not support",
    "restricted",
    "permission",
    "trial",
)


# --------------------------------------------------------------------------- #
# Small, dependency-free readers (no vendor value is trusted implicitly)
# --------------------------------------------------------------------------- #
def _text(value: Any) -> Optional[str]:
    """A stripped non-empty string, or ``None`` for anything else."""
    return value.strip() if isinstance(value, str) and value.strip() else None


def _as_int(value: Any) -> Optional[int]:
    """An integer from an int or a numeric string; ``None`` otherwise.

    ``bool`` is rejected explicitly: ``True`` is an ``int`` in Python and must never
    be read as "1 minute of delay".
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return None
    return None


def _parse_timestamp(value: Any) -> Optional[datetime.datetime]:
    """Parse an ISO 8601 timestamp, tolerating ``Z`` and a missing offset.

    A timestamp without an offset is read as UTC, which is the convention the vendor
    uses for ``scheduled``/``estimated``/``actual``. Anything unparseable is ``None``
    - never "now", and never an assumption about a local timezone.
    """
    text = _text(value)
    if text is None:
        return None
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def _minutes_between(
    start: Optional[datetime.datetime], end: Optional[datetime.datetime]
) -> Optional[int]:
    """Whole minutes from ``start`` to ``end``, or ``None`` if either is unusable."""
    if start is None or end is None:
        return None
    return int(round((end - start).total_seconds() / 60.0))


def _mapping(value: Any) -> Mapping[str, Any]:
    """A JSON object as a mapping, or an empty mapping for anything else."""
    return value if isinstance(value, Mapping) else {}


def _vendor_error(payload: Any) -> Tuple[Optional[str], Optional[str]]:
    """The vendor's own ``error.code`` / ``error.message``, when the body has one.

    Aviationstack reports failures as ``{"error": {"code": ..., "message": ...}}``
    (captured live for both key errors). Some apilayer surfaces deliver that body
    with a 2xx status, so the body is inspected regardless of the HTTP status.
    """
    error = _mapping(_mapping(payload).get("error"))
    return _text(error.get("code")), _text(error.get("message"))


def _mentions_plan(message: Optional[str]) -> bool:
    """Whether a vendor message describes a plan/entitlement restriction."""
    lowered = (message or "").lower()
    return any(hint in lowered for hint in _PLAN_MESSAGE_HINTS)


def _vendor_error_from_result(
    result: base.HttpResult,
) -> Tuple[Optional[str], Optional[str]]:
    """The vendor's ``error.code``/``error.message`` for a failed call.

    ``base.get_json`` deliberately returns ``payload=None`` for every non-2xx
    response, so a 401/403 error body is only preserved in two places: the raw
    evidence record written before parsing, and the truncated ``body_preview``. The
    raw record is the ground truth and is therefore read first - that is why the
    evidence layer exists. Without this, a plan restriction could only be seen as
    "HTTP 403", and the free-tier gap would be indistinguishable from an outage.
    """
    code, message = _vendor_error(result.payload)
    if code is not None:
        return code, message

    document: Any = None
    if result.raw_path:
        try:
            document = base.load_raw_response(result.raw_path)
        except (OSError, ValueError):
            document = None
    if isinstance(document, Mapping):
        body = _mapping(document.get("response")).get("body_text")
        if isinstance(body, str) and body.strip():
            try:
                document = json.loads(body)
            except ValueError:
                document = None
        else:
            document = None
    code, message = _vendor_error(document)
    if code is not None:
        return code, message

    if isinstance(result.body_preview, str) and result.body_preview.strip():
        try:
            return _vendor_error(json.loads(result.body_preview))
        except ValueError:
            return None, None
    return None, None


# --------------------------------------------------------------------------- #
# Failure classification - plan gaps are not outages, and neither is "no rows"
# --------------------------------------------------------------------------- #
def _classify_failure(
    result: base.HttpResult, vendor_code: Optional[str], vendor_message: Optional[str]
) -> str:
    """Map one failed call onto the shared error-code vocabulary.

    The order matters: a 401 that talks about the plan is a plan limit (the key is
    valid but the function is not included), while a 401 about the key itself is a
    credential problem. A 403 from this API is an access/entitlement refusal, so it is
    a plan limit; without a structured vendor error body the HTTP mapping is kept.
    """
    code = (vendor_code or "").strip().lower()
    if result.status_code == 429 or code in _QUOTA_CODES:
        return base.ErrorCode.RATE_LIMITED
    if code in _CREDENTIAL_CODES or (
        result.status_code == 401 and not _mentions_plan(vendor_message)
    ):
        return base.ErrorCode.UNAUTHORIZED
    if result.status_code == 403 or code in _PLAN_CODES or _mentions_plan(vendor_message):
        return base.ErrorCode.PLAN_LIMIT
    if result.error_code:
        return result.error_code
    # A 2xx body carrying an error object we do not recognize is a payload-shape
    # surprise, not an outage: it is reported as such rather than as "no flights".
    return (
        base.ErrorCode.UNEXPECTED_PAYLOAD_SHAPE
        if result.ok
        else base.ErrorCode.UNEXPECTED_ERROR
    )


def _vendor_detail(
    result: base.HttpResult, vendor_code: Optional[str], vendor_message: Optional[str]
) -> str:
    """One traceable phrase describing what the vendor said, with secrets scrubbed."""
    parts: List[str] = []
    if result.status_code is not None:
        parts.append("HTTP {0}".format(result.status_code))
    if vendor_code:
        parts.append("code {0!r}".format(vendor_code))
    if vendor_message:
        parts.append("message {0!r}".format(base.redact_text(vendor_message)))
    if not parts:
        return base.redact_text(result.error or "no reason was returned")
    return ", ".join(parts)


def _failure_envelope(
    result: base.HttpResult,
    *,
    location: base.Location,
    travel_date: datetime.date,
    travel_date_is_today: bool,
) -> base.NormalizedSignals:
    """Build the explicit envelope for a call that produced no usable flight data.

    ``plan_limit`` and ``no_data_for_query`` become ``unavailable`` (the source works,
    this query is outside what it can answer); everything else stays an ``error``.
    The vendor's own code and message are quoted, so a gap is never our guess.
    """
    vendor_code, vendor_message = _vendor_error_from_result(result)
    error_code = _classify_failure(result, vendor_code, vendor_message)
    detail = _vendor_detail(result, vendor_code, vendor_message)
    scope = (
        "the travel date is today, so the real-time board should have covered it"
        if travel_date_is_today
        else "free-tier keys serve only today's real-time board"
    )

    if error_code == base.ErrorCode.PLAN_LIMIT:
        return base.unavailable_signals(
            source=config.SOURCE_AVIATIONSTACK,
            error_code=base.ErrorCode.PLAN_LIMIT,
            error=(
                "Aviationstack refused this request as a plan restriction ({0}). This "
                "key's plan does not include flight schedules for {1}, so no flight "
                "status could be read ({2}). Free-tier keys return real-time flights "
                "only (100 requests/month) and no historical or future schedules, and "
                "no schedule was inferred to fill the gap."
            ).format(detail, travel_date.isoformat(), scope),
            location=location,
            travel_date=travel_date,
            raw_response_path=result.raw_path,
            raw_response_paths=result.raw_paths,
            latency_ms=result.latency_ms,
            warnings=[
                "A plan limit is a coverage gap: not a failure, and not 'no disruption'.",
                "Upgrade the plan, or query today's real-time board, to get flight signals.",
            ],
        )

    if error_code == base.ErrorCode.UNAUTHORIZED:
        return base.failure_signals(
            source=config.SOURCE_AVIATIONSTACK,
            error_code=base.ErrorCode.UNAUTHORIZED,
            error=(
                "Aviationstack rejected the API key ({0}). Check AVIATIONSTACK_API_KEY; "
                "no default key and no substitute source were used."
            ).format(detail),
            location=location,
            travel_date=travel_date,
            raw_response_path=result.raw_path,
            raw_response_paths=result.raw_paths,
            latency_ms=result.latency_ms,
        )

    if error_code == base.ErrorCode.RATE_LIMITED:
        return base.failure_signals(
            source=config.SOURCE_AVIATIONSTACK,
            error_code=base.ErrorCode.RATE_LIMITED,
            error=(
                "Aviationstack rate-limited this request after {0} attempt(s) ({1}). "
                "Retry-After was honoured by the HTTP layer; the free tier allows 100 "
                "requests/month."
            ).format(result.attempts, detail),
            location=location,
            travel_date=travel_date,
            raw_response_path=result.raw_path,
            raw_response_paths=result.raw_paths,
            latency_ms=result.latency_ms,
            warnings=[
                "This is throttling or an exhausted quota, not a missing route or 'no risk'."
            ],
        )

    if error_code in (
        base.ErrorCode.NO_DATA_FOR_QUERY,
        base.ErrorCode.NOT_IMPLEMENTED,
    ):
        return base.unavailable_signals(
            source=config.SOURCE_AVIATIONSTACK,
            error_code=error_code,
            error="Aviationstack returned no usable flight data ({0}).".format(detail),
            location=location,
            travel_date=travel_date,
            raw_response_path=result.raw_path,
            raw_response_paths=result.raw_paths,
            latency_ms=result.latency_ms,
        )

    return base.failure_signals(
        source=config.SOURCE_AVIATIONSTACK,
        error_code=error_code,
        error="Aviationstack request failed ({0}).".format(detail),
        location=location,
        travel_date=travel_date,
        raw_response_path=result.raw_path,
        raw_response_paths=result.raw_paths,
        latency_ms=result.latency_ms,
    )


def _unavailable_for_no_flights(
    *,
    location: base.Location,
    travel_date: datetime.date,
    travel_date_is_today: bool,
    returned: int,
    raw_path: Optional[str],
    raw_paths: Sequence[str],
    latency_ms: Optional[float],
) -> base.NormalizedSignals:
    """The explicit "this query has no flight data" envelope, with the reason named."""
    if returned:
        reason = (
            "Aviationstack returned {0} flight(s), but none of them departed {1} and "
            "arrived {2} on {3}, so nothing describes this route and date. The records "
            "were not re-dated or re-routed to fit, and no schedule was inferred."
        ).format(
            returned,
            location.origin.iata,
            location.destination.iata,
            travel_date.isoformat(),
        )
    elif travel_date_is_today:
        reason = (
            "Aviationstack returned no flights for {0} -> {1} on {2}. Either no flight "
            "is on the board for this route today, or this key's plan cannot see it; "
            "nothing was inferred."
        ).format(
            location.origin.iata,
            location.destination.iata,
            travel_date.isoformat(),
        )
    else:
        reason = (
            "Aviationstack returned no flights for {0} -> {1} on {2}, and the free-tier "
            "API does not return schedules for any date other than today's real-time "
            "board (100 requests/month, no historical or future flights). The empty "
            "result is that limitation, not evidence that the route is clear; no "
            "schedule was inferred."
        ).format(
            location.origin.iata,
            location.destination.iata,
            travel_date.isoformat(),
        )

    return base.unavailable_signals(
        source=config.SOURCE_AVIATIONSTACK,
        error_code=base.ErrorCode.NO_DATA_FOR_QUERY,
        error=reason,
        location=location,
        travel_date=travel_date,
        raw_response_path=raw_path,
        raw_response_paths=raw_paths,
        latency_ms=latency_ms,
        warnings=[
            "An empty flight list is unavailable data, never an 'ok' with no signals.",
            "The raw response is on disk, so the empty result itself can be inspected.",
        ],
    )


# --------------------------------------------------------------------------- #
# Flight records -> observations. No value here is assumed; every one is read.
# --------------------------------------------------------------------------- #
def _matches_route(
    flight: Mapping[str, Any], location: base.Location, travel_date: datetime.date
) -> bool:
    """Whether a returned flight really is this route on this date.

    The vendor filters server-side, but a filter is not a promise: a record naming a
    different date or a different airport is reported as unmatched rather than being
    stretched to fit the question.
    """
    departure = _mapping(flight.get("departure"))
    arrival = _mapping(flight.get("arrival"))
    if (_text(departure.get("iata")) or "").upper() != location.origin.iata:
        return False
    if (_text(arrival.get("iata")) or "").upper() != location.destination.iata:
        return False
    flight_date = _text(flight.get("flight_date"))
    return flight_date is None or flight_date == travel_date.isoformat()


def _leg_delay(leg: Mapping[str, Any]) -> Tuple[Optional[int], Optional[str]]:
    """The delay for one leg, preferring the vendor's number over a derivation.

    Returns ``(minutes, source)`` where source is ``"reported"``, ``"actual"`` or
    ``"estimated"``. Early flights keep their negative value: it is recorded as
    observed and simply never raises a severity.
    """
    reported = _as_int(leg.get("delay"))
    if reported is not None:
        return reported, "reported"
    scheduled = _parse_timestamp(leg.get("scheduled"))
    actual = _minutes_between(scheduled, _parse_timestamp(leg.get("actual")))
    if actual is not None:
        return actual, "actual"
    estimated = _minutes_between(scheduled, _parse_timestamp(leg.get("estimated")))
    if estimated is not None:
        return estimated, "estimated"
    return None, None


def _flight_delay(flight: Mapping[str, Any]) -> Tuple[Optional[int], Optional[str]]:
    """The largest delay across both legs: a late arrival disrupts the trip too.

    A departure that left on time but lands 90 minutes late is a 90-minute delay for
    the traveller, so the maximum is used and the evidence source is kept alongside it.
    """
    legs = [
        leg
        for leg in (_mapping(flight.get("departure")), _mapping(flight.get("arrival")))
        if leg
    ]
    usable = [pair for pair in (_leg_delay(leg) for leg in legs) if pair[0] is not None]
    if not usable:
        return None, None
    minutes = max(pair[0] for pair in usable if pair[0] is not None)
    # Strongest evidence wins a tie: reported > actual > estimated.
    strength = {"reported": 0, "actual": 1, "estimated": 2}
    source = min(
        (pair[1] for pair in usable if pair[0] == minutes),
        key=lambda name: strength.get(name or "", 9),
    )
    return minutes, source


def _severity_for(
    delay_minutes: Optional[int], status: Optional[str]
) -> Tuple[Optional[str], List[str]]:
    """Map one flight onto a risk level plus the reasons for that mapping.

    The bands are the ones documented in the module docstring. ``None`` means "no
    signal": the observation is real but not disruptive (or not measurable), which is
    different from "we do not know what happened".
    """
    if status in _DISRUPTIVE_STATUSES:
        return _DISRUPTIVE_STATUSES[status], ["flight_status={0}".format(status)]
    if delay_minutes is None:
        return None, ["no delay reported and no comparable scheduled timestamps"]
    if delay_minutes < 0:
        return None, [
            "{0} min early, which is not a disruption".format(abs(delay_minutes))
        ]
    if delay_minutes < _DELAY_FLOOR_MINUTES:
        return None, [
            "delay {0} min is below the {1} min floor".format(
                delay_minutes, _DELAY_FLOOR_MINUTES
            )
        ]
    if delay_minutes >= _DELAY_HIGH_MINUTES:
        return "high", [
            "delay {0} min >= {1} min".format(delay_minutes, _DELAY_HIGH_MINUTES)
        ]
    if delay_minutes >= _DELAY_MODERATE_MINUTES:
        return "moderate", [
            "{0} min <= delay {1} min < {2} min".format(
                _DELAY_MODERATE_MINUTES, delay_minutes, _DELAY_HIGH_MINUTES
            )
        ]
    return "low", [
        "{0} min <= delay {1} min < {2} min".format(
            _DELAY_FLOOR_MINUTES, delay_minutes, _DELAY_MODERATE_MINUTES
        )
    ]


def _confidence_for(status: Optional[str], delay_source: Optional[str]) -> float:
    """A declared confidence for the evidence strength behind one signal."""
    if status in _DISRUPTIVE_STATUSES:
        return _CONFIDENCE_REPORTED
    if delay_source == "actual":
        return _CONFIDENCE_ACTUAL
    if delay_source == "reported":
        return _CONFIDENCE_REPORTED
    return _CONFIDENCE_ESTIMATED


def _short_timestamp(value: Any) -> str:
    """A compact UTC rendering of a vendor timestamp, or a stated absence."""
    parsed = _parse_timestamp(value)
    if parsed is None:
        return "not published"
    return parsed.astimezone(datetime.timezone.utc).strftime("%Y-%m-%d %H:%MZ")


def _flight_label(flight: Mapping[str, Any]) -> str:
    """A human-readable flight identity built only from fields that were published."""
    flight_info = _mapping(flight.get("flight"))
    airline = _mapping(flight.get("airline"))
    identity = (
        _text(flight_info.get("iata"))
        or _text(flight_info.get("number"))
        or "unidentified flight"
    )
    name = _text(airline.get("name"))
    return identity if name is None else "{0} ({1})".format(identity, name)


def _leg_metrics(leg: Mapping[str, Any], prefix: str) -> Dict[str, Any]:
    """The published fields of one leg, carried through for the fusion layer."""
    return {
        "{0}_iata".format(prefix): _text(leg.get("iata")),
        "{0}_scheduled".format(prefix): _text(leg.get("scheduled")),
        "{0}_estimated".format(prefix): _text(leg.get("estimated")),
        "{0}_actual".format(prefix): _text(leg.get("actual")),
        "{0}_delay_minutes".format(prefix): _as_int(leg.get("delay")),
        "{0}_terminal".format(prefix): _text(leg.get("terminal")),
        "{0}_gate".format(prefix): _text(leg.get("gate")),
    }


def _sample_metrics(
    sample_size: int,
    *,
    returned_records: int,
    total_available: Optional[int],
) -> Dict[str, Any]:
    """How much the board we read actually covers, carried on every signal.

    ``sample_size`` is the number of flights *on the travel date* that were assessed;
    ``total_available`` is the vendor's own ``pagination.total`` for the query, and
    ``returned_records`` is how many of them this response contained (one page, because
    each page costs a request). ``page_coverage`` is ``None`` when the vendor published no
    total - unknown is reported as unknown, never as full coverage.
    """
    if total_available is not None and total_available > 0:
        page_coverage: Optional[float] = min(
            round(returned_records / float(total_available), 3), 1.0
        )
    else:
        page_coverage = None
    return {
        "sample_size": sample_size,
        "returned_records": returned_records,
        "total_available": total_available,
        "page_coverage": page_coverage,
        "sample_is_thin": sample_size < _FULL_SAMPLE_FLIGHTS,
    }


def _summary_confidence(sample: Mapping[str, Any]) -> float:
    """Confidence in a cohort-level claim, discounted for a thin or partial sample.

    Both factors are multiplicative and floored at 0.5, so a weak sample lowers
    confidence without ever reducing it to zero: a checked-and-clear result from 9 of 173
    flights is still a real observation, just a weak one.
    """
    coverage = sample.get("page_coverage")
    coverage_factor = 1.0 if coverage is None else 0.5 + 0.5 * float(coverage)
    sample_size = int(sample.get("sample_size") or 0)
    thinness = min(1.0, sample_size / float(_FULL_SAMPLE_FLIGHTS))
    return round(_CONFIDENCE_SUMMARY * coverage_factor * (0.5 + 0.5 * thinness), 3)


def _flight_evidence(
    flight: Mapping[str, Any],
    *,
    index: int,
    travel_date: datetime.date,
    observed_at: str,
    raw_path: Optional[str],
) -> base.EvidenceEntry:
    """One evidence entry per matched flight, pointing at the raw response."""
    departure = _mapping(flight.get("departure"))
    arrival = _mapping(flight.get("arrival"))
    delay, delay_source = _flight_delay(flight)
    return base.make_evidence(
        config.SOURCE_AVIATIONSTACK,
        field="data[{0}]".format(index),
        value={
            "flight": _flight_label(flight),
            "flight_status": _text(flight.get("flight_status")),
            "flight_date": _text(flight.get("flight_date")),
            "departure_scheduled": _text(departure.get("scheduled")),
            "departure_estimated": _text(departure.get("estimated")),
            "departure_actual": _text(departure.get("actual")),
            "arrival_scheduled": _text(arrival.get("scheduled")),
            "arrival_estimated": _text(arrival.get("estimated")),
            "arrival_actual": _text(arrival.get("actual")),
            "delay_minutes": delay,
            "delay_source": delay_source,
        },
        observed_at=observed_at,
        raw_response_path=raw_path,
        note=(
            "Flight {0} for {1}, as published by Aviationstack; delay read as the "
            "largest departure/arrival delay ({2})."
        ).format(_flight_label(flight), travel_date.isoformat(), delay_source or "n/a"),
        evidence_id="ev_flight_{0}".format(index + 1),
    )


def _flight_signal(
    flight: Mapping[str, Any],
    *,
    location: base.Location,
    travel_date: datetime.date,
    observed_at: str,
    evidence_id: str,
    sample: Mapping[str, Any],
) -> Optional[base.SignalEntry]:
    """One signal for a disruptive flight, or ``None`` when it is not disruptive.

    The sample fields are attached to every signal, not only the summary: a caller that
    sees *any* signal for this query then also knows what cohort it came from. They
    describe the cohort, not this observation - a cancellation is a fact whether it was
    found among 9 flights or 900 - so the signal's own confidence is left alone.
    """
    status = (_text(flight.get("flight_status")) or "").lower() or None
    delay, delay_source = _flight_delay(flight)
    severity, reasons = _severity_for(delay, status)
    if severity is None:
        return None

    departure = _mapping(flight.get("departure"))
    arrival = _mapping(flight.get("arrival"))
    label = _flight_label(flight)
    route = "{0} -> {1}".format(location.origin.iata, location.destination.iata)
    window = "departure {0}, arrival {1}".format(
        _short_timestamp(departure.get("scheduled")),
        _short_timestamp(arrival.get("scheduled")),
    )

    if status in _DISRUPTIVE_STATUSES:
        kind = _SIGNAL_KIND_CANCELLATION
        consequence = (
            "a cancellation removes the planned flight"
            if status == "cancelled"
            else "the flight did not operate as scheduled"
        )
        summary = (
            "Aviationstack reports flight {0} {1} on {2} as '{3}' ({4}); {5}. Mapped to "
            "risk level '{6}'."
        ).format(
            label, route, travel_date.isoformat(), status, window, consequence, severity
        )
    else:
        kind = _SIGNAL_KIND_DELAY
        summary = (
            "Aviationstack reports flight {0} {1} on {2} delayed {3} min ({4}; "
            "departure {5} -> {6}, arrival {7} -> {8}). Mapped to risk level '{9}'."
        ).format(
            label,
            route,
            travel_date.isoformat(),
            delay,
            delay_source or "source not published",
            _short_timestamp(departure.get("scheduled")),
            _short_timestamp(departure.get("estimated") or departure.get("actual")),
            _short_timestamp(arrival.get("scheduled")),
            _short_timestamp(arrival.get("estimated") or arrival.get("actual")),
            severity,
        )

    metrics: Dict[str, Any] = {
        "flight": _flight_label(flight),
        "flight_number": _text(_mapping(flight.get("flight")).get("number")),
        "airline": _text(_mapping(flight.get("airline")).get("name")),
        "flight_status": status,
        "flight_date": _text(flight.get("flight_date")),
        # Explicit flags: the fusion layer should not have to string-match a status.
        "cancelled": status == "cancelled",
        "diverted": status == "diverted",
        "delay_minutes": delay,
        "delay_source": delay_source,
        "travel_date": travel_date.isoformat(),
        "route": route,
        "severity_reasons": reasons,
    }
    metrics.update(_leg_metrics(departure, "departure"))
    metrics.update(_leg_metrics(arrival, "arrival"))
    metrics.update(sample)

    effective_at = (
        _text(arrival.get("actual"))
        or _text(arrival.get("estimated"))
        or _text(arrival.get("scheduled"))
    )

    return base.make_signal(
        kind,
        summary,
        severity=severity,
        confidence=_confidence_for(status, delay_source),
        observed_at=observed_at,
        effective_at=effective_at,
        metrics=metrics,
        evidence_ids=[evidence_id],
    )


def _summary_signal(
    *,
    location: base.Location,
    travel_date: datetime.date,
    observed_at: str,
    flights: Sequence[Mapping[str, Any]],
    delays: Sequence[Optional[int]],
    evidence_ids: Sequence[str],
    unmeasured: int = 0,
    sample: Mapping[str, Any],
) -> base.SignalEntry:
    """The explicit "checked, no disruption identified" summary signal.

    Emitted only when flights *were* returned and none of them is disruptive, so the
    fusion layer can tell a real "they ran on time" apart from ``unavailable``. A
    flight that carried neither a status nor a usable delay is counted as
    ``unmeasured`` instead of being silently declared fine.

    This is the one aviation signal that is an *inference over a cohort*, so it is the
    one whose confidence is discounted for a thin or partial sample - see
    :func:`_summary_confidence`. The sentence says so too, so a human reader is not
    misled by a 9-flight clear reading on a 173-flight board.
    """
    route = "{0} -> {1}".format(location.origin.iata, location.destination.iata)
    measured = [delay for delay in delays if delay is not None]
    largest = max(measured) if measured else None
    largest_text = (
        "no delay was reported for any of them"
        if largest is None
        else "the largest reported delay was {0} min".format(largest)
    )
    caveat = (
        ""
        if not unmeasured
        else (
            " {0} returned flight(s) carried neither a status nor a comparable delay, "
            "so nothing is claimed about them."
        ).format(unmeasured)
    )
    total_available = sample.get("total_available")
    if total_available is not None and int(total_available) > len(flights):
        sample_note = (
            " The route board reports {0} flight(s) and {1} were received; only {2} of "
            "them are dated {3}, so this rests on a thin sample and its confidence is "
            "discounted."
        ).format(
            total_available,
            sample.get("returned_records"),
            len(flights),
            travel_date.isoformat(),
        )
    elif sample.get("sample_is_thin"):
        sample_note = (
            " Only {0} flight(s) are dated {1}, which is a thin sample, so its "
            "confidence is discounted."
        ).format(len(flights), travel_date.isoformat())
    else:
        sample_note = ""
    return base.make_signal(
        _SIGNAL_KIND_SUMMARY,
        (
            "No disruption identified for {0} on {1}: Aviationstack returned {2} "
            "flight(s) and none is cancelled, diverted or delayed by {3} min or more "
            "({4}).{5}{6} This is a checked result, not missing data."
        ).format(
            route,
            travel_date.isoformat(),
            len(flights),
            _DELAY_FLOOR_MINUTES,
            largest_text,
            caveat,
            sample_note,
        ),
        severity="low",
        confidence=_summary_confidence(sample),
        observed_at=observed_at,
        metrics={
            "route": route,
            "travel_date": travel_date.isoformat(),
            "flights_assessed": len(flights),
            "max_delay_minutes": largest,
            "delay_floor_minutes": _DELAY_FLOOR_MINUTES,
            "unmeasured_flights": unmeasured,
            "disruptive_flights": 0,
            **sample,
        },
        evidence_ids=list(evidence_ids),
    )


# --------------------------------------------------------------------------- #
# The one public entry point
# --------------------------------------------------------------------------- #
_FLIGHTS_PATH: str = "/flights"


def _flights_url() -> str:
    """The ``/flights`` endpoint under the configured base URL (no key in the URL)."""
    return "{0}{1}".format(config.AVIATIONSTACK_BASE_URL.rstrip("/"), _FLIGHTS_PATH)


async def fetch_signals(
    location: base.Location, date: datetime.date
) -> base.NormalizedSignals:
    """Fetch Aviationstack flight-status signals for a route and a travel date.

    Returns ``ok`` with signals - disruptive flights individually, plus an explicit
    "no disruption identified" summary when flights were returned and none is
    disruptive - or ``unavailable``/``error`` with a machine-readable code and the raw
    response on disk. ``plan_limit`` is an ``unavailable`` state: the free tier cannot
    see schedules for that date, which is a coverage gap and never "no disruption".

    Nothing the vendor does can raise out of this function; only a programming error
    in the arguments raises ``ValueError``, matching ``base.get_json``.
    """
    if isinstance(date, datetime.datetime):
        travel_date = date.date()
    elif isinstance(date, datetime.date):
        travel_date = date
    else:
        raise ValueError("fetch_signals requires a datetime.date travel date")
    if not isinstance(location, base.Location):
        raise ValueError("fetch_signals requires a data_sources.base.Location route")

    blocked = base.missing_credential_signals(
        config.SOURCE_AVIATIONSTACK, location=location, travel_date=travel_date
    )
    if blocked is not None:
        return blocked

    travel_date_is_today = travel_date == base.utc_now().date()
    observed_at = base.utc_now_iso()
    params: Dict[str, Any] = {
        "access_key": config.AVIATIONSTACK_API_KEY,
        "dep_iata": location.origin.iata,
        "arr_iata": location.destination.iata,
        "limit": _FREE_TIER_LIMIT,
    }
    if not travel_date_is_today:
        # `flight_date` belongs to the paid "flight schedules" features. It is only
        # sent when it is needed, so a free key is not blocked on today's query - and
        # when it *is* refused, that refusal is the finding we report.
        params["flight_date"] = travel_date.isoformat()

    result: Optional[base.HttpResult] = None
    try:
        result = await base.get_json(
            config.SOURCE_AVIATIONSTACK,
            _flights_url(),
            params=params,
            context={
                "endpoint": "flights",
                "route": "{0}-{1}".format(location.origin.iata, location.destination.iata),
                "travel_date": travel_date.isoformat(),
                "plan": "free tier (100 requests/month, real-time only)",
            },
        )
        return _envelope_for_result(
            result,
            location=location,
            travel_date=travel_date,
            travel_date_is_today=travel_date_is_today,
            observed_at=observed_at,
        )
    except Exception as exc:  # rule 2: nothing escapes a data source module
        return base.failure_signals(
            source=config.SOURCE_AVIATIONSTACK,
            error_code=base.ErrorCode.UNEXPECTED_ERROR,
            error="Unexpected {0} while processing Aviationstack data: {1}".format(
                type(exc).__name__, base.redact_text(str(exc))
            ),
            location=location,
            travel_date=travel_date,
            raw_response_path=result.raw_path if result is not None else None,
            raw_response_paths=list(result.raw_paths) if result is not None else (),
            latency_ms=result.latency_ms if result is not None else None,
            warnings=[
                "This is a bug in this module, not an Aviationstack outage.",
                "The raw response on disk is the ground truth for this query.",
            ],
        )


def _envelope_for_result(
    result: base.HttpResult,
    *,
    location: base.Location,
    travel_date: datetime.date,
    travel_date_is_today: bool,
    observed_at: str,
) -> base.NormalizedSignals:
    """Turn one HTTP result into a well-formed envelope.

    Handles every outcome of the single request: a success with flights, a success
    with no rows, a 2xx body that is actually a vendor error, and a failed call. Split
    out of :func:`fetch_signals` so each branch is testable and so the request and the
    interpretation stay visibly separate.
    """
    raw_paths = list(result.raw_paths)

    # A failure response, or an error object delivered with a 2xx status, is
    # classified before anything is read as flight data.
    if not result.ok or _vendor_error(result.payload)[0] is not None:
        return _failure_envelope(
            result,
            location=location,
            travel_date=travel_date,
            travel_date_is_today=travel_date_is_today,
        )

    payload = _mapping(result.payload)
    data = payload.get("data")
    if not isinstance(data, list):
        return base.failure_signals(
            source=config.SOURCE_AVIATIONSTACK,
            error_code=base.ErrorCode.UNEXPECTED_PAYLOAD_SHAPE,
            error=(
                "Aviationstack returned a body with no 'data' array (top-level keys: "
                "{0}), so no flight could be read. The raw payload is on disk for "
                "inspection."
            ).format(", ".join(sorted(payload)) or "none"),
            location=location,
            travel_date=travel_date,
            raw_response_path=result.raw_path,
            raw_response_paths=raw_paths,
            latency_ms=result.latency_ms,
        )

    records = [item for item in data if isinstance(item, Mapping)]
    matched = [
        flight for flight in records if _matches_route(flight, location, travel_date)
    ]
    if not matched:
        return _unavailable_for_no_flights(
            location=location,
            travel_date=travel_date,
            travel_date_is_today=travel_date_is_today,
            returned=len(records),
            raw_path=result.raw_path,
            raw_paths=raw_paths,
            latency_ms=result.latency_ms,
        )

    # The cohort every signal from this call was drawn from. Read once, before the loop,
    # so the per-flight signals and the summary share one sample description.
    total = _as_int(_mapping(payload.get("pagination")).get("total"))
    sample = _sample_metrics(
        len(matched), returned_records=len(records), total_available=total
    )

    evidence: List[base.EvidenceEntry] = []
    signals: List[base.SignalEntry] = []
    delays: List[Optional[int]] = []
    for index, flight in enumerate(matched):
        evidence_id = "ev_flight_{0}".format(index + 1)
        delays.append(_flight_delay(flight)[0])
        evidence.append(
            _flight_evidence(
                flight,
                index=index,
                travel_date=travel_date,
                observed_at=observed_at,
                raw_path=result.raw_path,
            )
        )
        signal = _flight_signal(
            flight,
            location=location,
            travel_date=travel_date,
            observed_at=observed_at,
            evidence_id=evidence_id,
            sample=sample,
        )
        if signal is not None:
            signals.append(signal)

    warnings: List[str] = []
    unmeasured = sum(
        1
        for flight in matched
        if _flight_delay(flight)[0] is None
        and _text(flight.get("flight_status")) is None
    )
    if unmeasured:
        warnings.append(
            "{0} of {1} returned flight(s) carried neither a flight_status nor a "
            "comparable delay, so they are counted but not assessed.".format(
                unmeasured, len(matched)
            )
        )
    # `total` and `sample` were read before the signal loop; the warning below quotes the
    # same number the sample fields carry.
    if total is not None and total > len(records):
        warnings.append(
            "Aviationstack reports {0} flight(s) for this query but returned {1}. "
            "Further pages were not requested because each page costs one of the free "
            "tier's 100 monthly requests, so these signals describe the returned page "
            "only.".format(total, len(records))
        )
    if travel_date_is_today:
        warnings.append(
            "Queried today's real-time board without the paid 'flight_date' filter, "
            "which is what a free-tier key can serve."
        )

    if not signals:
        signals.append(
            _summary_signal(
                location=location,
                travel_date=travel_date,
                observed_at=observed_at,
                flights=matched,
                delays=delays,
                evidence_ids=[entry["id"] for entry in evidence],
                unmeasured=unmeasured,
                sample=sample,
            )
        )

    return base.ok_signals(
        source=config.SOURCE_AVIATIONSTACK,
        signals=signals,
        location=location,
        travel_date=travel_date,
        evidence=evidence,
        raw_response_path=result.raw_path,
        raw_response_paths=raw_paths,
        latency_ms=result.latency_ms,
        warnings=warnings,
        retrieved_at=observed_at,
    )


# Rule 4: exactly one public entry point per source module.
__all__ = ["fetch_signals"]







