"""Parsing, selection and severity-mapping tests for ``data_sources.nws``.

Transport is faked here; the *payloads* are not. Every document fed in is a real
capture from ``api.weather.gov`` stored verbatim in ``tests/fixtures/`` (provenance in
that directory's README), because a test cannot prove anything about a response shape
we invented ourselves (.clinerules rule 3). Only the network layer is replaced: the
fake returns the captured document and an evidence path that points at the fixture
file itself, so an evidence reference asserted here can actually be opened.
``test_nws_live.py`` re-checks the same contracts against the real API.

Partial routes
--------------
``base.partial_signals()`` lets this source keep the airport that answered while
recording the one that did not: ``status="partial"`` carries the surviving airport's
signals plus one ``error_code`` per failed airport. A route is still failed whole
(``error``/``unavailable``) when *no* airport can be read, and a same-airport route
never reports partial - there is no surviving half to report.
"""

from __future__ import annotations

import asyncio
import datetime
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

import config
from data_sources import base, iata_lookup, nws

FIXTURES: Path = Path(__file__).parent / "fixtures"

# The date the captured JFK forecast document itself covers (see the fixtures README).
FIXTURE_TRAVEL_DATE: datetime.date = datetime.date(2026, 10, 17)

ALERTS_URL: str = config.NWS_BASE_URL + "/alerts/active"
# The gridpoint URLs NWS itself returned for each captured point (fixtures README).
FORECAST_URL: str = config.NWS_BASE_URL + "/gridpoints/OKX/42,40/forecast"
SFO_FORECAST_URL: str = config.NWS_BASE_URL + "/gridpoints/MTR/85,98/forecast"

# A real capture of six active alerts with different severity/certainty/urgency.
ALERT_FIXTURE: str = "nws_alerts_tx_sample.json"


def _fixture(name: str) -> Dict[str, Any]:
    """A real captured payload from ``tests/fixtures``."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _fixture_path(name: str) -> str:
    """Repo-relative path of a fixture, used as the faked evidence path."""
    return str((FIXTURES / name).relative_to(Path(config.PROJECT_ROOT)))


def _points_url(iata: str) -> str:
    """The ``/points`` URL the source should request for a supported airport."""
    info = iata_lookup.lookup_airport(iata)
    assert info is not None, iata
    return "{0}/points/{1}".format(config.NWS_BASE_URL, info.coordinates)


def _result(
    payload: Any, url: str, raw_path: str, *, status_code: int = 200
) -> base.HttpResult:
    """A successful ``HttpResult`` wrapping a real captured payload.

    Only the transport is faked: ``payload`` is the fixture document and ``raw_path``
    points at the fixture file, so any evidence path a test asserts on can be opened.
    """
    return base.HttpResult(
        ok=True,
        method="GET",
        url=url,
        status_code=status_code,
        latency_ms=12.5,
        attempts=1,
        payload=payload,
        raw_path=raw_path,
        raw_paths=(raw_path,),
        error=None,
        error_code=None,
        response_headers={"content-type": "application/geo+json"},
        body_preview=None,
        content_type="application/geo+json",
    )


def _failure(
    error_code: str, url: str, raw_path: str, *, status_code: int = 503
) -> base.HttpResult:
    """A failed ``HttpResult``, as ``get_json`` returns after exhausting attempts."""
    return base.HttpResult(
        ok=False,
        method="GET",
        url=url,
        status_code=status_code,
        latency_ms=9.0,
        attempts=2,
        payload=None,
        raw_path=raw_path,
        raw_paths=(raw_path,),
        error="HTTP {0} from {1}".format(status_code, url),
        error_code=error_code,
        response_headers={},
        body_preview="upstream error",
        content_type="application/json",
    )


class _FakeTransport:
    """A ``get_json`` stand-in that replays captured results per request URL.

    Routing by URL keeps it explicit which document answers which request, and every
    call is recorded so a test can assert which requests were made - including that
    *no* request is made for an unsupported airport.
    """

    def __init__(self, routes: Dict[str, base.HttpResult]) -> None:
        self.routes = routes
        self.calls: List[Dict[str, Any]] = []

    async def __call__(self, source: str, url: str, **kwargs: Any) -> base.HttpResult:
        self.calls.append({"source": source, "url": url, "kwargs": kwargs})
        if url not in self.routes:
            raise AssertionError("unexpected request to {0}".format(url))
        return self.routes[url]

    def urls(self) -> List[str]:
        """Every URL requested, in order."""
        return [call["url"] for call in self.calls]


def _install(
    monkeypatch: pytest.MonkeyPatch, routes: Dict[str, base.HttpResult]
) -> _FakeTransport:
    """Route every ``base.get_json`` call made by the module under test to the fake."""
    transport = _FakeTransport(routes)
    monkeypatch.setattr(nws.base, "get_json", transport)
    return transport


def _route(
    iata: str,
    *,
    alerts: str = "nws_alerts_empty.json",
    forecast: str = "nws_forecast_JFK.json",
    points: str = "nws_points_JFK.json",
    forecast_url: str = FORECAST_URL,
) -> Dict[str, base.HttpResult]:
    """The three successful responses one airport needs, keyed by request URL."""
    return {
        _points_url(iata): _result(
            _fixture(points), _points_url(iata), _fixture_path(points)
        ),
        forecast_url: _result(_fixture(forecast), forecast_url, _fixture_path(forecast)),
        ALERTS_URL: _result(_fixture(alerts), ALERTS_URL, _fixture_path(alerts)),
    }


def _two_airport_routes(
    *, alerts: str = "nws_alerts_empty.json"
) -> Dict[str, base.HttpResult]:
    """Real captures for a JFK -> SFO route: two grids, two timezones."""
    routes = _route("JFK", alerts=alerts)
    routes.update(
        _route(
            "SFO",
            alerts=alerts,
            forecast="nws_forecast_SFO.json",
            points="nws_points_SFO.json",
            forecast_url=SFO_FORECAST_URL,
        )
    )
    return routes


def _fetch(
    routes: Dict[str, base.HttpResult],
    *,
    route: str = "JFK,SFO",
    travel_date: datetime.date = FIXTURE_TRAVEL_DATE,
) -> base.NormalizedSignals:
    """Run one fetch through the installed fake transport and return its envelope."""
    origin, destination = route.split(",")
    return asyncio.run(
        nws.fetch_signals(base.Location.from_iata(origin, destination), travel_date)
    )


def _signals_of_kind(envelope: Dict[str, Any], kind: str) -> List[Dict[str, Any]]:
    """The signals of one kind from an envelope."""
    return [signal for signal in envelope["signals"] if signal["kind"] == kind]


def _evidence_by_id(envelope: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Evidence entries of an envelope, keyed by their id."""
    return {entry["id"]: entry for entry in envelope["evidence"]}


# --------------------------------------------------------------------------- #
# Alert severity mapping: real NWS field combinations -> config.RISK_LEVELS
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "severity, urgency, certainty, expected",
    [
        # The combinations below are all taken from the captured TX alert sample and
        # the documented NWS vocabulary; the expected level is our banding of them.
        ("Extreme", "Immediate", "Observed", "severe"),
        ("Severe", "Immediate", "Observed", "severe"),
        ("Severe", "Expected", "Likely", "high"),
        ("Severe", "Expected", "Observed", "high"),
        ("Moderate", "Expected", "Likely", "moderate"),
        ("Severe", "Future", "Possible", "moderate"),
        ("Moderate", "Future", "Possible", "low"),
        ("Minor", "Likely", "Expected", "low"),
        ("Unknown", "Unknown", "Unknown", "unknown"),
        (None, None, None, "unknown"),
    ],
)
def test_alert_severity_bands(
    severity: Any, urgency: Any, certainty: Any, expected: str
) -> None:
    """Every documented NWS combination bands onto one of our risk levels."""
    props = {"severity": severity, "urgency": urgency, "certainty": certainty}

    level, score, reasons = nws._alert_severity(props)

    assert level == expected
    assert level in config.RISK_LEVELS
    assert isinstance(score, float)
    assert reasons, "a mapped level must come with its reasoning"


def test_alert_severity_never_treats_missing_fields_as_no_risk() -> None:
    """An alert with no severity data is 'unknown', never 'low'."""
    level, score, reasons = nws._alert_severity({})

    assert level == "unknown"
    assert level != "low"
    assert score == 0.0
    assert any("unset" in reason for reason in reasons)


def test_alert_severity_flags_vocabulary_it_does_not_know() -> None:
    """An unrecognised NWS value degrades to unknown and says so."""
    level, _, reasons = nws._alert_severity(
        {"severity": "Catastrophic", "urgency": "Expected", "certainty": "Likely"}
    )

    assert level == "low"  # expected+likely survive; the unknown severity contributes 0
    assert any("Catastrophic" in reason and "unknown" in reason for reason in reasons)


@pytest.mark.parametrize(
    "certainty, expected",
    [
        ("Observed", 0.95),
        ("Likely", 0.9),
        ("Possible", 0.6),
        ("Unlikely", 0.4),
        ("Unknown", 0.5),
        (None, 0.5),
        ("Something New", 0.5),
    ],
)
def test_alert_confidence_follows_nws_certainty(certainty: Any, expected: float) -> None:
    """Confidence comes from the one field NWS publishes that resembles it."""
    assert nws._alert_confidence({"certainty": certainty}) == expected


def test_alert_overlap_handles_missing_and_present_windows() -> None:
    """A window that does not cover the date is flagged, never silently dropped."""
    travel_date = datetime.date(2026, 10, 17)

    assert nws._alert_overlap({"onset": "2026-10-16T18:40:00-06:00", "ends": "2026-10-17T00:00:00-06:00"}, travel_date) is True
    assert nws._alert_overlap({"onset": "2026-10-18T19:00:00-05:00", "ends": "2026-10-19T07:00:00-05:00"}, travel_date) is False
    assert nws._alert_overlap({}, travel_date) is None
    # Falls back to effective/expires when onset/ends are absent.
    assert nws._alert_overlap({"effective": "2026-10-17T01:00:00Z", "expires": "2026-10-17T09:00:00Z"}, travel_date) is True


# --------------------------------------------------------------------------- #
# Forecast severity mapping
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "phrase, expected",
    [
        ("Blizzard", "high"),
        ("Heavy Snow", "high"),
        ("Freezing Rain", "high"),
        ("Severe Thunderstorm", "high"),
        ("Showers And Thunderstorms", "moderate"),
        ("Blowing Snow", "moderate"),
        ("Patchy Fog", "moderate"),
        ("Heavy Rain", "moderate"),
        ("Chance Light Rain then Mostly Cloudy", "low"),
        ("Slight Chance Rain Showers", "low"),
        ("Mostly Sunny", "low"),
        ("Sunny", "low"),
    ],
)
def test_forecast_severity_reads_the_nws_phrasing(phrase: str, expected: str) -> None:
    """The mapping is driven by NWS's own ``shortForecast`` wording."""
    severity, reasons, metrics = nws._forecast_severity(
        {
            "shortForecast": phrase,
            "windSpeed": "5 mph",
            "temperature": 70,
            "temperatureUnit": "F",
        }
    )

    assert severity == expected
    assert metrics["short_forecast"] == phrase
    assert reasons[-1].startswith("period assessed at")


def test_forecast_severity_says_when_nothing_is_disruptive() -> None:
    """A harmless phrase maps to 'low' with the reason spelled out."""
    severity, reasons, _ = nws._forecast_severity({"shortForecast": "Mostly Sunny"})

    assert severity == "low"
    assert any("no disruptive condition" in reason for reason in reasons)


@pytest.mark.parametrize(
    "period, expected",
    [
        ({"shortForecast": "Mostly Sunny", "windSpeed": "30 mph", "temperature": 60, "temperatureUnit": "F"}, "moderate"),
        ({"shortForecast": "Mostly Sunny", "windSpeed": "45 mph", "temperature": 60, "temperatureUnit": "F"}, "high"),
        ({"shortForecast": "Cloudy", "probabilityOfPrecipitation": {"value": 85}, "temperature": 60, "temperatureUnit": "F"}, "moderate"),
        ({"shortForecast": "Cloudy", "temperature": 10, "temperatureUnit": "F"}, "moderate"),
        ({"shortForecast": "Cloudy", "temperature": -5, "temperatureUnit": "F"}, "high"),
        ({"shortForecast": "Cloudy", "temperature": 104, "temperatureUnit": "F"}, "moderate"),
        ({"shortForecast": "Mostly Sunny", "windSpeed": "34 km/h", "temperature": 12, "temperatureUnit": "C"}, "low"),
    ],
)
def test_forecast_severity_uses_only_bands_the_period_supports(
    period: Dict[str, Any], expected: str
) -> None:
    """Wind/pop/temperature bands fire only on values the period itself states."""
    severity, reasons, metrics = nws._forecast_severity(period)

    assert severity == expected
    if "km/h" in str(period.get("windSpeed")):
        assert metrics["wind_max_mph"] is None
        assert any("not in mph" in reason for reason in reasons)
    if period.get("temperatureUnit") == "C":
        assert any("is not F" in reason for reason in reasons)


def test_forecast_severity_records_the_bands_that_did_not_fire() -> None:
    """A value below every band is still reported, so the level stays auditable."""
    severity, reasons, metrics = nws._forecast_severity(
        {
            "shortForecast": "Partly Sunny",
            "windSpeed": "23 mph",
            "temperature": 55,
            "temperatureUnit": "F",
        }
    )

    assert severity == "low"
    assert metrics["wind_max_mph"] == 23.0
    assert any("below every configured threshold" in reason for reason in reasons)


def test_forecast_severity_takes_the_most_severe_reading() -> None:
    """When several bands fire, the worst one wins."""
    severity, reasons, _ = nws._forecast_severity(
        {
            "shortForecast": "Heavy Snow",
            "windSpeed": "45 mph",
            "probabilityOfPrecipitation": {"value": 90},
            "temperature": -5,
            "temperatureUnit": "F",
        }
    )

    assert severity == "high"
    assert len(reasons) >= 5


# --------------------------------------------------------------------------- #
# Travel date -> periods, against the real captured documents
# --------------------------------------------------------------------------- #
def _periods(fixture: str) -> List[Dict[str, Any]]:
    """The ``periods`` array of a captured forecast document."""
    return _fixture(fixture)["properties"]["periods"]


@pytest.mark.parametrize(
    "travel_date, expected_names",
    [
        # NWS publishes day/night pairs, so a travel date normally overlaps the night
        # before, the day itself and the night after. The names are the ones in the
        # captured JFK document.
        (datetime.date(2026, 10, 17), ["Tonight", "Saturday", "Saturday Night"]),
        (datetime.date(2026, 10, 16), ["Tonight"]),
        (datetime.date(2026, 10, 23), ["Thursday Night", "Friday"]),
        (datetime.date(2026, 10, 24), []),
        (datetime.date(2026, 10, 1), []),
    ],
)
def test_travel_date_selects_the_overlapping_day_and_night_periods(
    travel_date: datetime.date, expected_names: List[str]
) -> None:
    """Selection uses each timestamp's own published local date, never a guess."""
    selected = [
        period.get("name")
        for period in _periods("nws_forecast_JFK.json")
        if nws._overlaps_travel_date(period, travel_date)
    ]

    assert selected == expected_names


def test_periods_without_usable_timestamps_are_skipped() -> None:
    """An unreadable period does not match, rather than defaulting to today."""
    travel_date = datetime.date(2026, 10, 17)

    assert nws._overlaps_travel_date({"name": "Broken"}, travel_date) is False
    assert (
        nws._overlaps_travel_date(
            {"startTime": "not-a-date", "endTime": "also-not"}, travel_date
        )
        is False
    )


def test_period_bounds_read_the_published_local_dates() -> None:
    """Bounds come from the offsets NWS publishes, not from a UTC conversion."""
    assert nws._period_bounds(
        {
            "startTime": "2026-10-16T20:00:00-04:00",
            "endTime": "2026-10-17T06:00:00-04:00",
        }
    ) == (datetime.date(2026, 10, 16), datetime.date(2026, 10, 17))
    assert nws._period_bounds({"startTime": None, "endTime": None}) is None


# --------------------------------------------------------------------------- #
# Value helpers
# --------------------------------------------------------------------------- #
def test_max_number_uses_the_upper_bound_of_a_range() -> None:
    assert nws._max_number("13 to 21 mph") == 21.0
    assert nws._max_number("24 mph") == 24.0
    assert nws._max_number(50) == 50.0
    assert nws._max_number("no gust data") is None
    assert nws._max_number(None) is None
    assert nws._max_number(True) is None


def test_short_local_formats_a_published_timestamp() -> None:
    assert nws._short_local("2026-10-17T06:00:00-04:00") == "2026-10-17 06:00 -04:00"
    assert nws._short_local(None) == "unknown time"


def test_parse_iso_datetime_accepts_offsets_and_zulu() -> None:
    assert nws._parse_iso_datetime("2026-10-17T06:00:00-04:00") is not None
    assert nws._parse_iso_datetime("2026-10-17T06:00:00Z") is not None
    assert nws._parse_iso_datetime("nonsense") is None
    assert nws._parse_iso_datetime(None) is None


def test_most_severe_never_lets_unknown_beat_a_real_level() -> None:
    assert nws._most_severe("low", "moderate") == "moderate"
    assert nws._most_severe("high", "unknown") == "high"
    assert nws._most_severe("unknown", "low") == "low"
    assert nws._severity_rank("severe") > nws._severity_rank("high")


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "http://api.weather.gov/gridpoints/OKX/42,40/forecast",
        "https://evil.example.com/gridpoints/OKX/42,40/forecast",
        "https://api.weather.gov/points/40.6,-73.7",
        "https://api.weather.gov/zones/forecast/NYZ178",
    ],
)
def test_urls_that_are_not_nws_gridpoints_are_refused(url: Any) -> None:
    """A payload cannot redirect this module at another host or resource."""
    assert nws._trusted_nws_url(url) is None


def test_the_forecast_url_from_the_real_capture_is_accepted() -> None:
    """The URL NWS actually hands back is one we follow."""
    payload = _fixture("nws_points_JFK.json")

    assert nws._trusted_nws_url(payload["properties"]["forecast"]) == FORECAST_URL


# --------------------------------------------------------------------------- #
# Envelopes: transport faked, documents real
# --------------------------------------------------------------------------- #
def test_benign_route_returns_ok_with_an_explicit_nothing_found_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No alerts plus unremarkable periods is a checked result, not an empty one.

    2026-10-20 sits inside both captured documents; the assertions read the levels
    the mapping under test produces for it, so this stays a claim about the code plus
    the real documents - not a hand-written expectation of what the weather was.
    """
    routes = _two_airport_routes()
    transport = _install(monkeypatch, routes)

    envelope = _fetch(routes, travel_date=datetime.date(2026, 10, 20))

    assert envelope["status"] == config.STATUS_OK
    assert envelope["error"] is None and envelope["error_code"] is None
    assert envelope["source"] == config.SOURCE_NWS
    assert envelope["travel_date"] == "2026-10-20"

    forecast = _signals_of_kind(envelope, nws._SIGNAL_KIND_FORECAST)
    summary = _signals_of_kind(envelope, nws._SIGNAL_KIND_SUMMARY)
    assert forecast, "the captured documents cover this date"
    assert all(signal["severity"] == "low" for signal in forecast)
    assert len(summary) == 1
    assert summary[0]["severity"] == "low"
    assert "No meaningful weather risk identified" in summary[0]["summary"]
    assert summary[0]["metrics"]["active_alerts"] == 0
    assert set(summary[0]["metrics"]["periods_assessed"]) == {"JFK", "SFO"}
    assert summary[0]["evidence_ids"], "the summary must cite what it rests on"

    # Three requests per airport, in order, and nothing else.
    assert transport.urls() == [
        _points_url("JFK"),
        FORECAST_URL,
        ALERTS_URL,
        _points_url("SFO"),
        SFO_FORECAST_URL,
        ALERTS_URL,
    ]
    # Both airports were asked about, with their own coordinates.
    points = [
        call["kwargs"].get("params", {}).get("point")
        for call in transport.calls
        if call["url"] == ALERTS_URL
    ]
    assert points == [
        iata_lookup.lookup_airport("JFK").coordinates,
        iata_lookup.lookup_airport("SFO").coordinates,
    ]
    assert envelope["latency_ms"] == pytest.approx(75.0)  # 6 requests * 12.5 ms


def test_every_signal_cites_evidence_that_resolves_to_a_real_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A claim without resolvable evidence does not reach the envelope."""
    routes = _two_airport_routes()
    _install(monkeypatch, routes)

    envelope = _fetch(routes, travel_date=datetime.date(2026, 10, 20))

    evidence = _evidence_by_id(envelope)
    assert evidence
    assert envelope["records"] == len(envelope["signals"])
    assert envelope["raw_response_path"] in envelope["raw_response_paths"]

    for signal in envelope["signals"]:
        assert signal["evidence_ids"], signal["kind"]
        assert signal["severity"] in config.RISK_LEVELS
        assert signal["summary"]
        for evidence_id in signal["evidence_ids"]:
            entry = evidence[evidence_id]
            assert entry["source"] == config.SOURCE_NWS
            assert entry["note"]
            path = entry["raw_response_path"]
            if path is None:
                # Only the static coordinate row has no HTTP record behind it.
                assert entry["field"] == "iata_lookup.row"
                continue
            assert (Path(config.PROJECT_ROOT) / path).is_file(), path


def test_forecast_signals_name_the_period_they_represent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each signal says which day/night period it came from, with that period's data."""
    routes = _two_airport_routes()
    _install(monkeypatch, routes)

    envelope = _fetch(routes, travel_date=FIXTURE_TRAVEL_DATE)
    forecast = _signals_of_kind(envelope, nws._SIGNAL_KIND_FORECAST)

    for airport in ("JFK", "SFO"):
        names = [
            signal["metrics"]["period_name"]
            for signal in forecast
            if signal["metrics"]["airport"] == airport
        ]
        assert names == ["Tonight", "Saturday", "Saturday Night"]

    for signal in forecast:
        assert "local" in signal["summary"]
        assert signal["metrics"]["travel_date"] == FIXTURE_TRAVEL_DATE.isoformat()
        assert signal["metrics"]["travel_date_overlap"] is True
        assert signal["metrics"]["start"] and signal["metrics"]["end"]
        assert signal["metrics"]["severity_reasons"]

    # The captured JFK periods for that date: thunderstorms overnight, light rain by day.
    jfk = {
        signal["metrics"]["period_name"]: signal
        for signal in forecast
        if signal["metrics"]["airport"] == "JFK"
    }
    assert jfk["Tonight"]["severity"] == "moderate"
    assert jfk["Tonight"]["metrics"]["precipitation_probability"] == 79.0
    assert jfk["Tonight"]["metrics"]["short_forecast"] == "Showers And Thunderstorms"
    assert jfk["Saturday"]["severity"] == "low"
    assert jfk["Saturday"]["metrics"]["short_forecast"] == "Chance Light Rain then Mostly Cloudy"


def test_forecast_signals_name_their_airport_the_same_way_alerts_do(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One key for "which airport", so fusion needs no per-kind special case.

    Alerts and the summary already carry ``airports`` as a list (an alert can be active at
    both ends of the route). Forecast signals keep their singular ``airport`` for
    compatibility but now carry the same list, so a fusion prompt can read
    ``metrics["airports"]`` for every NWS signal.
    """
    routes = _two_airport_routes()
    _install(monkeypatch, routes)

    envelope = _fetch(routes, travel_date=FIXTURE_TRAVEL_DATE)
    forecast = _signals_of_kind(envelope, nws._SIGNAL_KIND_FORECAST)

    assert forecast
    for signal in forecast:
        assert signal["metrics"]["airports"] == [signal["metrics"]["airport"]]
        assert signal["metrics"]["airports"][0] in {"JFK", "SFO"}
    assert {tuple(signal["metrics"]["airports"]) for signal in forecast} == {
        ("JFK",),
        ("SFO",),
    }


def test_the_envelope_echoes_the_query_and_invents_no_coordinates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolved coordinates are evidence, not a rewrite of the caller's query."""
    routes = _route("JFK")
    _install(monkeypatch, routes)

    envelope = _fetch(routes, route="JFK,JFK", travel_date=FIXTURE_TRAVEL_DATE)

    assert envelope["location"]["route_label"] == "JFK -> JFK"
    assert envelope["location"]["origin"]["latitude"] is None
    assert envelope["location"]["origin"]["longitude"] is None


# --------------------------------------------------------------------------- #
# Alerts end to end, from the real captured alert document
# --------------------------------------------------------------------------- #
def test_active_alerts_become_one_signal_each_citing_both_airports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same alert at both airports is one signal, with two evidence records."""
    routes = _two_airport_routes(alerts=ALERT_FIXTURE)
    _install(monkeypatch, routes)

    envelope = _fetch(routes, travel_date=FIXTURE_TRAVEL_DATE)

    alert_signals = _signals_of_kind(envelope, nws._SIGNAL_KIND_ALERT)
    assert len(alert_signals) == len(_fixture(ALERT_FIXTURE)["features"])
    assert envelope["status"] == config.STATUS_OK

    severities = {
        signal["metrics"]["event"]: signal["severity"] for signal in alert_signals
    }
    assert severities == {
        "High Wind Warning": "high",  # Severe / Expected / Likely
        "Wind Advisory": "moderate",  # Moderate / Expected / Likely
        "Coastal Flood Advisory": "low",  # Minor / Likely / Expected
        "Flood Warning": "severe",  # Severe / Observed / Immediate
        "Flood Watch": "moderate",  # Severe / Possible / Future
        "Flood Advisory": "moderate",  # Minor / Observed / Expected
    }

    evidence = _evidence_by_id(envelope)
    for signal in alert_signals:
        assert signal["metrics"]["airports"] == ["JFK", "SFO"]
        assert len(signal["evidence_ids"]) == 2
        assert signal["confidence"] is not None
        assert signal["metrics"]["severity_reasons"]
        assert signal["metrics"]["nws_severity"] in config.RISK_LEVELS or signal[
            "metrics"
        ]["nws_severity"] in {"Extreme", "Severe", "Moderate", "Minor", "Unknown"}
        for evidence_id in signal["evidence_ids"]:
            assert evidence[evidence_id]["raw_response_path"] == _fixture_path(
                ALERT_FIXTURE
            )


def test_an_alert_that_misses_the_travel_date_is_reported_and_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An out-of-window alert is labelled, never filtered away or counted as on-date."""
    routes = _two_airport_routes(alerts=ALERT_FIXTURE)
    _install(monkeypatch, routes)

    envelope = _fetch(routes, travel_date=FIXTURE_TRAVEL_DATE)

    flooding = [
        signal
        for signal in _signals_of_kind(envelope, nws._SIGNAL_KIND_ALERT)
        if signal["metrics"]["event"] == "Flood Warning"
    ]
    assert len(flooding) == 1
    assert flooding[0]["metrics"]["overlaps_travel_date"] is False
    assert "does not overlap the travel date" in flooding[0]["summary"]
    assert any(
        "do not cover the travel date" in warning for warning in envelope["warnings"]
    )


# --------------------------------------------------------------------------- #
# Coverage limits are explicit, never a guess
# --------------------------------------------------------------------------- #
def test_a_date_outside_the_published_window_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NWS publishes about a week ahead; beyond that there is no data, not 'clear'."""
    routes = _two_airport_routes()
    transport = _install(monkeypatch, routes)

    envelope = _fetch(routes, travel_date=datetime.date(2026, 12, 25))

    assert envelope["status"] == config.STATUS_UNAVAILABLE
    assert envelope["error_code"] == base.ErrorCode.NO_DATA_FOR_QUERY
    assert envelope["signals"] == []
    assert envelope["evidence"] == []
    assert "no forecast period overlapping 2026-12-25" in envelope["error"]
    assert "2026-10-23" in envelope["error"]  # names the window it fell outside of
    assert "Nothing was extrapolated" in envelope["error"]
    # Both airports were still consulted: three requests each, and no more.
    assert len(transport.urls()) == 6
    assert len(transport.calls) == 6
    # Every raw record is referenced. This fake serves both airports' alert responses
    # from the same fixture file, so those two paths collapse to one entry here (the
    # list never repeats a path).
    assert envelope["raw_response_paths"] == [
        _fixture_path("nws_points_JFK.json"),
        _fixture_path("nws_forecast_JFK.json"),
        _fixture_path("nws_alerts_empty.json"),
        _fixture_path("nws_points_SFO.json"),
        _fixture_path("nws_forecast_SFO.json"),
    ]
    assert transport.urls() == [
        _points_url("JFK"),
        FORECAST_URL,
        ALERTS_URL,
        _points_url("SFO"),
        SFO_FORECAST_URL,
        ALERTS_URL,
    ]


def test_an_unsupported_airport_is_unavailable_without_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-US or unknown code is reported as a coverage limit, never guessed at."""
    transport = _install(monkeypatch, {})

    envelope = _fetch({}, route="LHR,JFK")

    assert envelope["status"] == config.STATUS_UNAVAILABLE
    assert envelope["error_code"] == base.ErrorCode.NO_DATA_FOR_QUERY
    assert "LHR" in envelope["error"]
    assert str(len(iata_lookup.supported_airports())) in envelope["error"]
    assert envelope["signals"] == []
    assert transport.calls == [], "nothing may be requested for an unsupported endpoint"
    assert envelope["warnings"]


# --------------------------------------------------------------------------- #
# Failure modes become explicit states, never exceptions
# --------------------------------------------------------------------------- #
def test_one_airports_failure_becomes_a_partial_result_with_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 503 on JFK's /points must not discard SFO's real forecast."""
    routes = _two_airport_routes()
    raw_path = "logs/raw/nws_TEST_points_503.json"
    routes[_points_url("JFK")] = _failure(
        base.ErrorCode.SERVER_ERROR, _points_url("JFK"), raw_path
    )
    transport = _install(monkeypatch, routes)

    envelope = _fetch(routes, travel_date=FIXTURE_TRAVEL_DATE)

    assert envelope["status"] == config.STATUS_PARTIAL
    assert envelope["error_code"] == base.ErrorCode.SERVER_ERROR
    assert envelope["error_codes"] == [base.ErrorCode.SERVER_ERROR]
    assert "JFK" in envelope["error"] and "503" in envelope["error"]
    # Real signals survive, and every one of them belongs to the airport that worked.
    assert envelope["signals"]
    forecast = _signals_of_kind(envelope, nws._SIGNAL_KIND_FORECAST)
    assert forecast, "SFO's forecast periods must still be reported"
    assert {signal["metrics"]["airport"] for signal in forecast} == {"SFO"}
    for signal in _signals_of_kind(envelope, nws._SIGNAL_KIND_SUMMARY):
        assert signal["metrics"]["airports"] == ["SFO"]
    # The failure keeps its raw record and is never hidden by the partial envelope.
    assert raw_path in envelope["raw_response_paths"]
    assert any("weaker evidence" in warning for warning in envelope["warnings"])
    # The other airport was still attempted after JFK failed.
    assert transport.urls() == [
        _points_url("JFK"),
        _points_url("SFO"),
        SFO_FORECAST_URL,
        ALERTS_URL,
    ]


def test_both_airports_failing_is_a_whole_source_failure_not_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no surviving airport there is nothing partial about the result."""
    routes = _two_airport_routes()
    jfk_raw = "logs/raw/nws_TEST_points_503.json"
    sfo_raw = "logs/raw/nws_TEST_sfo_points_503.json"
    routes[_points_url("JFK")] = _failure(
        base.ErrorCode.SERVER_ERROR, _points_url("JFK"), jfk_raw
    )
    routes[_points_url("SFO")] = _failure(
        base.ErrorCode.SERVER_ERROR, _points_url("SFO"), sfo_raw
    )
    _install(monkeypatch, routes)

    envelope = _fetch(routes, travel_date=FIXTURE_TRAVEL_DATE)

    assert envelope["status"] == config.STATUS_ERROR
    assert envelope["error_code"] == base.ErrorCode.SERVER_ERROR
    assert "error_codes" not in envelope, "only partial envelopes carry error_codes"
    assert envelope["signals"] == []
    assert "JFK" in envelope["error"] and "SFO" in envelope["error"]
    assert "nothing is reported as partial" in envelope["error"]
    assert jfk_raw in envelope["raw_response_paths"]
    assert sfo_raw in envelope["raw_response_paths"]


def test_mixed_failures_report_the_outage_rather_than_the_milder_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real outage outranks a coverage note when every airport failed."""
    routes = _two_airport_routes()
    routes[_points_url("JFK")] = _failure(
        base.ErrorCode.NOT_FOUND,
        _points_url("JFK"),
        "logs/raw/nws_TEST_points_404.json",
        status_code=404,
    )
    routes[_points_url("SFO")] = _failure(
        base.ErrorCode.SERVER_ERROR,
        _points_url("SFO"),
        "logs/raw/nws_TEST_sfo_points_503.json",
    )
    _install(monkeypatch, routes)

    envelope = _fetch(routes, travel_date=FIXTURE_TRAVEL_DATE)

    assert envelope["status"] == config.STATUS_ERROR
    assert envelope["error_code"] == base.ErrorCode.SERVER_ERROR
    assert "no forecast grid" in envelope["error"]  # the 404 is still named


def test_a_points_404_is_no_data_for_that_airport_not_an_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 404 from /points means NWS has no grid there: that airport is unavailable."""
    routes = _two_airport_routes()
    routes[_points_url("JFK")] = _failure(
        base.ErrorCode.NOT_FOUND,
        _points_url("JFK"),
        "logs/raw/nws_TEST_points_404.json",
        status_code=404,
    )
    _install(monkeypatch, routes)

    envelope = _fetch(routes, travel_date=FIXTURE_TRAVEL_DATE)

    assert envelope["status"] == config.STATUS_PARTIAL
    assert envelope["error_code"] == base.ErrorCode.NO_DATA_FOR_QUERY
    assert envelope["error_codes"] == [base.ErrorCode.NO_DATA_FOR_QUERY]
    assert "no forecast grid" in envelope["error"]
    forecast = _signals_of_kind(envelope, nws._SIGNAL_KIND_FORECAST)
    assert {signal["metrics"]["airport"] for signal in forecast} == {"SFO"}


def test_a_body_that_is_not_a_featurecollection_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real NWS 400 error document, delivered with a 200, is not read as alerts."""
    routes = _two_airport_routes()
    fixture = "nws_alerts_bad_request.json"
    routes[ALERTS_URL] = _result(_fixture(fixture), ALERTS_URL, _fixture_path(fixture))
    _install(monkeypatch, routes)

    envelope = _fetch(routes, travel_date=FIXTURE_TRAVEL_DATE)

    assert envelope["status"] == config.STATUS_ERROR
    assert envelope["error_code"] == base.ErrorCode.UNEXPECTED_PAYLOAD_SHAPE
    assert "not a FeatureCollection" in envelope["error"]
    assert envelope["signals"] == []


def test_a_payload_cannot_redirect_the_forecast_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tampered ``forecast`` URL is refused before anything is requested from it."""
    tampered = _fixture("nws_points_JFK.json")
    # A real capture with exactly one field replaced, to prove the URL is validated
    # before it is followed. Nothing else about the document is changed.
    tampered["properties"]["forecast"] = (
        "https://evil.example.com/gridpoints/OKX/42,40/forecast"
    )
    routes = _two_airport_routes()
    routes[_points_url("JFK")] = _result(
        tampered, _points_url("JFK"), "logs/raw/nws_TEST_tampered.json"
    )
    transport = _install(monkeypatch, routes)

    envelope = _fetch(routes, travel_date=FIXTURE_TRAVEL_DATE)

    assert envelope["status"] == config.STATUS_PARTIAL
    assert envelope["error_code"] == base.ErrorCode.UNEXPECTED_PAYLOAD_SHAPE
    assert "not an HTTPS gridpoint resource" in envelope["error"]
    # The tampered URL was never followed, and the rest of the route still ran.
    assert all("evil.example.com" not in url for url in transport.urls())
    assert transport.urls() == [
        _points_url("JFK"),
        _points_url("SFO"),
        SFO_FORECAST_URL,
        ALERTS_URL,
    ]


def test_a_forecast_document_without_periods_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing 'periods' array is a payload-shape error, never an empty forecast."""
    routes = _two_airport_routes()
    body = _fixture("nws_forecast_JFK.json")
    del body["properties"]["periods"]  # a real document with one field removed
    routes[FORECAST_URL] = _result(
        body, FORECAST_URL, "logs/raw/nws_TEST_no_periods.json"
    )
    _install(monkeypatch, routes)

    envelope = _fetch(routes, travel_date=FIXTURE_TRAVEL_DATE)

    assert envelope["status"] == config.STATUS_PARTIAL
    assert envelope["error_code"] == base.ErrorCode.UNEXPECTED_PAYLOAD_SHAPE
    assert "no 'periods' array" in envelope["error"]
    assert envelope["signals"], "SFO's forecast is real data and must survive"


def _shifted_forecast(name: str, old: str, new: str) -> Dict[str, Any]:
    """A real capture with every period timestamp moved to another month.

    Only the timestamps change - a labelled mutation, like the tampered-URL test - so
    the document keeps its real period shape while covering a window that no longer
    includes the travel date.
    """
    body = _fixture(name)
    for period in body["properties"]["periods"]:
        for field in ("startTime", "endTime"):
            if isinstance(period.get(field), str):
                period[field] = period[field].replace(old, new)
    return body


def test_one_airport_outside_the_window_is_partial_with_the_covered_airport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NWS coverage is per gridpoint: one airport's window can miss the date alone."""
    routes = _two_airport_routes()
    shifted = "logs/raw/nws_TEST_forecast_SFO_shifted.json"
    routes[SFO_FORECAST_URL] = _result(
        _shifted_forecast("nws_forecast_SFO.json", "2026-10-", "2026-12-"),
        SFO_FORECAST_URL,
        shifted,
    )
    _install(monkeypatch, routes)

    envelope = _fetch(routes, travel_date=FIXTURE_TRAVEL_DATE)

    assert envelope["status"] == config.STATUS_PARTIAL
    assert envelope["error_codes"] == [base.ErrorCode.NO_DATA_FOR_QUERY]
    assert "no forecast period overlapping 2026-10-17" in envelope["error"]
    assert "SFO" in envelope["error"] and "2026-12-16" in envelope["error"]
    forecast = _signals_of_kind(envelope, nws._SIGNAL_KIND_FORECAST)
    assert {signal["metrics"]["airport"] for signal in forecast} == {"JFK"}
    assert envelope["signals"], "JFK's forecast is a real result for the travel date"


def test_a_single_airport_route_never_reports_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With one airport there is no surviving half: the source fails whole."""
    routes = _route("JFK")
    routes[_points_url("JFK")] = _failure(
        base.ErrorCode.TIMEOUT, _points_url("JFK"), "logs/raw/nws_TEST_timeout.json"
    )
    _install(monkeypatch, routes)

    envelope = _fetch(routes, route="JFK,JFK")

    assert envelope["status"] == config.STATUS_ERROR
    assert envelope["error_code"] == base.ErrorCode.TIMEOUT
    assert envelope["signals"] == []
    assert "error_codes" not in envelope


def test_a_same_airport_route_is_fetched_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A route like JFK -> JFK costs one airport's worth of requests."""
    routes = _route("JFK")
    transport = _install(monkeypatch, routes)

    envelope = _fetch(routes, route="JFK,JFK", travel_date=FIXTURE_TRAVEL_DATE)

    assert transport.urls() == [_points_url("JFK"), FORECAST_URL, ALERTS_URL]
    assert envelope["status"] == config.STATUS_OK
    assert {
        signal["metrics"]["airport"]
        for signal in _signals_of_kind(envelope, nws._SIGNAL_KIND_FORECAST)
    } == {"JFK"}


# --------------------------------------------------------------------------- #
# Argument guards and module surface
# --------------------------------------------------------------------------- #
def test_a_missing_user_agent_stops_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NWS rejects anonymous clients, so a blank User-Agent is our error, up front."""
    monkeypatch.setattr(config, "NWS_USER_AGENT", "")
    transport = _install(monkeypatch, {})

    envelope = _fetch({}, travel_date=FIXTURE_TRAVEL_DATE)

    assert envelope["status"] == config.STATUS_ERROR
    assert envelope["error_code"] == base.ErrorCode.MISSING_CREDENTIAL
    assert "NWS_USER_AGENT" in envelope["error"]
    assert transport.calls == []


def test_programming_errors_raise_instead_of_producing_an_envelope() -> None:
    """A wrong-typed argument is a caller bug, as it is for ``base.get_json``."""
    location = base.Location.from_iata("JFK", "SFO")

    with pytest.raises(ValueError):
        asyncio.run(nws.fetch_signals(location, "2026-10-17"))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        asyncio.run(
            nws.fetch_signals("JFK-SFO", datetime.date(2026, 10, 17))  # type: ignore[arg-type]
        )


def test_a_datetime_travel_date_is_accepted_as_its_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``datetime`` is a ``date`` subclass, so passing one must not crash."""
    routes = _route("JFK")
    _install(monkeypatch, routes)

    envelope = asyncio.run(
        nws.fetch_signals(
            base.Location.from_iata("JFK", "JFK"),
            datetime.datetime(2026, 10, 17, 8, 30),
        )
    )

    assert envelope["travel_date"] == "2026-10-17"
    assert envelope["status"] == config.STATUS_OK


def test_the_module_exposes_exactly_one_public_entry_point() -> None:
    """Rule 4: ``fetch_signals`` is the only public callable of a source module."""
    public = {
        name
        for name in dir(nws)
        if not name.startswith("_")
        and callable(getattr(nws, name))
        and getattr(getattr(nws, name), "__module__", None) == nws.__name__
    }

    assert public == {"fetch_signals"}
    assert nws.__all__ == ["fetch_signals"]


def test_the_envelope_is_json_serializable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The API returns this envelope as JSON, so every value must serialize as-is."""
    routes = _two_airport_routes(alerts=ALERT_FIXTURE)
    _install(monkeypatch, routes)

    envelope = _fetch(routes, travel_date=FIXTURE_TRAVEL_DATE)

    rendered = json.dumps(envelope)
    assert json.loads(rendered) == envelope
