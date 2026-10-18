"""Parsing, mapping, date-window and failure-mode tests for ``data_sources.tomtom``.

Transport is faked (``base.get_json`` is replaced); the network is not. Most fixtures are
**real captures** made on 2026-10-17 with a working free-tier key: ``tomtom_401_unauthorized``
(the live API answers a missing *and* an invalid key with HTTP 401, although the docs list
403), ``tomtom_incidents_JFK`` (86 v5 features around JFK) and ``tomtom_incidents_LAX``
(110 around LAX). Three documents are still **[INVENTED]**, and for stated reasons:
``tomtom_incidents_bands`` is a *unit fixture* carrying one incident per severity band plus
a road closure, an ungraded incident, one outside the radius and one with unusable
geometry - real payloads cannot be relied on to contain every band on any given day -
``tomtom_incidents_empty`` is the ``{"incidents": []}`` shape, because none of the fourteen
airports probed answered with zero incidents, and ``tomtom_403_not_entitled`` is the
documented entitlement body: this key *is* entitled to incident details, so a 403 could not
be provoked. Provenance is in ``tests/fixtures/README.md``.

The severity table is covered twice: directly on the pure helper (so every band is pinned
without any payload) and end-to-end through the band fixture. The real captures are then
held to the same mapping, plus the parser's tolerance for what the live feed actually
contains: ``delay: null`` on closures, ``endTime: null``, ``magnitudeOfDelay: 0`` and up to
five ``events`` per incident.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

import config
from backend.models import SourceStatus
from data_sources import base, iata_lookup, tomtom

FIXTURES: Path = Path(__file__).parent / "fixtures"

INCIDENT_URL: str = (
    config.TOMTOM_BASE_URL.rstrip("/") + "/traffic/services/5/incidentDetails"
)
JFK_LATITUDE: float = 40.6413
JFK_LONGITUDE: float = -73.7781

# The invented, one-incident-per-band document. Real captures cannot guarantee every band
# on a given day, so the mapping table keeps its own fixture (see the module docstring).
BANDS_FIXTURE: str = "tomtom_incidents_bands.json"


def _fixture(name: str) -> Dict[str, Any]:
    """A fixture document from ``tests/fixtures`` (see the README for provenance)."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _fixture_path(name: str) -> str:
    """Repo-relative path of a fixture, used as the faked raw-evidence path."""
    return str((FIXTURES / name).relative_to(Path(config.PROJECT_ROOT)))


def _sentinel_key() -> str:
    """A throwaway key sentinel, never a real credential."""
    return "sentinel-" + uuid.uuid4().hex


def _route(origin: str = "JFK", destination: str = "LAX") -> base.Location:
    """A route built the way the API builds it: IATA codes only, no coordinates."""
    return base.Location.from_iata(origin, destination)


def _result(
    payload: Any,
    raw_path: str,
    *,
    status_code: int = 200,
    url: str = INCIDENT_URL,
) -> base.HttpResult:
    """A ``HttpResult`` wrapping a fixture document; only the transport is faked."""
    return base.HttpResult(
        ok=True,
        method="GET",
        url=url,
        status_code=status_code,
        latency_ms=12.0,
        attempts=1,
        payload=payload,
        raw_path=raw_path,
        raw_paths=(raw_path,),
        error=None,
        error_code=None,
        response_headers={"content-type": "application/json"},
        body_preview=None,
        content_type="application/json",
    )


def _failure(
    raw_path: str,
    *,
    status_code: int,
    payload: Any = None,
    body_preview: Optional[str] = None,
    url: str = INCIDENT_URL,
    attempts: int = 1,
) -> base.HttpResult:
    """A failed ``HttpResult``, as ``get_json`` returns after exhausting attempts.

    ``payload`` is ``None`` on purpose: ``base.get_json`` drops the body of every non-2xx
    response, which is exactly the situation the module must cope with.
    """
    return base.HttpResult(
        ok=False,
        method="GET",
        url=url,
        status_code=status_code,
        latency_ms=9.0,
        attempts=attempts,
        payload=payload,
        raw_path=raw_path,
        raw_paths=(raw_path,),
        error="HTTP {0} from {1}".format(status_code, url),
        error_code=base.error_code_for_status(status_code),
        response_headers={},
        body_preview=body_preview,
        content_type="application/json",
    )


class _FakeTransport:
    """A ``get_json`` stand-in returning prepared results in call order."""

    def __init__(self, *results: base.HttpResult, error: Optional[BaseException] = None):
        self.results = list(results)
        self.calls: List[Dict[str, Any]] = []
        self.error = error

    async def __call__(self, source: str, url: str, **kwargs: Any) -> base.HttpResult:
        self.calls.append({"source": source, "url": url, "kwargs": kwargs})
        if self.error is not None:
            raise self.error
        if len(self.calls) > len(self.results):
            raise AssertionError(
                "unexpected extra request: {0} (only {1} prepared)".format(
                    url, len(self.results)
                )
            )
        return self.results[len(self.calls) - 1]

    def params(self, index: int = 0) -> Dict[str, Any]:
        """The query parameters of the request at ``index``."""
        return dict(self.calls[index]["kwargs"].get("params") or {})


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every test a credential, so behaviour never depends on the environment."""
    monkeypatch.setattr(config, "TOMTOM_API_KEY", _sentinel_key())


def _fetch(
    location: base.Location,
    travel_date: datetime.date,
    transport: _FakeTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> base.NormalizedSignals:
    """Run ``fetch_signals`` against a faked transport."""
    monkeypatch.setattr(tomtom.base, "get_json", transport)
    return asyncio.run(tomtom.fetch_signals(location, travel_date))

# --------------------------------------------------------------------------- #
# Pure helpers: geography
# --------------------------------------------------------------------------- #
def test_bbox_contains_the_radius_in_every_direction() -> None:
    """The box must be a superset of the circle, else a real incident gets dropped."""
    bbox = tomtom._bbox_around(JFK_LATITUDE, JFK_LONGITUDE)
    min_lon, min_lat, max_lon, max_lat = bbox
    assert min_lat < JFK_LATITUDE < max_lat
    assert min_lon < JFK_LONGITUDE < max_lon
    for edge in (
        (max_lat, JFK_LONGITUDE),
        (min_lat, JFK_LONGITUDE),
        (JFK_LATITUDE, max_lon),
        (JFK_LATITUDE, min_lon),
    ):
        assert (
            tomtom._haversine_km(JFK_LATITUDE, JFK_LONGITUDE, edge[0], edge[1])
            >= tomtom._RADIUS_KM
        )


def test_bbox_stays_inside_tomtoms_area_limit() -> None:
    """v5 caps a bbox at 10,000 km²; a 15 km box must be far below that."""
    min_lon, min_lat, max_lon, max_lat = tomtom._bbox_around(
        JFK_LATITUDE, JFK_LONGITUDE
    )
    height = tomtom._haversine_km(JFK_LATITUDE, JFK_LONGITUDE, max_lat, JFK_LONGITUDE)
    width = tomtom._haversine_km(JFK_LATITUDE, JFK_LONGITUDE, JFK_LATITUDE, max_lon)
    assert (2 * height) * (2 * width) < 10_000


def test_bbox_is_formatted_as_min_lon_min_lat_max_lon_max_lat() -> None:
    """TomTom expects ``minLon,minLat,maxLon,maxLat`` in EPSG:4326, no spaces."""
    bbox = tomtom._bbox_around(JFK_LATITUDE, JFK_LONGITUDE)
    parts = tomtom._format_bbox(bbox).split(",")
    assert len(parts) == 4
    assert all(" " not in part for part in parts)
    assert all(len(part.split(".")[-1]) == 6 for part in parts)
    assert float(parts[0]) == bbox[0]
    assert float(parts[1]) == bbox[1]


def test_bbox_handles_a_near_polar_point_without_blowing_up() -> None:
    """A longitude delta near the pole is huge; the box must still be valid."""
    min_lon, min_lat, max_lon, max_lat = tomtom._bbox_around(89.9, 10.0)
    assert -180.0 <= min_lon <= max_lon <= 180.0
    assert -90.0 <= min_lat <= max_lat <= 90.0
    assert max_lat == 90.0


@pytest.mark.parametrize(
    "latitude, longitude, radius",
    [(91.0, 0.0, 15.0), (-91.0, 0.0, 15.0), (0.0, 181.0, 15.0), (0.0, 0.0, 0.0)],
)
def test_bbox_rejects_impossible_input(
    latitude: float, longitude: float, radius: float
) -> None:
    with pytest.raises(ValueError):
        tomtom._bbox_around(latitude, longitude, radius)


def test_haversine_matches_known_distances() -> None:
    """One degree of longitude at the equator is ~111.2 km; JFK-LAX is ~3,974 km."""
    assert tomtom._haversine_km(0.0, 0.0, 0.0, 1.0) == pytest.approx(111.2, abs=0.1)
    assert tomtom._haversine_km(
        JFK_LATITUDE, JFK_LONGITUDE, 33.9416, -118.4085
    ) == pytest.approx(3974.0, abs=25.0)


def test_geometry_points_swaps_geojson_lon_lat_order() -> None:
    """GeoJSON is ``[lon, lat]``; reading it in the other order is the classic bug."""
    points = tomtom._geometry_points(
        {"type": "Point", "coordinates": [JFK_LONGITUDE, JFK_LATITUDE]}
    )
    assert points == [(JFK_LATITUDE, JFK_LONGITUDE)]


def test_geometry_points_accepts_linestrings_and_drops_bad_pairs() -> None:
    points = tomtom._geometry_points(
        {
            "type": "LineString",
            "coordinates": [
                [-73.78, 40.64],
                ["not-a-number", 40.65],
                [10.0],
                [200.0, 40.66],
                [-73.79, 40.67],
            ],
        }
    )
    assert points == [(40.64, -73.78), (40.67, -73.79)]


def test_geometry_points_ignores_unknown_geometry_types() -> None:
    assert tomtom._geometry_points({"type": "Polygon", "coordinates": []}) == []
    assert tomtom._geometry_points(None) == []

# --------------------------------------------------------------------------- #
# Pure helpers: severity, confidence and the date window
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "icon_category, magnitude, expected",
    [
        # magnitudeOfDelay: 1 minor, 2 moderate, 3 major, 4 undefined/indefinite
        (6, 1, "low"),
        (6, 2, "moderate"),
        (6, 3, "high"),
        (6, 4, "severe"),
        # 0 is documented as "Unknown": a real incident with an ungraded severity
        (6, 0, None),
        (6, None, None),
        # a road closure floors at high, and magnitude 4 raises it to severe
        (8, None, "high"),
        (8, 0, "high"),
        (8, 1, "high"),
        (8, 2, "high"),
        (8, 3, "high"),
        (8, 4, "severe"),
        # a broken-down vehicle and an unmapped icon id both grade by magnitude
        (14, 2, "moderate"),
        (99, 3, "high"),
        (None, None, None),
    ],
)
def test_severity_mapping_table(
    icon_category: Optional[int], magnitude: Optional[int], expected: Optional[str]
) -> None:
    assert tomtom._severity_for(icon_category, magnitude) == expected


def test_magnitude_zero_is_never_reported_as_low() -> None:
    """TomTom documents 0 as "Unknown"; calling that low risk would be fabricated."""
    assert tomtom._severity_for(1, 0) is None
    assert tomtom._confidence_for(None, tomtom._DATE_WINDOW_TODAY) < (
        tomtom._confidence_for("low", tomtom._DATE_WINDOW_TODAY)
    )


def test_tomorrow_confidence_is_scaled_for_every_kind_of_signal() -> None:
    for severity in (None, "low", "high", "severe"):
        today = tomtom._confidence_for(severity, tomtom._DATE_WINDOW_TODAY)
        tomorrow = tomtom._confidence_for(severity, tomtom._DATE_WINDOW_TOMORROW)
        assert tomorrow == pytest.approx(
            today * tomtom._TOMORROW_CONFIDENCE_FACTOR, abs=1e-3
        )
        assert tomorrow < today


@pytest.mark.parametrize(
    "offset, expected",
    [
        (-3, tomtom._DATE_WINDOW_OUT_OF_RANGE),
        (-1, tomtom._DATE_WINDOW_OUT_OF_RANGE),
        (0, tomtom._DATE_WINDOW_TODAY),
        (1, tomtom._DATE_WINDOW_TOMORROW),
        (2, tomtom._DATE_WINDOW_OUT_OF_RANGE),
        (10, tomtom._DATE_WINDOW_OUT_OF_RANGE),
    ],
)
def test_date_window(offset: int, expected: str) -> None:
    today = datetime.date(2026, 10, 16)
    assert (
        tomtom._date_window(today + datetime.timedelta(days=offset), today=today)
        == expected
    )

# --------------------------------------------------------------------------- #
# Parsing one vendor document (no HTTP)
# --------------------------------------------------------------------------- #
def _outcome_for(
    payload: Dict[str, Any],
    *,
    scope: str = "origin",
    iata: str = "JFK",
    travel_date: Optional[datetime.date] = None,
    date_window: str = tomtom._DATE_WINDOW_TODAY,
    latitude: float = JFK_LATITUDE,
    longitude: float = JFK_LONGITUDE,
    fixture: str = BANDS_FIXTURE,
) -> tomtom._AirportOutcome:
    """Interpret a fixture payload exactly as the module does after a real call."""
    endpoint = base.Endpoint(iata=iata, latitude=latitude, longitude=longitude)
    bbox = tomtom._bbox_around(latitude, longitude)
    return tomtom._outcome_for_result(
        _result(payload, _fixture_path(fixture)),
        scope=scope,
        endpoint=endpoint,
        latitude=latitude,
        longitude=longitude,
        bbox=bbox,
        travel_date=travel_date or datetime.date(2026, 10, 16),
        date_window=date_window,
        observed_at="2026-10-16T14:10:00Z",
    )


def _real_capture_for(iata: str) -> tomtom._AirportOutcome:
    """Interpret a **real** capture using the coordinates the module itself would use.

    The real documents were written by the module's own requests, so the airport centre
    must come from the static table rather than the test's rounded ``JFK_*`` constants,
    otherwise the distance filter would be measured from a slightly different point.
    """
    info = iata_lookup.lookup_airport(iata)
    assert info is not None, "{0} must be in the static airport table".format(iata)
    name = "tomtom_incidents_{0}.json".format(iata)
    return _outcome_for(
        _fixture(name),
        iata=iata,
        latitude=info.latitude,
        longitude=info.longitude,
        travel_date=datetime.date(2026, 10, 17),
        fixture=name,
    )


def test_fixture_parses_four_incidents_inside_the_radius() -> None:
    """Six records: four inside 15 km, one outside it, one with no usable geometry."""
    outcome = _outcome_for(_fixture("tomtom_incidents_bands.json"))

    assert outcome.failure is None
    assert len(outcome.signals) == 4
    assert all(signal["kind"] == "traffic_incident" for signal in outcome.signals)
    assert {signal["metrics"]["vendor_incident_id"] for signal in outcome.signals} == {
        "TT-EXCL-0001",
        "TT-EXCL-0002",
        "TT-EXCL-0004",
        "TT-EXCL-0005",
    }


def test_incident_outside_the_radius_is_excluded_but_counted() -> None:
    """TT-EXCL-0006 is inside the bbox but ~18 km away, so the circle filter drops it."""
    outcome = _outcome_for(_fixture("tomtom_incidents_bands.json"))

    assert "TT-EXCL-0006" not in {
        signal["metrics"]["vendor_incident_id"] for signal in outcome.signals
    }
    query_evidence = outcome.evidence[0]
    assert query_evidence["field"] == "incidents_within_radius"
    assert query_evidence["value"] == "4"
    assert "vendor returned 6 incident(s)" in (query_evidence["note"] or "")
    assert "4 inside the radius" in (query_evidence["note"] or "")
    assert "1 outside it" in (query_evidence["note"] or "")
    assert "1 could not be placed" in (query_evidence["note"] or "")


def test_unusable_geometry_is_warned_about_not_silently_dropped() -> None:
    outcome = _outcome_for(_fixture("tomtom_incidents_bands.json"))

    assert any("no usable Point/LineString geometry" in item for item in outcome.warnings)


def test_signals_are_ordered_worst_first_then_closest() -> None:
    outcome = _outcome_for(_fixture("tomtom_incidents_bands.json"))

    assert [signal["severity"] for signal in outcome.signals] == [
        "severe",
        "high",
        "low",
        None,
    ]


def test_incident_metrics_convert_vendor_units() -> None:
    """TomTom delays are seconds and lengths are meters; metrics expose both units."""
    outcome = _outcome_for(_fixture("tomtom_incidents_bands.json"))
    by_id = {
        signal["metrics"]["vendor_incident_id"]: signal for signal in outcome.signals
    }

    jam = by_id["TT-EXCL-0002"]
    assert jam["metrics"]["delay_seconds"] == 900
    assert jam["metrics"]["delay_minutes"] == 15.0
    assert jam["metrics"]["length_meters"] == 2400.0
    assert jam["metrics"]["distance_km"] == pytest.approx(13.29, abs=0.05)
    assert jam["metrics"]["scope"] == "origin"
    assert jam["metrics"]["radius_km"] == tomtom._RADIUS_KM
    assert jam["metrics"]["severity_source"] == "magnitudeOfDelay 3"

    works = by_id["TT-EXCL-0004"]
    assert works["metrics"]["icon_label"] == "road works"
    assert works["metrics"]["probability_of_occurrence"] == "certain"


def test_closure_severity_comes_from_the_floor_not_only_the_magnitude() -> None:
    """A road closure with a minor magnitude is still a high-severity disruption."""
    outcome = _outcome_for(_fixture("tomtom_incidents_bands.json"))
    closure = next(
        signal
        for signal in outcome.signals
        if signal["metrics"]["vendor_incident_id"] == "TT-EXCL-0001"
    )

    # magnitude 4 (undefined) + closure: severe, and the floor rule is recorded.
    assert closure["severity"] == "severe"
    assert "closure floor" in closure["metrics"]["severity_source"]


def test_ungraded_incident_keeps_severity_none_and_says_why() -> None:
    outcome = _outcome_for(_fixture("tomtom_incidents_bands.json"))
    ungraded = next(
        signal
        for signal in outcome.signals
        if signal["metrics"]["vendor_incident_id"] == "TT-EXCL-0005"
    )

    assert ungraded["severity"] is None
    assert "Severity is ungraded" in ungraded["summary"]
    assert "not evidence that the disruption is minor" in ungraded["summary"]
    assert ungraded["confidence"] == tomtom._CONFIDENCE_UNGRADED


def test_every_signal_cites_evidence_that_exists() -> None:
    outcome = _outcome_for(_fixture("tomtom_incidents_bands.json"))
    evidence_ids = {entry["id"] for entry in outcome.evidence}

    assert len(outcome.evidence) == 5  # one query record + four incidents
    for signal in outcome.signals:
        assert signal["evidence_ids"]
        assert set(signal["evidence_ids"]) <= evidence_ids
    assert all(
        entry["raw_response_path"] == _fixture_path("tomtom_incidents_bands.json")
        for entry in outcome.evidence
    )

def test_clear_signal_says_when_incidents_were_outside_the_radius() -> None:
    """A bbox hit that the circle filter rejects is reported, not silently ignored."""
    far = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [-73.62, 40.755]},
        "properties": {
            "id": "TT-FAR-1",
            "iconCategory": 6,
            "magnitudeOfDelay": 2,
            "events": [{"description": "Congestion far from the airport"}],
        },
    }

    outcome = _outcome_for({"incidents": [far]})

    assert len(outcome.signals) == 1
    signal = outcome.signals[0]
    assert signal["kind"] == "traffic_conditions"
    assert signal["metrics"]["incidents_within_radius"] == 0
    assert signal["metrics"]["incidents_returned"] == 1
    assert signal["metrics"]["incidents_outside_radius"] == 1
    assert "outside the 15 km radius" in signal["summary"]


# --------------------------------------------------------------------------- #
# fetch_signals: the happy path, request shaping and the checked-and-clear signal
# --------------------------------------------------------------------------- #
def _today() -> datetime.date:
    return base.utc_now().date()


def _two_airport_transport() -> _FakeTransport:
    """JFK with incidents, LAX with none - the demo path."""
    return _FakeTransport(
        _result(
            _fixture("tomtom_incidents_bands.json"),
            _fixture_path("tomtom_incidents_bands.json"),
        ),
        _result(
            _fixture("tomtom_incidents_empty.json"),
            _fixture_path("tomtom_incidents_empty.json"),
        ),
    )


def test_ok_envelope_from_two_airports(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _two_airport_transport()
    envelope = _fetch(_route(), _today(), transport, monkeypatch)

    assert envelope["status"] == "ok"
    assert envelope["error"] is None
    assert envelope["error_code"] is None
    assert envelope["records"] == len(envelope["signals"]) == 5  # 4 JFK + 1 LAX
    assert envelope["travel_date"] == _today().isoformat()
    assert envelope["location"]["route_label"] == "JFK -> LAX"
    assert envelope["raw_response_paths"] == [
        _fixture_path("tomtom_incidents_bands.json"),
        _fixture_path("tomtom_incidents_empty.json"),
    ]
    assert len(transport.calls) == 2


def test_empty_incident_list_becomes_a_checked_and_clear_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No incidents near one airport is a finding, not an empty signal list."""
    envelope = _fetch(_route(), _today(), _two_airport_transport(), monkeypatch)
    clear = [
        signal
        for signal in envelope["signals"]
        if signal["kind"] == "traffic_conditions"
    ]

    assert len(clear) == 1
    signal = clear[0]
    assert signal["severity"] == "low"
    assert signal["confidence"] == tomtom._CONFIDENCE_CLEAR
    assert signal["metrics"]["scope"] == "destination"
    assert signal["metrics"]["airport_iata"] == "LAX"
    assert signal["metrics"]["incidents_within_radius"] == 0
    assert "LAX" in signal["summary"]
    assert "checked-and-clear" in signal["summary"]
    assert signal["evidence_ids"] == ["ev_destination_query"]


def test_requests_are_shaped_for_the_free_tier_and_the_v5_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _two_airport_transport()
    _fetch(_route(), _today(), transport, monkeypatch)

    assert len(transport.calls) == 2
    first = transport.calls[0]
    assert first["source"] == config.SOURCE_TOMTOM
    assert first["url"] == INCIDENT_URL
    assert first["url"].endswith("/traffic/services/5/incidentDetails")

    params = transport.params(0)
    assert params["key"] == config.TOMTOM_API_KEY
    assert params["language"] == "en-GB"
    assert params["timeValidityFilter"] == "present"
    # The full field selection is explicit: TomTom's default returns iconCategory only.
    for field in ("magnitudeOfDelay", "iconCategory", "events", "delay", "length"):
        assert field in params["fields"]
    # v5 has no point/radius surface; the radius must travel as a bbox.
    assert "point" not in params
    assert "radius" not in params
    assert len(params["bbox"].split(",")) == 4
    assert first["kwargs"]["context"]["scope"] == "origin"
    assert first["kwargs"]["context"]["radius_km"] == tomtom._RADIUS_KM
    assert transport.params(1)["bbox"] != params["bbox"]


def test_tomorrow_prefixes_every_summary_and_reduces_confidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = _fetch(
        _route(), _today() + datetime.timedelta(days=1), _two_airport_transport(), monkeypatch
    )

    assert envelope["status"] == "ok"
    assert all(
        signal["summary"].startswith(tomtom._TOMORROW_PREFIX)
        for signal in envelope["signals"]
    )
    assert all(
        signal["metrics"]["date_window"] == tomtom._DATE_WINDOW_TOMORROW
        for signal in envelope["signals"]
    )
    assert envelope["signals"][0]["confidence"] == pytest.approx(
        tomtom._CONFIDENCE_INCIDENT * tomtom._TOMORROW_CONFIDENCE_FACTOR, abs=1e-3
    )
    assert any("indicative only" in warning for warning in envelope["warnings"])


def test_route_level_warning_states_the_feed_is_real_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = _fetch(_route(), _today(), _two_airport_transport(), monkeypatch)

    assert any(
        "reports current conditions" in warning for warning in envelope["warnings"]
    )


def test_to_source_status_projects_a_partial_aware_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = _fetch(_route(), _today(), _two_airport_transport(), monkeypatch)
    projected = base.to_source_status(envelope)

    assert projected["source"] == config.SOURCE_TOMTOM
    assert projected["status"] == config.STATUS_OK
    assert projected["records"] == 5
    assert projected["error_codes"] == []
    assert projected["raw_response_path"] == _fixture_path(
        "tomtom_incidents_bands.json"
    )

# --------------------------------------------------------------------------- #
# fetch_signals: the real-time-only date window
# --------------------------------------------------------------------------- #
def test_future_date_returns_unavailable_without_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The module must not spend quota on a call the feed cannot answer."""
    transport = _FakeTransport(error=AssertionError("no HTTP request may be made"))
    envelope = _fetch(
        _route(), _today() + datetime.timedelta(days=3), transport, monkeypatch
    )

    assert envelope["status"] == "unavailable"
    assert envelope["error_code"] == "no_data_for_query"
    assert "real-time only" in envelope["error"]
    assert "3 days from today" in envelope["error"]
    assert "planned future incidents" in envelope["error"]
    assert envelope["signals"] == []
    assert envelope["raw_response_paths"] == []
    assert transport.calls == []
    assert any("No quota was spent" in item for item in envelope["warnings"])


def test_past_date_is_unavailable_and_says_how_far_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _FakeTransport(error=AssertionError("no HTTP request may be made"))
    envelope = _fetch(
        _route(), _today() - datetime.timedelta(days=5), transport, monkeypatch
    )

    assert envelope["status"] == "unavailable"
    assert envelope["error_code"] == "no_data_for_query"
    assert "5 day(s) before today" in envelope["error"]
    assert transport.calls == []


def test_datetime_travel_date_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    stamp = datetime.datetime.combine(_today(), datetime.time(12, 0))
    envelope = _fetch(_route(), stamp, _two_airport_transport(), monkeypatch)

    assert envelope["status"] == "ok"
    assert envelope["travel_date"] == _today().isoformat()


@pytest.mark.parametrize("bad_date", ["2026-10-16", None, 20261016])
def test_non_date_travel_dates_raise_value_error(bad_date: Any) -> None:
    with pytest.raises(ValueError):
        asyncio.run(tomtom.fetch_signals(_route(), bad_date))


def test_non_location_route_raises_value_error() -> None:
    with pytest.raises(ValueError):
        asyncio.run(tomtom.fetch_signals("JFK-LAX", _today()))


# --------------------------------------------------------------------------- #
# fetch_signals: one airport failing must not discard the other
# --------------------------------------------------------------------------- #
def test_one_airport_failing_yields_partial_with_the_surviving_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _FakeTransport(
        _result(
            _fixture("tomtom_incidents_bands.json"),
            _fixture_path("tomtom_incidents_bands.json"),
        ),
        _failure(_fixture_path("tomtom_incidents_empty.json"), status_code=500),
    )
    envelope = _fetch(_route(), _today(), transport, monkeypatch)

    assert envelope["status"] == "partial"
    assert envelope["error_codes"] == ["server_error"]
    assert envelope["error_code"] == "server_error"
    assert "destination (LAX)" in envelope["error"]
    assert len(envelope["signals"]) == 4
    assert all(signal["metrics"]["scope"] == "origin" for signal in envelope["signals"])
    assert base.has_usable_signals(envelope) is True
    assert any("weaker evidence" in item for item in envelope["warnings"])

    model = SourceStatus(**base.to_source_status(envelope))
    assert model.status == config.STATUS_PARTIAL
    assert model.error_codes == ["server_error"]


def test_both_airports_failing_is_an_error_not_an_empty_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _FakeTransport(
        _failure(_fixture_path("tomtom_incidents_bands.json"), status_code=500),
        _failure(_fixture_path("tomtom_incidents_empty.json"), status_code=502),
    )
    envelope = _fetch(_route(), _today(), transport, monkeypatch)

    assert envelope["status"] == "error"
    assert envelope["error_code"] == "server_error"
    assert envelope["signals"] == []
    assert "either airport" in envelope["error"]
    assert "JFK" in envelope["error"] and "LAX" in envelope["error"]


def test_unexpected_exception_is_reported_per_airport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bug in this module must not escape, and must not be called an outage."""
    transport = _FakeTransport(error=RuntimeError("boom"))
    envelope = _fetch(_route(), _today(), transport, monkeypatch)

    assert envelope["status"] == "error"
    assert envelope["error_code"] == "unexpected_error"
    assert "RuntimeError" in envelope["error"]
    assert any("not a TomTom outage" in item for item in envelope["warnings"])

# --------------------------------------------------------------------------- #
# fetch_signals: each failure class gets its own honest envelope
# --------------------------------------------------------------------------- #
def test_403_is_a_plan_limit_and_therefore_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preview = json.dumps(_fixture("tomtom_403_not_entitled.json"))
    transport = _FakeTransport(
        _failure(
            _fixture_path("tomtom_403_not_entitled.json"),
            status_code=403,
            body_preview=preview,
        ),
        _failure(
            _fixture_path("tomtom_403_not_entitled.json"),
            status_code=403,
            body_preview=preview,
        ),
    )
    envelope = _fetch(_route(), _today(), transport, monkeypatch)

    assert envelope["status"] == "unavailable"
    assert envelope["error_code"] == "plan_limit"
    assert "entitlement" in envelope["error"]
    assert "2.5K requests/month" in envelope["error"]
    assert any("coverage gap" in item for item in envelope["warnings"])


def test_missing_key_uses_the_real_401_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real capture: the live API answers a missing/invalid key with 401, not 403."""
    preview = (FIXTURES / "tomtom_401_unauthorized.json").read_text(encoding="utf-8")
    transport = _FakeTransport(
        _failure(
            _fixture_path("tomtom_401_unauthorized.json"),
            status_code=401,
            body_preview=preview,
        ),
        _failure(
            _fixture_path("tomtom_401_unauthorized.json"),
            status_code=401,
            body_preview=preview,
        ),
    )
    envelope = _fetch(_route(), _today(), transport, monkeypatch)

    assert envelope["status"] == "error"
    assert envelope["error_code"] == "unauthorized"
    assert "HTTP 401" in envelope["error"]
    assert "You are missing valid authentication credentials" in envelope["error"]
    assert "TOMTOM_API_KEY" in envelope["error"]


def test_rate_limiting_is_not_reported_as_no_incidents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _FakeTransport(
        _failure(_fixture_path("tomtom_401_unauthorized.json"), status_code=429),
        _failure(_fixture_path("tomtom_401_unauthorized.json"), status_code=429),
    )
    envelope = _fetch(_route(), _today(), transport, monkeypatch)

    assert envelope["status"] == "error"
    assert envelope["error_code"] == "rate_limited"
    assert "exhausted quota" in envelope["error"]
    assert any("not 'no incidents'" in item for item in envelope["warnings"])


def test_body_without_an_incidents_array_is_unexpected_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _FakeTransport(
        _result({}, _fixture_path("tomtom_incidents_bands.json")),
        _result({"incidents": "nope"}, _fixture_path("tomtom_incidents_empty.json")),
    )
    envelope = _fetch(_route(), _today(), transport, monkeypatch)

    assert envelope["status"] == "error"
    assert envelope["error_code"] == "unexpected_payload_shape"
    assert "no 'incidents' array" in envelope["error"]
    assert envelope["signals"] == []


def test_error_object_delivered_with_a_2xx_status_is_not_read_as_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrecognised 2xx error body must fail loudly, never become 'no incidents'."""
    payload = {
        "detailedError": {
            "code": "INVALID_REQUEST",
            "message": "Unknown field in fields=incidents.properties.last",
        }
    }
    transport = _FakeTransport(
        _result(payload, _fixture_path("tomtom_incidents_bands.json")),
        _result(payload, _fixture_path("tomtom_incidents_empty.json")),
    )
    envelope = _fetch(_route(), _today(), transport, monkeypatch)

    assert envelope["status"] == "error"
    assert envelope["error_code"] == "unexpected_payload_shape"
    assert "INVALID_REQUEST" in envelope["error"]


def test_vendor_error_is_read_back_from_the_raw_evidence_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``get_json`` drops non-2xx bodies, so the evidence file is the ground truth."""
    record = {
        "response": {
            "body_text": json.dumps(_fixture("tomtom_403_not_entitled.json"))
        }
    }
    monkeypatch.setattr(base, "load_raw_response", lambda *a, **k: record)
    result = _failure("logs/raw/tomtom_20261016T140000Z_test.json", status_code=403)

    code, message = tomtom._vendor_error_from_result(result)

    assert code == "Forbidden"
    assert message == "The supplied API Key is not valid for this request"

# --------------------------------------------------------------------------- #
# fetch_signals: credentials, coverage gaps and envelope invariants
# --------------------------------------------------------------------------- #
def test_missing_credential_returns_an_error_without_a_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "TOMTOM_API_KEY", None)
    transport = _FakeTransport(error=AssertionError("no HTTP request may be made"))
    envelope = _fetch(_route(), _today(), transport, monkeypatch)

    assert envelope["status"] == "error"
    assert envelope["error_code"] == "missing_credential"
    assert "TOMTOM_API_KEY" in envelope["error"]
    assert any(".env" in item for item in envelope["warnings"])
    assert transport.calls == []


def test_unsupported_airport_is_partial_when_the_other_one_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A code outside the static table is a coverage gap for that end only."""
    transport = _FakeTransport(
        _result(
            _fixture("tomtom_incidents_empty.json"),
            _fixture_path("tomtom_incidents_empty.json"),
        )
    )
    envelope = _fetch(_route("ZZZ", "LAX"), _today(), transport, monkeypatch)

    assert envelope["status"] == "partial"
    assert envelope["error_codes"] == ["no_data_for_query"]
    assert "ZZZ" in envelope["error"]
    assert len(transport.calls) == 1
    assert envelope["signals"][0]["metrics"]["airport_iata"] == "LAX"


def test_two_unsupported_airports_are_unavailable_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _FakeTransport(error=AssertionError("no HTTP request may be made"))
    envelope = _fetch(_route("ZZZ", "QQQ"), _today(), transport, monkeypatch)

    assert envelope["status"] == "unavailable"
    assert envelope["error_code"] == "no_data_for_query"
    assert "coverage gap" in envelope["error"]
    assert transport.calls == []


def test_explicit_endpoint_coordinates_are_used_instead_of_the_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller that supplies coordinates is not overruled by the static table."""
    location = base.Location(
        origin=base.Endpoint(
            iata="ZZZ", latitude=JFK_LATITUDE, longitude=JFK_LONGITUDE
        ),
        destination=base.Endpoint(iata="LAX"),
    )
    transport = _two_airport_transport()
    envelope = _fetch(location, _today(), transport, monkeypatch)

    assert envelope["status"] == "ok"
    assert transport.calls[0]["kwargs"]["params"]["bbox"].startswith("-73.956")


def test_envelope_invariants_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    envelope = _fetch(_route(), _today(), _two_airport_transport(), monkeypatch)
    signal_ids = [signal["id"] for signal in envelope["signals"]]
    evidence_ids = [entry["id"] for entry in envelope["evidence"]]

    assert len(signal_ids) == len(set(signal_ids)) == envelope["records"]
    # Evidence ids are unique across *both* airports, not just within one.
    assert len(evidence_ids) == len(set(evidence_ids))
    for signal in envelope["signals"]:
        assert signal["kind"] in {"traffic_incident", "traffic_conditions"}
        assert signal["severity"] in config.RISK_LEVELS or signal["severity"] is None
        assert signal["confidence"] is None or 0.0 <= signal["confidence"] <= 1.0
        assert set(signal["evidence_ids"]) <= set(evidence_ids)
        assert signal["metrics"]["travel_date"] == _today().isoformat()
        assert signal["summary"]


# --------------------------------------------------------------------------- #
# The real captures: what the live v5 feed actually returned on 2026-10-17
#
# These documents were written by this module's own requests, so they are held to the
# same mapping as the band fixture, plus the things only real data shows: closures with
# ``delay: null`` and ``endTime: null``, incidents whose ``magnitudeOfDelay`` is 0
# ("Unknown"), and a feed in which most records are roadworks or closures. Where a count
# is asserted it is derived from the document itself, so replacing a capture with a newer
# one does not turn these tests into a change detector.
# --------------------------------------------------------------------------- #
def test_the_real_jfk_capture_accounts_for_every_returned_incident() -> None:
    document = _fixture("tomtom_incidents_JFK.json")
    outcome = _real_capture_for("JFK")

    assert outcome.failure is None
    assert len(outcome.evidence) == len(outcome.signals) + 1, "one query record + incidents"

    query = outcome.evidence[0]
    note = query["note"] or ""
    assert query["field"] == "incidents_within_radius"
    assert query["value"] == str(len(outcome.signals))
    assert "vendor returned {0} incident(s)".format(len(document["incidents"])) in note
    assert "{0} inside the radius".format(len(outcome.signals)) in note
    # Nothing in this capture had unusable geometry, so nothing needed a warning.
    assert "0 could not be placed" in note
    assert not any("no usable" in warning for warning in outcome.warnings)


def test_every_real_incident_is_measured_from_the_airport_and_inside_the_radius() -> None:
    outcome = _real_capture_for("JFK")

    assert len(outcome.signals) > 50, "a busy airport should be far from empty"
    assert all(signal["metrics"]["airport_iata"] == "JFK" for signal in outcome.signals)
    assert all(signal["metrics"]["radius_km"] == 15.0 for signal in outcome.signals)
    assert max(signal["metrics"]["distance_km"] for signal in outcome.signals) <= 15.0


def test_every_real_road_closure_is_severe() -> None:
    """The live feed is dominated by closures; the floor must lift all of them."""
    outcome = _real_capture_for("JFK")
    closures = [
        signal for signal in outcome.signals if signal["metrics"]["icon_category"] == 8
    ]

    assert closures, "the capture contains closures within 15 km of JFK"
    assert {signal["severity"] for signal in closures} == {"severe"}
    assert all(
        signal["metrics"]["severity_source"].startswith("iconCategory 8 closure floor")
        for signal in closures
    )


def test_real_incidents_with_an_unknown_magnitude_stay_ungraded() -> None:
    """``magnitudeOfDelay: 0`` means *Unknown*, and the live feed is full of them."""
    outcome = _real_capture_for("JFK")
    ungraded = [
        signal for signal in outcome.signals if signal["metrics"]["magnitude_of_delay"] == 0
    ]

    assert ungraded, "the capture contains incidents with an unknown magnitude"
    assert {signal["severity"] for signal in ungraded} == {None}
    assert all(
        signal["metrics"]["severity_source"] == "ungraded: magnitudeOfDelay 0"
        for signal in ungraded
    )
    assert all(
        signal["confidence"] == tomtom._CONFIDENCE_UNGRADED for signal in ungraded
    )


def test_real_null_delay_and_end_time_are_absent_rather_than_zero() -> None:
    """Closures carry ``delay: null`` and open-ended events ``endTime: null``."""
    outcome = _real_capture_for("JFK")

    assert any(signal["metrics"]["delay_minutes"] is None for signal in outcome.signals)
    assert any(signal["metrics"]["end_time"] is None for signal in outcome.signals)
    assert not any("None" in signal["summary"] for signal in outcome.signals)
    assert all(
        signal["metrics"]["delay_minutes"] is None
        or signal["metrics"]["delay_minutes"] >= 0
        for signal in outcome.signals
    )


def test_the_real_lax_capture_is_populated_so_the_clear_path_needs_its_own_document() -> None:
    """LAX was *not* empty, which is why ``tomtom_incidents_empty`` is invented."""
    document = _fixture("tomtom_incidents_LAX.json")
    outcome = _real_capture_for("LAX")

    assert len(document["incidents"]) > 0
    assert outcome.failure is None
    assert len(outcome.signals) > 50
    assert len(outcome.evidence) == len(outcome.signals) + 1
    assert max(signal["metrics"]["distance_km"] for signal in outcome.signals) <= 15.0
    assert any(signal["severity"] == "severe" for signal in outcome.signals)


def test_every_real_incident_carries_the_street_names_fusion_needs() -> None:
    """Distance alone cannot tell a Van Wyck closure from a side-street one.

    TomTom publishes no street-name *field*: the only location facts in
    ``incidentDetails`` are ``from``/``to``, which is exactly what the summary sentence was
    already built from. They now reach ``metrics`` too, because 40 severe closures around
    JFK are not 40 trip risks and the fusion layer can only judge relevance if it can read
    the road name it is judging.
    """
    outcome = _real_capture_for("JFK")

    assert len(outcome.signals) > 50
    assert all(signal["metrics"]["from_place"] for signal in outcome.signals)
    assert all(signal["metrics"]["to_place"] for signal in outcome.signals)
    assert all(signal["metrics"]["place"] for signal in outcome.signals)

    places = {signal["metrics"]["place"] for signal in outcome.signals}
    assert any("Expy" in place for place in places), "the capture includes trunk routes"
    assert any("Ave" in place for place in places), "the capture includes local streets"

    for signal in outcome.signals:
        metrics = signal["metrics"]
        # `place` is the same composition the sentence uses, never a second interpretation.
        assert metrics["place"] == "{0} to {1}".format(
            metrics["from_place"], metrics["to_place"]
        )
        assert metrics["from_place"] in signal["summary"]


def test_real_incidents_distinguish_a_numbered_route_from_an_unknown_class() -> None:
    """``roadNumbers`` is the only class fact in the payload, so it is all we assert.

    ``unclassified`` means *unknown*, never *minor*: the first unclassified incident in
    the capture is on ``JFK Expy S``, the airport's own expressway, which carries no route
    number. A fusion layer that read "unclassified" as "side street" would dismiss the
    single most relevant road in the dataset.
    """
    outcome = _real_capture_for("JFK")
    numbered = [
        signal for signal in outcome.signals if signal["metrics"]["on_numbered_route"]
    ]
    unclassified = [
        signal for signal in outcome.signals if not signal["metrics"]["on_numbered_route"]
    ]

    assert numbered, "the capture contains incidents on numbered routes"
    assert unclassified, "most incidents around JFK carry no road number"
    assert all(signal["metrics"]["road_numbers"] for signal in numbered)
    assert all(signal["metrics"]["road_numbers"] == [] for signal in unclassified)
    assert {signal["metrics"]["road_class"] for signal in numbered} == {
        tomtom._ROAD_CLASS_NUMBERED
    }
    assert {signal["metrics"]["road_class"] for signal in unclassified} == {
        tomtom._ROAD_CLASS_UNCLASSIFIED
    }
    assert any("Expy" in (signal["metrics"]["place"] or "") for signal in unclassified)

    for signal in outcome.signals:
        basis = signal["metrics"]["road_class_basis"]
        assert basis, "the class must be auditable"
        assert "roadNumbers" in basis


def test_real_incidents_keep_every_vendor_event_not_just_the_first() -> None:
    """One incident can be several events (``Closed`` *and* ``Roadworks``)."""
    outcome = _real_capture_for("JFK")
    multi = [
        signal
        for signal in outcome.signals
        if len(signal["metrics"]["event_descriptions"]) > 1
    ]

    assert multi, "the live feed pairs events on the same incident"
    assert any(
        signal["metrics"]["event_descriptions"] == ["Closed", "Roadworks"]
        for signal in multi
    )

    for signal in outcome.signals:
        events = signal["metrics"]["event_descriptions"]
        assert events, "every incident in the capture published at least one event"
        assert len(events) == len(set(events)), "duplicates would inflate the count"
        # The sentence quotes the first event; the rest are context, not a rewrite of it.
        assert events[0] in signal["summary"]


