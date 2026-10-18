"""Live contract tests for the TomTom source (``data_sources.tomtom``).

These call the real API. They assert the *contract* - a well-formed envelope, evidence
that resolves to a real record on disk, severities drawn from our own vocabulary, and an
explicit ``unavailable`` state where the feed or the plan cannot answer. They never
assert which incidents exist, so they do not rot.

The key-error test needs no credential: a request with an invalid key is a real
response this module must classify (the live API answers HTTP 401 with
``{"detailedError": {"code": "Unauthorized", ...}}``). The real-route tests skip when
``TOMTOM_API_KEY`` is unset and skip - rather than fail - when the upstream is
throttling, erroring or rejecting the configured key: what is under test is this
module's handling of real responses, not the vendor's uptime or the developer's key. A
``plan_limit`` is *not* skipped: an entitlement gap is a finding, and it is asserted.
A defect of *ours* (an unusable payload, an unexpected exception) still fails.
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
from data_sources import base, tomtom


def _network_available() -> bool:
    """Whether a TCP connection to the TomTom host can be established."""
    try:
        with socket.create_connection(("api.tomtom.com", 443), timeout=5.0):
            return True
    except OSError:
        return False


NETWORK_AVAILABLE: bool = _network_available()

pytestmark = pytest.mark.live

skip_without_network = pytest.mark.skipif(
    not NETWORK_AVAILABLE,
    reason="no outbound network access: live source contract tests cannot run",
)

skip_without_key = pytest.mark.skipif(
    not config.TOMTOM_API_KEY,
    reason=(
        "TOMTOM_API_KEY is not set: the live incident query cannot run. The key-error "
        "and projection tests still run, because an invalid key is itself a real "
        "response the module must classify."
    ),
)

# Vendor states that are not this module's fault: quota, key, upstream faults.
CONFIG_OR_UPSTREAM_CODES = frozenset(
    {
        base.ErrorCode.TIMEOUT,
        base.ErrorCode.NETWORK_ERROR,
        base.ErrorCode.SERVER_ERROR,
        base.ErrorCode.RATE_LIMITED,
        base.ErrorCode.CLIENT_ERROR,
        base.ErrorCode.UNAUTHORIZED,
        base.ErrorCode.FORBIDDEN,
        base.ErrorCode.MISSING_CREDENTIAL,
    }
)


def _load(path: str) -> Dict[str, Any]:
    """Load a raw evidence record written by ``base.get_json``."""
    record_path = Path(config.PROJECT_ROOT) / path
    assert record_path.is_file(), path
    return json.loads(record_path.read_text(encoding="utf-8"))


def _fetch(travel_date: datetime.date) -> Dict[str, Any]:
    """Fetch one route live, skipping when the *vendor* or the key is the problem."""
    envelope = asyncio.run(
        tomtom.fetch_signals(base.Location.from_iata("JFK", "LAX"), travel_date)
    )
    if (
        envelope["status"] == config.STATUS_ERROR
        and envelope["error_code"] in CONFIG_OR_UPSTREAM_CODES
    ):
        pytest.skip(
            "vendor, quota or key problem, not a module defect: {0}".format(
                envelope["error"]
            )
        )
    return envelope


def _assert_traceable(envelope: Dict[str, Any]) -> None:
    """Every signal cites evidence, and every evidence entry names a real record."""
    evidence = {entry["id"]: entry for entry in envelope["evidence"]}
    records = set(envelope["raw_response_paths"])
    assert records, "a live result must name its raw record(s)"
    for path in records:
        record = _load(path)
        assert (record.get("response") or {}).get("status_code") == 200
    for signal in envelope["signals"]:
        assert signal["evidence_ids"], "a signal must cite evidence"
        for evidence_id in signal["evidence_ids"]:
            assert evidence_id in evidence
            assert evidence[evidence_id]["raw_response_path"] in records

@skip_without_network
@skip_without_key
def test_live_current_query_produces_a_well_formed_traceable_envelope() -> None:
    """Today: real signals or an explicit limitation - never an empty 'ok'."""
    travel_date = base.utc_now().date()

    envelope = _fetch(travel_date)

    assert envelope["source"] == config.SOURCE_TOMTOM
    assert envelope["travel_date"] == travel_date.isoformat()
    assert envelope["status"] in (
        config.STATUS_OK,
        config.STATUS_UNAVAILABLE,
    )

    if envelope["status"] == config.STATUS_UNAVAILABLE:
        # The finding, not a failure: the plan or the feed cannot answer today.
        assert envelope["error_code"] in (
            base.ErrorCode.PLAN_LIMIT,
            base.ErrorCode.NO_DATA_FOR_QUERY,
        )
        assert envelope["signals"] == []
        assert envelope["error"]
        return

    assert envelope["error"] is None and envelope["error_code"] is None
    assert envelope["signals"], "an 'ok' envelope must contain real signals"
    assert envelope["records"] == len(envelope["signals"])
    for signal in envelope["signals"]:
        assert signal["kind"] in {"traffic_incident", "traffic_conditions"}
        assert signal["severity"] in config.RISK_LEVELS or signal["severity"] is None
        assert signal["metrics"]["scope"] in {"origin", "destination"}
        assert signal["metrics"]["radius_km"] == tomtom._RADIUS_KM
        assert signal["metrics"]["distance_km"] <= tomtom._RADIUS_KM
    _assert_traceable(envelope)


@skip_without_network
@skip_without_key
def test_live_tomorrow_query_is_prefixed_and_reduced_confidence() -> None:
    """Tomorrow is served from today's incidents, and must say so on every signal."""
    envelope = _fetch(base.utc_now().date() + datetime.timedelta(days=1))

    if envelope["status"] == config.STATUS_UNAVAILABLE:
        assert envelope["error_code"] in (
            base.ErrorCode.PLAN_LIMIT,
            base.ErrorCode.NO_DATA_FOR_QUERY,
        )
        return

    assert envelope["status"] == config.STATUS_OK
    for signal in envelope["signals"]:
        assert signal["summary"].startswith(tomtom._TOMORROW_PREFIX)
        assert signal["metrics"]["date_window"] == tomtom._DATE_WINDOW_TOMORROW
        assert signal["confidence"] <= tomtom._CONFIDENCE_INCIDENT
    _assert_traceable(envelope)


@skip_without_network
def test_live_invalid_key_is_reported_and_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No real key needed: the live 401 is a real response this module must classify."""
    monkeypatch.setattr(
        config, "TOMTOM_API_KEY", "invalid-sentinel-" + "0" * 12, raising=False
    )

    envelope = asyncio.run(
        tomtom.fetch_signals(
            base.Location.from_iata("JFK", "LAX"), base.utc_now().date()
        )
    )

    assert envelope["status"] == config.STATUS_ERROR
    assert envelope["error_code"] == base.ErrorCode.UNAUTHORIZED
    assert envelope["signals"] == []
    assert "TOMTOM_API_KEY" in (envelope["error"] or "")
    assert envelope["raw_response_path"]
    record = _load(envelope["raw_response_path"])
    assert (record.get("response") or {}).get("status_code") == 401
    assert "Unauthorized" in (record.get("response") or {}).get("body_text", "")


@skip_without_network
def test_live_envelope_projects_onto_the_api_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whatever the live answer is, the API projection must accept it."""
    monkeypatch.setattr(
        config, "TOMTOM_API_KEY", "invalid-sentinel-" + "0" * 12, raising=False
    )

    envelope = asyncio.run(
        tomtom.fetch_signals(
            base.Location.from_iata("JFK", "LAX"), base.utc_now().date()
        )
    )

    model = SourceStatus(**base.to_source_status(envelope))
    assert model.source == config.SOURCE_TOMTOM
    assert model.status in (
        config.STATUS_OK,
        config.STATUS_ERROR,
        config.STATUS_UNAVAILABLE,
    )
    assert model.raw_response_path == envelope["raw_response_path"]
    assert model.consulted_at == envelope["retrieved_at"]
