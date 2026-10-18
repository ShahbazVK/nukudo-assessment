"""NWS (National Weather Service) weather signals for a route and a travel date.

This is a *source* module: it exposes exactly one public function (.clinerules
rule 4)::

    async def fetch_signals(location: Location, date: datetime.date) -> NormalizedSignals

Everything else here is private, and every value it reports comes from NWS. The
API is keyless, but it rejects requests without a descriptive ``User-Agent``
naming the app and a contact address (``config.NWS_USER_AGENT``); a blank one is
reported as ``missing_credential`` rather than sent unauthenticated.

What one call does
------------------
For each airport on the route: three requests, one at a time, no bulk polling.

1. ``GET {NWS_BASE_URL}/points/{lat},{lon}`` - resolves the NWS grid for the
   airport and returns the resource URLs to follow, plus the grid's timezone.
   Coordinates come from the static table in ``iata_lookup``; an unsupported or
   non-US code returns ``unavailable``/``no_data_for_query``, because guessing a
   location is not an option.
2. ``GET <properties.forecast>`` - that grid's forecast as day/night periods. NWS
   advertises two gridpoint resources in the same payload: ``forecast`` (day/night
   periods) and ``forecastGridData`` (raw time-series grids, no periods). The
   periods resource is the one followed, because a travel *date* maps onto
   periods; a later refinement can read ``forecastGridData`` for sub-period detail
   (gusts, visibility, freezing-rain probability) without changing this contract.
3. ``GET {NWS_BASE_URL}/alerts/active?point={lat},{lon}`` - active watches,
   warnings and advisories: the highest-signal disruption data NWS publishes.

Both URLs handed back by ``/points`` are checked to be HTTPS on the
``NWS_BASE_URL`` host before they are followed, so a tampered payload cannot point
this module at another host.

Travel date -> periods
----------------------
NWS periods are day/night pairs, so a travel date normally overlaps three of them:
the preceding night, the day itself, and the following night. Every overlapping
period becomes a signal that names the period it represents and carries that
period's own timestamps. Outside the published window (NWS goes roughly seven days
ahead) the answer is ``unavailable``/``no_data_for_query``: this module never
extrapolates a forecast, and never treats a past date as "clear".

Severity mapping (deterministic, documented, unit-tested)
---------------------------------------------------------
*Alerts*: NWS ``severity``/``urgency``/``certainty`` are weighted into a score
that is banded onto ``config.RISK_LEVELS`` (``_ALERT_*_WEIGHTS``,
``_ALERT_SCORE_BANDS``).

*Forecast*: the period's own words and numbers - ``shortForecast`` keywords,
maximum wind, precipitation probability, temperature - set a floor severity, and
the most severe reading wins (``_FORECAST_TEXT_RULES`` and the band tables).

``confidence`` is a *stated heuristic, not a calibrated probability*: alert
signals use NWS's own ``certainty``, forecast signals are capped because mapping
prose onto a disruption level is our inference, and the "nothing found" summary
uses one fixed value (``_FORECAST_CONFIDENCE``, ``_SUMMARY_CONFIDENCE``).

Deliberate design decisions
---------------------------
* **No event-type filtering, no dropped alerts.** A lower-precision signal beats a
  silently discarded one, so every active alert is mapped by the same rule and
  reported, with a flag saying whether its window overlaps the travel date.
* **"Checked, nothing found" is a result.** No alerts plus benign periods returns
  ``ok`` with an explicit summary signal, which the fusion layer can tell apart
  from ``unavailable`` ("could not check").
* **An incomplete route is reported as incomplete.** Each airport is fetched
  independently; if one cannot be read, the airports that succeeded are still
  reported - as ``partial``, with one ``error_code`` per failed airport. Only when
  *no* airport on the route can be read does the source return a whole-source
  ``error``/``unavailable`` envelope. A half-covered route is therefore never
  presented as ``ok``, and a single endpoint's hiccup does not throw away the other
  endpoint's real signal. Raw responses stay in ``logs/raw/`` either way.
"""

from __future__ import annotations

import dataclasses
import datetime
import re
from typing import Any, Dict, List, Mapping, NoReturn, Optional, Sequence, Tuple
from urllib.parse import urlsplit

import config
from data_sources import base, iata_lookup

__all__ = ["fetch_signals"]


# --------------------------------------------------------------------------- #
# Alert severity mapping: NWS severity/urgency/certainty -> config.RISK_LEVELS
# --------------------------------------------------------------------------- #
# Weights are a documented judgement call: `severity` says how bad the phenomenon
# is, `urgency` says how soon it applies, `certainty` says how sure NWS is. Summing
# them means no single missing field can hide a severe event.
_ALERT_SEVERITY_WEIGHTS: Dict[str, float] = {
    "extreme": 4.0,
    "severe": 3.0,
    "moderate": 2.0,
    "minor": 1.0,
    "unknown": 0.0,
}
_ALERT_URGENCY_WEIGHTS: Dict[str, float] = {
    "immediate": 2.0,
    "expected": 1.0,
    "future": 0.0,
    "past": 0.0,
    "unknown": 0.0,
}
_ALERT_CERTAINTY_WEIGHTS: Dict[str, float] = {
    "observed": 1.5,
    "likely": 1.0,
    "possible": 0.5,
    "unlikely": 0.0,
    "unknown": 0.0,
}
# Score -> risk level. Checked against real NWS field combinations:
#   Extreme/Immediate/Observed (7.5) -> severe
#   Severe/Immediate/Observed  (6.5) -> severe   (an observed flood warning)
#   Severe/Expected/Likely     (5.0) -> high     (a high wind warning)
#   Moderate/Expected/Likely   (4.0) -> moderate (a wind advisory)
#   Severe/Possible/Future     (3.5) -> moderate (a watch, not a warning)
#   Minor/Likely/Expected      (3.0) -> low      (a coastal flood advisory)
#   unset/unknown/unknown      (0.0) -> unknown  (never reported as "no risk")
_ALERT_SCORE_BANDS: Tuple[Tuple[float, str], ...] = (
    (6.0, "severe"),
    (4.5, "high"),
    (3.5, "moderate"),
    (1.0, "low"),
    (0.0, "unknown"),
)
# NWS `certainty` is the closest thing to a confidence statement in the payload,
# so it is the one field mapped straight onto our signal confidence.
_ALERT_CERTAINTY_CONFIDENCE: Dict[str, float] = {
    "observed": 0.95,
    "likely": 0.9,
    "possible": 0.6,
    "unlikely": 0.4,
    "unknown": 0.5,
}


# --------------------------------------------------------------------------- #
# Forecast mapping: the period's own words and numbers -> config.RISK_LEVELS
# --------------------------------------------------------------------------- #
# Matched against the lowercased `shortForecast`, most specific phrase first; the
# first match sets a floor severity. The phrases are NWS's own, e.g.
# "Showers And Thunderstorms", "Chance Light Rain then Mostly Cloudy", "Mostly
# Sunny". `breezy`, `sunny`, `cloudy` and friends map to no floor at all.
_FORECAST_TEXT_RULES: Tuple[Tuple[str, str], ...] = (
    ("blizzard", "high"),
    ("heavy snow", "high"),
    ("ice storm", "high"),
    ("freezing rain", "high"),
    ("sleet", "high"),
    ("severe thunderstorm", "high"),
    ("squall", "high"),
    ("thunderstorm", "moderate"),
    ("heavy rain", "moderate"),
    ("snow", "moderate"),
    ("freezing", "moderate"),
    ("fog", "moderate"),
    ("hail", "moderate"),
    ("windy", "moderate"),
    ("wintry mix", "moderate"),
    ("rain", "low"),
    ("showers", "low"),
    ("drizzle", "low"),
    ("mist", "low"),
    ("smoke", "low"),
    ("haze", "low"),
)
# (threshold, floor severity) pairs, applied to the highest value published for the
# period. Wind uses the upper bound of a range ("13 to 21 mph" -> 21 mph), and the
# bands are set where aviation disruption actually starts: 25 mph (~22 kt) is where
# crosswind and flow programs begin to bite, 40 mph (~35 kt) is where ground stops
# and cancellations become likely. A merely breezy day stays out of the moderate band.
_FORECAST_WIND_RULES: Tuple[Tuple[float, str], ...] = (
    (40.0, "high"),
    (25.0, "moderate"),
)
_FORECAST_POP_RULES: Tuple[Tuple[float, str], ...] = (
    (70.0, "moderate"),
    (40.0, "low"),
)
_FORECAST_COLD_RULES: Tuple[Tuple[float, str], ...] = (
    (0.0, "high"),
    (15.0, "moderate"),
)
_FORECAST_HEAT_RULES: Tuple[Tuple[float, str], ...] = ((100.0, "moderate"),)

# Stated confidences (heuristics, not calibrated probabilities - see docstring).
_FORECAST_CONFIDENCE: float = 0.7
_SUMMARY_CONFIDENCE: float = 0.8

# `unknown` ranks below `low`, so a real risk level always wins a comparison.
_RISK_ORDER: Dict[str, int] = {"low": 0, "moderate": 1, "high": 2, "severe": 3}
_UNKNOWN_RISK: str = "unknown"

_SIGNAL_KIND_ALERT: str = "weather_alert"
_SIGNAL_KIND_FORECAST: str = "weather_forecast"
_SIGNAL_KIND_SUMMARY: str = "weather_summary"

# Only gridpoint resources under the configured host are ever followed.
_GRID_PATH_PREFIX: str = "/gridpoints/"
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_ALERT_SNIPPET_CHARS: int = 240
_AREA_SNIPPET_CHARS: int = 200


# --------------------------------------------------------------------------- #
# Request plumbing
# --------------------------------------------------------------------------- #
def _nws_headers() -> Dict[str, str]:
    """Headers NWS asks for: a descriptive User-Agent and a geo+json Accept."""
    return {"User-Agent": config.NWS_USER_AGENT, "Accept": "application/geo+json"}


def _has_user_agent() -> bool:
    """Whether a descriptive User-Agent is set (NWS answers 403 without one)."""
    agent = config.NWS_USER_AGENT
    return isinstance(agent, str) and bool(agent.strip())


def _trusted_nws_url(url: Any) -> Optional[str]:
    """Return ``url`` when it is an HTTPS gridpoint resource on the NWS host.

    ``/points`` hands back absolute URLs to follow, so following one unchecked
    would let that payload decide which host this module talks to. Scheme, host and
    path prefix are all validated before anything is requested.
    """
    if not isinstance(url, str) or not url.strip():
        return None
    candidate = urlsplit(url.strip())
    allowed = urlsplit(config.NWS_BASE_URL)
    if candidate.scheme != "https" or candidate.netloc != allowed.netloc:
        return None
    if not candidate.path.startswith(_GRID_PATH_PREFIX):
        return None
    return url.strip()


def _points_url(info: iata_lookup.AirportInfo) -> str:
    """``/points/{lat},{lon}`` for one airport, from the audited coordinate table."""
    return "{0}/points/{1}".format(config.NWS_BASE_URL.rstrip("/"), info.coordinates)


def _alerts_url() -> str:
    """``/alerts/active`` - the active-alert collection (filtered by point below)."""
    return "{0}/alerts/active".format(config.NWS_BASE_URL.rstrip("/"))


# --------------------------------------------------------------------------- #
# Value helpers - NWS fields are optional and may arrive as the wrong type
# --------------------------------------------------------------------------- #
def _text(value: Any) -> Optional[str]:
    """A non-empty stripped string, or ``None`` when the value is unusable."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _parse_iso_datetime(value: Any) -> Optional[datetime.datetime]:
    """Parse an NWS timestamp (ISO 8601 with an offset), or return ``None``."""
    raw = _text(value)
    if raw is None:
        return None
    if raw.endswith(("Z", "z")):  # fromisoformat() on 3.9 wants a numeric offset
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None


def _short_local(timestamp: Any) -> str:
    """``2026-10-17T06:00:00-04:00`` -> ``2026-10-17 06:00 -04:00``, for prose."""
    raw = _text(timestamp)
    if raw is None or len(raw) < 16:
        return "unknown time"
    offset = raw[19:] if len(raw) > 19 else ""
    return "{0} {1} {2}".format(raw[:10], raw[11:16], offset).strip()


def _max_number(value: Any) -> Optional[float]:
    """Largest number in a value such as ``"13 to 21 mph"`` (the range's top end)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    raw = _text(value)
    if raw is None:
        return None
    numbers = [float(match) for match in _NUMBER.findall(raw)]
    return max(numbers) if numbers else None


def _severity_rank(severity: Optional[str]) -> int:
    """Comparable rank for a risk level; ``unknown``/``None`` rank below ``low``."""
    if severity is None:
        return -1
    return _RISK_ORDER.get(severity, -1)


def _most_severe(current: str, candidate: str) -> str:
    """The more severe of two risk levels (``unknown`` never beats a real level)."""
    if _severity_rank(candidate) > _severity_rank(current):
        return candidate
    return current


def _apply_threshold_rules(
    value: Optional[float],
    rules: Sequence[Tuple[float, str]],
    current: str,
    *,
    label: str,
    unit: str,
    fires_below: bool = False,
) -> Tuple[str, Optional[str]]:
    """Apply ``(threshold, floor)`` rules to ``value``; return (severity, reason).

    Rules are ordered most severe first, so the first match is the strongest floor
    the value meets. ``fires_below`` flips the comparison for cold-temperature
    bands. The reason string always quotes the measured value and the threshold, so
    a mapped severity can be re-derived from the evidence.
    """
    if value is None:
        return current, None
    for threshold, floor in rules:
        met = value <= threshold if fires_below else value >= threshold
        if not met:
            continue
        comparison = "at or below" if fires_below else "at or above"
        raised = _most_severe(current, floor)
        if raised == current:
            return current, (
                "{0} {1:g}{2} is {3} the {4:g}{2} threshold, which does not raise the "
                "level above {5}"
            ).format(label, value, unit, comparison, threshold, current)
        return raised, "{0} {1:g}{2} is {3} the {4:g}{2} threshold -> at least {5}".format(
            label, value, unit, comparison, threshold, floor
        )
    # Below (or above) every band: recorded anyway, so a mapped level can always be
    # re-derived from the reasons instead of having to be trusted.
    thresholds = [threshold for threshold, _ in rules]
    if not thresholds:
        return current, None
    if fires_below:
        return current, (
            "{0} {1:g}{2} is above every configured threshold (highest {3:g}{2})"
        ).format(label, value, unit, max(thresholds))
    return current, "{0} {1:g}{2} is below every configured threshold (lowest {3:g}{2})".format(
        label, value, unit, min(thresholds)
    )


# --------------------------------------------------------------------------- #
# Alerts: read a FeatureCollection and map each alert onto our vocabulary
# --------------------------------------------------------------------------- #
def _alert_properties(payload: Any) -> Optional[List[Mapping[str, Any]]]:
    """The ``properties`` blocks of an alert FeatureCollection, or ``None``.

    ``None`` means the payload is not the shape NWS documents for this endpoint
    (for example its ``{"parameterErrors": ...}`` error document), so the caller can
    report ``unexpected_payload_shape`` instead of reading into it.
    """
    if not isinstance(payload, Mapping):
        return None
    features = payload.get("features")
    if not isinstance(features, list):
        return None
    alerts: List[Mapping[str, Any]] = []
    for feature in features:
        if not isinstance(feature, Mapping):
            continue
        props = feature.get("properties")
        if isinstance(props, Mapping):
            alerts.append(props)
    return alerts


def _alert_severity(props: Mapping[str, Any]) -> Tuple[str, float, List[str]]:
    """Map one alert onto ``(risk_level, score, reasons)``.

    A field value outside NWS's documented vocabulary is counted as ``unknown`` and
    recorded in the reasons, so a new vocabulary value degrades visibly instead of
    being silently dropped or misread.
    """
    total = 0.0
    reasons: List[str] = []
    for field, weights in (
        ("severity", _ALERT_SEVERITY_WEIGHTS),
        ("urgency", _ALERT_URGENCY_WEIGHTS),
        ("certainty", _ALERT_CERTAINTY_WEIGHTS),
    ):
        raw = _text(props.get(field))
        key = raw.lower() if raw else "unknown"
        if key not in weights:
            reasons.append(
                "{0} {1!r} is outside the NWS vocabulary; counted as unknown".format(
                    field, raw
                )
            )
            key = "unknown"
        total += weights[key]
        reasons.append("{0}={1} (+{2:g})".format(field, raw or "unset", weights[key]))

    severity = _UNKNOWN_RISK
    for floor, level in _ALERT_SCORE_BANDS:
        if total >= floor:
            severity = level
            break
    reasons.append("score {0:g} -> {1}".format(total, severity))
    return severity, total, reasons


def _alert_confidence(props: Mapping[str, Any]) -> float:
    """NWS ``certainty`` as our signal confidence (a stated, documented mapping)."""
    key = (_text(props.get("certainty")) or "unknown").lower()
    return _ALERT_CERTAINTY_CONFIDENCE.get(key, _ALERT_CERTAINTY_CONFIDENCE["unknown"])


def _alert_window(
    props: Mapping[str, Any],
) -> Tuple[Optional[datetime.date], Optional[datetime.date]]:
    """Local start/end dates of an alert, falling back to effective/expires."""
    onset = _parse_iso_datetime(props.get("onset")) or _parse_iso_datetime(
        props.get("effective")
    )
    ends = _parse_iso_datetime(props.get("ends")) or _parse_iso_datetime(
        props.get("expires")
    )
    return (
        onset.date() if onset is not None else None,
        ends.date() if ends is not None else None,
    )


def _alert_overlap(
    props: Mapping[str, Any], travel_date: datetime.date
) -> Optional[bool]:
    """Whether the alert's window includes the travel date, or ``None`` if unknown."""
    start, end = _alert_window(props)
    if start is None and end is None:
        return None
    if start is not None and start > travel_date:
        return False
    if end is not None and end < travel_date:
        return False
    return True


# --------------------------------------------------------------------------- #
# Forecast: pick the periods overlapping the travel date, then map each one
# --------------------------------------------------------------------------- #
def _period_bounds(
    period: Mapping[str, Any],
) -> Optional[Tuple[datetime.date, datetime.date]]:
    """Local start/end dates of one forecast period, or ``None`` when unusable."""
    start = _parse_iso_datetime(period.get("startTime"))
    end = _parse_iso_datetime(period.get("endTime"))
    if start is None or end is None:
        return None
    return start.date(), end.date()


def _overlaps_travel_date(period: Mapping[str, Any], travel_date: datetime.date) -> bool:
    """Whether one period's published window covers the travel date.

    NWS timestamps carry the grid's own UTC offset, so the date parts are compared
    exactly as published: a "Tonight" period that runs into 06:00 on the travel date
    does overlap it, and is labelled as such in the signal it produces.
    """
    bounds = _period_bounds(period)
    if bounds is None:
        return False
    start_date, end_date = bounds
    return start_date <= travel_date <= end_date


def _forecast_severity(
    period: Mapping[str, Any],
) -> Tuple[str, List[str], Dict[str, Any]]:
    """Map one forecast period onto ``(risk_level, reasons, metrics)``.

    Everything is read from the period itself: its own ``shortForecast`` wording,
    its wind string (only when it says mph) and its ``temperatureUnit`` (only when
    it says F). A value that cannot be read that way is reported in the reasons
    instead of being converted, and nothing is estimated.
    """
    severity = "low"
    reasons: List[str] = []
    metrics: Dict[str, Any] = {}

    phrase = _text(period.get("shortForecast"))
    metrics["short_forecast"] = phrase
    if phrase is None:
        reasons.append("this period carries no shortForecast text")
    else:
        lowered = phrase.lower()
        for keyword, floor in _FORECAST_TEXT_RULES:
            if keyword in lowered:
                severity = _most_severe(severity, floor)
                reasons.append(
                    "forecast text mentions {0!r} -> at least {1}".format(keyword, floor)
                )
                break
        else:
            reasons.append("forecast text mentions no disruptive condition")

    wind = _text(period.get("windSpeed"))
    metrics["wind_speed"] = wind
    if wind is None:
        reasons.append("this period publishes no wind speed")
    elif "mph" in wind.lower():
        wind_max = _max_number(wind)
        metrics["wind_max_mph"] = wind_max
        severity, reason = _apply_threshold_rules(
            wind_max, _FORECAST_WIND_RULES, severity, label="wind up to", unit=" mph"
        )
        if reason is not None:
            reasons.append(reason)
    else:
        metrics["wind_max_mph"] = None
        reasons.append(
            "wind speed {0!r} is not in mph, so no wind band was applied".format(wind)
        )

    precipitation = period.get("probabilityOfPrecipitation")
    probability = (
        precipitation.get("value") if isinstance(precipitation, Mapping) else None
    )
    probability_value = _max_number(probability)
    metrics["precipitation_probability"] = probability_value
    if probability_value is not None:
        severity, reason = _apply_threshold_rules(
            probability_value,
            _FORECAST_POP_RULES,
            severity,
            label="precipitation probability of",
            unit="%",
        )
        if reason is not None:
            reasons.append(reason)

    temperature = _max_number(period.get("temperature"))
    unit = _text(period.get("temperatureUnit"))
    metrics["temperature"] = temperature
    metrics["temperature_unit"] = unit
    if temperature is None:
        reasons.append("this period publishes no temperature")
    elif unit == "F":
        severity, reason = _apply_threshold_rules(
            temperature,
            _FORECAST_COLD_RULES,
            severity,
            label="temperature of",
            unit=" F",
            fires_below=True,
        )
        if reason is not None:
            reasons.append(reason)
        severity, reason = _apply_threshold_rules(
            temperature,
            _FORECAST_HEAT_RULES,
            severity,
            label="temperature of",
            unit=" F",
        )
        if reason is not None:
            reasons.append(reason)
    else:
        reasons.append(
            "temperature unit {0!r} is not F, so no temperature band was applied".format(
                unit
            )
        )

    reasons.append("period assessed at {0}".format(severity))
    return severity, reasons, metrics


# --------------------------------------------------------------------------- #
# What a fetch collects, and the envelopes it can end with
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class _AlertFinding:
    """One active alert as seen from one airport, already parsed and evidenced."""

    airport: str
    props: Mapping[str, Any]
    evidence_id: str


@dataclasses.dataclass(frozen=True)
class _PeriodFinding:
    """One forecast period overlapping the travel date, already evidenced."""

    airport: str
    period: Mapping[str, Any]
    evidence_id: str


@dataclasses.dataclass(frozen=True)
class _AirportWeather:
    """Everything one airport contributed, all of it already parsed and evidenced.

    ``period_window`` is the span that airport's forecast document covers, kept so a
    travel date outside it can be reported against the window it fell outside of.
    """

    info: iata_lookup.AirportInfo
    grid: str
    timezone: Optional[str]
    periods: List[_PeriodFinding]
    period_window: Tuple[Optional[datetime.date], Optional[datetime.date]]
    alerts: List[_AlertFinding]
    evidence: List[base.EvidenceEntry]
    latency_ms: float


@dataclasses.dataclass
class _AirportFailure:
    """One airport that could not be read, kept so the rest of the route survives.

    ``status`` and ``error_code`` come straight from the envelope the failed fetch
    produced, so a route-level result never re-invents a reason for a gap.
    """

    iata: str
    error_code: str
    error: str
    status: str


def _failure_from_envelope(
    info: iata_lookup.AirportInfo, envelope: base.NormalizedSignals
) -> _AirportFailure:
    """Turn one airport's abort envelope into a route-level failure record."""
    return _AirportFailure(
        iata=info.iata,
        error_code=envelope["error_code"] or base.ErrorCode.UNEXPECTED_ERROR,
        error=envelope["error"] or "NWS returned no reason for this failure.",
        status=envelope["status"],
    )


def _primary_failure(failures: Sequence[_AirportFailure]) -> _AirportFailure:
    """The failure that decides the whole-source result when every airport failed.

    A genuine outage outranks "no data for this date": reporting the milder of two
    failures would hide a real problem behind a coverage note.
    """
    for failure in failures:
        if failure.status == config.STATUS_ERROR:
            return failure
    return failures[0]


def _failures_message(failures: Sequence[_AirportFailure]) -> str:
    """One message naming every failed airport, or the single failure's own text."""
    if len(failures) == 1:
        return failures[0].error
    return "{0} No airport on this route could be read, so nothing is reported as partial.".format(
        " ".join(
            "{0}: {1} ({2})".format(failure.iata, failure.error, failure.error_code)
            for failure in failures
        )
    )


class _SourceAbort(Exception):
    """Internal control flow: this call must end with a non-``ok`` envelope."""

    def __init__(self, envelope: base.NormalizedSignals) -> None:
        super().__init__(envelope.get("error") or "nws source aborted")
        self.envelope = envelope


def _remember(paths: List[str], result: base.HttpResult) -> None:
    """Record every raw evidence path a request produced (one per attempt)."""
    for path in result.raw_paths:
        if path and path not in paths:
            paths.append(path)


def _evidence_id(airport: str, index: int) -> str:
    """Readable, stable evidence id (``ev_JFK_1``) assigned before the envelope."""
    return "ev_{0}_{1}".format(airport, index)


def _abort(
    *,
    error_code: str,
    error: str,
    location: base.Location,
    travel_date: datetime.date,
    raw_paths: Sequence[str],
    latency_ms: Optional[float] = None,
    warnings: Sequence[str] = (),
    status: Optional[str] = None,
    raw_response_path: Optional[str] = None,
) -> NoReturn:
    """End this call with an explicit non-``ok`` envelope, carrying the evidence.

    ``status`` defaults to whatever ``base`` derives from the code; pass
    ``config.STATUS_UNAVAILABLE`` for "the source worked, but this query has no
    data" (a date outside the forecast window, an unsupported airport).
    """
    paths = list(raw_paths)
    latest = (
        raw_response_path
        if raw_response_path is not None
        else (paths[-1] if paths else None)
    )
    raise _SourceAbort(
        base.failure_signals(
            source=config.SOURCE_NWS,
            error_code=error_code,
            error=error,
            location=location,
            travel_date=travel_date,
            raw_response_path=latest,
            raw_response_paths=paths,
            latency_ms=latency_ms,
            warnings=list(warnings),
            status=status,
        )
    )


# --------------------------------------------------------------------------- #
# Signal and evidence builders
# --------------------------------------------------------------------------- #
def _evidence(
    *,
    field: str,
    value: Any,
    observed_at: str,
    note: str,
    evidence_id: str,
    raw_path: Optional[str] = None,
) -> base.EvidenceEntry:
    """One evidence entry, pointing at the raw record the value came from."""
    return base.make_evidence(
        config.SOURCE_NWS,
        field=field,
        value=value,
        observed_at=observed_at,
        raw_response_path=raw_path,
        note=note,
        evidence_id=evidence_id,
    )


def _alert_signal(
    findings: Sequence[_AlertFinding],
    travel_date: datetime.date,
    observed_at: str,
) -> base.SignalEntry:
    """Build the signal for one alert, citing every airport's evidence record.

    The same alert can be active at both airports; it then becomes one signal whose
    ``airports`` metric lists both, so a route-level alert is never double counted.
    """
    props = findings[0].props
    airports = sorted({finding.airport for finding in findings})
    evidence_ids = [finding.evidence_id for finding in findings]

    severity, score, reasons = _alert_severity(props)
    event = _text(props.get("event")) or "Unnamed NWS alert"
    onset = _text(props.get("onset")) or _text(props.get("effective"))
    ends = _text(props.get("ends")) or _text(props.get("expires"))
    overlap = _alert_overlap(props, travel_date)
    if overlap is True:
        overlap_note = "its window includes the travel date"
    elif overlap is False:
        overlap_note = (
            "its window does not overlap the travel date; it is reported because it is "
            "active at the airport"
        )
    else:
        overlap_note = (
            "it publishes no onset/ends, so its overlap with the travel date is unknown"
        )

    detail = (
        _text(props.get("headline"))
        or _text(props.get("description"))
        or "no description published"
    )
    summary = (
        "{0} at {1}: {2} Window {3} to {4}, so {5}. Mapped to risk level '{6}' from "
        "NWS severity/urgency/certainty."
    ).format(
        event,
        "/".join(airports),
        base.truncate_text(detail, _ALERT_SNIPPET_CHARS),
        _short_local(onset),
        _short_local(ends),
        overlap_note,
        severity,
    )

    return base.make_signal(
        _SIGNAL_KIND_ALERT,
        summary,
        severity=severity,
        confidence=_alert_confidence(props),
        observed_at=observed_at,
        effective_at=onset,
        metrics={
            "airports": airports,
            "alert_id": _text(props.get("id")),
            "event": event,
            "category": _text(props.get("category")),
            "nws_severity": _text(props.get("severity")),
            "nws_urgency": _text(props.get("urgency")),
            "nws_certainty": _text(props.get("certainty")),
            "area": base.truncate_text(
                _text(props.get("areaDesc")) or "area not published", _AREA_SNIPPET_CHARS
            ),
            "onset": onset,
            "ends": ends,
            "overlaps_travel_date": overlap,
            "severity_score": score,
            "severity_reasons": reasons,
        },
        evidence_ids=evidence_ids,
    )


def _forecast_signal(
    finding: _PeriodFinding,
    travel_date: datetime.date,
    observed_at: str,
) -> base.SignalEntry:
    """Build the signal for one forecast period that overlaps the travel date."""
    period = finding.period
    severity, reasons, metrics = _forecast_severity(period)
    name = _text(period.get("name")) or "Unnamed period"
    start = _text(period.get("startTime"))
    end = _text(period.get("endTime"))
    number = period.get("number")
    is_daytime = period.get("isDaytime")

    temperature_text = "temperature not published"
    if metrics.get("temperature") is not None:
        temperature_text = "temperature {0:g} {1}".format(
            metrics["temperature"], metrics.get("temperature_unit") or ""
        ).strip()
    precipitation_text = "precipitation probability not published"
    if metrics.get("precipitation_probability") is not None:
        precipitation_text = "precipitation probability {0:g}%".format(
            metrics["precipitation_probability"]
        )

    summary = (
        "NWS forecast period '{0}' at {1}, {2} to {3} local: {4}; {5}; wind {6}; {7}. "
        "Mapped to risk level '{8}'."
    ).format(
        name,
        finding.airport,
        _short_local(start),
        _short_local(end),
        _text(period.get("shortForecast")) or "no forecast text published",
        temperature_text,
        metrics.get("wind_speed") or "not published",
        precipitation_text,
        severity,
    )

    period_metrics: Dict[str, Any] = dict(metrics)
    period_metrics.update(
        {
            "airport": finding.airport,
            # The same key the alert and summary signals use, so fusion reads one shape:
            # a forecast period is reported for exactly one airport, but the field is a
            # list because an alert can be active at both ends of the route.
            "airports": [finding.airport],
            "period_name": name,
            "period_number": number if isinstance(number, int) else None,
            "is_daytime": is_daytime if isinstance(is_daytime, bool) else None,
            "start": start,
            "end": end,
            "travel_date": travel_date.isoformat(),
            "travel_date_overlap": True,
            "severity_reasons": reasons,
        }
    )

    return base.make_signal(
        _SIGNAL_KIND_FORECAST,
        summary,
        severity=severity,
        confidence=_FORECAST_CONFIDENCE,
        observed_at=observed_at,
        effective_at=start,
        metrics=period_metrics,
        evidence_ids=[finding.evidence_id],
    )


def _summary_signal(
    *,
    airports: Sequence[str],
    travel_date: datetime.date,
    observed_at: str,
    periods_assessed: Mapping[str, Sequence[str]],
    evidence_ids: Sequence[str],
) -> base.SignalEntry:
    """The explicit "checked, nothing found" signal.

    Emitted only when no alert is active at the route and every overlapping period
    was assessed at ``low``, so the fusion layer can tell this real result apart
    from an ``unavailable`` source that could not be checked at all.
    """
    periods_text = "; ".join(
        "{0}: {1}".format(code, ", ".join(names) if names else "no periods published")
        for code, names in sorted(periods_assessed.items())
    )
    summary = (
        "No meaningful weather risk identified for {0} on {1}: NWS reports no active "
        "alerts at these airports and no overlapping forecast period shows a disruptive "
        "condition (periods assessed - {2}). This is a checked result, not missing data."
    ).format(", ".join(airports), travel_date.isoformat(), periods_text)

    return base.make_signal(
        _SIGNAL_KIND_SUMMARY,
        summary,
        severity="low",
        confidence=_SUMMARY_CONFIDENCE,
        observed_at=observed_at,
        metrics={
            "airports": list(airports),
            "active_alerts": 0,
            "max_forecast_severity": "low",
            "periods_assessed": {
                code: list(names) for code, names in periods_assessed.items()
            },
        },
        evidence_ids=list(evidence_ids),
    )


# --------------------------------------------------------------------------- #
# Per-airport collection
# --------------------------------------------------------------------------- #
async def _fetch_airport(
    info: iata_lookup.AirportInfo,
    *,
    location: base.Location,
    travel_date: datetime.date,
    observed_at: str,
    raw_paths: List[str],
    warnings: List[str],
) -> _AirportWeather:
    """Fetch and parse both NWS resources for one airport.

    ``raw_paths`` accumulates every raw evidence record this call produces (across
    airports, so an error envelope can still point at everything that was logged) and
    ``warnings`` is the call-level warning list, extended in place. Every failure ends
    *this airport* through :func:`_abort`; ``fetch_signals`` records it and keeps the
    other airport, so an incomplete route is reported as ``partial`` rather than as
    ``ok``.
    """
    evidence: List[base.EvidenceEntry] = []
    latency = 0.0
    points_url = _points_url(info)
    request_context: Dict[str, Any] = {
        "airport": info.iata,
        "travel_date": travel_date.isoformat(),
        "coordinates": info.coordinates,
    }

    points = await base.get_json(
        config.SOURCE_NWS,
        points_url,
        headers=_nws_headers(),
        expect_keys=["properties"],
        context=dict(request_context, endpoint="points"),
    )
    _remember(raw_paths, points)
    latency += points.latency_ms
    if not points.ok:
        if points.status_code == 404:
            _abort(
                error_code=base.ErrorCode.NO_DATA_FOR_QUERY,
                error=(
                    "NWS publishes no forecast grid for {0} at {1} (HTTP 404), so neither "
                    "the forecast nor the alerts for it could be read. Nothing was "
                    "substituted for that airport."
                ).format(info.iata, info.coordinates),
                location=location,
                travel_date=travel_date,
                raw_paths=raw_paths,
                latency_ms=latency,
                warnings=warnings,
                status=config.STATUS_UNAVAILABLE,
            )
        _abort(
            error_code=points.error_code or base.ErrorCode.UNEXPECTED_ERROR,
            error="NWS /points failed for {0} ({1}): {2}".format(
                info.iata, points_url, points.error or "no error message"
            ),
            location=location,
            travel_date=travel_date,
            raw_paths=raw_paths,
            latency_ms=latency,
            warnings=warnings,
            status=points.failure_status,
        )

    properties = (
        points.payload.get("properties") if isinstance(points.payload, Mapping) else None
    )
    if not isinstance(properties, Mapping):
        _abort(
            error_code=base.ErrorCode.UNEXPECTED_PAYLOAD_SHAPE,
            error=(
                "NWS /points for {0} returned no 'properties' object, so its grid could "
                "not be read. The raw payload is on disk for inspection."
            ).format(info.iata),
            location=location,
            travel_date=travel_date,
            raw_paths=raw_paths,
            latency_ms=latency,
            warnings=warnings,
        )

    grid = "{0}/{1},{2}".format(
        _text(properties.get("gridId")) or "unknown office",
        properties.get("gridX") if isinstance(properties.get("gridX"), int) else "?",
        properties.get("gridY") if isinstance(properties.get("gridY"), int) else "?",
    )
    timezone_name = _text(properties.get("timeZone"))

    forecast_url = _trusted_nws_url(properties.get("forecast"))
    if forecast_url is None:
        _abort(
            error_code=base.ErrorCode.UNEXPECTED_PAYLOAD_SHAPE,
            error=(
                "NWS /points for {0} returned a 'forecast' URL that is not an HTTPS "
                "gridpoint resource on {1}, so it was not followed. Payload on disk."
            ).format(info.iata, config.NWS_BASE_URL),
            location=location,
            travel_date=travel_date,
            raw_paths=raw_paths,
            latency_ms=latency,
            warnings=warnings,
        )

    forecast = await base.get_json(
        config.SOURCE_NWS,
        forecast_url,
        headers=_nws_headers(),
        expect_keys=["properties"],
        context=dict(request_context, endpoint="forecast", grid=grid),
    )
    _remember(raw_paths, forecast)
    latency += forecast.latency_ms
    if not forecast.ok:
        _abort(
            error_code=forecast.error_code or base.ErrorCode.UNEXPECTED_ERROR,
            error="NWS gridpoint forecast failed for {0} ({1}): {2}".format(
                info.iata, forecast_url, forecast.error or "no error message"
            ),
            location=location,
            travel_date=travel_date,
            raw_paths=raw_paths,
            latency_ms=latency,
            warnings=warnings,
            status=forecast.failure_status,
        )

    forecast_properties = (
        forecast.payload.get("properties")
        if isinstance(forecast.payload, Mapping)
        else None
    )
    published = (
        forecast_properties.get("periods")
        if isinstance(forecast_properties, Mapping)
        else None
    )
    if not isinstance(published, list):
        _abort(
            error_code=base.ErrorCode.UNEXPECTED_PAYLOAD_SHAPE,
            error=(
                "The NWS forecast document for {0} carries no 'periods' array, so no "
                "period could be matched to {1}. Payload on disk for inspection."
            ).format(info.iata, travel_date.isoformat()),
            location=location,
            travel_date=travel_date,
            raw_paths=raw_paths,
            latency_ms=latency,
            warnings=warnings,
        )

    periods = [period for period in published if isinstance(period, Mapping)]
    if len(periods) != len(published):
        warnings.append(
            "{0} period entries for {1} were not objects and were skipped".format(
                len(published) - len(periods), info.iata
            )
        )
    if not periods:
        _abort(
            error_code=base.ErrorCode.UNEXPECTED_PAYLOAD_SHAPE,
            error=(
                "The NWS forecast document for {0} published an empty 'periods' array, so "
                "nothing could be matched to {1}."
            ).format(info.iata, travel_date.isoformat()),
            location=location,
            travel_date=travel_date,
            raw_paths=raw_paths,
            latency_ms=latency,
            warnings=warnings,
        )

    bounds = [
        bound
        for bound in (_period_bounds(period) for period in periods)
        if bound is not None
    ]
    if not bounds:
        _abort(
            error_code=base.ErrorCode.UNEXPECTED_PAYLOAD_SHAPE,
            error=(
                "No forecast period for {0} carried a parseable startTime/endTime, so {1} "
                "could not be matched against the published periods."
            ).format(info.iata, travel_date.isoformat()),
            location=location,
            travel_date=travel_date,
            raw_paths=raw_paths,
            latency_ms=latency,
            warnings=warnings,
        )
    period_window = (
        min(bound[0] for bound in bounds),
        max(bound[1] for bound in bounds),
    )

    alerts = await base.get_json(
        config.SOURCE_NWS,
        _alerts_url(),
        params={"point": info.coordinates},
        headers=_nws_headers(),
        expect_keys=["features"],
        context=dict(request_context, endpoint="alerts"),
    )
    _remember(raw_paths, alerts)
    latency += alerts.latency_ms
    if not alerts.ok:
        _abort(
            error_code=alerts.error_code or base.ErrorCode.UNEXPECTED_ERROR,
            error="NWS active-alert lookup failed for {0} (point {1}): {2}".format(
                info.iata, info.coordinates, alerts.error or "no error message"
            ),
            location=location,
            travel_date=travel_date,
            raw_paths=raw_paths,
            latency_ms=latency,
            warnings=warnings,
            status=alerts.failure_status,
        )

    alert_properties = _alert_properties(alerts.payload)
    if alert_properties is None:
        _abort(
            error_code=base.ErrorCode.UNEXPECTED_PAYLOAD_SHAPE,
            error=(
                "The NWS active-alert response for {0} is not a FeatureCollection with a "
                "'features' array, so it was not parsed. Payload on disk for inspection."
            ).format(info.iata),
            location=location,
            travel_date=travel_date,
            raw_paths=raw_paths,
            latency_ms=latency,
            warnings=warnings,
        )

    evidence.append(
        _evidence(
            field="iata_lookup.row",
            value=info.to_dict(),
            observed_at=observed_at,
            note=(
                "Static table row for {0}: coordinates from the OurAirports dataset and "
                "timezone from the NWS grid, verified against NWS /points. No geocoding "
                "was performed."
            ).format(info.iata),
            evidence_id=_evidence_id(info.iata, len(evidence) + 1),
        )
    )
    evidence.append(
        _evidence(
            field="properties.gridId/gridX/gridY",
            value=grid,
            observed_at=observed_at,
            raw_path=points.raw_path,
            note="NWS grid that {0} resolved to ({1}).".format(info.iata, points_url),
            evidence_id=_evidence_id(info.iata, len(evidence) + 1),
        )
    )
    evidence.append(
        _evidence(
            field="properties.timeZone",
            value=timezone_name or "not published",
            observed_at=observed_at,
            raw_path=points.raw_path,
            note=(
                "Timezone NWS applies to this grid, used to read the forecast period "
                "timestamps exactly as published."
            ),
            evidence_id=_evidence_id(info.iata, len(evidence) + 1),
        )
    )

    period_findings: List[_PeriodFinding] = []
    for index, period in enumerate(periods):
        if not _overlaps_travel_date(period, travel_date):
            continue
        evidence_id = _evidence_id(info.iata, len(evidence) + 1)
        evidence.append(
            _evidence(
                field="properties.periods[{0}]".format(index),
                value={
                    key: period.get(key)
                    for key in (
                        "name",
                        "number",
                        "startTime",
                        "endTime",
                        "isDaytime",
                        "shortForecast",
                        "temperature",
                        "temperatureUnit",
                        "windSpeed",
                        "probabilityOfPrecipitation",
                    )
                },
                observed_at=observed_at,
                raw_path=forecast.raw_path,
                note=(
                    "Forecast period overlapping {0} for {1}; these are the fields the "
                    "severity mapping read."
                ).format(travel_date.isoformat(), info.iata),
                evidence_id=evidence_id,
            )
        )
        period_findings.append(
            _PeriodFinding(airport=info.iata, period=period, evidence_id=evidence_id)
        )

    alert_findings: List[_AlertFinding] = []
    for index, props in enumerate(alert_properties):
        evidence_id = _evidence_id(info.iata, len(evidence) + 1)
        evidence.append(
            _evidence(
                field="features[{0}].properties".format(index),
                value={
                    key: props.get(key)
                    for key in (
                        "id",
                        "event",
                        "severity",
                        "urgency",
                        "certainty",
                        "category",
                        "onset",
                        "ends",
                        "effective",
                        "expires",
                        "areaDesc",
                    )
                },
                observed_at=observed_at,
                raw_path=alerts.raw_path,
                note=(
                    "Active NWS alert as published for the point {0}; severity, urgency "
                    "and certainty are the fields the risk mapping read."
                ).format(info.coordinates),
                evidence_id=evidence_id,
            )
        )
        alert_findings.append(
            _AlertFinding(airport=info.iata, props=props, evidence_id=evidence_id)
        )

    return _AirportWeather(
        info=info,
        grid=grid,
        timezone=timezone_name,
        periods=period_findings,
        period_window=period_window,
        alerts=alert_findings,
        evidence=evidence,
        latency_ms=latency,
    )


# --------------------------------------------------------------------------- #
# The one public entry point
# --------------------------------------------------------------------------- #
async def fetch_signals(
    location: base.Location, date: datetime.date
) -> base.NormalizedSignals:
    """Fetch NWS weather signals for a route and a travel date.

    Returns the shared envelope: ``ok`` with signals - including an explicit "no
    meaningful weather risk identified" signal when nothing noteworthy was found -
    ``partial`` when some airports could be read and some could not (the surviving
    airports' signals plus one ``error_code`` per failed airport), or
    ``unavailable``/``error`` with a machine-readable ``error_code`` and the raw
    evidence still on disk. ``unavailable``/``error`` are used only when *no* airport
    on the route could be read.

    Nothing NWS does can raise out of this function: transport failures, HTTP errors,
    unusable payloads, an unsupported airport code and a travel date outside the
    published forecast window all become explicit states. ``ValueError`` is raised
    only for a programming error in the arguments, matching ``base.get_json``.
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
        config.SOURCE_NWS, location=location, travel_date=travel_date
    )
    if blocked is not None:
        return blocked
    if not _has_user_agent():
        return base.failure_signals(
            source=config.SOURCE_NWS,
            error_code=base.ErrorCode.MISSING_CREDENTIAL,
            error=(
                "config.NWS_USER_AGENT is empty, and api.weather.gov rejects requests "
                "without a descriptive User-Agent naming the app and a contact address. "
                "No request was sent."
            ),
            location=location,
            travel_date=travel_date,
            warnings=[
                "Set NWS_USER_AGENT in .env, e.g. "
                "'(travel-disruption-assessor, you@example.com)'."
            ],
        )

    airports: List[iata_lookup.AirportInfo] = []
    unsupported: List[str] = []
    for endpoint in (location.origin, location.destination):
        info = iata_lookup.lookup_airport(endpoint.iata)
        if info is None:
            unsupported.append(endpoint.iata)
            continue
        if info not in airports:  # a same-airport route is only fetched once
            airports.append(info)

    if unsupported:
        return base.unavailable_signals(
            source=config.SOURCE_NWS,
            error_code=base.ErrorCode.NO_DATA_FOR_QUERY,
            error=(
                "This app's coordinate table and NWS coverage are US-only, and {0} is not a "
                "supported airport code ({1} US airports are supported), so no weather "
                "could be looked up for it. No coordinates were guessed and no other "
                "location was substituted."
            ).format(
                ", ".join(sorted(set(unsupported))),
                len(iata_lookup.supported_airports()),
            ),
            location=location,
            travel_date=travel_date,
            warnings=[
                "Supported codes: iata_lookup.supported_airports().",
                "A non-US route is a known coverage limit of this source, not an outage.",
            ],
        )

    observed_at = base.utc_now_iso()
    evidence: List[base.EvidenceEntry] = []
    raw_paths: List[str] = []
    warnings: List[str] = []
    collected: List[_AirportWeather] = []
    failures: List[_AirportFailure] = []
    latency = 0.0

    try:
        # One airport at a time, three requests each: NWS asks to be used politely,
        # and a route is at most two airports. A failed airport is recorded and the
        # loop continues, so one endpoint's hiccup cannot discard the other's real
        # signals: if anything survives, the route is reported as `partial` (with the
        # failure listed); only when *nothing* survives is the source failed whole.
        for info in airports:
            try:
                weather = await _fetch_airport(
                    info,
                    location=location,
                    travel_date=travel_date,
                    observed_at=observed_at,
                    raw_paths=raw_paths,
                    warnings=warnings,
                )
            except _SourceAbort as abort:
                failures.append(_failure_from_envelope(info, abort.envelope))
                latency += abort.envelope["latency_ms"] or 0.0
                continue
            collected.append(weather)
            evidence.extend(weather.evidence)
            latency += weather.latency_ms

        windows_text = "; ".join(
            "{0}: {1} to {2}".format(
                weather.info.iata,
                weather.period_window[0] or "unknown",
                weather.period_window[1] or "unknown",
            )
            for weather in collected
        )
        without_periods = [weather for weather in collected if not weather.periods]
        if without_periods and len(without_periods) == len(collected):
            alerts_seen = sum(len(weather.alerts) for weather in collected)
            alerts_note = (
                ""
                if not alerts_seen
                else (
                    " {0} active alert(s) were seen at the route airports, but they describe "
                    "current conditions and do not cover the travel date; their raw responses "
                    "are on disk."
                ).format(alerts_seen)
            )
            _abort(
                error_code=base.ErrorCode.NO_DATA_FOR_QUERY,
                status=config.STATUS_UNAVAILABLE,
                error=(
                    "NWS published no forecast period overlapping {0} for {1}, so that date "
                    "cannot be assessed from the gridpoint forecast (NWS goes about a week "
                    "ahead). Published windows - {2}. Nothing was extrapolated.{3}"
                ).format(
                    travel_date.isoformat(),
                    ", ".join(weather.info.iata for weather in without_periods),
                    windows_text,
                    alerts_note,
                ),
                location=location,
                travel_date=travel_date,
                raw_paths=raw_paths,
                latency_ms=latency,
                warnings=warnings,
            )

        # Only part of the route has a published window covering the travel date. The
        # other airports are out of coverage for this date, so they become "no data"
        # failures and the airports that did cover it are still reported - as a partial
        # result, never as a complete one.
        for weather in without_periods:
            failures.append(
                _AirportFailure(
                    iata=weather.info.iata,
                    error_code=base.ErrorCode.NO_DATA_FOR_QUERY,
                    error=(
                        "NWS published no forecast period overlapping {0} for {1} "
                        "(published window {2} to {3}), so that airport's travel-date "
                        "conditions could not be assessed. Nothing was extrapolated."
                    ).format(
                        travel_date.isoformat(),
                        weather.info.iata,
                        weather.period_window[0] or "unknown",
                        weather.period_window[1] or "unknown",
                    ),
                    status=config.STATUS_UNAVAILABLE,
                )
            )
            collected.remove(weather)

        if not collected:
            # Every airport failed: this is a whole-source failure, not a partial one.
            primary = _primary_failure(failures)
            _abort(
                error_code=primary.error_code,
                error=_failures_message(failures),
                location=location,
                travel_date=travel_date,
                raw_paths=raw_paths,
                latency_ms=latency,
                warnings=warnings,
                status=primary.status,
            )

        # One signal per alert, even when the same alert covers both airports.
        alerts_by_id: Dict[str, List[_AlertFinding]] = {}
        for weather in collected:
            for finding in weather.alerts:
                alert_id = _text(
                    finding.props.get("id")
                ) or "unidentified-alert-{0}-{1}".format(
                    finding.airport, len(alerts_by_id)
                )
                alerts_by_id.setdefault(alert_id, []).append(finding)

        out_of_window = [
            alert_id
            for alert_id, findings in alerts_by_id.items()
            if _alert_overlap(findings[0].props, travel_date) is False
        ]
        if out_of_window:
            warnings.append(
                "{0} of {1} active alert(s) at the route airports do not cover the travel "
                "date; they are reported and flagged as out of window, not filtered "
                "out.".format(len(out_of_window), len(alerts_by_id))
            )

        signals: List[base.SignalEntry] = [
            _alert_signal(findings, travel_date, observed_at)
            for findings in alerts_by_id.values()
        ]
        signals.extend(
            _forecast_signal(finding, travel_date, observed_at)
            for weather in collected
            for finding in weather.periods
        )

        max_forecast_severity = "low"
        for signal in signals:
            if signal["kind"] == _SIGNAL_KIND_FORECAST:
                max_forecast_severity = _most_severe(
                    max_forecast_severity, signal["severity"] or _UNKNOWN_RISK
                )

        if not alerts_by_id and max_forecast_severity == "low":
            signals.append(
                _summary_signal(
                    airports=[weather.info.iata for weather in collected],
                    travel_date=travel_date,
                    observed_at=observed_at,
                    periods_assessed={
                        weather.info.iata: [
                            _text(finding.period.get("name")) or "unnamed period"
                            for finding in weather.periods
                        ]
                        for weather in collected
                    },
                    evidence_ids=[
                        finding.evidence_id
                        for weather in collected
                        for finding in weather.periods
                    ],
                )
            )

        if failures:
            sub_query_failures: List[base.SubQueryFailure] = [
                {
                    "scope": failure.iata,
                    "error_code": failure.error_code,
                    "error": failure.error,
                }
                for failure in failures
            ]
            return base.partial_signals(
                source=config.SOURCE_NWS,
                signals=signals,
                failures=sub_query_failures,
                location=location,
                travel_date=travel_date,
                evidence=evidence,
                raw_response_path=raw_paths[-1] if raw_paths else None,
                raw_response_paths=raw_paths,
                latency_ms=latency,
                warnings=warnings,
                retrieved_at=observed_at,
            )

        return base.ok_signals(
            source=config.SOURCE_NWS,
            signals=signals,
            location=location,
            travel_date=travel_date,
            evidence=evidence,
            raw_response_path=raw_paths[-1] if raw_paths else None,
            raw_response_paths=raw_paths,
            latency_ms=latency,
            warnings=warnings,
            retrieved_at=observed_at,
        )
    except _SourceAbort as abort:
        return abort.envelope
    except Exception as exc:  # rule 2: nothing escapes a data source module
        return base.failure_signals(
            source=config.SOURCE_NWS,
            error_code=base.ErrorCode.UNEXPECTED_ERROR,
            error="Unexpected {0} while fetching NWS signals: {1}".format(
                type(exc).__name__, base.redact_text(str(exc))
            ),
            location=location,
            travel_date=travel_date,
            raw_response_path=raw_paths[-1] if raw_paths else None,
            raw_response_paths=raw_paths,
            latency_ms=latency,
            warnings=list(warnings) + ["This is a bug in this module, not an NWS outage."],
            retrieved_at=observed_at,
        )
