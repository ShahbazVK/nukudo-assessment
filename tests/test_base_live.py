"""Live contract tests for the shared HTTP wrapper in ``data_sources.base``.

These tests call real, keyless public APIs. Nothing here is mocked or fabricated
(.clinerules rule 3, extended to tests): every assertion is about the *wrapper's*
behaviour - timeout handling, error-state mapping, and the raw evidence record it
writes - never about the weather values themselves.

The module is skipped automatically when the host has no outbound network access.
"""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path

import pytest

import config
from data_sources import base

NWS_POINTS_URL = "https://api.weather.gov/points/{0},{1}"


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


@skip_without_network
def test_live_success_returns_payload_with_traceable_raw_evidence() -> None:
    """A real NWS response is parsed, and its evidence record is verifiable."""
    result = asyncio.run(
        base.get_json(
            config.SOURCE_NWS,
            NWS_POINTS_URL.format(40.6413, -73.7781),
            headers={"User-Agent": config.NWS_USER_AGENT},
            timeout=10.0,
            max_retries=0,
            expect_keys=["properties"],
        )
    )

    assert result.ok is True, result.error
    assert result.status_code == 200
    assert isinstance(result.payload, dict)
    assert "properties" in result.payload
    assert result.attempts == 1
    assert result.raw_path is not None

    record_path = Path(config.PROJECT_ROOT) / result.raw_path
    assert record_path.is_file()
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["raw_logged_before_parsing"] is True
    assert record["source"] == config.SOURCE_NWS
    assert record["response"]["status_code"] == 200

    # The stored body is verbatim: it re-parses to exactly the payload the wrapper
    # returned, and its recorded hash matches, so the evidence is verifiable.
    stored_body = record["response"]["body_text"]
    assert json.loads(stored_body) == result.payload
    assert record["response"]["body_sha256"] == base.sha256_text(stored_body)

    reloaded = base.load_raw_response(result.raw_path)
    assert reloaded["response"]["status_code"] == 200
    assert reloaded["body_truncated"] in (True, False)


@skip_without_network
def test_live_non_2xx_becomes_an_explicit_error_state() -> None:
    """A real 4xx is an error state: no exception, no payload, evidence kept."""
    result = asyncio.run(
        base.get_json(
            config.SOURCE_NWS,
            NWS_POINTS_URL.format(999, 999),
            headers={"User-Agent": config.NWS_USER_AGENT},
            timeout=10.0,
            max_retries=0,
        )
    )

    assert result.ok is False
    assert result.payload is None
    assert result.status_code is not None and result.status_code >= 400
    assert result.error_code is not None
    assert result.error_code == base.error_code_for_status(result.status_code)
    assert result.error and str(result.status_code) in result.error
    assert result.raw_path is not None  # failures are evidence too
    assert result.failure_status in (config.STATUS_ERROR, config.STATUS_UNAVAILABLE)


@skip_without_network
def test_live_unreachable_host_returns_error_state_and_logs_every_attempt() -> None:
    """A dead endpoint yields a timeout/network code, never an exception."""
    result = asyncio.run(
        base.get_json(
            config.SOURCE_NWS,
            "http://10.255.255.1/points",
            timeout=0.75,
            max_retries=1,
        )
    )

    assert result.ok is False
    assert result.error_code in (base.ErrorCode.TIMEOUT, base.ErrorCode.NETWORK_ERROR)
    assert result.attempts == 2  # one retry was permitted
    assert len(result.raw_paths) == 2  # every attempt leaves its own evidence record
    assert result.raw_path == result.raw_paths[-1]
    assert result.failure_status == config.STATUS_ERROR
