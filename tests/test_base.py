"""Contract tests for ``data_sources.base``.

No network and no fabricated vendor data here: these tests exercise the shared
envelope contract, the error-state vocabulary, secret redaction and the evidence
reader using values generated inside the test. Anything that needs a real payload
lives in ``test_base_live.py``, which hits a real keyless API.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Dict, Set

import pytest

import config
from backend.models import SourceStatus
from data_sources import base


def _generated_secret() -> str:
    """A throwaway sentinel used to prove redaction (never a real credential)."""
    return "sentinel-" + uuid.uuid4().hex


def _location() -> base.Location:
    """The route used across these contract tests."""
    return base.Location.from_iata("jfk", "sfo")


def _model_field_names(model: type) -> Set[str]:
    """Field names of a pydantic model, whether v1 or v2 is installed."""
    fields = getattr(model, "model_fields", None)
    if fields is None:
        fields = getattr(model, "__fields__")
    return set(fields)


# --------------------------------------------------------------------------- #
# Error-state vocabulary
# --------------------------------------------------------------------------- #
def test_error_codes_are_declared_and_unique() -> None:
    codes = base.ErrorCode.all_codes()
    assert len(codes) >= 15
    assert len(codes) == len(set(codes))
    assert all(code.strip() == code and code for code in codes)


@pytest.mark.parametrize(
    "error_code",
    [
        base.ErrorCode.NO_DATA_FOR_QUERY,
        base.ErrorCode.PLAN_LIMIT,
        base.ErrorCode.NOT_IMPLEMENTED,
    ],
)
def test_no_data_codes_map_to_unavailable_not_error(error_code: str) -> None:
    """A plan limit or empty date window is 'unavailable', never a source failure."""
    assert base.status_for_error_code(error_code) == config.STATUS_UNAVAILABLE


@pytest.mark.parametrize(
    "error_code",
    [
        base.ErrorCode.MISSING_CREDENTIAL,
        base.ErrorCode.TIMEOUT,
        base.ErrorCode.NETWORK_ERROR,
        base.ErrorCode.RATE_LIMITED,
        base.ErrorCode.MALFORMED_PAYLOAD,
        base.ErrorCode.UNEXPECTED_PAYLOAD_SHAPE,
        base.ErrorCode.LOGGING_FAILED,
        base.ErrorCode.LOGGING_DISABLED,
        base.ErrorCode.UNEXPECTED_ERROR,
    ],
)
def test_failure_codes_map_to_error(error_code: str) -> None:
    assert base.status_for_error_code(error_code) == config.STATUS_ERROR


def test_unknown_code_falls_back_to_error_status() -> None:
    assert base.status_for_error_code(None) == config.STATUS_ERROR
    assert base.status_for_error_code("not-a-real-code") == config.STATUS_ERROR


@pytest.mark.parametrize(
    "status_code,expected",
    [
        (401, base.ErrorCode.UNAUTHORIZED),
        (403, base.ErrorCode.FORBIDDEN),
        (404, base.ErrorCode.NOT_FOUND),
        (429, base.ErrorCode.RATE_LIMITED),
        (418, base.ErrorCode.CLIENT_ERROR),
        (500, base.ErrorCode.SERVER_ERROR),
        (503, base.ErrorCode.SERVER_ERROR),
    ],
)
def test_http_status_maps_to_error_code(status_code: int, expected: str) -> None:
    assert base.error_code_for_status(status_code) == expected


def test_only_transient_statuses_are_retryable() -> None:
    assert base.is_retryable_status(429)
    assert base.is_retryable_status(500)
    assert base.is_retryable_status(503)
    assert not base.is_retryable_status(400)
    assert not base.is_retryable_status(401)
    assert not base.is_retryable_status(404)


# --------------------------------------------------------------------------- #
# Secret redaction (rule 7)
# --------------------------------------------------------------------------- #
def test_secret_query_parameter_values_are_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = _generated_secret()
    monkeypatch.setattr(config, "AVIATIONSTACK_API_KEY", secret, raising=False)
    url = "https://api.example.test/v1/flights?access_key={0}&limit=10".format(secret)

    redacted = base.redact_url(url)

    assert secret not in redacted
    assert "REDACTED" in redacted
    assert "limit=10" in redacted  # non-secret parameters stay for traceability


def test_key_style_parameters_are_redacted_without_knowing_the_value() -> None:
    unknown_value = uuid.uuid4().hex
    redacted = base.redact_url(
        "https://api.example.test/traffic?key={0}&point=40.6,-73.7".format(
            unknown_value
        )
    )

    assert unknown_value not in redacted
    assert "point=40.6,-73.7" in redacted


def test_credentials_are_scrubbed_from_text_headers_and_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = _generated_secret()
    monkeypatch.setattr(config, "TOMTOM_API_KEY", secret, raising=False)

    assert secret not in base.redact_text("call failed for key {0}".format(secret))

    headers = base.redact_headers(
        {"Authorization": "Bearer " + secret, "Accept": "application/json"}
    )
    assert secret not in json.dumps(headers)
    assert headers["Accept"] == "application/json"

    params = base.redact_params({"key": secret, "point": "40.6,-73.7"})
    assert secret not in json.dumps(params)
    assert params["point"] == "40.6,-73.7"


def test_redaction_leaves_clean_urls_untouched() -> None:
    url = "https://api.weather.gov/points/40.6413,-73.7781"
    assert base.redact_url(url) == url


# --------------------------------------------------------------------------- #
# Route input
# --------------------------------------------------------------------------- #
def test_location_normalizes_iata_codes() -> None:
    location = _location()

    assert location.route_iata == ("JFK", "SFO")
    assert location.route_label == "JFK -> SFO"
    assert location.origin.has_coordinates is False
    assert location.to_dict()["origin"]["iata"] == "JFK"
    assert location.to_dict()["route_label"] == "JFK -> SFO"


def test_endpoint_requires_an_iata_code_and_paired_coordinates() -> None:
    with pytest.raises(ValueError):
        base.Endpoint(iata="   ")
    with pytest.raises(ValueError):
        base.Endpoint(iata="JFK", latitude=40.6413)
    with pytest.raises(ValueError):
        base.Endpoint(iata="JFK", longitude=-73.7781)

    with_coordinates = base.Endpoint(iata="JFK", latitude=40.6413, longitude=-73.7781)
    assert with_coordinates.has_coordinates is True
    assert with_coordinates.to_dict()["latitude"] == 40.6413


# --------------------------------------------------------------------------- #
# Normalized envelope builders
# --------------------------------------------------------------------------- #
def test_ok_envelope_requires_at_least_one_signal() -> None:
    """An empty 'ok' is the most dangerous fake result, so it is rejected outright."""
    with pytest.raises(ValueError):
        base.ok_signals(source=config.SOURCE_NWS, signals=[])


def test_ok_envelope_shape_ids_and_evidence_links() -> None:
    evidence = [
        base.make_evidence(
            config.SOURCE_NWS,
            field="properties.forecastGridData",
            value="https://api.weather.gov/gridpoints/OKX/33,37",
            observed_at="2026-10-16T18:00:00Z",
            raw_response_path="logs/raw/nws_point.json",
        )
    ]
    signals = [
        base.make_signal(
            "weather_forecast",
            "Forecast for the travel date was retrieved and normalized",
            severity="low",
            confidence=0.5,
            observed_at="2026-10-16T18:00:00Z",
            effective_at="2026-11-03",
            metrics={"wind_mph": 12},
            evidence_ids=["ev_1"],
        )
    ]

    envelope = base.ok_signals(
        source=config.SOURCE_NWS,
        signals=signals,
        evidence=evidence,
        location=_location(),
        travel_date=datetime.date(2026, 11, 3),
        raw_response_path="logs/raw/nws_point.json",
        raw_response_paths=["logs/raw/nws_point.json"],
        latency_ms=123.4,
    )

    assert envelope["status"] == config.STATUS_OK
    assert envelope["error"] is None and envelope["error_code"] is None
    assert envelope["records"] == 1
    assert envelope["travel_date"] == "2026-11-03"
    assert envelope["signals"][0]["id"] == "sig_1"
    assert envelope["evidence"][0]["id"] == "ev_1"
    assert envelope["signals"][0]["evidence_ids"] == ["ev_1"]
    assert envelope["location"] is not None
    assert envelope["location"]["route_label"] == "JFK -> SFO"
    assert envelope["retrieved_at"].endswith("Z")
    assert base.has_usable_signals(envelope) is True
    assert set(envelope) == {
        "source",
        "status",
        "error",
        "error_code",
        "retrieved_at",
        "latency_ms",
        "travel_date",
        "location",
        "records",
        "signals",
        "evidence",
        "raw_response_path",
        "raw_response_paths",
        "warnings",
    }


def test_signals_may_not_cite_unknown_evidence() -> None:
    signals = [
        base.make_signal(
            "weather_alert", "Alert retrieved", evidence_ids=["ev_missing"]
        )
    ]
    with pytest.raises(ValueError):
        base.ok_signals(source=config.SOURCE_NWS, signals=signals)


def test_builder_copies_its_inputs() -> None:
    signals = [base.make_signal("flight_delay", "Departure delayed 45 minutes")]
    evidence = [base.make_evidence(config.SOURCE_AVIATIONSTACK, field="delay")]

    envelope = base.ok_signals(
        source=config.SOURCE_AVIATIONSTACK, signals=signals, evidence=evidence
    )
    signals[0]["summary"] = "mutated after the fact"
    evidence[0]["field"] = "mutated after the fact"

    assert envelope["signals"][0]["summary"] == "Departure delayed 45 minutes"
    assert envelope["evidence"][0]["field"] == "delay"


def test_explicit_signal_ids_are_preserved() -> None:
    signals = [
        base.make_signal("weather_alert", "Alert", signal_id="nws_alert_1"),
        base.make_signal("weather_forecast", "Forecast"),
    ]
    envelope = base.ok_signals(source=config.SOURCE_NWS, signals=signals)

    assert envelope["signals"][0]["id"] == "nws_alert_1"
    assert envelope["signals"][1]["id"] == "sig_2"


# --------------------------------------------------------------------------- #
# Normalized signal validation
# --------------------------------------------------------------------------- #
def test_severity_is_normalized_or_rejected() -> None:
    with pytest.raises(ValueError):
        base.make_signal("weather_alert", "Alert", severity="catastrophic")

    assert base.make_signal("weather_alert", "Alert", severity=" HIGH ")[
        "severity"
    ] == "high"
    assert base.make_signal("weather_alert", "Alert")["severity"] is None


def test_signal_requires_kind_and_summary() -> None:
    with pytest.raises(ValueError):
        base.make_signal("   ", "A summary")
    with pytest.raises(ValueError):
        base.make_signal("weather_alert", "   ")


@pytest.mark.parametrize("confidence", [-0.1, 1.5, "not-a-number"])
def test_confidence_must_be_a_probability(confidence: Any) -> None:
    with pytest.raises(ValueError):
        base.make_signal("weather_alert", "Alert", confidence=confidence)


def test_confidence_bounds_are_inclusive() -> None:
    assert base.make_signal("weather_alert", "Alert", confidence=0)["confidence"] == 0.0
    assert base.make_signal("weather_alert", "Alert", confidence=1)["confidence"] == 1.0


def test_evidence_requires_a_source_id() -> None:
    with pytest.raises(ValueError):
        base.make_evidence("   ")


def test_evidence_value_is_rendered_as_text_and_truncated() -> None:
    entry = base.make_evidence(
        config.SOURCE_NWS, field="properties", value={"a": 1, "b": [2, 3]}
    )
    assert entry["value"] == '{"a": 1, "b": [2, 3]}'

    long_entry = base.make_evidence(
        config.SOURCE_NWS,
        field="body",
        value="x" * (config.EVIDENCE_VALUE_MAX_CHARS + 50),
    )
    assert long_entry["value"] is not None
    assert "truncated" in long_entry["value"]

