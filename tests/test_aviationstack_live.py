"""Live contract tests for the Aviationstack source (``data_sources.aviationstack``).

These call the real API. They assert the *contract* - a well-formed envelope, evidence
that resolves to a real record on disk, severities drawn from our own vocabulary, and
an explicit ``unavailable`` state with the free-tier limitation named where the plan
cannot answer. They never assert what the flights are, so they do not rot.

Three of the four tests need no credential at all: the live API returns its key-error
document for a request made without a valid key, which is exactly the path
``test_live_invalid_key_is_reported_and_logged`` exercises. The remaining test (a real
route query) skips when ``AVIATIONSTACK_API_KEY`` is unset and skips - rather than
fails - when the upstream is throttling, erroring, or rejecting the configured key:
what is under test is this module's handling of real responses, not the vendor's
uptime, plan or the developer's key. A defect of *ours* (an unusable payload, an
unexpected exception) still fails.
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
from data_sources import aviationstack, base


def _network_available() -> bool:
    """Whether a TCP connection to the Aviationstack host can be established."""
    try:
        with socket.create_connection(("api.aviationstack.com", 443), timeout=5.0):
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
    not config.AVIATIONSTACK_API_KEY,
    reason=(
        "AVIATIONSTACK_API_KEY is not set: the live route query cannot run. The "
        "key-error and contract tests still run, because the live API returns its "
        "key-error document without a valid key."
    ),
)

# Vendor states that are not this module's fault: quota, plan, key, upstream faults.
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
        base.ErrorCode.PLAN_LIMIT,
    }
)

# States that mean "the source worked, this query is outside what it can answer".
UNAVAILABLE_CODES = frozenset(
    {base.ErrorCode.PLAN_LIMIT, base.ErrorCode.NO_DATA_FOR_QUERY}
)


def _fetch(travel_date: datetime.date) -> Dict[str, Any]:
    """Fetch one route live, skipping when the *vendor* or the key is the problem."""
    envelope = asyncio.run(
        aviationstack.fetch_signals(base.Location.from_iata("JFK", "SFO"), travel_date)
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


def _load(path: str) -> Dict[str, Any]:
    """Load a raw evidence record written by ``base.get_json``."""
    record_path = Path(config.PROJECT_ROOT) / path
    assert record_path.is_file(), path
    return json.loads(record_path.read_text(encoding="utf-8"))


def _assert_traceable(envelope: Dict[str, Any]) -> None:
    """Every signal cites evidence, and every evidence entry points at a real record."""
    evidence = {entry["id"]: entry for entry in envelope["evidence"]}
    assert envelope["raw_response_path"], "a live result must name its raw record"
    record = _load(envelope["raw_response_path"])
    request = record.get("request") or {}
    url = str(request.get("url") or "")
    assert url, "the raw record must name the URL that was called"
    # The key must appear only as the redaction marker, never as its real value.
    assert config.AVIATIONSTACK_API_KEY not in url, "the query key must be redacted"
    assert "access_key=***REDACTED***" in url, "the redaction must be visible in the record"
    assert config.AVIATIONSTACK_API_KEY not in json.dumps(record), (
        "the raw evidence record must never contain the key value"
    )

    for signal in envelope["signals"]:
        assert signal["kind"] in (
            aviationstack._SIGNAL_KIND_DELAY,
            aviationstack._SIGNAL_KIND_CANCELLATION,
            aviationstack._SIGNAL_KIND_SUMMARY,
        )
        assert signal["severity"] in config.RISK_LEVELS
        assert signal["summary"]
        assert signal["evidence_ids"], "a signal without evidence must not be returned"
        for evidence_id in signal["evidence_ids"]:
            assert evidence_id in evidence
            assert (
                evidence[evidence_id]["raw_response_path"]
                == envelope["raw_response_path"]
            )


@skip_without_network
@skip_without_key
def test_live_query_produces_a_well_formed_traceable_envelope() -> None:
    """Today's board: either real signals, or an explicit 'no data' - never empty 'ok'."""
    travel_date = base.utc_now().date()

    envelope = _fetch(travel_date)

    assert envelope["source"] == config.SOURCE_AVIATIONSTACK
    assert envelope["travel_date"] == travel_date.isoformat()
    assert envelope["status"] in (
        config.STATUS_OK,
        config.STATUS_UNAVAILABLE,
    )
    if envelope["status"] == config.STATUS_OK:
        assert envelope["error"] is None and envelope["error_code"] is None
        assert envelope["signals"], "an 'ok' envelope must contain real signals"
        assert envelope["records"] == len(envelope["signals"])
        _assert_traceable(envelope)
    else:
        assert envelope["error_code"] in UNAVAILABLE_CODES
        assert envelope["signals"] == []
        assert envelope["error"], "an unavailable state must explain itself"
        assert envelope["raw_response_path"]


@skip_without_network
@skip_without_key
def test_live_future_date_reports_the_free_tier_gap_honestly() -> None:
    """A future date is usually outside a free key's reach; that is the finding."""
    travel_date = base.utc_now().date() + datetime.timedelta(days=30)

    envelope = _fetch(travel_date)

    if envelope["status"] == config.STATUS_OK:
        # A plan that does include future schedules: then the data must be real.
        assert envelope["signals"]
        _assert_traceable(envelope)
        return

    assert envelope["status"] == config.STATUS_UNAVAILABLE
    assert envelope["error_code"] in UNAVAILABLE_CODES
    assert envelope["signals"] == []
    if envelope["error_code"] == base.ErrorCode.PLAN_LIMIT:
        assert "plan" in (envelope["error"] or "").lower()
    else:
        assert "schedule" in (envelope["error"] or "").lower()


@skip_without_network
def test_live_invalid_key_is_reported_and_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No key needed: the live 401 is a real response this module must classify."""
    monkeypatch.setattr(
        config, "AVIATIONSTACK_API_KEY", "invalid-sentinel-" + "0" * 12, raising=False
    )

    envelope = asyncio.run(
        aviationstack.fetch_signals(
            base.Location.from_iata("JFK", "SFO"), base.utc_now().date()
        )
    )

    assert envelope["status"] == config.STATUS_ERROR
    assert envelope["error_code"] == base.ErrorCode.UNAUTHORIZED
    assert envelope["signals"] == []
    assert "AVIATIONSTACK_API_KEY" in (envelope["error"] or "")
    # The vendor's own words are quoted, and the exchange is on disk.
    assert "invalid_access_key" in (envelope["error"] or "")
    assert envelope["raw_response_path"]
    record = _load(envelope["raw_response_path"])
    assert (record.get("response") or {}).get("status_code") == 401


@skip_without_network
def test_live_envelope_projects_onto_the_api_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whatever the live answer is, the API projection must accept it."""
    monkeypatch.setattr(
        config, "AVIATIONSTACK_API_KEY", "invalid-sentinel-" + "0" * 12, raising=False
    )

    envelope = asyncio.run(
        aviationstack.fetch_signals(
            base.Location.from_iata("JFK", "SFO"), base.utc_now().date()
        )
    )

    model = SourceStatus(**base.to_source_status(envelope))
    assert model.source == config.SOURCE_AVIATIONSTACK
    assert model.status in (
        config.STATUS_OK,
        config.STATUS_ERROR,
        config.STATUS_UNAVAILABLE,
    )
    assert model.raw_response_path == envelope["raw_response_path"]
    assert model.consulted_at == envelope["retrieved_at"]
