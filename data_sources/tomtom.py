"""TomTom Traffic source: road incidents around each airport of a route.

What this source is for
-----------------------
The question is **"are there known disruptions on the ground segment of this trip?"**
- not "what is the fastest route". TomTom's Traffic *Incident Details* feed reports
accidents, closures, roadworks, jams and similar events with a severity indicator, so
this module turns those into ``traffic_incident`` signals scoped to the origin or the
destination airport. Routing, ETA and travel-time signals are deliberately out of
scope: they answer a different question and would need a departure-time assumption.

Endpoint and version (verified 2026-10-16)
------------------------------------------
``GET {TOMTOM_BASE_URL}/traffic/services/5/incidentDetails``

* Service **version 5** is current; version 4 is marked deprecated by TomTom, so v4 is
  not used.
* v5 accepts ``bbox`` (or ``ids``) and **has no ``point``/``radius`` parameters** -
  those belonged to the older surface. The radius below is therefore expressed as a
  bounding box around the airport, and the response is then filtered by great-circle
  distance so "within 15 km" is literally true rather than a box-corner approximation.
* ``fields`` is optional but defaults to ``iconCategory`` only, so the full field
  selection this module needs is always sent explicitly.
* ``delay`` is in **seconds** (except for road closures, where TomTom omits it) and
  ``length`` is in **meters**; both are converted before they reach the metrics.

Geography: 15 km around each airport
------------------------------------
15 km is the default radius because it covers the airport access roads, the rental-car
return loop and the immediate approach ring - the ground segment a traveller actually
drives - without dragging in unrelated city-wide congestion. One request is made per
airport (origin, then destination): two requests per assessment, which is negligible
even on the free tier.

Time relevance: this feed is real-time, and the module says so
-------------------------------------------------------------
Traffic incidents are *current conditions*. There is no way to know today what a jam
will look like in three days, so the travel date determines what can honestly be
returned:

=========================  ==========================================================
Travel date                Behaviour
=========================  ==========================================================
today                      query, ``ok`` with signals at full confidence
tomorrow                   query, but every summary is prefixed with
                           ``current conditions (indicative for tomorrow):`` and the
                           confidence is reduced - a deliberate heuristic, not a
                           forecast
day after tomorrow or      no request at all: ``unavailable`` with
later, or a past date      ``error_code=no_data_for_query`` and a message naming the
                           limitation ("traffic incidents are real-time only; cannot
                           forecast ground conditions N days out"). Current incidents
                           are never re-dated to look like a forecast.
=========================  ==========================================================

The comparison uses UTC, because this module has no per-airport timezone (the static
table carries one, but a route can straddle timezones and a trip is a date rather than
an instant). Within a few hours of local midnight the window can therefore be off by one
day; that limitation is stated rather than guessed around.

TomTom does expose *planned* future incidents (``timeValidityFilter=future``, e.g.
roadworks booked weeks ahead). This module intentionally does not use them yet: they
skew heavily towards roadworks, they are not a forecast of congestion, and mixing them
into a date-window answer would silently change what the signal means. The
``unavailable`` message says so, so the gap is visible rather than papered over.

Severity mapping (deliberate, deterministic, documented)
--------------------------------------------------------
TomTom documents ``magnitudeOfDelay`` as ``0 Unknown, 1 Minor, 2 Moderate, 3 Major,
4 Undefined (used for road closures and other indefinite delays)`` and
``iconCategory 8`` as *Road Closed*. Two details matter and both are handled
explicitly:

* **0 means "unknown", not "no delay"** - it is *not* treated as "no incident". An
  incident whose magnitude is unknown is still reported, with ``severity=None`` (the
  envelope allows "we saw something, we cannot grade it") and reduced confidence.
  Dropping it would turn a real incident into a clean result.
* **4 means "undefined/indefinite"**, which for ground access is at least as bad as
  "major": a closure with no stated end is a ``severe`` disruption, so 4 maps to
  ``severe``.

=========================================  ======================================
Observation                                Level
=========================================  ======================================
``magnitudeOfDelay`` 4 (undefined)          ``severe``
``magnitudeOfDelay`` 3 (major)              ``high``
``iconCategory`` 8 (road closed)            floor of ``high``, whatever the
                                            magnitude says (a closed road is a
                                            binary disruption)
``magnitudeOfDelay`` 2 (moderate)           ``moderate``
``magnitudeOfDelay`` 1 (minor)              ``low``
``magnitudeOfDelay`` 0 / missing            ``None`` - reported as ungraded
=========================================  ======================================

The closure floor is applied with ``max()``, never as an override, so a closure that
also carries magnitude 4 stays ``severe``; a closure reported with magnitude 2 is
raised to ``high``.

What a traffic signal carries, and why severity alone is not enough
-------------------------------------------------------------------
The live JFK capture is 86 incidents, 72 of them inside the radius, 40 of them
closures - all ``severe``. A fusion layer that counts severe signals would call every
trip catastrophic, so each signal's ``metrics`` carries the context needed to judge
*relevance to this trip* instead:

* ``airport_iata`` + ``scope`` - which end of the route, and which airport.
* ``distance_km`` + ``radius_km`` - how close, and to what.
* ``place`` / ``from_place`` / ``to_place`` - the vendor's own street names, verbatim
  (e.g. ``"96th St/Exit 14 (FDR Dr)"``, ``"I-405 N / San Diego Fwy N"``). Distance alone
  cannot separate a closure on the Van Wyck from one on a side street.
* ``road_numbers`` + ``road_class`` + ``on_numbered_route`` - ``numbered_route`` when
  TomTom published ``roadNumbers``, otherwise ``unclassified``. There is **no**
  road-class field in the payload, so ``unclassified`` means *unknown*, not *minor*;
  ``road_class_basis`` states which fact produced the value.
* ``event_descriptions`` - every vendor event, not just the first (``Closed`` and
  ``Roadworks`` can describe the same incident).
* ``icon_category`` / ``magnitude_of_delay`` / ``severity_source`` - what it is, how bad
  the vendor says it is, and which rule produced our severity.

None of this changes the envelope: ``SignalEntry.metrics`` is the open extension point.

Checked-and-clear is a signal, not silence
------------------------------------------
An airport with **no** incidents inside the radius produces an explicit
``traffic_conditions`` signal saying so (severity ``low``, confidence 0.75). "Checked
and clear" must be distinguishable from "we could not check", so an empty incident list
never turns into an empty signal list. If one airport fails and the other succeeds, the
envelope is ``partial``: real signals from the airport that answered, plus the recorded
failure for the one that did not. Both airports failing is ``error``/``unavailable``,
never an empty ``ok``.

Free tier and failure handling
------------------------------
The published pricing page (read 2026-10-16) lists the Traffic APIs at **2.5K requests
per month** free; older documentation quoted 2,500/day. Either way two requests per
assessment is well inside the limit, and the module never paginates or retries a
successful call. Live probing on 2026-10-16 showed that a **missing or invalid key
returns HTTP 401** with ``{"detailedError": {"code": "Unauthorized", ...}}``, even
though the documentation lists 403 - both are classified as credential problems, and
403 (key not valid *for this request*, i.e. not entitled) is classified as
``plan_limit``. Rate limits and 5xx are retried by :func:`data_sources.base.get_json`
with ``Retry-After`` honoured, and every attempt's raw response is written to
``logs/raw/`` before it is parsed.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import config

from . import base, iata_lookup

# --------------------------------------------------------------------------- #
# Normalized categories, geography and thresholds
# --------------------------------------------------------------------------- #
_SIGNAL_KIND_INCIDENT: str = "traffic_incident"
_SIGNAL_KIND_CLEAR: str = "traffic_conditions"

_INCIDENT_PATH: str = "/traffic/services/5/incidentDetails"

# 15 km around each airport; the rationale is in the module docstring.
_RADIUS_KM: float = 15.0
_EARTH_RADIUS_KM: float = 6371.0088
# The bbox is built on the same sphere the distance filter uses, then grown by 0.1 %
# (about 15 m at 15 km) so spherical chord effects and the outward rounding below can
# never leave a point that is exactly on the radius outside the box.
_BBOX_SAFETY_FACTOR: float = 1.001

# TomTom returns only the fields that are requested, and its default is iconCategory
# alone, so the full selection this module reads is always sent explicitly.
_FIELDS: str = (
    "{incidents{type,geometry{type,coordinates},properties{"
    "id,iconCategory,magnitudeOfDelay,"
    "events{description,code,iconCategory},"
    "startTime,endTime,from,to,length,delay,roadNumbers,timeValidity,"
    "probabilityOfOccurrence,numberOfReports,lastReportTime}}}"
)

_DATE_WINDOW_TODAY: str = "today"
_DATE_WINDOW_TOMORROW: str = "tomorrow"
_DATE_WINDOW_OUT_OF_RANGE: str = "out_of_range"

_MAGNITUDE_LABELS: Mapping[int, str] = {
    0: "unknown",
    1: "minor",
    2: "moderate",
    3: "major",
    4: "undefined (indefinite delay)",
}

# The vendor's iconCategory values, verbatim from the v5 documentation.
_ICON_LABELS: Mapping[int, str] = {
    0: "unknown incident",
    1: "accident",
    2: "fog",
    3: "dangerous conditions",
    4: "rain",
    5: "ice",
    6: "jam",
    7: "lane closed",
    8: "road closed",
    9: "road works",
    10: "wind",
    11: "flooding",
    14: "broken down vehicle",
}

_ROAD_CLOSED_ICON: int = 8
_CLOSURE_SEVERITY_FLOOR: str = "high"

# ``magnitudeOfDelay`` 0 ("unknown") is deliberately absent: it is ungraded, not minor.
_MAGNITUDE_SEVERITY: Mapping[int, str] = {
    1: "low",
    2: "moderate",
    3: "high",
    4: "severe",
}
_SEVERITY_ORDER: Tuple[str, ...] = ("low", "moderate", "high", "severe")

# Road class. TomTom's incidentDetails response has **no** road-class field, so only
# one class claim is defensible: the incident sits on a *numbered route* because the
# vendor published ``roadNumbers`` ("I-495 E", "CA-1", "Grand Central Pkwy E"). Everything
# else is ``unclassified`` - which means *unknown*, never *minor*: the FDR Drive in the
# JFK capture carries no road number yet is a motorway. Fusion needs the distinction
# because 82 severe closures are not 82 trip risks, and "unclassified" must not be read
# as "safe to ignore".
_ROAD_CLASS_NUMBERED: str = "numbered_route"
_ROAD_CLASS_UNCLASSIFIED: str = "unclassified"


# Confidence is a declared value per evidence strength, not a per-request guess: a
# directly observed incident is strong, a clear reading is weaker (absence of reports),
# and an ungraded magnitude is weaker still because the severity is unreadable.
_CONFIDENCE_INCIDENT: float = 0.90
_CONFIDENCE_UNGRADED: float = 0.55
_CONFIDENCE_CLEAR: float = 0.75
# Tomorrow's trip is being judged on today's incidents, so every confidence is scaled.
_TOMORROW_CONFIDENCE_FACTOR: float = 0.60

# The literal prefix required on every summary when the travel date is tomorrow.
_TOMORROW_PREFIX: str = "current conditions (indicative for tomorrow): "

# Vendor ``detailedError.code`` values are free-form strings, so they are matched by
# hint rather than by an invented exhaustive list. The live 401 body is
# ``{"detailedError": {"code": "Unauthorized", ...}}``.
_CREDENTIAL_CODE_HINTS: Tuple[str, ...] = (
    "unauthorized",
    "unauthenticated",
    "invalidkey",
    "invalid key",
    "missingkey",
    "missing key",
    "forbidden",
)
_QUOTA_CODE_HINTS: Tuple[str, ...] = (
    "quota",
    "ratelimit",
    "rate limit",
    "too many requests",
    "throttl",
)
_PLAN_CODE_HINTS: Tuple[str, ...] = (
    "not authorized for",
    "not included",
    "subscription",
    "entitlement",
    "plan",
)

# --------------------------------------------------------------------------- #
# Small parsers - vendor values are never trusted to have the documented type
# --------------------------------------------------------------------------- #
def _text(value: Any) -> Optional[str]:
    """A stripped, non-empty string; ``"null"`` and ``"none"`` are treated as absent.

    TomTom's own example response carries the string ``"null"`` for absent fields, so a
    naive ``str()`` would turn "missing" into the literal word "null".
    """
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or cleaned.lower() in {"null", "none"}:
        return None
    return cleaned


def _as_int(value: Any) -> Optional[int]:
    """Best-effort integer, or ``None`` when the value is absent or not numeric."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) else None
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            return int(float(cleaned))
        except ValueError:
            return None
    return None


def _as_float(value: Any) -> Optional[float]:
    """Best-effort finite float, or ``None`` when the value is absent or unusable."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _mapping(value: Any) -> Mapping[str, Any]:
    """``value`` as a mapping, or an empty one - never raises on a vendor surprise."""
    return value if isinstance(value, Mapping) else {}


def _vendor_error(payload: Any) -> Tuple[Optional[str], Optional[str]]:
    """TomTom's ``detailedError.code`` / ``.message`` when the body carries one.

    The documented error body is ``{"detailedError": {"code": ..., "message": ...}}``
    (captured live for the 401 key error). A 2xx response is inspected too, because a
    body that is an error object must never be read as "no incidents".
    """
    detail = _mapping(_mapping(payload).get("detailedError"))
    return _text(detail.get("code")), _text(detail.get("message"))

# --------------------------------------------------------------------------- #
# Geography - a bbox because v5 has no point+radius, then a real distance filter
# --------------------------------------------------------------------------- #
def _bbox_around(
    latitude: float, longitude: float, radius_km: float = _RADIUS_KM
) -> Tuple[float, float, float, float]:
    """A ``(min_lon, min_lat, max_lon, max_lat)`` box circumscribing the radius.

    The box is a *superset* of the circle: its corners are up to ``radius * sqrt(2)``
    from the centre, so callers must still filter by distance (this module does). The
    box exists only because v5 removed the ``point``+``radius`` surface - it is not the
    radius itself. Edges are rounded outward (floor for minima, ceil for maxima) so
    rounding can never shrink the box below the circle.
    """
    if not -90.0 <= latitude <= 90.0:
        raise ValueError("latitude {0!r} is outside [-90, 90]".format(latitude))
    if not -180.0 <= longitude <= 180.0:
        raise ValueError("longitude {0!r} is outside [-180, 180]".format(longitude))
    if radius_km <= 0:
        raise ValueError("radius_km must be greater than 0")

    latitude_delta = math.degrees(
        radius_km * _BBOX_SAFETY_FACTOR / _EARTH_RADIUS_KM
    )
    # Guard the cosine near the poles so the box cannot divide by (almost) zero.
    cosine = max(math.cos(math.radians(latitude)), 1e-6)
    longitude_delta = math.degrees(
        radius_km * _BBOX_SAFETY_FACTOR / (_EARTH_RADIUS_KM * cosine)
    )

    min_lat = max(latitude - latitude_delta, -90.0)
    max_lat = min(latitude + latitude_delta, 90.0)
    if longitude_delta >= 180.0:
        # A circle that wide is only honest as a full sweep; clamping one side would
        # silently drop incidents on the other.
        min_lon, max_lon = -180.0, 180.0
    else:
        min_lon = max(longitude - longitude_delta, -180.0)
        max_lon = min(longitude + longitude_delta, 180.0)

    def _down(value: float) -> float:
        return math.floor(value * 1e6) / 1e6

    def _up(value: float) -> float:
        return math.ceil(value * 1e6) / 1e6

    return (_down(min_lon), _down(min_lat), _up(max_lon), _up(max_lat))


def _format_bbox(bbox: Tuple[float, float, float, float]) -> str:
    """``minLon,minLat,maxLon,maxLat`` as TomTom expects (no spaces, EPSG:4326)."""
    return ",".join("{0:.6f}".format(value) for value in bbox)


def _haversine_km(
    latitude: float, longitude: float, other_latitude: float, other_longitude: float
) -> float:
    """Great-circle distance in kilometres between two WGS84 points."""
    phi1 = math.radians(latitude)
    phi2 = math.radians(other_latitude)
    delta_phi = phi2 - phi1
    delta_lambda = math.radians(other_longitude - longitude)
    inner = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    )
    return 2.0 * _EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(inner)))


def _geometry_points(geometry: Any) -> List[Tuple[float, float]]:
    """Valid ``(lat, lon)`` pairs from a GeoJSON Point or LineString geometry.

    GeoJSON stores coordinates as ``[longitude, latitude]``; getting that backwards is
    the classic bug here, so the swap happens in exactly one place. Unusable pairs are
    dropped rather than guessed.
    """
    document = _mapping(geometry)
    kind = (_text(document.get("type")) or "").lower()
    raw = document.get("coordinates")
    candidates: Sequence[Any] = ()
    if kind == "point":
        candidates = [raw]
    elif kind == "linestring" and isinstance(raw, (list, tuple)):
        candidates = list(raw)

    points: List[Tuple[float, float]] = []
    for pair in candidates:
        if not isinstance(pair, (list, tuple)) or len(pair) < 2:
            continue
        longitude = _as_float(pair[0])
        latitude = _as_float(pair[1])
        if longitude is None or latitude is None:
            continue
        if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
            continue
        points.append((latitude, longitude))
    return points


def _distance_to_geometry_km(
    points: Sequence[Tuple[float, float]], latitude: float, longitude: float
) -> Optional[float]:
    """Closest approach of an incident to a point, or ``None`` if its geometry is empty.

    The closest vertex is used: for a road segment reaching into the radius, the nearest
    point is the honest measure of "how close is this disruption to the airport".
    """
    if not points:
        return None
    return min(
        _haversine_km(latitude, longitude, point_lat, point_lon)
        for point_lat, point_lon in points
    )

# --------------------------------------------------------------------------- #
# Severity, confidence and the travel-date window
# --------------------------------------------------------------------------- #
def _severity_rank(severity: Optional[str]) -> int:
    """Position on the ``low < moderate < high < severe`` scale, or ``-1`` for None."""
    return _SEVERITY_ORDER.index(severity) if severity in _SEVERITY_ORDER else -1


def _severity_for(
    icon_category: Optional[int], magnitude: Optional[int]
) -> Optional[str]:
    """Map ``(iconCategory, magnitudeOfDelay)`` onto ``config.RISK_LEVELS``.

    ``None`` means "the incident is real but its severity is not stated" - magnitude 0
    is documented by TomTom as *Unknown*, so it must not be reported as low risk. A road
    closure carries a floor of ``high`` applied with ``max()``, so a closure is never
    downgraded by a low magnitude and a closure with magnitude 4 stays ``severe``.
    """
    severity = _MAGNITUDE_SEVERITY.get(magnitude) if magnitude is not None else None
    if icon_category == _ROAD_CLOSED_ICON and _severity_rank(severity) < _severity_rank(
        _CLOSURE_SEVERITY_FLOOR
    ):
        severity = _CLOSURE_SEVERITY_FLOOR
    return severity


def _confidence_for(severity: Optional[str], date_window: str) -> float:
    """Declared confidence for one incident signal, scaled for the date window."""
    confidence = _CONFIDENCE_INCIDENT if severity is not None else _CONFIDENCE_UNGRADED
    if date_window == _DATE_WINDOW_TOMORROW:
        confidence *= _TOMORROW_CONFIDENCE_FACTOR
    return round(confidence, 3)


def _date_window(
    travel_date: datetime.date, *, today: Optional[datetime.date] = None
) -> str:
    """Classify the travel date as ``today``, ``tomorrow`` or ``out_of_range``.

    The comparison is done in UTC for determinism and for consistency with the other
    sources. A trip is a date rather than an instant, so within a few hours of local
    midnight the UTC date can differ from the airport's local date by one day; that is a
    documented limitation of this window, not something this module silently guesses at.
    """
    reference = today if today is not None else base.utc_now().date()
    days_ahead = (travel_date - reference).days
    if days_ahead == 0:
        return _DATE_WINDOW_TODAY
    if days_ahead == 1:
        return _DATE_WINDOW_TOMORROW
    return _DATE_WINDOW_OUT_OF_RANGE

# --------------------------------------------------------------------------- #
# Incidents - parsed once, then rendered as evidence + signals
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class _Incident:
    """One traffic incident inside the radius, parsed from a vendor record."""

    vendor_id: Optional[str]
    icon_category: Optional[int]
    magnitude: Optional[int]
    description: Optional[str]
    event_descriptions: Tuple[str, ...]
    from_place: Optional[str]
    to_place: Optional[str]
    road_numbers: Tuple[str, ...]
    delay_seconds: Optional[int]
    length_meters: Optional[float]
    start_time: Optional[str]
    end_time: Optional[str]
    time_validity: Optional[str]
    probability: Optional[str]
    distance_km: float

    @property
    def label(self) -> str:
        """Human label for the icon category, e.g. ``road closed``."""
        if self.icon_category is None:
            return "traffic incident"
        return _ICON_LABELS.get(self.icon_category, "traffic incident")

    @property
    def severity(self) -> Optional[str]:
        """Mapped risk level, or ``None`` when the vendor did not state one."""
        return _severity_for(self.icon_category, self.magnitude)

    @property
    def place(self) -> Optional[str]:
        """Where on the road network the incident sits, when the vendor says so."""
        if self.from_place and self.to_place:
            return "{0} to {1}".format(self.from_place, self.to_place)
        return self.from_place or self.to_place

    def evidence_value(self) -> Dict[str, Any]:
        """The vendor facts this module read, kept together for citation."""
        return {
            "id": self.vendor_id,
            "icon_category": self.icon_category,
            "icon_label": self.label,
            "magnitude_of_delay": self.magnitude,
            "magnitude_label": (
                _MAGNITUDE_LABELS.get(self.magnitude)
                if self.magnitude is not None
                else None
            ),
            "description": self.description,
            "event_descriptions": list(self.event_descriptions),
            "from": self.from_place,
            "to": self.to_place,
            "road_numbers": list(self.road_numbers),
            "delay_seconds": self.delay_seconds,
            "length_meters": self.length_meters,
            "time_validity": self.time_validity,
            "probability_of_occurrence": self.probability,
            "distance_km": round(self.distance_km, 2),
        }


def _parse_incident(
    record: Any, *, latitude: float, longitude: float
) -> Tuple[Optional[_Incident], Optional[str]]:
    """Parse one vendor record, applying the radius filter.

    Returns ``(incident, exclusion_reason)``:

    * ``(incident, None)`` - inside the radius and usable.
    * ``(None, None)`` - outside the radius. That is the expected, silent case: the bbox
      is a superset of the circle by construction.
    * ``(None, reason)`` - unusable geometry. Counted and warned about, never assumed to
      be either inside or outside.
    """
    document = _mapping(record)
    if not document:
        return None, "record was not a JSON object"

    points = _geometry_points(document.get("geometry"))
    distance = _distance_to_geometry_km(points, latitude, longitude)
    if distance is None:
        return None, "no usable GeoJSON Point/LineString geometry, so its position is unknown"
    if distance > _RADIUS_KM:
        return None, None

    properties = _mapping(document.get("properties"))
    events = properties.get("events")
    description: Optional[str] = None
    event_descriptions: List[str] = []
    if isinstance(events, (list, tuple)):
        for event in events:
            text = _text(_mapping(event).get("description"))
            if not text:
                continue
            if description is None:
                description = text
            if text not in event_descriptions:
                event_descriptions.append(text)

    road_numbers: List[str] = []
    raw_roads = properties.get("roadNumbers")
    if isinstance(raw_roads, (list, tuple)):
        road_numbers = [
            road for road in (_text(item) for item in raw_roads) if road is not None
        ]

    return (
        _Incident(
            vendor_id=_text(properties.get("id")),
            icon_category=_as_int(properties.get("iconCategory")),
            magnitude=_as_int(properties.get("magnitudeOfDelay")),
            description=description,
            event_descriptions=tuple(event_descriptions),
            from_place=_text(properties.get("from")),
            to_place=_text(properties.get("to")),
            road_numbers=tuple(road_numbers),
            delay_seconds=_as_int(properties.get("delay")),
            length_meters=_as_float(properties.get("length")),
            start_time=_text(properties.get("startTime")),
            end_time=_text(properties.get("endTime")),
            time_validity=_text(properties.get("timeValidity")),
            probability=_text(properties.get("probabilityOfOccurrence")),
            distance_km=distance,
        ),
        None,
    )

def _magnitude_label(magnitude: Optional[int]) -> Optional[str]:
    """Human label for ``magnitudeOfDelay``; ``None`` when the vendor sent nothing."""
    if magnitude is None:
        return None
    return _MAGNITUDE_LABELS.get(magnitude, "unmapped value {0}".format(magnitude))


def _incident_summary(
    incident: _Incident, *, scope: str, endpoint: base.Endpoint
) -> str:
    """One readable sentence per incident: what, how close, how bad, where.

    The summary never promotes an ungraded incident to "low": when TomTom reports
    ``magnitudeOfDelay`` 0 the sentence says the severity is unknown, and the signal's
    ``severity`` stays ``None`` so the fusion layer can see the difference.
    """
    parts = [
        "{0} {1:.1f} km from {2} ({3} airport)".format(
            incident.label.capitalize(), incident.distance_km, endpoint.iata, scope
        )
    ]
    if incident.place:
        parts.append("near {0}".format(incident.place))
    if incident.road_numbers:
        parts.append("on {0}".format(", ".join(incident.road_numbers)))
    sentence = ", ".join(parts) + "."

    details: List[str] = []
    if incident.magnitude is not None:
        details.append(
            "Vendor magnitude {0} ({1})".format(
                incident.magnitude, _magnitude_label(incident.magnitude)
            )
        )
    if incident.delay_seconds is not None:
        details.append(
            "{0:.1f} min reported delay".format(incident.delay_seconds / 60.0)
        )
    if incident.length_meters is not None:
        details.append("affects {0:.0f} m of road".format(incident.length_meters))
    if details:
        sentence += " " + "; ".join(details) + "."

    if incident.severity is None:
        if incident.magnitude is None:
            reason = "the vendor sent no magnitude at all"
        else:
            reason = "magnitude {0} means 'unknown' to TomTom".format(
                incident.magnitude
            )
        sentence += (
            " Severity is ungraded ({0}); it is not evidence that the disruption is "
            "minor."
        ).format(reason)

    if incident.description:
        sentence += " " + incident.description.rstrip(".") + "."
    return sentence


def _severity_source(incident: _Incident) -> str:
    """Which rule produced the severity - quoted in metrics so it can be audited."""
    if incident.icon_category == _ROAD_CLOSED_ICON:
        return "iconCategory 8 closure floor (high) applied over magnitudeOfDelay"
    if incident.magnitude in _MAGNITUDE_SEVERITY:
        return "magnitudeOfDelay {0}".format(incident.magnitude)
    return "ungraded: magnitudeOfDelay {0}".format(incident.magnitude)


def _road_class(incident: _Incident) -> str:
    """The only road class the vendor actually supports: numbered route, or unknown."""
    return _ROAD_CLASS_NUMBERED if incident.road_numbers else _ROAD_CLASS_UNCLASSIFIED


def _road_class_basis(incident: _Incident) -> str:
    """Why the class is what it is, quoted in the metrics so a reader can audit it."""
    if incident.road_numbers:
        return (
            "TomTom published roadNumbers ({0}), so this incident is on a numbered "
            "route."
        ).format(", ".join(incident.road_numbers))
    return (
        "TomTom published no roadNumbers for this incident and incidentDetails carries "
        "no road-class field, so the class is unknown rather than minor; "
        "from_place/to_place carry the vendor's street names for relevance reasoning."
    )


def _incident_metrics(
    incident: _Incident,
    *,
    scope: str,
    endpoint: base.Endpoint,
    travel_date: datetime.date,
    date_window: str,
) -> Dict[str, Any]:
    """The numbers behind one incident signal, in our units."""
    return {
        "scope": scope,
        "airport_iata": endpoint.iata,
        "travel_date": travel_date.isoformat(),
        "date_window": date_window,
        "radius_km": _RADIUS_KM,
        "distance_km": round(incident.distance_km, 2),
        # Where it is, in the vendor's own words: fusion needs to tell a closure on a
        # numbered trunk route from one on a side street, and distance alone cannot.
        "place": incident.place,
        "from_place": incident.from_place,
        "to_place": incident.to_place,
        "road_class": _road_class(incident),
        "road_class_basis": _road_class_basis(incident),
        "on_numbered_route": bool(incident.road_numbers),
        "event_descriptions": list(incident.event_descriptions),
        "vendor_incident_id": incident.vendor_id,
        "icon_category": incident.icon_category,
        "icon_label": incident.label,
        "magnitude_of_delay": incident.magnitude,
        "magnitude_label": _magnitude_label(incident.magnitude),
        "severity_source": _severity_source(incident),
        "delay_seconds": incident.delay_seconds,
        "delay_minutes": (
            round(incident.delay_seconds / 60.0, 1)
            if incident.delay_seconds is not None
            else None
        ),
        "length_meters": incident.length_meters,
        "road_numbers": list(incident.road_numbers),
        "time_validity": incident.time_validity,
        "probability_of_occurrence": incident.probability,
        "start_time": incident.start_time,
        "end_time": incident.end_time,
    }


def _incident_signal(
    incident: _Incident,
    *,
    scope: str,
    endpoint: base.Endpoint,
    travel_date: datetime.date,
    date_window: str,
    observed_at: str,
    evidence_id: str,
) -> base.SignalEntry:
    """Turn one parsed incident into a normalized signal citing its evidence."""
    prefix = _TOMORROW_PREFIX if date_window == _DATE_WINDOW_TOMORROW else ""
    return base.make_signal(
        _SIGNAL_KIND_INCIDENT,
        prefix + _incident_summary(incident, scope=scope, endpoint=endpoint),
        severity=incident.severity,
        confidence=_confidence_for(incident.severity, date_window),
        observed_at=observed_at,
        metrics=_incident_metrics(
            incident,
            scope=scope,
            endpoint=endpoint,
            travel_date=travel_date,
            date_window=date_window,
        ),
        evidence_ids=[evidence_id],
    )

def _incident_evidence(
    incident: _Incident,
    *,
    scope: str,
    endpoint: base.Endpoint,
    observed_at: str,
    raw_path: Optional[str],
    evidence_id: str,
) -> base.EvidenceEntry:
    """Evidence for one incident: the vendor facts, the distance, and the raw file."""
    return base.make_evidence(
        config.SOURCE_TOMTOM,
        field="incident",
        value=incident.evidence_value(),
        observed_at=observed_at,
        raw_response_path=raw_path,
        note=(
            "Incident {0} was returned by the {1} km bounding box around {2} and is "
            "{3:.2f} km from the airport (closest geometry vertex)."
        ).format(
            incident.vendor_id or "without an id",
            _RADIUS_KM,
            endpoint.iata,
            incident.distance_km,
        ),
        evidence_id=evidence_id,
    )


def _query_evidence(
    *,
    scope: str,
    endpoint: base.Endpoint,
    observed_at: str,
    raw_path: Optional[str],
    evidence_id: str,
    returned: int,
    inside: int,
    unplaced: int,
    bbox: Tuple[float, float, float, float],
) -> base.EvidenceEntry:
    """Evidence for the airport-level query: the bbox, the counts, and the raw file.

    This is what a "checked and clear" signal cites, so the absence of incidents is
    traceable to a real response rather than to an empty signal list.
    """
    return base.make_evidence(
        config.SOURCE_TOMTOM,
        field="incidents_within_radius",
        value=inside,
        observed_at=observed_at,
        raw_response_path=raw_path,
        note=(
            "{0} km around {1} ({2} airport): bbox={3}; vendor returned {4} incident(s); "
            "{5} inside the radius; {6} outside it; {7} could not be placed."
        ).format(
            _RADIUS_KM,
            endpoint.iata,
            scope,
            _format_bbox(bbox),
            returned,
            inside,
            max(returned - inside - unplaced, 0),
            unplaced,
        ),
        evidence_id=evidence_id,
    )


def _clear_signal(
    *,
    scope: str,
    endpoint: base.Endpoint,
    travel_date: datetime.date,
    date_window: str,
    observed_at: str,
    evidence_id: str,
    returned: int,
    outside: int,
    unplaced: int = 0,
) -> base.SignalEntry:
    """The explicit "checked, no incidents inside the radius" signal.

    Without this, an airport with a clean incident list would contribute no signal at
    all, and the fusion layer could not tell "we looked and it is clear" apart from "we
    could not look". Severity is ``low`` because that is the finding: no known ground
    disruption at query time. Confidence is below an observed incident because absence
    of a report is weaker evidence than a report.
    """
    prefix = _TOMORROW_PREFIX if date_window == _DATE_WINDOW_TOMORROW else ""
    elsewhere = (
        ""
        if not returned
        else (
            " TomTom returned {0} incident(s) inside the bounding box but outside the "
            "{1:.0f} km radius, so they are not counted.".format(returned, _RADIUS_KM)
        )
    )
    confidence = _CONFIDENCE_CLEAR
    if date_window == _DATE_WINDOW_TOMORROW:
        confidence *= _TOMORROW_CONFIDENCE_FACTOR
    return base.make_signal(
        _SIGNAL_KIND_CLEAR,
        (
            "{0}No traffic incidents within {1:.0f} km of {2} ({3} airport) at {4}.{5} "
            "This is a checked-and-clear result for the airport's ground segment, not "
            "missing data."
        ).format(
            prefix,
            _RADIUS_KM,
            endpoint.iata,
            scope,
            observed_at,
            elsewhere,
        ),
        severity="low",
        confidence=round(confidence, 3),
        observed_at=observed_at,
        metrics={
            "scope": scope,
            "airport_iata": endpoint.iata,
            "travel_date": travel_date.isoformat(),
            "date_window": date_window,
            "radius_km": _RADIUS_KM,
            "incidents_within_radius": 0,
            "incidents_returned": returned,
            "incidents_outside_radius": outside,
            "incidents_unplaced": unplaced,
        },
        evidence_ids=[evidence_id],
    )

# --------------------------------------------------------------------------- #
# Failure classification - credentials, quota, plan gaps and outages are different
# --------------------------------------------------------------------------- #
def _mentions(text: str, hints: Sequence[str]) -> bool:
    """Whether a vendor code/message contains one of the known hint substrings."""
    lowered = text.lower()
    return any(hint in lowered for hint in hints)


def _vendor_error_from_result(
    result: base.HttpResult,
) -> Tuple[Optional[str], Optional[str]]:
    """The vendor's ``detailedError`` for a failed call.

    ``base.get_json`` returns ``payload=None`` for every non-2xx response, so a 401/403
    error body survives only in the raw evidence record written before parsing (and in
    the truncated ``body_preview``). The raw record is read first: without it a plan
    restriction would be indistinguishable from an outage, since all that would remain
    is "HTTP 403". This is the same gap that was found and fixed for Aviationstack.
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


def _classify_failure(
    result: base.HttpResult, vendor_code: Optional[str], vendor_message: Optional[str]
) -> str:
    """Map one failed call onto the shared error-code vocabulary.

    Order is deliberate:

    * 429 (or a quota-shaped vendor code) is throttling - the source works.
    * 401 is the *observed* key error (the docs say 403, the live API returns 401 with
      ``code: "Unauthorized"``), so it is a credential problem.
    * 403 is documented as "API key not valid **for this request**". Invalid keys are
      already caught as 401, so a 403 is an entitlement/coverage refusal: ``plan_limit``
      (``unavailable``), which is exactly the free-tier story this source must report
      honestly rather than as an outage.
    * Anything else keeps the HTTP layer's own classification.
    """
    code = (vendor_code or "").strip()
    message = (vendor_message or "").strip()
    if result.status_code == 429 or _mentions(code, _QUOTA_CODE_HINTS):
        return base.ErrorCode.RATE_LIMITED
    if result.status_code == 401:
        return base.ErrorCode.UNAUTHORIZED
    if result.status_code == 403:
        return base.ErrorCode.PLAN_LIMIT
    if _mentions(code, _CREDENTIAL_CODE_HINTS):
        return base.ErrorCode.UNAUTHORIZED
    if _mentions(code, _PLAN_CODE_HINTS) or _mentions(message, _PLAN_CODE_HINTS):
        return base.ErrorCode.PLAN_LIMIT
    if result.error_code:
        return result.error_code
    # A 2xx body carrying an error object is a payload surprise, not "no incidents".
    return (
        base.ErrorCode.UNEXPECTED_PAYLOAD_SHAPE
        if result.ok
        else base.ErrorCode.UNEXPECTED_ERROR
    )


def _failure_detail(
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

# --------------------------------------------------------------------------- #
# Per-airport query - one request, one outcome, never an exception
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class _AirportOutcome:
    """What one airport's query produced: signals/evidence, or a recorded failure."""

    scope: str
    endpoint: base.Endpoint
    signals: List[base.SignalEntry] = dataclasses.field(default_factory=list)
    evidence: List[base.EvidenceEntry] = dataclasses.field(default_factory=list)
    failure: Optional[base.SubQueryFailure] = None
    raw_path: Optional[str] = None
    raw_paths: List[str] = dataclasses.field(default_factory=list)
    latency_ms: Optional[float] = None
    warnings: List[str] = dataclasses.field(default_factory=list)


def _incident_url() -> str:
    """The Incident Details v5 URL under the configured base URL (key never in it)."""
    return "{0}{1}".format(config.TOMTOM_BASE_URL.rstrip("/"), _INCIDENT_PATH)


def _resolve_point(
    endpoint: base.Endpoint,
) -> Tuple[Optional[Tuple[float, float]], str, Optional[str]]:
    """Coordinates for an endpoint: ``(point, source, problem)``.

    Explicit coordinates on the endpoint win; otherwise the static, auditable
    ``iata_lookup`` table is used. An unknown code with no coordinates is a named
    coverage gap - this source never geocodes and never guesses a location.
    """
    if endpoint.has_coordinates:
        return (
            (float(endpoint.latitude), float(endpoint.longitude)),
            "request",
            None,
        )
    info = iata_lookup.lookup_airport(endpoint.iata)
    if info is None:
        return (
            None,
            "none",
            (
                "{0} is not in this app's static airport table ({1} US airports) and the "
                "request carried no coordinates, so no bounding box could be built around "
                "it. No location was guessed and no other airport was substituted."
            ).format(endpoint.iata, len(iata_lookup.supported_airports())),
        )
    return ((info.latitude, info.longitude), "iata_lookup", None)


def _failure_message(
    error_code: str,
    detail: str,
    *,
    endpoint: base.Endpoint,
    scope: str,
) -> str:
    """A code-specific sentence naming the limitation, quoting the vendor's words."""
    if error_code == base.ErrorCode.PLAN_LIMIT:
        return (
            "TomTom refused the incident query for {0} ({1} airport) as an entitlement "
            "limit ({2}). The free tier lists 2.5K requests/month for the Traffic APIs; "
            "if incident details are not included for this key, that is a coverage gap "
            "and not an outage, and no incidents were inferred from the gap."
        ).format(endpoint.iata, scope, detail)
    if error_code == base.ErrorCode.UNAUTHORIZED:
        return (
            "TomTom rejected the API key for {0} ({1} airport): {2}. Check "
            "TOMTOM_API_KEY; no default key and no substitute source were used."
        ).format(endpoint.iata, scope, detail)
    if error_code == base.ErrorCode.RATE_LIMITED:
        return (
            "TomTom rate-limited the incident query for {0} ({1} airport) ({2}). This is "
            "throttling or an exhausted quota, not an empty road network."
        ).format(endpoint.iata, scope, detail)
    return "TomTom incident query failed for {0} ({1} airport): {2}.".format(
        endpoint.iata, scope, detail
    )


def _failure_warnings(error_code: str) -> List[str]:
    """The caveat that belongs with each failure class."""
    if error_code == base.ErrorCode.PLAN_LIMIT:
        return [
            "A plan or entitlement limit is a coverage gap: neither a failure nor "
            "'no disruption'."
        ]
    if error_code == base.ErrorCode.UNAUTHORIZED:
        return [
            "Set TOMTOM_API_KEY in .env to enable this source; a missing key returns "
            "HTTP 401 from the live API."
        ]
    if error_code == base.ErrorCode.RATE_LIMITED:
        return ["This is throttling or an exhausted quota, not 'no incidents'."]
    return []

def _outcome_for_result(
    result: base.HttpResult,
    *,
    scope: str,
    endpoint: base.Endpoint,
    latitude: float,
    longitude: float,
    bbox: Tuple[float, float, float, float],
    travel_date: datetime.date,
    date_window: str,
    observed_at: str,
) -> _AirportOutcome:
    """Interpret one HTTP result for one airport.

    Split out of the request so every branch is testable without a network: a failure,
    a success with incidents, a success with none, and a 2xx body that is really an
    error are all decided here.
    """
    outcome = _AirportOutcome(
        scope=scope,
        endpoint=endpoint,
        raw_path=result.raw_path,
        raw_paths=list(result.raw_paths),
        latency_ms=result.latency_ms,
    )

    # A failed call, or an error object delivered with a 2xx status, is classified
    # before anything is read as incident data.
    if not result.ok or _vendor_error(result.payload)[0] is not None:
        vendor_code, vendor_message = _vendor_error_from_result(result)
        error_code = _classify_failure(result, vendor_code, vendor_message)
        detail = _failure_detail(result, vendor_code, vendor_message)
        outcome.failure = {
            "scope": "{0} ({1})".format(scope, endpoint.iata),
            "error_code": error_code,
            "error": _failure_message(
                error_code, detail, endpoint=endpoint, scope=scope
            ),
        }
        outcome.warnings.extend(_failure_warnings(error_code))
        return outcome

    payload = _mapping(result.payload)
    records = payload.get("incidents")
    if not isinstance(records, list):
        outcome.failure = {
            "scope": "{0} ({1})".format(scope, endpoint.iata),
            "error_code": base.ErrorCode.UNEXPECTED_PAYLOAD_SHAPE,
            "error": (
                "TomTom returned a body with no 'incidents' array (top-level keys: {0}), "
                "so no incident could be read for {1}. The raw payload is on disk."
            ).format(", ".join(sorted(payload)) or "none", endpoint.iata),
        }
        outcome.warnings.append(
            "An unexpected payload shape is reported as such, never as 'no incidents'."
        )
        return outcome

    parsed: List[_Incident] = []
    unplaced: List[str] = []
    for record in records:
        incident, reason = _parse_incident(
            record, latitude=latitude, longitude=longitude
        )
        if incident is not None:
            parsed.append(incident)
        elif reason:
            unplaced.append(reason)

    # Worst first, then closest: deterministic, and the signal that matters most to a
    # traveller is the one a reader sees first. Fusion re-weighs anyway.
    parsed.sort(key=lambda item: (-_severity_rank(item.severity), item.distance_km))
    outside = max(len(records) - len(parsed) - len(unplaced), 0)

    query_evidence_id = "ev_{0}_query".format(scope)
    outcome.evidence.append(
        _query_evidence(
            scope=scope,
            endpoint=endpoint,
            observed_at=observed_at,
            raw_path=result.raw_path,
            evidence_id=query_evidence_id,
            returned=len(records),
            inside=len(parsed),
            unplaced=len(unplaced),
            bbox=bbox,
        )
    )

    for index, incident in enumerate(parsed, start=1):
        evidence_id = "ev_{0}_{1}".format(scope, index)
        outcome.evidence.append(
            _incident_evidence(
                incident,
                scope=scope,
                endpoint=endpoint,
                observed_at=observed_at,
                raw_path=result.raw_path,
                evidence_id=evidence_id,
            )
        )
        outcome.signals.append(
            _incident_signal(
                incident,
                scope=scope,
                endpoint=endpoint,
                travel_date=travel_date,
                date_window=date_window,
                observed_at=observed_at,
                evidence_id=evidence_id,
            )
        )

    if not parsed:
        outcome.signals.append(
            _clear_signal(
                scope=scope,
                endpoint=endpoint,
                travel_date=travel_date,
                date_window=date_window,
                observed_at=observed_at,
                evidence_id=query_evidence_id,
                returned=len(records),
                outside=outside,
                unplaced=len(unplaced),
            )
        )

    if unplaced:
        outcome.warnings.append(
            "{0} incident(s) near {1} had no usable Point/LineString geometry, so they "
            "could be neither placed inside nor outside the {2:.0f} km radius and are "
            "not assessed.".format(len(unplaced), endpoint.iata, _RADIUS_KM)
        )
    future = [item for item in parsed if (item.time_validity or "").lower() == "future"]
    if future:
        outcome.warnings.append(
            "{0} incident(s) near {1} were marked 'future' even though the request asked "
            "for present incidents only; they are reported as returned.".format(
                len(future), endpoint.iata
            )
        )
    return outcome

async def _query_airport(
    *,
    scope: str,
    endpoint: base.Endpoint,
    latitude: float,
    longitude: float,
    travel_date: datetime.date,
    date_window: str,
    observed_at: str,
) -> _AirportOutcome:
    """Run one incident query for one airport, converting anything into an outcome.

    No exception escapes: a transport failure comes back from ``base.get_json`` as a
    failed ``HttpResult``, and a bug in this module becomes an ``unexpected_error``
    outcome for this airport only, so the other airport's real signals survive.
    """
    bbox = _bbox_around(latitude, longitude)
    params: Dict[str, Any] = {
        "key": config.TOMTOM_API_KEY,
        "bbox": _format_bbox(bbox),
        "fields": _FIELDS,
        "language": "en-GB",
        # Present incidents only: the feed's "future" bucket holds planned works, which
        # this source deliberately does not treat as a forecast (see the docstring).
        "timeValidityFilter": "present",
    }
    result: Optional[base.HttpResult] = None
    try:
        result = await base.get_json(
            config.SOURCE_TOMTOM,
            _incident_url(),
            params=params,
            context={
                "endpoint": "incidentDetails",
                "version": 5,
                "scope": scope,
                "airport": endpoint.iata,
                "radius_km": _RADIUS_KM,
                "travel_date": travel_date.isoformat(),
                "date_window": date_window,
            },
        )
        return _outcome_for_result(
            result,
            scope=scope,
            endpoint=endpoint,
            latitude=latitude,
            longitude=longitude,
            bbox=bbox,
            travel_date=travel_date,
            date_window=date_window,
            observed_at=observed_at,
        )
    except Exception as exc:  # rule 2: nothing escapes a data source module
        outcome = _AirportOutcome(scope=scope, endpoint=endpoint)
        if result is not None:
            outcome.raw_path = result.raw_path
            outcome.raw_paths = list(result.raw_paths)
            outcome.latency_ms = result.latency_ms
        outcome.failure = {
            "scope": "{0} ({1})".format(scope, endpoint.iata),
            "error_code": base.ErrorCode.UNEXPECTED_ERROR,
            "error": (
                "Unexpected {0} while processing TomTom data for {1}: {2}"
            ).format(
                type(exc).__name__,
                endpoint.iata,
                base.redact_text(str(exc)),
            ),
        }
        outcome.warnings.append(
            "This is a bug in this module, not a TomTom outage; the raw response on disk "
            "is the ground truth for this query."
        )
        return outcome

def _route_warnings(date_window: str, observed_at: str) -> List[str]:
    """Warnings that apply to the whole route, whatever the individual airports said."""
    warnings = [
        "TomTom's incident feed reports current conditions: these signals describe the "
        "road network at query time ({0}), not the travel date.".format(observed_at),
    ]
    if date_window == _DATE_WINDOW_TOMORROW:
        warnings.append(
            "Travel date is tomorrow: current incidents are indicative only, every "
            "summary is prefixed accordingly, and confidence is scaled by {0}. That is a "
            "documented heuristic, not a forecast.".format(_TOMORROW_CONFIDENCE_FACTOR)
        )
    return warnings


def _combine(
    outcomes: Sequence[_AirportOutcome],
    *,
    location: base.Location,
    travel_date: datetime.date,
    date_window: str,
    observed_at: str,
) -> base.NormalizedSignals:
    """Fold the per-airport outcomes into one envelope with an honest status.

    * no failures → ``ok`` (every airport either produced incident signals or an explicit
      checked-and-clear signal);
    * some signals and some failures → ``partial``, with one ``error_code`` per failed
      airport and every surviving signal still citing evidence;
    * nothing survived → ``error`` when a call actually failed, ``unavailable`` when the
      failures only mean "this query cannot be answered" (plan limit, unknown airport).
    """
    signals = [signal for outcome in outcomes for signal in outcome.signals]
    evidence = [entry for outcome in outcomes for entry in outcome.evidence]
    raw_paths = [path for outcome in outcomes for path in outcome.raw_paths]
    failures = [
        outcome.failure for outcome in outcomes if outcome.failure is not None
    ]
    latencies = [
        outcome.latency_ms
        for outcome in outcomes
        if outcome.latency_ms is not None
    ]
    latency = round(sum(latencies), 3) if latencies else None
    raw_primary = next(
        (outcome.raw_path for outcome in outcomes if outcome.raw_path), None
    )
    warnings = _route_warnings(date_window, observed_at) + [
        warning for outcome in outcomes for warning in outcome.warnings
    ]
    shared: Dict[str, Any] = {
        "source": config.SOURCE_TOMTOM,
        "location": location,
        "travel_date": travel_date,
        "raw_response_path": raw_primary,
        "raw_response_paths": raw_paths,
        "latency_ms": latency,
        "warnings": warnings,
        "retrieved_at": observed_at,
    }

    if not failures:
        return base.ok_signals(signals=signals, evidence=evidence, **shared)

    if signals:
        return base.partial_signals(
            signals=signals, failures=failures, evidence=evidence, **shared
        )

    codes = [str(failure["error_code"]) for failure in failures]
    detail = "; ".join(
        "{0}: {1}".format(failure["scope"], failure["error"])
        for failure in failures
    )
    if all(
        base.status_for_error_code(code) == config.STATUS_UNAVAILABLE for code in codes
    ):
        return base.unavailable_signals(
            error_code=codes[0],
            error=(
                "No airport on this route could be assessed for ground disruptions, and "
                "that is a coverage gap rather than an outage. {0}"
            ).format(detail),
            **shared,
        )
    failed_code = next(
        code
        for code in codes
        if base.status_for_error_code(code) != config.STATUS_UNAVAILABLE
    )
    return base.failure_signals(
        error_code=failed_code,
        error=(
            "TomTom incident data could not be fetched for either airport, so no ground "
            "disruption was assessed. {0}"
        ).format(detail),
        **shared,
    )


def _out_of_range_envelope(
    *, location: base.Location, travel_date: datetime.date
) -> base.NormalizedSignals:
    """The explicit "this feed cannot answer that date" envelope.

    No request is made: the limitation is the feed's, so spending quota on a call that
    cannot answer the question would be waste, and the answer would still be the same.
    """
    days = (travel_date - base.utc_now().date()).days
    if days < 0:
        clause = "{0} day(s) before today".format(abs(days))
    else:
        clause = "{0} days from today".format(days)
    return base.unavailable_signals(
        source=config.SOURCE_TOMTOM,
        error_code=base.ErrorCode.NO_DATA_FOR_QUERY,
        error=(
            "TomTom traffic incidents are real-time only: this source can answer for "
            "today and, with a documented caveat, tomorrow. The travel date {0} is {1}, "
            "so there is no way to know now what ground conditions will be like then. "
            "No current incident was returned as if it predicted that date, and no "
            "request was made. TomTom does expose planned future incidents (roadworks "
            "and similar); this source does not consume them yet."
        ).format(travel_date.isoformat(), clause),
        location=location,
        travel_date=travel_date,
        warnings=[
            "Real-time feed, future date: this is a limitation, not an outage and not "
            "'clear roads'.",
            "No quota was spent: the module returned before making any HTTP request.",
        ],
    )

# --------------------------------------------------------------------------- #
# The one public entry point (rule 4)
# --------------------------------------------------------------------------- #
async def fetch_signals(
    location: base.Location, date: datetime.date
) -> base.NormalizedSignals:
    """Fetch TomTom road-incident signals around the route's airports.

    * ``ok`` - both airports answered. Each airport contributes either one signal per
      incident inside 15 km, or an explicit ``traffic_conditions`` signal saying it was
      checked and nothing was found.
    * ``partial`` - one airport answered and the other did not. The surviving signals
      are returned with the failure recorded (``error_codes``), never discarded.
    * ``error`` / ``unavailable`` - neither airport could be assessed; the code says
      whether the source failed or the query simply cannot be answered.

    The travel-date window is checked before the credential: it is a property of the
    query, not of the key, and a date this feed cannot answer (today + 1 at most) gets
    the same honest answer with or without a key - and spends no quota.

    Nothing the vendor does can raise out of this function; the only ``ValueError`` is
    for a programming error in the arguments, matching ``base.get_json``.
    """
    if isinstance(date, datetime.datetime):
        travel_date = date.date()
    elif isinstance(date, datetime.date):
        travel_date = date
    else:
        raise ValueError("fetch_signals requires a datetime.date travel date")
    if not isinstance(location, base.Location):
        raise ValueError("fetch_signals requires a data_sources.base.Location route")

    date_window = _date_window(travel_date)
    if date_window == _DATE_WINDOW_OUT_OF_RANGE:
        return _out_of_range_envelope(location=location, travel_date=travel_date)

    blocked = base.missing_credential_signals(
        config.SOURCE_TOMTOM, location=location, travel_date=travel_date
    )
    if blocked is not None:
        return blocked

    observed_at = base.utc_now_iso()
    try:
        outcomes: List[_AirportOutcome] = []
        # One airport at a time, two requests in total: sequential keeps the evidence
        # order deterministic and never opens a second client for one query.
        for scope, endpoint in (
            ("origin", location.origin),
            ("destination", location.destination),
        ):
            point, _source, problem = _resolve_point(endpoint)
            if point is None:
                unresolved = _AirportOutcome(scope=scope, endpoint=endpoint)
                unresolved.failure = {
                    "scope": "{0} ({1})".format(scope, endpoint.iata),
                    "error_code": base.ErrorCode.NO_DATA_FOR_QUERY,
                    "error": problem or "No coordinates are available for {0}.".format(
                        endpoint.iata
                    ),
                }
                unresolved.warnings.append(
                    "Supported codes: iata_lookup.supported_airports(). A code outside "
                    "that table is a coverage limit, not an outage."
                )
                outcomes.append(unresolved)
                continue
            outcomes.append(
                await _query_airport(
                    scope=scope,
                    endpoint=endpoint,
                    latitude=point[0],
                    longitude=point[1],
                    travel_date=travel_date,
                    date_window=date_window,
                    observed_at=observed_at,
                )
            )
        return _combine(
            outcomes,
            location=location,
            travel_date=travel_date,
            date_window=date_window,
            observed_at=observed_at,
        )
    except Exception as exc:  # rule 2: nothing escapes a data source module
        return base.failure_signals(
            source=config.SOURCE_TOMTOM,
            error_code=base.ErrorCode.UNEXPECTED_ERROR,
            error=(
                "Unexpected {0} while assembling TomTom signals: {1}"
            ).format(type(exc).__name__, base.redact_text(str(exc))),
            location=location,
            travel_date=travel_date,
            warnings=[
                "This is a bug in this module, not a TomTom outage.",
                "Any raw response already written to logs/raw is the ground truth.",
            ],
        )


# Rule 4: exactly one public entry point per source module.
__all__ = ["fetch_signals"]
