"""Live contract tests for the NWS source (``data_sources.nws``).

Real API, real keyless data: these tests call ``api.weather.gov`` with the project's
own User-Agent. They assert the *contract* - a well-formed envelope, evidence that
resolves to real records on disk, severities drawn from our own vocabulary, and an
explicit state where NWS genuinely has no data for the query. They never assert what
the weather is, so they do not rot when it changes.

An upstream outage (timeout, 429/5xx) is reported as a skip rather than a failure:
what is under test is this module's handling of real documents, not NWS uptime. Any
error *we* produce (an unusable payload, an unexpected exception) still fails.

The module is skipped automatically when the host has no outbound network access.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import socket
from pathlib import Path
from typing import Any, Dict

import pytest

import config
from backend.models import SourceStatus
from data_sources import base, iata_lookup, nws


def _network_available() -> bool:
    """Whether a TCP connection to the NWS host can be established."""
    try:
        with socket.create_connection(("api.weather.gov", 443), timeout=5.0):
            return True
    except OSError:
        return False


NETWORK_AVAILABLE: bool = _network_available()

pytestmark = pytest.mark.live

skip_without_network = pytest.mark.skipif(
    not NETWORK_AVAILABLE,
    reason="no outbound network access: live source contract tests cannot run",
)

# Upstream problems, as opposed to problems in this module.
UPSTREAM_ERROR_CODES = frozenset(
    {
        base.ErrorCode.TIMEOUT,
        base.ErrorCode.NETWORK_ERROR,
        base.ErrorCode.SERVER_ERROR,
        base.ErrorCode.RATE_LIMITED,
        base.ErrorCode.CLIENT_ERROR,
    }
)


def _travel_date() -> datetime.date:
    """Tomorrow, which is always inside the window NWS publishes."""
    return datetime.date.today() + datetime.timedelta(days=1)


def _fetch(origin: str, destination: str, travel_date: datetime.date) -> Dict[str, Any]:
    """Fetch one route live; skip (not fail) when the *upstream* is having trouble."""
    envelope = asyncio.run(
        nws.fetch_signals(base.Location.from_iata(origin, destination), travel_date)
    )
    if (
        envelope["status"] == config.STATUS_ERROR
        and envelope["error_code"] in UPSTREAM_ERROR_CODES
    ):
        pytest.skip("upstream NWS problem: {0}".format(envelope["error"]))
    return envelope


def _evidence_by_id(envelope: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Evidence entries of an envelope, keyed by id."""
    return {entry["id"]: entry for entry in envelope["evidence"]}


def _load(path: str) -> Dict[str, Any]:
    """Load a raw evidence record written by ``base.get_json``."""
    record_path = Path(config.PROJECT_ROOT) / path
    assert record_path.is_file(), path
    return json.loads(record_path.read_text(encoding="utf-8"))


@skip_without_network
def test_live_route_produces_a_traceable_envelope() -> None:
    """A real fetch is well formed, cites real records, and invents nothing."""
    travel_date = _travel_date()

    envelope = _fetch("JFK", "SFO", travel_date)

    assert envelope["source"] == config.SOURCE_NWS
    assert envelope["status"] == config.STATUS_OK
    assert envelope["error"] is None and envelope["error_code"] is None
    assert envelope["travel_date"] == travel_date.isoformat()
    assert envelope["retrieved_at"].endswith("Z")
    assert envelope["latency_ms"] and envelope["latency_ms"] > 0
    assert envelope["records"] == len(envelope["signals"]) >= 3
    assert envelope["raw_response_path"] in envelope["raw_response_paths"]

    evidence = _evidence_by_id(envelope)
    assert len(evidence) >= 4

    for signal in envelope["signals"]:
        assert signal["kind"] in (
            nws._SIGNAL_KIND_ALERT,
            nws._SIGNAL_KIND_FORECAST,
            nws._SIGNAL_KIND_SUMMARY,
        )
        assert signal["severity"] in config.RISK_LEVELS
        assert signal["confidence"] is not None and 0.0 <= signal["confidence"] <= 1.0
        assert signal["summary"]
        assert signal["evidence_ids"], "a signal without evidence must not be returned"
        for evidence_id in signal["evidence_ids"]:
            entry = evidence[evidence_id]
            assert entry["source"] == config.SOURCE_NWS
            assert entry["note"]
            path = entry["raw_response_path"]
            if path is None:
                # The static table row is the only value with no HTTP record behind it.
                assert entry["field"] == "iata_lookup.row"
                continue
            record = _load(path)
            assert record["raw_logged_before_parsing"] is True
            assert record["source"] == config.SOURCE_NWS
            assert record["response"]["status_code"] == 200
            assert record["context"]["airport"] in ("JFK", "SFO")
            assert record["context"]["endpoint"] in ("points", "forecast", "alerts")

    # Forecast signals are the periods covering the travel date, for both airports,
    # and each one names the period it represents.
    forecast = [
        signal for signal in envelope["signals"] if signal["kind"] == nws._SIGNAL_KIND_FORECAST
    ]
    assert {signal["metrics"]["airport"] for signal in forecast} == {"JFK", "SFO"}
    for signal in forecast:
        assert signal["metrics"]["travel_date"] == travel_date.isoformat()
        assert signal["metrics"]["travel_date_overlap"] is True
        assert signal["metrics"]["period_name"]
        assert signal["metrics"]["start"] and signal["metrics"]["end"]
        assert signal["metrics"]["severity_reasons"]
    assert any(signal["metrics"]["is_daytime"] for signal in forecast)

    # The airports sit in different timezones, and each signal keeps its own offset.
    offsets = {
        airport: {
            signal["metrics"]["start"][-6:]
            for signal in forecast
            if signal["metrics"]["airport"] == airport
        }
        for airport in ("JFK", "SFO")
    }
    assert offsets["JFK"] != offsets["SFO"]

    # The envelope projects straight onto the API's per-source status model.
    status = SourceStatus(**base.to_source_status(envelope))
    assert status.source == config.SOURCE_NWS
    assert status.status == config.STATUS_OK
    assert status.records == envelope["records"]


@skip_without_network
def test_live_alerts_and_forecast_agree_with_the_documents_on_disk() -> None:
    """The alert signals rest on the alert records, and quote their own fields."""
    travel_date = _travel_date()

    envelope = _fetch("JFK", "SFO", travel_date)

    alert_signals = [
        signal for signal in envelope["signals"] if signal["kind"] == nws._SIGNAL_KIND_ALERT
    ]
    if not alert_signals:
        pytest.skip("no active NWS alerts at these airports right now")

    for signal in alert_signals:
        assert signal["metrics"]["event"]
        assert signal["metrics"]["nws_severity"]
        assert signal["metrics"]["overlaps_travel_date"] in (True, False, None)
        assert 0.0 <= signal["confidence"] <= 1.0
        for evidence_id in signal["evidence_ids"]:
            entry = _evidence_by_id(envelope)[evidence_id]
            record = _load(entry["raw_response_path"])
            body = json.loads(record["response"]["body_text"])

            # The alert we quote really is in the document we logged.
            quoted = json.loads(entry["value"])
            published = [
                feature["properties"]
                for feature in body["features"]
                if feature.get("properties")
            ]
            assert any(
                props.get("id") == quoted["id"]
                and props.get("event") == quoted["event"]
                and props.get("severity") == quoted["severity"]
                for props in published
            )


# --------------------------------------------------------------------------- #
# Coverage limits, live
# --------------------------------------------------------------------------- #
@skip_without_network
def test_live_date_beyond_the_forecast_horizon_is_unavailable() -> None:
    """Far-out dates are an explicit 'no data', and the evidence is still on disk."""
    travel_date = datetime.date.today() + datetime.timedelta(days=45)

    envelope = _fetch("JFK", "SFO", travel_date)

    assert envelope["status"] == config.STATUS_UNAVAILABLE
    assert envelope["error_code"] == base.ErrorCode.NO_DATA_FOR_QUERY
    assert envelope["signals"] == []
    assert travel_date.isoformat() in envelope["error"]
    assert "Nothing was extrapolated" in envelope["error"]
    assert envelope["raw_response_paths"], "an uncovered query still leaves evidence"
    for path in envelope["raw_response_paths"]:
        assert _load(path)["response"]["status_code"] == 200


@skip_without_network
def test_live_unsupported_route_is_unavailable_not_a_guess() -> None:
    """A non-US code is a coverage limit of this source, stated as such."""
    envelope = asyncio.run(
        nws.fetch_signals(base.Location.from_iata("LHR", "JFK"), _travel_date())
    )

    assert envelope["status"] == config.STATUS_UNAVAILABLE
    assert envelope["error_code"] == base.ErrorCode.NO_DATA_FOR_QUERY
    assert "LHR" in envelope["error"]
    assert envelope["raw_response_paths"] == [], "nothing may be fetched for it"


@skip_without_network
def test_live_logging_disabled_refuses_to_return_untraceable_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With raw logging off, the source cannot evidence a claim, so it returns none.

    This is the end-to-end version of the README's "logging is load-bearing" note: the
    call fails fast with ``logging_disabled`` instead of returning weather values that
    no record on disk can support.
    """
    monkeypatch.setattr(config, "LOG_RAW_RESPONSES", False, raising=False)

    envelope = asyncio.run(
        nws.fetch_signals(base.Location.from_iata("JFK", "JFK"), _travel_date())
    )

    assert envelope["status"] == config.STATUS_ERROR
    assert envelope["error_code"] == base.ErrorCode.LOGGING_DISABLED
    assert envelope["signals"] == []
    assert envelope["raw_response_paths"] == []


# --------------------------------------------------------------------------- #
# The static table, checked against NWS itself
# --------------------------------------------------------------------------- #
@skip_without_network
@pytest.mark.parametrize("iata", ["JFK", "SFO", "DEN", "MIA", "ANC", "HNL", "SJU"])
def test_live_table_rows_are_accepted_by_nws(iata: str) -> None:
    """Cross-check table rows against the live ``/points`` endpoint.

    NWS reports the *nearest* municipality rather than the served city, so the check
    is what this app actually depends on: the point is inside NWS coverage, in the
    expected state, and the timezone we publish is the grid's own.
    """
    info = iata_lookup.lookup_airport(iata)
    assert info is not None

    result = asyncio.run(
        base.get_json(
            config.SOURCE_NWS,
            "{0}/points/{1}".format(config.NWS_BASE_URL, info.coordinates),
            headers={
                "User-Agent": config.NWS_USER_AGENT,
                "Accept": "application/geo+json",
            },
            timeout=10.0,
            max_retries=0,
            expect_keys=["properties"],
            context={"check": "iata_lookup cross-check", "airport": info.iata},
        )
    )
    if not result.ok and result.error_code in UPSTREAM_ERROR_CODES:
        pytest.skip("upstream NWS problem: {0}".format(result.error))

    assert result.ok is True, result.error
    properties = result.payload["properties"]
    relative = properties["relativeLocation"]["properties"]
    assert relative["state"] == info.state
    assert properties["timeZone"] == info.timezone
    assert isinstance(properties["gridX"], int) and isinstance(properties["gridY"], int)
    assert result.raw_path is not None
    assert _load(result.raw_path)["response"]["status_code"] == 200
