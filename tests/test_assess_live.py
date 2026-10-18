"""Live end-to-end test for the orchestrator (``backend/assess.py``).

This is the demo path with nothing substituted: real Aviationstack, NWS and TomTom
fetches for JFK -> LAX today, one real fusion call, one persisted record read back
through ``load_assessment`` (what ``GET /evidence/{id}`` serves).

What it asserts is the *contract*, never the verdict: a valid assessment id, a legal
risk level, per-source statuses that match reality, every citation resolving to a real
input evidence entry, and a stored record holding the exact prompt, the exact answer and
the provider's token usage. It says nothing about today's traffic or weather, so it does
not rot as conditions change.

It costs quota (one Aviationstack request, two TomTom requests, six NWS requests, one
model call), so it is gated and skipped -- never failed -- when it cannot run:

* no outbound network access;
* no configured fusion model/key (``config.fusion_readiness()``).

A provider outage, quota exhaustion or a missing source credential is reported as a
skip: those are upstream conditions, and the assessment itself says which source was
missing. Bad output from our own code fails.
"""

from __future__ import annotations

import asyncio
import json
import socket
import uuid
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlsplit

import pytest

import config
from backend import assess as orchestrator
from backend import fusion
from data_sources import base

LOCATION = base.Location.from_iata("JFK", "LAX")
TODAY = base.utc_now().date()

pytestmark = pytest.mark.live


def _endpoint() -> Any:
    """The configured endpoint, parsed (never the key, never logged)."""
    return urlsplit(config.FUSION_MODEL_BASE_URL or "")


def _network_available() -> bool:
    """Whether a TCP connection to the fusion endpoint's host can be established."""
    parsed = _endpoint()
    if not parsed.hostname:
        return False
    try:
        with socket.create_connection(
            (parsed.hostname, parsed.port or 443), timeout=5.0
        ):
            return True
    except OSError:
        return False


NETWORK_AVAILABLE: bool = _network_available()
READINESS: Dict[str, Any] = config.fusion_readiness()

skip_without_network = pytest.mark.skipif(
    not NETWORK_AVAILABLE,
    reason=(
        "no outbound network access to {0}: the live assess test cannot run".format(
            _endpoint().hostname or "the fusion endpoint"
        )
    ),
)
skip_without_model = pytest.mark.skipif(
    not READINESS["ready"],
    reason="fusion model not configured: {0}".format(
        "; ".join(str(reason) for reason in READINESS["reasons"] or ())
        or "unknown reason"
    ),
)

# Answers that mean "a provider, the quota or a key is the problem", not our code.
UPSTREAM_MARKERS = (
    "could not be reached",
    "HTTP 4",
    "HTTP 5",
    "401",
    "403",
    "429",
    "rate_limit",
    "plan limit",
    "missing_credential",
)


def _skip_if_upstream(assessment: Any, record: Dict[str, Any]) -> None:
    """Skip when the pipeline produced no verdict for an *upstream* reason.

    Two shapes count: a fully ``error`` assessment, and a ``partial`` one where the
    sources worked but fusion did not (the orchestrator downgrades that to ``partial``
    on purpose, so ``status`` alone is not enough).
    """
    no_verdict = assessment.status == config.STATUS_ERROR or (
        assessment.status == config.STATUS_PARTIAL
        and assessment.risk_level in (None, "unknown")
    )
    if not no_verdict:
        return

    problems = " ".join(assessment.uncertainty)
    for envelope in record.get("sources", {}).values():
        problems += " {0} {1}".format(
            envelope.get("error_code") or "", envelope.get("error") or ""
        )
    response = record.get("fusion_response") or {}
    status_code = response.get("status_code")
    if isinstance(status_code, int) and status_code >= 400:
        problems += " fusion HTTP {0}".format(status_code)
    if any(marker in problems for marker in UPSTREAM_MARKERS):
        pytest.skip(
            "provider, quota or credential problem, not a module defect: {0}".format(
                base.truncate_text(problems, 500)
            )
        )
    pytest.fail(
        "assess produced no verdict for reasons inside the pipeline: {0}".format(
            base.truncate_text(problems, 800)
        )
    )


@skip_without_network
@skip_without_model
def test_live_assessment_is_persisted_and_served_from_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Real fetches + a real model call, then read the record back like /evidence does."""
    monkeypatch.setattr(config, "RAW_RESPONSE_DIR", tmp_path / "raw")
    monkeypatch.setattr(config, "ASSESSMENT_DIR", tmp_path / "assessments")
    monkeypatch.setattr(config, "LOG_RAW_RESPONSES", True)

    assessment = asyncio.run(orchestrator.assess(LOCATION, TODAY))

    uuid.UUID(assessment.assessment_id)
    record = orchestrator.load_assessment(assessment.assessment_id)
    _skip_if_upstream(assessment, record)

    assert assessment.status in (config.STATUS_OK, config.STATUS_PARTIAL)
    assert assessment.origin == "JFK" and assessment.destination == "LAX"
    assert assessment.travel_date == TODAY.isoformat()
    assert assessment.risk_level in config.RISK_LEVELS
    assert assessment.summary and assessment.summary.strip()

    # Every source is reported with the status it really had, and a degraded one says why.
    reported = {source.source: source for source in assessment.sources}
    assert set(reported) == {name for name, _ in orchestrator._source_modules()}
    for source in reported.values():
        if source.status != config.STATUS_OK:
            assert source.error, "{0} is not ok but gives no reason".format(
                source.source
            )
    if any(source.status != config.STATUS_OK for source in reported.values()):
        assert assessment.status == config.STATUS_PARTIAL

    # Evidence integrity: every citation resolves, in the source it names.
    envelopes: List[base.NormalizedSignals] = [
        record["sources"][name] for name in record["sources"]
    ]
    index = fusion._evidence_index(envelopes)
    for risk in assessment.risks:
        assert risk.severity in config.RISK_LEVELS
        assert risk.evidence, "a live risk must cite something"
        for reference in risk.evidence:
            assert reference.evidence_id in index, reference.evidence_id
            assert reference.source == index[reference.evidence_id]

    # The record holds the reasoning and the cost, not a summary of them.
    assert record["assessment"]["assessment_id"] == assessment.assessment_id
    messages = record["fusion_prompt"]["messages"]
    assert [message["role"] for message in messages] == ["system", "user"]
    assert "ROUTE: JFK -> LAX" in messages[1]["content"]
    assert record["fusion_response"]["status_code"] == 200
    body_text = record["fusion_response"]["body_text"]
    assert isinstance(body_text, str) and body_text.strip()
    # The served copy may be truncated for display; the file on disk never is.
    assert record["fusion_response_body_truncated"] in (True, False)
    stored = json.loads(
        (Path(config.ASSESSMENT_DIR) / "{0}.json".format(assessment.assessment_id))
        .read_text(encoding="utf-8")
    )
    full_body = json.loads(stored["fusion_response"]["body_text"])
    assert full_body["choices"][0]["message"]["content"]
    if not record["fusion_response_body_truncated"]:
        assert body_text == stored["fusion_response"]["body_text"]

    metadata = record["fusion_metadata"]
    assert metadata["model"] == config.FUSION_MODEL_NAME
    assert metadata["usage_reported"] is True
    assert isinstance(metadata["prompt_tokens"], int) and metadata["prompt_tokens"] > 0
    assert isinstance(metadata["total_tokens"], int) and metadata["total_tokens"] > 0
    assert record["latency"]["total_ms"] > 0
    assert record["latency"]["fusion_ms"] > 0
    assert set(record["latency"]["sources"]) == set(record["sources"])

    # No credential may appear anywhere in the stored record (rule 7).
    stored_text = json.dumps(record)
    for secret in (
        config.FUSION_MODEL_API_KEY,
        config.AVIATIONSTACK_API_KEY,
        config.TOMTOM_API_KEY,
    ):
        if secret:
            assert secret not in stored_text

    print(
        "\nlive assess {0} {1} -> {2} [{3}]\n"
        "risk={4} confidence={5} sources={6}\n"
        "tokens: prompt={7} completion={8} reasoning={9} total={10}\n"
        "latency: total={11}ms fusion={12}ms".format(
            assessment.assessment_id,
            assessment.origin,
            assessment.destination,
            assessment.status,
            assessment.risk_level,
            assessment.confidence,
            ", ".join(
                "{0}:{1}".format(item.source, item.status)
                for item in assessment.sources
            ),
            metadata["prompt_tokens"],
            metadata["completion_tokens"],
            metadata["reasoning_tokens"],
            metadata["total_tokens"],
            record["latency"]["total_ms"],
            record["latency"]["fusion_ms"],
        )
    )
    for risk in assessment.risks:
        print("  RISK [{0}] {1}".format(risk.severity, risk.risk))
    for action in assessment.recommended_actions:
        print("  ACTION {0}".format(action))
