"""Failure-state and API-projection tests for ``data_sources.base``.

Pure contract tests: no network and no fabricated vendor payloads. The
``HttpResult`` objects below are built directly with ``payload=None`` to exercise
the failure mapping; real HTTP behaviour is covered by ``test_base_live.py``.
"""

from __future__ import annotations

import datetime
import json
import uuid
from typing import Any, Dict

import pytest

import config
from backend.models import SourceStatus
from data_sources import base


def _generated_secret() -> str:
    """A throwaway sentinel used to prove credential handling, never a real key."""
    return "sentinel-" + uuid.uuid4().hex


def _location() -> base.Location:
    """The route used across these contract tests."""
    return base.Location.from_iata("jfk", "sfo")


def _model_field_names(model: type) -> set:
    """Field names of a pydantic model, whether v1 or v2 is installed."""
    fields = getattr(model, "model_fields", None)
    if fields is None:
        fields = getattr(model, "__fields__")
    return set(fields)


def _http_result(**overrides: Any) -> base.HttpResult:
    """A bare HttpResult for contract tests; it carries no vendor payload."""
    values: Dict[str, Any] = {
        "ok": False,
        "method": "GET",
        "url": "https://api.example.test/v1/resource",
        "status_code": None,
        "latency_ms": 1.0,
        "attempts": 1,
        "payload": None,
        "raw_path": None,
        "raw_paths": (),
        "error": "no data for this query",
        "error_code": base.ErrorCode.NO_DATA_FOR_QUERY,
        "response_headers": {},
        "body_preview": None,
        "content_type": None,
    }
    values.update(overrides)
    return base.HttpResult(**values)


# --------------------------------------------------------------------------- #
# Failure and "unavailable" envelopes
# --------------------------------------------------------------------------- #
def test_failure_envelope_requires_both_reason_and_code() -> None:
    with pytest.raises(ValueError):
        base.failure_signals(
            source=config.SOURCE_NWS, error_code=base.ErrorCode.TIMEOUT, error="  "
        )
    with pytest.raises(ValueError):
        base.failure_signals(
            source=config.SOURCE_NWS, error_code="  ", error="Request failed"
        )


def test_failure_envelope_carries_no_signals_but_keeps_the_evidence_path() -> None:
    envelope = base.failure_signals(
        source=config.SOURCE_AVIATIONSTACK,
        error_code=base.ErrorCode.RATE_LIMITED,
        error="HTTP 429 from the vendor after 3 attempts.",
        location=_location(),
        travel_date=datetime.date(2026, 11, 3),
        raw_response_path="logs/raw/aviationstack_429.json",
    )

    assert envelope["status"] == config.STATUS_ERROR
    assert envelope["signals"] == []
    assert envelope["evidence"] == []
    assert envelope["records"] == 0
    assert envelope["error_code"] == base.ErrorCode.RATE_LIMITED
    assert envelope["raw_response_path"] == "logs/raw/aviationstack_429.json"
    assert base.has_usable_signals(envelope) is False


def test_unavailable_envelope_reports_missing_data_without_claiming_failure() -> None:
    envelope = base.unavailable_signals(
        source=config.SOURCE_AVIATIONSTACK,
        error_code=base.ErrorCode.PLAN_LIMIT,
        error="The free plan does not return schedules for this date.",
        location=_location(),
        travel_date=datetime.date(2026, 11, 3),
        warnings=["Flight signals are missing from this assessment."],
    )

    assert envelope["status"] == config.STATUS_UNAVAILABLE
    assert envelope["signals"] == []
    assert envelope["warnings"] == ["Flight signals are missing from this assessment."]
    assert base.has_usable_signals(envelope) is False


def test_missing_credential_signals_fires_only_when_the_env_var_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "TOMTOM_API_KEY", None, raising=False)

    envelope = base.missing_credential_signals(
        config.SOURCE_TOMTOM, location=_location()
    )

    assert envelope is not None
    assert envelope["status"] == config.STATUS_ERROR
    assert envelope["error_code"] == base.ErrorCode.MISSING_CREDENTIAL
    assert "TOMTOM_API_KEY" in (envelope["error"] or "")
    assert envelope["signals"] == []

    monkeypatch.setattr(config, "TOMTOM_API_KEY", _generated_secret(), raising=False)
    assert base.missing_credential_signals(config.SOURCE_TOMTOM) is None
    # A keyless source is never blocked by a credential check.
    assert base.missing_credential_signals(config.SOURCE_NWS) is None


# --------------------------------------------------------------------------- #
# HttpResult -> envelope / API model projection
# --------------------------------------------------------------------------- #
def test_failure_status_property_reflects_the_error_code() -> None:
    assert _http_result().failure_status == config.STATUS_UNAVAILABLE
    assert (
        _http_result(error_code=base.ErrorCode.TIMEOUT).failure_status
        == config.STATUS_ERROR
    )
    assert (
        _http_result(ok=True, error=None, error_code=None).failure_status
        == config.STATUS_OK
    )


def test_signals_from_failure_rejects_a_successful_result() -> None:
    with pytest.raises(ValueError):
        base.signals_from_failure(
            _http_result(ok=True, error=None, error_code=None),
            source=config.SOURCE_NWS,
        )


def test_signals_from_failure_keeps_every_raw_evidence_path() -> None:
    envelope = base.signals_from_failure(
        _http_result(
            error_code=base.ErrorCode.TIMEOUT,
            error="Timeout calling the vendor after 10s.",
            attempts=2,
            raw_path="logs/raw/nws_2.json",
            raw_paths=("logs/raw/nws_1.json", "logs/raw/nws_2.json"),
        ),
        source=config.SOURCE_NWS,
        location=_location(),
    )

    assert envelope["status"] == config.STATUS_ERROR
    assert envelope["error_code"] == base.ErrorCode.TIMEOUT
    assert envelope["raw_response_path"] == "logs/raw/nws_2.json"
    assert envelope["raw_response_paths"] == [
        "logs/raw/nws_1.json",
        "logs/raw/nws_2.json",
    ]
    assert envelope["signals"] == []


def test_signals_from_failure_reports_no_data_as_unavailable() -> None:
    envelope = base.signals_from_failure(
        _http_result(),  # error_code = NO_DATA_FOR_QUERY
        source=config.SOURCE_AVIATIONSTACK,
        location=_location(),
    )

    assert envelope["status"] == config.STATUS_UNAVAILABLE
    assert envelope["error_code"] == base.ErrorCode.NO_DATA_FOR_QUERY
    assert envelope["signals"] == []


def test_source_status_projection_matches_the_api_model() -> None:
    envelope = base.failure_signals(
        source=config.SOURCE_TOMTOM,
        error_code=base.ErrorCode.MISSING_CREDENTIAL,
        error="TOMTOM_API_KEY is not set.",
        location=_location(),
    )

    projection = base.to_source_status(envelope)

    assert set(projection) == _model_field_names(SourceStatus)
    model = SourceStatus(**projection)
    assert model.status == config.STATUS_ERROR
    assert model.error_code == base.ErrorCode.MISSING_CREDENTIAL
    assert model.records == 0
    assert model.consulted_at == envelope["retrieved_at"]


# --------------------------------------------------------------------------- #
# Partial envelopes - real signals *and* recorded failures, never a quiet "ok"
# --------------------------------------------------------------------------- #
def _partial_evidence() -> base.EvidenceEntry:
    """One real evidence entry, pointing at the raw response it came from."""
    return base.make_evidence(
        config.SOURCE_NWS,
        field="periods[0].shortForecast",
        value="Mostly Clear",
        observed_at="2026-11-03T12:00:00Z",
        raw_response_path="logs/raw/nws_forecast.json",
        evidence_id="ev_JFK_1",
    )


def _partial_signal() -> base.SignalEntry:
    return base.make_signal(
        "forecast_conditions",
        "JFK: Mostly Clear on the travel date",
        severity="low",
        confidence=0.6,
        evidence_ids=["ev_JFK_1"],
    )


def _partial_failure(
    error_code: str = base.ErrorCode.TIMEOUT, **overrides: Any
) -> base.SubQueryFailure:
    values: Dict[str, Any] = {
        "scope": "SFO",
        "error_code": error_code,
        "error": "NWS did not answer within 10s.",
    }
    values.update(overrides)
    return values  # type: ignore[return-value]


def _partial(**overrides: Any) -> base.NormalizedSignals:
    """A valid partial envelope; overrides let each test break exactly one rule."""
    values: Dict[str, Any] = {
        "source": config.SOURCE_NWS,
        "signals": [_partial_signal()],
        "failures": [_partial_failure()],
        "evidence": [_partial_evidence()],
        "location": _location(),
        "travel_date": datetime.date(2026, 11, 3),
    }
    values.update(overrides)
    return base.partial_signals(**values)


def test_partial_envelope_keeps_the_successes_and_names_the_failures() -> None:
    envelope = _partial()

    assert envelope["status"] == config.STATUS_PARTIAL
    assert envelope["status"] != config.STATUS_OK
    assert [signal["summary"] for signal in envelope["signals"]] == [
        "JFK: Mostly Clear on the travel date"
    ]
    assert envelope["records"] == 1
    assert envelope["error_code"] == base.ErrorCode.TIMEOUT
    assert envelope["error_codes"] == [base.ErrorCode.TIMEOUT]
    assert "SFO" in (envelope["error"] or "")
    assert base.ErrorCode.TIMEOUT in (envelope["error"] or "")
    # The reduced confidence is visible in the payload, not only in the docs.
    assert any("weaker evidence" in warning for warning in envelope["warnings"])
    assert base.has_usable_signals(envelope) is True


def test_partial_envelope_without_signals_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one real signal"):
        _partial(signals=[])


def test_partial_envelope_without_failures_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one failed sub-query"):
        _partial(failures=[])


def test_partial_failure_needs_both_a_code_and_a_message() -> None:
    for broken in (
        {"error": "NWS did not answer"},
        {"error_code": base.ErrorCode.TIMEOUT},
        {"error_code": "   ", "error": "  "},
    ):
        with pytest.raises(ValueError, match="needs both"):
            _partial(failures=[broken])


def test_partial_signal_must_cite_resolvable_evidence() -> None:
    with pytest.raises(ValueError, match="cites no evidence"):
        _partial(signals=[base.make_signal("forecast_conditions", "No evidence")])

    with pytest.raises(ValueError, match="unknown evidence id"):
        _partial(
            signals=[
                base.make_signal(
                    "forecast_conditions", "Dangling citation", evidence_ids=["ev_nope"]
                )
            ]
        )


def test_partial_keeps_one_code_per_failure_in_order() -> None:
    envelope = _partial(
        failures=[
            _partial_failure(base.ErrorCode.TIMEOUT, scope="JFK"),
            _partial_failure(base.ErrorCode.NO_DATA_FOR_QUERY, scope="SFO"),
            _partial_failure(base.ErrorCode.TIMEOUT, scope="SFO"),
        ]
    )

    assert envelope["error_codes"] == [
        base.ErrorCode.TIMEOUT,
        base.ErrorCode.NO_DATA_FOR_QUERY,
        base.ErrorCode.TIMEOUT,
    ]
    assert envelope["error_code"] == base.ErrorCode.TIMEOUT
    assert "JFK" in (envelope["error"] or "")
    assert "SFO" in (envelope["error"] or "")


def test_partial_projects_onto_the_source_status_model() -> None:
    envelope = _partial()
    projection = base.to_source_status(envelope)

    assert set(projection) == _model_field_names(SourceStatus)
    model = SourceStatus(**projection)
    assert model.status == config.STATUS_PARTIAL
    assert model.error_code == base.ErrorCode.TIMEOUT
    assert model.error_codes == [base.ErrorCode.TIMEOUT]
    assert model.records == 1
    assert model.consulted_at == envelope["retrieved_at"]


def test_partial_serialization_preserves_signals_and_error_codes() -> None:
    envelope = _partial()

    restored = json.loads(json.dumps(envelope))

    assert restored["status"] == config.STATUS_PARTIAL
    assert [signal["summary"] for signal in restored["signals"]] == [
        signal["summary"] for signal in envelope["signals"]
    ]
    assert restored["signals"][0]["evidence_ids"] == ["ev_JFK_1"]
    assert restored["error_codes"] == [base.ErrorCode.TIMEOUT]
    assert restored["error"] == envelope["error"]
    assert restored["evidence"][0]["raw_response_path"] == "logs/raw/nws_forecast.json"


def test_only_partial_envelopes_carry_error_codes() -> None:
    ok = base.ok_signals(
        source=config.SOURCE_NWS,
        signals=[_partial_signal()],
        evidence=[_partial_evidence()],
    )
    failed = base.failure_signals(
        source=config.SOURCE_NWS,
        error_code=base.ErrorCode.TIMEOUT,
        error="NWS did not answer within 10s.",
    )

    # Shape stability: existing consumers see exactly the keys they always did.
    assert "error_codes" not in ok
    assert "error_codes" not in failed
    assert "error_codes" in _partial()

    # The projection always carries a list, so partial needs no special case.
    assert base.to_source_status(ok)["error_codes"] == []
    assert base.to_source_status(failed)["error_codes"] == [base.ErrorCode.TIMEOUT]


def test_partial_is_never_derived_from_an_error_code() -> None:
    for code in base.ErrorCode.all_codes():
        assert base.status_for_error_code(code) != config.STATUS_PARTIAL

