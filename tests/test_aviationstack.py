"""Parsing, mapping and failure-mode tests for ``data_sources.aviationstack``.

Transport is faked (``base.get_json`` is replaced); the network is not. Most aviationstack
fixtures are **real captures** made on 2026-10-17 with a working free-tier key:
``aviationstack_401_*`` (the live API's key errors), ``aviationstack_403_plan_restricted``
(a future ``flight_date``, which the free tier refuses with ``function_access_restricted``),
``aviationstack_flights_empty`` (a genuine zero-row route) and
``aviationstack_flights_JFK_LAX`` (100 of 173 flights on the real-time board).
``aviationstack_flights_bands`` is an **[INVENTED]** unit fixture kept on purpose: it holds
one flight per delay band plus a cancellation and a below-floor delay, which a real capture
cannot be relied on to contain on any given day. Provenance is in
``tests/fixtures/README.md``.

The delay-to-severity boundaries are covered twice: directly on the pure helper (so the
bands are pinned without any payload at all) and once end-to-end through a document.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import uuid
from pathlib import Path
from typing import Any, Dict, List

import pytest

import config
from data_sources import aviationstack, base

FIXTURES: Path = Path(__file__).parent / "fixtures"

# The date the synthetic flight records themselves report. Fixed in the past so it is
# never "today": that keeps the free-tier `flight_date` behaviour deterministic.
FIXTURE_TRAVEL_DATE: datetime.date = datetime.date(2025, 6, 12)

FLIGHTS_URL: str = config.AVIATIONSTACK_BASE_URL.rstrip("/") + "/flights"


def _fixture(name: str) -> Dict[str, Any]:
    """A fixture document from ``tests/fixtures`` (see the README for provenance)."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _fixture_path(name: str) -> str:
    """Repo-relative path of a fixture, used as the faked raw-evidence path."""
    return str((FIXTURES / name).relative_to(Path(config.PROJECT_ROOT)))


def _sentinel_key() -> str:
    """A throwaway key sentinel, never a real credential."""
    return "sentinel-" + uuid.uuid4().hex


def _result(
    payload: Any,
    raw_path: str,
    *,
    status_code: int = 200,
    url: str = FLIGHTS_URL,
) -> base.HttpResult:
    """A ``HttpResult`` wrapping a fixture document; only the transport is faked."""
    return base.HttpResult(
        ok=True,
        method="GET",
        url=url,
        status_code=status_code,
        latency_ms=11.0,
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
    payload: Any,
    raw_path: str,
    *,
    status_code: int,
    error_code: str,
    url: str = FLIGHTS_URL,
    attempts: int = 1,
) -> base.HttpResult:
    """A failed ``HttpResult``, as ``get_json`` returns after exhausting attempts."""
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
        error_code=error_code,
        response_headers={},
        body_preview="vendor error",
        content_type="application/json",
    )


class _FakeTransport:
    """A ``get_json`` stand-in that returns one prepared result and records the call."""

    def __init__(self, result: base.HttpResult) -> None:
        self.result = result
        self.calls: List[Dict[str, Any]] = []

    async def __call__(self, source: str, url: str, **kwargs: Any) -> base.HttpResult:
        self.calls.append({"source": source, "url": url, "kwargs": kwargs})
        return self.result

    def params(self) -> Dict[str, Any]:
        """The query parameters of the single request that was made."""
        assert len(self.calls) == 1, "expected exactly one request, got {0}".format(
            len(self.calls)
        )
        return dict(self.calls[0]["kwargs"].get("params") or {})


def _install(
    monkeypatch: pytest.MonkeyPatch, result: base.HttpResult
) -> _FakeTransport:
    """Route ``base.get_json`` through the fake and provide a fake API key."""
    transport = _FakeTransport(result)
    monkeypatch.setattr(aviationstack.base, "get_json", transport)
    monkeypatch.setattr(config, "AVIATIONSTACK_API_KEY", _sentinel_key(), raising=False)
    return transport


def _fetch(
    monkeypatch: pytest.MonkeyPatch,
    result: base.HttpResult,
    *,
    travel_date: datetime.date = FIXTURE_TRAVEL_DATE,
    route: str = "JFK,SFO",
) -> base.NormalizedSignals:
    """Run one fetch through the fake transport and return its envelope."""
    _install(monkeypatch, result)
    origin, destination = route.split(",")
    return asyncio.run(
        aviationstack.fetch_signals(
            base.Location.from_iata(origin, destination), travel_date
        )
    )


def _signals_of_kind(envelope: Dict[str, Any], kind: str) -> List[Dict[str, Any]]:
    """The signals of one kind from an envelope."""
    return [signal for signal in envelope["signals"] if signal["kind"] == kind]


def _signals_for(envelope: Dict[str, Any], flight: str) -> List[Dict[str, Any]]:
    """The signals whose metrics name one flight (``XX100 (Example Airways)``)."""
    return [
        signal
        for signal in envelope["signals"]
        if str(signal["metrics"].get("flight") or "").startswith(flight)
    ]


# --------------------------------------------------------------------------- #
# Severity bands - pinned directly, without any payload in the way
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "delay, expected",
    [
        (0, None),
        (14, None),  # below the reporting floor
        (15, "low"),  # the floor itself is a signal
        (59, "low"),
        (60, "moderate"),
        (179, "moderate"),
        (180, "high"),
        (999, "high"),
        (-25, None),  # early is not a disruption
    ],
)
def test_delay_bands(delay: int, expected: Any) -> None:
    severity, reasons = aviationstack._severity_for(delay, "active")
    assert severity == expected
    assert reasons, "a mapping decision must always state why"


@pytest.mark.parametrize(
    "status, expected",
    [
        ("cancelled", "severe"),
        ("diverted", "high"),
        ("incident", "high"),
        ("active", None),
        ("landed", None),
        ("scheduled", None),
        ("vendor_status_we_do_not_know", None),
        (None, None),
    ],
)
def test_status_bands(status: Any, expected: Any) -> None:
    severity, reasons = aviationstack._severity_for(None, status)
    assert severity == expected
    assert reasons


def test_a_disruptive_status_outranks_a_reported_delay() -> None:
    assert aviationstack._severity_for(5, "cancelled")[0] == "severe"


def test_every_mapped_level_is_in_the_shared_vocabulary() -> None:
    for delay in range(-90, 400, 5):
        for status in (
            None,
            "cancelled",
            "diverted",
            "incident",
            "active",
            "landed",
            "scheduled",
            "vendor_status_we_do_not_know",
        ):
            severity, _ = aviationstack._severity_for(delay, status)
            assert severity is None or severity in config.RISK_LEVELS


def test_the_largest_delay_of_the_two_legs_is_used() -> None:
    """A flight that leaves on time and lands 150 minutes late is 150 minutes late."""
    flight = dict(_fixture("aviationstack_flights_bands.json")["data"][0])
    flight["departure"] = dict(flight["departure"], delay=0)
    flight["arrival"] = dict(flight["arrival"], delay=150)

    assert aviationstack._flight_delay(flight) == (150, "reported")


def test_a_reported_delay_outranks_a_derived_one() -> None:
    """When the vendor states a delay, that number wins over our subtraction."""
    flight = {
        "departure": {
            "scheduled": "2026-10-17T12:00:00+00:00",
            "estimated": "2026-10-17T12:30:00+00:00",
            "delay": 45,
        },
        "arrival": {"scheduled": "2026-10-17T15:30:00+00:00", "delay": None},
    }

    assert aviationstack._flight_delay(flight) == (45, "reported")


def test_actual_timestamps_are_stronger_evidence_than_estimates() -> None:
    flight = {
        "departure": {
            "scheduled": "2026-10-17T12:00:00+00:00",
            "estimated": "2026-10-17T12:40:00+00:00",
            "actual": "2026-10-17T12:50:00+00:00",
        },
        "arrival": {},
    }

    assert aviationstack._flight_delay(flight) == (50, "actual")


# --------------------------------------------------------------------------- #
# A flight list -> normalized signals
# --------------------------------------------------------------------------- #
def test_reported_delays_and_cancellations_become_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = "aviationstack_flights_bands.json"
    raw_path = _fixture_path(fixture)

    envelope = _fetch(monkeypatch, _result(_fixture(fixture), raw_path))

    assert envelope["source"] == config.SOURCE_AVIATIONSTACK
    assert envelope["status"] == config.STATUS_OK
    assert envelope["error"] is None and envelope["error_code"] is None
    assert envelope["travel_date"] == FIXTURE_TRAVEL_DATE.isoformat()
    assert envelope["raw_response_path"] == raw_path
    assert envelope["records"] == len(envelope["signals"]) == 3

    delayed = _signals_for(envelope, "XX100")
    assert len(delayed) == 1
    assert delayed[0]["kind"] == aviationstack._SIGNAL_KIND_DELAY
    assert delayed[0]["severity"] == "low"
    assert delayed[0]["metrics"]["delay_minutes"] == 45
    assert delayed[0]["metrics"]["delay_source"] == "reported"
    assert delayed[0]["metrics"]["departure_iata"] == "JFK"
    assert delayed[0]["metrics"]["arrival_iata"] == "SFO"
    assert delayed[0]["confidence"] == aviationstack._CONFIDENCE_REPORTED
    assert delayed[0]["effective_at"] == "2025-06-12T16:15:00+00:00"

    cancelled = _signals_for(envelope, "XX200")
    assert len(cancelled) == 1
    assert cancelled[0]["kind"] == aviationstack._SIGNAL_KIND_CANCELLATION
    assert cancelled[0]["severity"] == "severe"
    assert cancelled[0]["metrics"]["flight_status"] == "cancelled"
    assert cancelled[0]["metrics"]["cancelled"] is True
    assert cancelled[0]["metrics"]["diverted"] is False
    assert "removes the planned flight" in cancelled[0]["summary"]

    derived = _signals_for(envelope, "XX400")
    assert len(derived) == 1
    assert derived[0]["metrics"]["delay_minutes"] == 95
    assert derived[0]["metrics"]["delay_source"] == "estimated"
    assert derived[0]["severity"] == "moderate"

    # The 12-minute flight is real evidence, but not a signal.
    assert _signals_for(envelope, "XX300") == []
    assert not _signals_of_kind(envelope, aviationstack._SIGNAL_KIND_SUMMARY)


def test_every_signal_cites_evidence_that_resolves_to_a_real_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = "aviationstack_flights_bands.json"
    envelope = _fetch(monkeypatch, _result(_fixture(fixture), _fixture_path(fixture)))

    evidence = {entry["id"]: entry for entry in envelope["evidence"]}
    assert len(evidence) == 4, "one evidence entry per matched flight"

    for signal in envelope["signals"]:
        assert signal["evidence_ids"], "a signal without evidence must not be returned"
        for evidence_id in signal["evidence_ids"]:
            assert evidence_id in evidence
            assert evidence[evidence_id]["raw_response_path"] == _fixture_path(fixture)
            assert evidence[evidence_id]["observed_at"] == signal["observed_at"]


def test_a_below_floor_delay_is_a_checked_result_not_a_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Twelve minutes late is a real observation and not a disruption."""
    document = _fixture("aviationstack_flights_bands.json")
    document["data"] = [
        flight for flight in document["data"] if flight["flight"]["iata"] == "XX300"
    ]
    document["pagination"]["count"] = document["pagination"]["total"] = 1

    envelope = _fetch(
        monkeypatch, _result(document, "logs/raw/aviationstack_TEST_below_floor.json")
    )

    assert envelope["status"] == config.STATUS_OK
    assert len(envelope["signals"]) == 1
    summary = envelope["signals"][0]
    assert summary["kind"] == aviationstack._SIGNAL_KIND_SUMMARY
    assert summary["severity"] == "low"
    assert summary["metrics"]["flights_assessed"] == 1
    assert summary["metrics"]["max_delay_minutes"] == 12
    assert summary["metrics"]["disruptive_flights"] == 0
    assert summary["evidence_ids"] == ["ev_flight_1"]
    assert "not missing data" in summary["summary"]


def test_a_flight_without_status_or_timestamps_is_counted_not_judged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blank record is reported as unmeasured instead of being called fine."""
    document = {
        "pagination": {"limit": 100, "offset": 0, "count": 1, "total": 1},
        "data": [
            {
                "flight_date": FIXTURE_TRAVEL_DATE.isoformat(),
                "flight_status": None,
                "departure": {"iata": "JFK"},
                "arrival": {"iata": "SFO"},
                "airline": {"name": "Example Airways"},
                "flight": {"iata": "XX900"},
            }
        ],
    }

    envelope = _fetch(
        monkeypatch, _result(document, "logs/raw/aviationstack_TEST_unmeasured.json")
    )

    assert envelope["status"] == config.STATUS_OK
    summary = envelope["signals"][0]
    assert summary["metrics"]["unmeasured_flights"] == 1
    assert "nothing is claimed about them" in summary["summary"]
    assert any("not assessed" in warning for warning in envelope["warnings"])


def test_a_pagination_gap_is_reported_rather_than_silently_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _fixture("aviationstack_flights_bands.json")
    document["pagination"]["total"] = 250  # the vendor says there are more pages

    envelope = _fetch(
        monkeypatch, _result(document, "logs/raw/aviationstack_TEST_paged.json")
    )

    assert envelope["status"] == config.STATUS_OK
    assert any("250" in warning for warning in envelope["warnings"])
    assert any("100 monthly requests" in warning for warning in envelope["warnings"])


# --------------------------------------------------------------------------- #
# Failures: plan gaps, no data, bad keys and outages are all different states
# --------------------------------------------------------------------------- #
def test_missing_credential_is_reported_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _FakeTransport(_result(_fixture("aviationstack_flights_empty.json"), "x"))
    monkeypatch.setattr(aviationstack.base, "get_json", transport)
    monkeypatch.setattr(config, "AVIATIONSTACK_API_KEY", None, raising=False)

    envelope = asyncio.run(
        aviationstack.fetch_signals(
            base.Location.from_iata("JFK", "SFO"), FIXTURE_TRAVEL_DATE
        )
    )

    assert envelope["status"] == config.STATUS_ERROR
    assert envelope["error_code"] == base.ErrorCode.MISSING_CREDENTIAL
    assert "AVIATIONSTACK_API_KEY" in (envelope["error"] or "")
    assert envelope["signals"] == []
    assert transport.calls == [], "no request may be sent without a key"


def test_a_plan_restriction_is_unavailable_not_an_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = "aviationstack_403_plan_restricted.json"
    raw_path = _fixture_path(fixture)

    envelope = _fetch(
        monkeypatch,
        _failure(
            _fixture(fixture),
            raw_path,
            status_code=403,
            error_code=base.ErrorCode.FORBIDDEN,
        ),
    )

    assert envelope["status"] == config.STATUS_UNAVAILABLE
    assert envelope["error_code"] == base.ErrorCode.PLAN_LIMIT
    assert envelope["signals"] == []
    assert envelope["raw_response_path"] == raw_path
    assert "function_access_restricted" in (envelope["error"] or "")
    assert "100 requests/month" in (envelope["error"] or "")
    assert "not 'no disruption'" in " ".join(envelope["warnings"])
    assert base.has_usable_signals(envelope) is False


def test_a_plan_message_on_a_401_is_a_plan_limit_not_a_bad_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The key is valid but the function is not included: that is a plan gap."""
    fixture = "aviationstack_403_plan_restricted.json"

    envelope = _fetch(
        monkeypatch,
        _failure(
            _fixture(fixture),
            _fixture_path(fixture),
            status_code=401,
            error_code=base.ErrorCode.UNAUTHORIZED,
        ),
    )

    assert envelope["status"] == config.STATUS_UNAVAILABLE
    assert envelope["error_code"] == base.ErrorCode.PLAN_LIMIT


def test_a_key_error_is_an_error_and_names_the_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real capture of the live API's invalid-key body."""
    fixture = "aviationstack_401_invalid_key.json"

    envelope = _fetch(
        monkeypatch,
        _failure(
            _fixture(fixture),
            _fixture_path(fixture),
            status_code=401,
            error_code=base.ErrorCode.UNAUTHORIZED,
        ),
    )

    assert envelope["status"] == config.STATUS_ERROR
    assert envelope["error_code"] == base.ErrorCode.UNAUTHORIZED
    assert "AVIATIONSTACK_API_KEY" in (envelope["error"] or "")
    assert "invalid_access_key" in (envelope["error"] or "")
    assert envelope["signals"] == []


def test_the_real_missing_key_capture_is_classified_as_a_credential_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = "aviationstack_401_missing_key.json"

    envelope = _fetch(
        monkeypatch,
        _failure(
            _fixture(fixture),
            _fixture_path(fixture),
            status_code=401,
            error_code=base.ErrorCode.UNAUTHORIZED,
        ),
    )

    assert envelope["status"] == config.STATUS_ERROR
    assert envelope["error_code"] == base.ErrorCode.UNAUTHORIZED
    assert "missing_access_key" in (envelope["error"] or "")


def test_a_plan_body_is_read_back_from_the_raw_record_when_payload_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``base`` returns ``payload=None`` for non-2xx, so the raw record is the source.

    Without this read-back a free-tier restriction would look like a plain HTTP 403
    and the plan gap would be indistinguishable from an outage.
    """
    body = json.dumps(_fixture("aviationstack_403_plan_restricted.json"))
    monkeypatch.setattr(
        aviationstack.base,
        "load_raw_response",
        lambda path, **kwargs: {"response": {"body_text": body}},
    )
    result = _failure(
        None,
        "logs/raw/aviationstack_TEST_403.json",
        status_code=403,
        error_code=base.ErrorCode.FORBIDDEN,
    )

    envelope = _fetch(monkeypatch, result)

    assert envelope["status"] == config.STATUS_UNAVAILABLE
    assert envelope["error_code"] == base.ErrorCode.PLAN_LIMIT
    assert "function_access_restricted" in (envelope["error"] or "")


def test_a_403_with_no_readable_body_still_counts_as_a_plan_restriction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Aviationstack uses 403 for entitlement refusals; the gap is still reported."""
    result = _failure(
        None,
        "logs/raw/aviationstack_TEST_403_blind.json",
        status_code=403,
        error_code=base.ErrorCode.FORBIDDEN,
    )

    envelope = _fetch(monkeypatch, result)

    assert envelope["status"] == config.STATUS_UNAVAILABLE
    assert envelope["error_code"] == base.ErrorCode.PLAN_LIMIT
    assert "HTTP 403" in (envelope["error"] or "")


def test_an_empty_result_is_unavailable_and_names_the_free_tier_limitation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = "aviationstack_flights_empty.json"

    envelope = _fetch(monkeypatch, _result(_fixture(fixture), _fixture_path(fixture)))

    assert envelope["status"] == config.STATUS_UNAVAILABLE
    assert envelope["error_code"] == base.ErrorCode.NO_DATA_FOR_QUERY
    assert envelope["signals"] == []
    assert "does not return schedules" in (envelope["error"] or "")
    assert "no schedule was inferred" in (envelope["error"] or "")
    assert envelope["raw_response_path"] == _fixture_path(fixture)


def test_an_empty_result_today_does_not_blame_the_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On today's board an empty answer means "nothing on the board", not "plan gap"."""
    fixture = "aviationstack_flights_empty.json"

    envelope = _fetch(
        monkeypatch,
        _result(_fixture(fixture), _fixture_path(fixture)),
        travel_date=base.utc_now().date(),
    )

    assert envelope["status"] == config.STATUS_UNAVAILABLE
    assert envelope["error_code"] == base.ErrorCode.NO_DATA_FOR_QUERY
    assert "on the board for this route today" in (envelope["error"] or "")
    assert "nothing was inferred" in (envelope["error"] or "")
    assert "does not return schedules" not in (envelope["error"] or "")


def test_flights_for_another_date_are_not_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A record that names a different date is reported, never re-dated to fit."""
    fixture = "aviationstack_flights_bands.json"

    envelope = _fetch(
        monkeypatch,
        _result(_fixture(fixture), _fixture_path(fixture)),
        travel_date=FIXTURE_TRAVEL_DATE + datetime.timedelta(days=1),
    )

    assert envelope["status"] == config.STATUS_UNAVAILABLE
    assert envelope["error_code"] == base.ErrorCode.NO_DATA_FOR_QUERY
    assert "returned 4 flight(s)" in (envelope["error"] or "")
    assert "were not re-dated" in (envelope["error"] or "")
    assert envelope["signals"] == []


def test_flights_for_another_route_are_not_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = "aviationstack_flights_bands.json"

    envelope = _fetch(
        monkeypatch, _result(_fixture(fixture), _fixture_path(fixture)), route="SFO,JFK"
    )

    assert envelope["status"] == config.STATUS_UNAVAILABLE
    assert envelope["error_code"] == base.ErrorCode.NO_DATA_FOR_QUERY
    assert "SFO" in (envelope["error"] or "") and "JFK" in (envelope["error"] or "")


def test_a_body_without_a_data_array_is_a_payload_shape_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = _fetch(
        monkeypatch,
        _result({"pagination": {"total": 0}}, "logs/raw/aviationstack_TEST_no_data.json"),
    )

    assert envelope["status"] == config.STATUS_ERROR
    assert envelope["error_code"] == base.ErrorCode.UNEXPECTED_PAYLOAD_SHAPE
    assert "no 'data' array" in (envelope["error"] or "")
    assert envelope["signals"] == []


def test_a_2xx_body_with_an_error_object_is_never_read_as_flight_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """apilayer can deliver an error body with a 200; it is still an error."""
    fixture = "aviationstack_403_plan_restricted.json"

    envelope = _fetch(
        monkeypatch, _result(_fixture(fixture), _fixture_path(fixture), status_code=200)
    )

    assert envelope["status"] == config.STATUS_UNAVAILABLE
    assert envelope["error_code"] == base.ErrorCode.PLAN_LIMIT
    assert envelope["signals"] == []


def test_rate_limiting_is_an_error_with_the_quota_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "error": {
            "code": "usage_limit_reached",
            "message": "Your monthly usage limit has been reached.",
        }
    }

    envelope = _fetch(
        monkeypatch,
        _failure(
            payload,
            "logs/raw/aviationstack_TEST_429.json",
            status_code=429,
            error_code=base.ErrorCode.RATE_LIMITED,
            attempts=3,
        ),
    )

    assert envelope["status"] == config.STATUS_ERROR
    assert envelope["error_code"] == base.ErrorCode.RATE_LIMITED
    assert "3 attempt(s)" in (envelope["error"] or "")
    assert "100 requests/month" in (envelope["error"] or "")
    assert any("not a missing route" in warning for warning in envelope["warnings"])


def test_an_upstream_outage_is_an_error_with_its_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_path = "logs/raw/aviationstack_TEST_503.json"

    envelope = _fetch(
        monkeypatch,
        _failure(
            None,
            raw_path,
            status_code=503,
            error_code=base.ErrorCode.SERVER_ERROR,
            attempts=2,
        ),
    )

    assert envelope["status"] == config.STATUS_ERROR
    assert envelope["error_code"] == base.ErrorCode.SERVER_ERROR
    assert "HTTP 503" in (envelope["error"] or "")
    assert envelope["raw_response_path"] == raw_path
    assert envelope["raw_response_paths"] == [raw_path]
    assert envelope["signals"] == []


# --------------------------------------------------------------------------- #
# The request itself: correct parameters, no key anywhere it should not be
# --------------------------------------------------------------------------- #
def test_the_request_carries_the_route_limit_and_date_but_no_key_in_the_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = "aviationstack_flights_empty.json"
    transport = _install(monkeypatch, _result(_fixture(fixture), _fixture_path(fixture)))
    key = config.AVIATIONSTACK_API_KEY

    envelope = asyncio.run(
        aviationstack.fetch_signals(
            base.Location.from_iata("JFK", "SFO"), FIXTURE_TRAVEL_DATE
        )
    )

    params = transport.params()
    assert params["access_key"] == key
    assert params["dep_iata"] == "JFK"
    assert params["arr_iata"] == "SFO"
    assert params["limit"] == aviationstack._FREE_TIER_LIMIT
    assert params["flight_date"] == FIXTURE_TRAVEL_DATE.isoformat()

    sent_url = transport.calls[0]["url"]
    assert sent_url == FLIGHTS_URL
    assert "access_key" not in sent_url
    assert transport.calls[0]["source"] == config.SOURCE_AVIATIONSTACK
    assert transport.calls[0]["kwargs"]["context"]["endpoint"] == "flights"

    # The key is never echoed into anything the caller can see or store.
    assert key not in json.dumps(envelope)
    assert envelope["raw_response_path"] == _fixture_path(fixture)


def test_today_does_not_send_the_paid_flight_date_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The paid schedule filter must not block a query the free board can answer."""
    fixture = "aviationstack_flights_empty.json"
    transport = _install(monkeypatch, _result(_fixture(fixture), _fixture_path(fixture)))

    asyncio.run(
        aviationstack.fetch_signals(
            base.Location.from_iata("JFK", "SFO"), base.utc_now().date()
        )
    )

    assert "flight_date" not in transport.params()


def test_programming_errors_raise_instead_of_producing_an_envelope() -> None:
    with pytest.raises(ValueError):
        asyncio.run(
            aviationstack.fetch_signals(
                base.Location.from_iata("JFK", "SFO"),
                "2026-10-17",  # type: ignore[arg-type]
            )
        )
    with pytest.raises(ValueError):
        asyncio.run(
            aviationstack.fetch_signals(
                "JFK-SFO",  # type: ignore[arg-type]
                FIXTURE_TRAVEL_DATE,
            )
        )


def test_the_module_exposes_exactly_one_public_entry_point() -> None:
    """Rule 4: ``fetch_signals`` is the only public callable of a source module."""
    public = {
        name
        for name in dir(aviationstack)
        if not name.startswith("_")
        and callable(getattr(aviationstack, name))
        and getattr(getattr(aviationstack, name), "__module__", None)
        == aviationstack.__name__
    }

    assert public == {"fetch_signals"}
    assert aviationstack.__all__ == ["fetch_signals"]


def test_the_envelope_is_json_serializable(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = "aviationstack_flights_bands.json"

    envelope = _fetch(monkeypatch, _result(_fixture(fixture), _fixture_path(fixture)))

    restored = json.loads(json.dumps(envelope))
    assert restored["status"] == config.STATUS_OK
    assert len(restored["signals"]) == 3
    assert restored["signals"][0]["metrics"]["flight"] == "XX100 (Example Airways)"
    assert restored["signals"][0]["evidence_ids"] == ["ev_flight_1"]



# --------------------------------------------------------------------------- #
# The real capture: what the live free-tier API actually returns on a route/date
#
# Captured 2026-10-17T05:09:56Z for JFK -> LAX without `flight_date` (the only shape a
# free key can serve). It returned 100 of 173 flights and - the surprise - those 100 rows
# mix two travel dates: 91 dated 2026-10-16 (overnight and still-in-progress flights) and
# 9 dated 2026-10-17. The module filters on the travel date, so a "today" assessment is
# built from the 9 rows that actually belong to today.
# --------------------------------------------------------------------------- #
REAL_CAPTURE: str = "aviationstack_flights_JFK_LAX.json"
REAL_CAPTURE_DATE: datetime.date = datetime.date(2026, 10, 17)
REAL_CAPTURE_PREVIOUS_DATE: datetime.date = datetime.date(2026, 10, 16)


def _real_capture_envelope(
    monkeypatch: pytest.MonkeyPatch, travel_date: datetime.date
) -> base.NormalizedSignals:
    """Run the module over the real capture for one travel date."""
    return _fetch(
        monkeypatch,
        _result(_fixture(REAL_CAPTURE), _fixture_path(REAL_CAPTURE)),
        travel_date=travel_date,
        route="JFK,LAX",
    )


def test_the_real_capture_parses_into_signals_for_the_travel_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = _real_capture_envelope(monkeypatch, REAL_CAPTURE_DATE)

    assert envelope["status"] == config.STATUS_OK
    assert envelope["error"] is None and envelope["error_code"] is None
    assert envelope["raw_response_path"] == _fixture_path(REAL_CAPTURE)

    # Nine rows belong to the travel date; eight of them are late enough to be signals.
    assert len(envelope["evidence"]) == 9
    assert len(envelope["signals"]) == 8
    assert {signal["kind"] for signal in envelope["signals"]} == {
        aviationstack._SIGNAL_KIND_DELAY
    }
    assert {signal["severity"] for signal in envelope["signals"]} == {"low"}
    assert all(
        signal["metrics"]["delay_minutes"] >= 15 for signal in envelope["signals"]
    )
    assert all(
        signal["metrics"]["departure_iata"] == "JFK"
        and signal["metrics"]["arrival_iata"] == "LAX"
        for signal in envelope["signals"]
    )

    evidence_ids = {entry["id"] for entry in envelope["evidence"]}
    for signal in envelope["signals"]:
        assert set(signal["evidence_ids"]) <= evidence_ids


def test_the_real_board_mixes_travel_dates_and_only_the_travel_date_is_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real-time board carries yesterday's still-running flights; they are not evidence."""
    document = _fixture(REAL_CAPTURE)
    dates = [flight["flight_date"] for flight in document["data"]]

    assert dates.count("2026-10-17") == 9
    assert dates.count("2026-10-16") == 91

    today = _real_capture_envelope(monkeypatch, REAL_CAPTURE_DATE)
    assert {
        json.loads(entry["value"])["flight_date"] for entry in today["evidence"]
    } == {"2026-10-17"}

    # Asking for the previous date uses the other 91 rows of the very same response.
    previous = _real_capture_envelope(monkeypatch, REAL_CAPTURE_PREVIOUS_DATE)
    assert len(previous["evidence"]) == 91
    assert {
        json.loads(entry["value"])["flight_date"] for entry in previous["evidence"]
    } == {"2026-10-16"}


def test_the_real_capture_reports_the_free_tier_pagination_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """100 rows were returned of 173 reported, and the gap is stated, not hidden."""
    envelope = _real_capture_envelope(monkeypatch, REAL_CAPTURE_DATE)

    gap = [warning for warning in envelope["warnings"] if "173" in warning]
    assert gap, "the real pagination gap must be reported"
    assert "returned 100" in gap[0]
    assert "100 monthly requests" in gap[0]


def test_the_real_capture_never_names_a_flight_that_is_not_in_the_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _fixture(REAL_CAPTURE)
    known = {
        "{0} ({1})".format(flight["flight"]["iata"], flight["airline"]["name"])
        for flight in document["data"]
    }

    envelope = _real_capture_envelope(monkeypatch, REAL_CAPTURE_DATE)

    assert envelope["signals"]
    assert all(signal["metrics"]["flight"] in known for signal in envelope["signals"])


# --------------------------------------------------------------------------- #
# Sample size: the real-time board is a snapshot, not a schedule
# --------------------------------------------------------------------------- #
def test_the_real_nine_flight_sample_reports_what_it_was_drawn_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """9 of 100 received of 173 reported: the cohort is stated on every signal.

    It has to be on the per-flight signals too, not only the summary: when disruptions
    *are* found the summary is never emitted, and fusion would otherwise see eight delays
    with no idea they came from nine flights on a 173-flight board.
    """
    envelope = _real_capture_envelope(monkeypatch, REAL_CAPTURE_DATE)

    assert len(envelope["signals"]) == 8
    for signal in envelope["signals"]:
        metrics = signal["metrics"]
        assert metrics["sample_size"] == 9
        assert metrics["returned_records"] == 100
        assert metrics["total_available"] == 173
        assert metrics["page_coverage"] == 0.578
        assert metrics["sample_is_thin"] is True


def _summary_for_sample(
    *,
    flights: int,
    returned_records: int,
    total_available: Any,
) -> base.SignalEntry:
    """A summary signal as the module builds it for one described sample."""
    return aviationstack._summary_signal(
        location=base.Location.from_iata("JFK", "LAX"),
        travel_date=REAL_CAPTURE_DATE,
        observed_at="2026-10-17T05:09:56Z",
        flights=[{} for _ in range(flights)],
        delays=[None] * flights,
        evidence_ids=[],
        sample=aviationstack._sample_metrics(
            flights,
            returned_records=returned_records,
            total_available=total_available,
        ),
    )


def test_a_thin_sample_is_weaker_evidence_than_a_complete_one() -> None:
    """The whole point of ``sample_size``: 9 of 173 must not read like 100 of 100."""
    thin = _summary_for_sample(flights=9, returned_records=100, total_available=173)
    full = _summary_for_sample(flights=100, returned_records=100, total_available=100)

    assert thin["confidence"] < full["confidence"]
    assert full["confidence"] == aviationstack._CONFIDENCE_SUMMARY
    assert 0.0 < thin["confidence"] < aviationstack._CONFIDENCE_SUMMARY
    # Both penalties are stated in the sentence, not only encoded in the number.
    assert "thin sample" in thin["summary"]
    assert "173" in thin["summary"] and "100 were received" in thin["summary"]
    assert "not missing data" in thin["summary"]


def test_an_unknown_total_is_not_discounted_as_if_it_were_a_gap() -> None:
    """No published total means unknown coverage: no coverage penalty is invented."""
    unknown = _summary_for_sample(flights=25, returned_records=25, total_available=None)

    assert unknown["metrics"]["page_coverage"] is None
    assert unknown["metrics"]["total_available"] is None
    assert unknown["metrics"]["sample_is_thin"] is False
    assert unknown["confidence"] == aviationstack._CONFIDENCE_SUMMARY
    assert "thin sample" not in unknown["summary"]

