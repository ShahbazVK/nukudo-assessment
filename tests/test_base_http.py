"""HTTP-wrapper and evidence-reader contract tests for ``data_sources.base``.

Pure and local: the arguments-validation tests never open a socket, and the two
failure-path tests point at an unroutable address (plus a deliberately unusable
evidence directory) so they behave identically online and offline. Real HTTP
behaviour, including a real evidence record, is covered by ``test_base_live.py``.
"""

from __future__ import annotations

import asyncio
import datetime
from pathlib import Path

import pytest

import config
from data_sources import base

UNROUTABLE_URL = "http://10.255.255.1/points"


# --------------------------------------------------------------------------- #
# Argument validation - no unbounded request is possible
# --------------------------------------------------------------------------- #
def test_get_json_rejects_an_empty_source() -> None:
    with pytest.raises(ValueError):
        asyncio.run(base.get_json("   ", "https://api.example.test/x"))


def test_get_json_rejects_a_non_positive_timeout() -> None:
    with pytest.raises(ValueError):
        asyncio.run(
            base.get_json(config.SOURCE_NWS, "https://api.example.test/x", timeout=0)
        )


def test_get_json_rejects_a_negative_retry_budget() -> None:
    with pytest.raises(ValueError):
        asyncio.run(
            base.get_json(
                config.SOURCE_NWS, "https://api.example.test/x", max_retries=-1
            )
        )


# --------------------------------------------------------------------------- #
# Rule 5: raw logging is mandatory, and its failure is never hidden
# --------------------------------------------------------------------------- #
def test_raw_logging_disabled_is_a_failure_not_a_silent_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "LOG_RAW_RESPONSES", False, raising=False)

    with pytest.raises(base._RawLoggingError) as excinfo:
        base._write_raw_record(
            source=config.SOURCE_NWS, record={"probe": "raw logging disabled"}
        )

    assert excinfo.value.error_code == base.ErrorCode.LOGGING_DISABLED


def test_disabled_raw_logging_turns_a_call_into_an_error_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With raw logging off, no result can be presented as traceable."""
    monkeypatch.setattr(config, "LOG_RAW_RESPONSES", False, raising=False)

    result = asyncio.run(
        base.get_json(config.SOURCE_NWS, UNROUTABLE_URL, timeout=0.2, max_retries=0)
    )

    assert result.ok is False
    assert result.error_code == base.ErrorCode.LOGGING_DISABLED
    assert result.payload is None
    assert result.raw_path is None
    assert result.failure_status == config.STATUS_ERROR


def test_unwritable_evidence_directory_turns_a_call_into_an_error_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("this path cannot hold evidence records", encoding="utf-8")
    monkeypatch.setattr(config, "RAW_RESPONSE_DIR", blocked, raising=False)

    result = asyncio.run(
        base.get_json(config.SOURCE_NWS, UNROUTABLE_URL, timeout=0.2, max_retries=0)
    )

    assert result.ok is False
    assert result.error_code == base.ErrorCode.LOGGING_FAILED
    assert result.raw_path is None
    assert result.payload is None


# --------------------------------------------------------------------------- #
# Evidence reader guards
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw_path",
    [
        "   ",
        "/etc/passwd",
        "../../etc/passwd",
        "logs/raw/../../config.py",
    ],
)
def test_load_raw_response_refuses_paths_outside_the_evidence_dir(
    raw_path: str,
) -> None:
    with pytest.raises(ValueError):
        base.load_raw_response(raw_path)


def test_load_raw_response_reports_a_missing_record() -> None:
    missing = str(Path(config.RAW_RESPONSE_DIR) / "definitely_not_written_1234.json")

    with pytest.raises(FileNotFoundError):
        base.load_raw_response(missing)


def test_raw_evidence_dir_is_the_configured_directory() -> None:
    assert base.raw_evidence_dir() == Path(config.RAW_RESPONSE_DIR)


# --------------------------------------------------------------------------- #
# Text and time helpers
# --------------------------------------------------------------------------- #
def test_truncate_text_marks_removed_characters() -> None:
    assert base.truncate_text("abc", 10) == "abc"

    truncated = base.truncate_text("a" * 20, 5)
    assert truncated.startswith("aaaaa")
    assert "truncated" in truncated


def test_sha256_text_matches_the_published_test_vector() -> None:
    # The published SHA-256 digest of "abc"; proves this is a real SHA-256.
    assert base.sha256_text("abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_date_and_timestamp_formats_are_iso_utc() -> None:
    assert base.iso_date(datetime.date(2026, 11, 3)) == "2026-11-03"
    assert base.iso_date(None) is None

    stamp = base.utc_now_iso()
    assert stamp.endswith("Z")
    assert "T" in stamp
    assert len(stamp) == 24  # YYYY-MM-DDTHH:MM:SS.mmmZ
