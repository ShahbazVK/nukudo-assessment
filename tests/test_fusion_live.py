"""Live end-to-end test for the fusion layer (``backend.fusion``).

Real envelopes in, a real model call, a validated ``AssessmentResponse`` out. The
envelopes come from ``captured_envelopes`` (see ``tests/conftest.py``): verbatim vendor
payloads replayed through the **real** fetchers, so this exercises the whole path --
prompt construction, the chat-completions call, evidence logging, strict parsing,
Pydantic validation, the semantic checks and the final merge.

It costs tokens, so it is gated and skipped (never failed) when it cannot run:

* no outbound network access to the configured endpoint;
* no configured model or key (``config.fusion_readiness()``), the normal state of a
  fresh checkout.

What it asserts is the *contract*, never the verdict: a legal risk level, a confidence
inside [0, 1] whose derived label agrees with it, every cited evidence id resolving to
a real input entry from the source it names, per-source statuses reflected, and a stored
record holding the exact prompt and the exact answer. It asserts nothing about the
traffic or weather, so it does not rot as conditions change.

An upstream outage (unreachable endpoint, 401/403/429/5xx) is reported as a skip: it is
the provider's problem. Bad output -- unparseable, schema-invalid, unsupported citation
-- is our problem and fails.
"""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlsplit

import pytest

import config
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
        "no outbound network access to {0}: the live fusion test cannot run".format(
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

# Answers that mean "the provider or the key is the problem", not "our code is broken".
UPSTREAM_MARKERS = ("could not be reached", "HTTP 4", "HTTP 5", "401", "403", "429")


def _skip_if_upstream(assessment: Any) -> None:
    """Turn a provider outage into a skip; a defect of ours still fails."""
    if assessment.status != config.STATUS_ERROR:
        return
    problems = " ".join(assessment.uncertainty)
    if any(marker in problems for marker in UPSTREAM_MARKERS):
        pytest.skip(
            "fusion endpoint, quota or key problem, not a module defect: {0}".format(
                base.truncate_text(problems, 400)
            )
        )
    pytest.fail(
        "fusion returned an error state for reasons inside the module: {0}".format(
            base.truncate_text(problems, 600)
        )
    )


@skip_without_network
@skip_without_model
def test_live_fusion_produces_a_validated_traceable_assessment(
    captured_envelopes: Dict[str, base.NormalizedSignals],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """One real call over real signals: legal shape, real citations, stored evidence."""
    evidence_dir = tmp_path / "raw"
    monkeypatch.setattr(config, "RAW_RESPONSE_DIR", evidence_dir)
    monkeypatch.setattr(config, "LOG_RAW_RESPONSES", True)

    envelopes: List[base.NormalizedSignals] = list(captured_envelopes.values())
    assessment = asyncio.run(fusion.fuse(envelopes, LOCATION, TODAY))

    _skip_if_upstream(assessment)

    # Shape: the pipeline-owned fields describe the query we actually asked about.
    assert assessment.status in (config.STATUS_OK, config.STATUS_PARTIAL)
    assert assessment.origin == "JFK" and assessment.destination == "LAX"
    assert assessment.travel_date == TODAY.isoformat()
    assert assessment.schema_version == config.SCHEMA_VERSION
    assert assessment.generated_at and assessment.generated_at.endswith("Z")

    # The verdict is inside our vocabulary, and the label agrees with the number.
    assert assessment.risk_level in config.RISK_LEVELS
    assert assessment.confidence is not None
    assert 0.0 <= assessment.confidence <= 1.0
    assert assessment.confidence_label == fusion._confidence_label(assessment.confidence)
    assert assessment.summary and assessment.summary.strip()

    # Every source handed in is reported, with the status it really had.
    reported = {source.source: source.status for source in assessment.sources}
    assert reported == {
        envelope["source"]: envelope["status"] for envelope in envelopes
    }
    if any(status != config.STATUS_OK for status in reported.values()):
        assert assessment.status == config.STATUS_PARTIAL

    # Evidence integrity: every citation resolves, in the source it names.
    index = fusion._evidence_index(envelopes)
    for risk in assessment.risks:
        assert risk.severity in config.RISK_LEVELS
        assert risk.evidence, "a live risk must cite something"
        for reference in risk.evidence:
            assert reference.evidence_id in index, reference.evidence_id
            assert reference.source == index[reference.evidence_id]

    # Anything withheld from the prompt for size is disclosed in the answer itself.
    for envelope in envelopes:
        withheld = max(
            0, len(envelope["signals"]) - config.FUSION_MAX_SIGNALS_PER_SOURCE
        )
        if withheld:
            source = str(envelope["source"])
            assert any(
                source in note and "withheld for prompt size" in note
                for note in assessment.uncertainty
            ), "no truncation note for {0}".format(source)

    # The call is on disk: exact prompt in, exact body out, no credential anywhere.
    records = sorted(evidence_dir.glob("*.json"))
    assert records, "a live fusion call must be recorded before it is parsed"
    record_text = records[0].read_text(encoding="utf-8")
    stored = json.loads(record_text)
    assert stored["raw_logged_before_parsing"] is True
    assert stored["request"]["messages"][0]["role"] == "system"
    assert "ROUTE: JFK -> LAX" in stored["request"]["messages"][1]["content"]
    assert stored["response"] and stored["response"]["status_code"] == 200
    assert config.FUSION_MODEL_API_KEY not in record_text
    assert "Bearer <redacted>" in json.dumps(stored["request"]["headers"])
