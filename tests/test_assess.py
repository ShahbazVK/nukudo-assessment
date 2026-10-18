"""Unit tests for the orchestrator (``backend/assess.py``).

What is real here
-----------------
The envelopes come from ``captured_envelopes`` (see ``tests/conftest.py``): vendor
payloads captured live and replayed through the **real** ``fetch_signals``
implementations, so every signal, metric and evidence id in these tests was produced
by our own parsing code from a payload a vendor actually served. Nothing here
hand-writes an envelope.

Three things are substituted, and each is a seam rather than a fact:

* the three ``fetch_signals`` entry points -- replaced with coroutines returning
  those real envelopes, so failure isolation and concurrency can be exercised. One
  test does patch a source's return value with a non-envelope; that is deliberately a
  bug injection, and the assertion is that it is *reported*, not accepted.
* ``fusion._send_chat_completion`` -- the single socket. Prompt construction, evidence
  logging before parsing, validation, the status reconciliation and persistence all
  run for real, and the scripted reply is a provider-shaped body.
* ``config`` paths -- so no test writes into the repo's ``logs/`` directory.

The model's own output is canned for the same reason as in ``tests/test_fusion.py``:
these tests check how the pipeline treats an answer, not what the model happens to
say about today's weather.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import pytest

import config
from backend import assess as orchestrator
from backend import fusion
from backend.models import AssessmentResponse
from data_sources import base

LOCATION = base.Location.from_iata("JFK", "LAX")
TODAY = datetime.date.today()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _model_body(content: str) -> Dict[str, Any]:
    """A provider-shaped chat-completions body, usage block included.

    ``completion_tokens_details.reasoning_tokens`` is how Groq reports the reasoning
    tokens that also count against its per-minute budget, so the record must carry it.
    """
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": "test-model-1",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 3210,
            "completion_tokens": 512,
            "total_tokens": 3722,
            "completion_tokens_details": {"reasoning_tokens": 288},
        },
    }


def _answer(envelope: Mapping[str, Any]) -> Dict[str, Any]:
    """A complete model answer citing one real evidence entry from ``envelope``."""
    entries = list(envelope.get("evidence") or ())
    assert entries, "the replayed envelope should carry evidence"
    first = entries[0]
    return {
        "risk_level": "moderate",
        "confidence": 0.55,
        "summary": "One cited signal was weighed against this route and date.",
        "risks": [
            {
                "risk": "A cited signal may affect the airport approach",
                "severity": "moderate",
                "confidence": 0.5,
                "evidence": [
                    {
                        "source": first["source"],
                        "evidence_id": first["id"],
                        "field": first.get("field"),
                        # Native scalar on purpose: models really emit this, and the
                        # contract stringifies it (pinned in tests/test_models.py).
                        "value": 0.96,
                    }
                ],
            }
        ],
        "recommended_actions": ["Leave extra time for the airport transfer."],
        "uncertainty": ["Only the cited signal was available in this test."],
    }


class _Model:
    """The socket seam: replay scripted provider bodies and count the calls."""

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.calls = 0

    async def __call__(
        self,
        payload: Mapping[str, Any],
        *,
        url: str,
        api_key: str,
        timeout: float,
    ) -> fusion._Attempt:
        self.calls += 1
        reply = self.replies[min(self.calls - 1, len(self.replies) - 1)]
        if isinstance(reply, fusion._Attempt):
            return reply
        if isinstance(reply, str):
            return fusion._Attempt(200, reply, 9.0, None, None)
        return fusion._Attempt(200, json.dumps(reply), 9.0, None, None)


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A configured model, an isolated evidence dir and an isolated record dir."""
    raw_dir = tmp_path / "raw"
    assessments = tmp_path / "assessments"
    monkeypatch.setattr(config, "FUSION_MODEL_PROVIDER", "openai_compatible")
    monkeypatch.setattr(config, "FUSION_PROVIDER_WARNING", None)
    monkeypatch.setattr(config, "FUSION_MODEL_NAME", "test-model-1")
    monkeypatch.setattr(config, "FUSION_MODEL_BASE_URL", "https://fusion.test/v1")
    monkeypatch.setattr(config, "FUSION_MODEL_API_KEY", "test-key-0123456789abcdef")
    monkeypatch.setattr(config, "FUSION_TRANSPORT_RETRIES", 0)
    monkeypatch.setattr(config, "RAW_RESPONSE_DIR", raw_dir)
    monkeypatch.setattr(config, "ASSESSMENT_DIR", assessments)
    monkeypatch.setattr(config, "LOG_RAW_RESPONSES", True)
    assert config.fusion_readiness()["ready"] is True
    return assessments


def _install_sources(
    monkeypatch: pytest.MonkeyPatch,
    envelopes: Mapping[str, base.NormalizedSignals],
    overrides: Optional[Mapping[str, Any]] = None,
) -> Dict[str, int]:
    """Point each source's ``fetch_signals`` at a coroutine returning a real envelope.

    ``overrides`` maps a source id to an alternate coroutine (used to inject a
    failure or a malformed return value). Returns the per-source call counter.
    """
    calls: Dict[str, int] = {}
    replacements = dict(overrides or {})

    def make_fetcher(name: str, alternate: Any) -> Any:
        async def fetch(location: base.Location, travel_date: datetime.date) -> Any:
            calls[name] = calls.get(name, 0) + 1
            if alternate is not None:
                return await alternate(location, travel_date)
            return envelopes[name]

        return fetch

    for source, module in orchestrator._source_modules():
        monkeypatch.setattr(
            module, "fetch_signals", make_fetcher(source, replacements.get(source))
        )
    return calls


def _record(store: Path, assessment: AssessmentResponse) -> Dict[str, Any]:
    """The stored record for ``assessment``, parsed back from disk."""
    path = store / "{0}.json".format(assessment.assessment_id)
    assert path.is_file(), "the assessment record must be written before returning"
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# The happy path: three real envelopes in, one persisted assessment out
# --------------------------------------------------------------------------- #
def test_assessment_persists_the_answer_the_envelopes_and_the_reasoning(
    captured_envelopes: Mapping[str, base.NormalizedSignals],
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_sources(monkeypatch, captured_envelopes)
    model = _Model(_model_body(json.dumps(_answer(captured_envelopes["tomtom"]))))
    monkeypatch.setattr(fusion, "_send_chat_completion", model)

    assessment = asyncio.run(orchestrator.assess(LOCATION, TODAY))

    assert isinstance(assessment, AssessmentResponse)
    uuid.UUID(assessment.assessment_id)
    assert assessment.origin == "JFK" and assessment.destination == "LAX"
    assert assessment.risk_level == "moderate"
    assert model.calls == 1
    assert calls == {"aviationstack": 1, "nws": 1, "tomtom": 1}
    # The model's native float was stringified by the contract, not by the caller.
    assert assessment.risks[0].evidence[0].value == "0.96"
    assert assessment.risks[0].evidence[0].source == "tomtom"

    record = _record(store, assessment)
    assert record["assessment_id"] == assessment.assessment_id
    assert record["assessment"]["assessment_id"] == assessment.assessment_id
    assert record["request"] == {
        "origin": "JFK",
        "destination": "LAX",
        "route_label": "JFK -> LAX",
        "travel_date": TODAY.isoformat(),
    }
    assert set(record["sources"]) == {"aviationstack", "nws", "tomtom"}
    # The stored envelopes are the ones fusion read, not a re-fetch.
    for source, envelope in record["sources"].items():
        assert envelope["source"] == source
        assert envelope["retrieved_at"] == captured_envelopes[source]["retrieved_at"]

    # The prompt is the exact one sent; the response is the exact body returned.
    messages = record["fusion_prompt"]["messages"]
    assert [message["role"] for message in messages] == ["system", "user"]
    assert "ROUTE: JFK -> LAX" in messages[1]["content"]
    assert record["fusion_response"]["status_code"] == 200
    assert json.loads(record["fusion_response"]["body_text"])["model"] == "test-model-1"

    metadata = record["fusion_metadata"]
    assert metadata["model"] == "test-model-1"
    assert metadata["prompt_tokens"] == 3210
    assert metadata["completion_tokens"] == 512
    assert metadata["reasoning_tokens"] == 288
    assert metadata["total_tokens"] == 3722
    assert metadata["latency_ms"] == 9.0
    assert metadata["usage_reported"] is True
    assert metadata["raw_response_paths"]

    latency = record["latency"]
    assert set(latency["sources"]) == {"aviationstack", "nws", "tomtom"}
    assert latency["total_ms"] >= latency["fusion_ms"] >= 0


def test_the_stored_record_is_readable_through_load_assessment(
    captured_envelopes: Mapping[str, base.NormalizedSignals],
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_sources(monkeypatch, captured_envelopes)
    monkeypatch.setattr(
        fusion,
        "_send_chat_completion",
        _Model(_model_body(json.dumps(_answer(captured_envelopes["tomtom"])))),
    )

    assessment = asyncio.run(orchestrator.assess(LOCATION, TODAY))
    loaded = orchestrator.load_assessment(assessment.assessment_id)

    assert loaded["assessment_id"] == assessment.assessment_id
    assert loaded["assessment_record_path"].endswith(
        "{0}.json".format(assessment.assessment_id)
    )
    assert loaded["fusion_response_body_truncated"] is False
    assert loaded["fusion_prompt"]["messages"]


def test_assessment_ids_are_unique_per_request(
    captured_envelopes: Mapping[str, base.NormalizedSignals],
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_sources(monkeypatch, captured_envelopes)
    monkeypatch.setattr(
        fusion,
        "_send_chat_completion",
        _Model(_model_body(json.dumps(_answer(captured_envelopes["tomtom"])))),
    )

    first = asyncio.run(orchestrator.assess(LOCATION, TODAY))
    second = asyncio.run(orchestrator.assess(LOCATION, TODAY))

    assert first.assessment_id != second.assessment_id
    assert len(list(store.glob("*.json"))) == 2


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #
def test_sources_are_fetched_concurrently(
    captured_envelopes: Mapping[str, base.NormalizedSignals],
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All three fetches must overlap, not queue behind each other.

    Each fake fetcher blocks until all three have started. A sequential
    implementation would hit the 2s guard and surface as a timed-out, non-``ok``
    source instead of passing.
    """
    gate: Dict[str, Any] = {"count": 0, "release": None}
    windows: Dict[str, Any] = {}

    def make_fetcher(source: str) -> Any:
        async def fetch(location: base.Location, travel_date: datetime.date) -> Any:
            began = time.perf_counter()
            if gate["release"] is None:
                # Created inside the running loop, then shared with the siblings.
                gate["release"] = asyncio.Event()
            gate["count"] += 1
            if gate["count"] == 3:
                gate["release"].set()
            else:
                await asyncio.wait_for(gate["release"].wait(), timeout=2.0)
            windows[source] = (began, time.perf_counter())
            return captured_envelopes[source]

        return fetch

    for source, module in orchestrator._source_modules():
        monkeypatch.setattr(module, "fetch_signals", make_fetcher(source))
    monkeypatch.setattr(
        fusion,
        "_send_chat_completion",
        _Model(_model_body(json.dumps(_answer(captured_envelopes["tomtom"])))),
    )

    assessment = asyncio.run(orchestrator.assess(LOCATION, TODAY))

    assert all(
        source.status == config.STATUS_OK for source in assessment.sources
    ), "a fetch timed out, which means the calls were serialized"
    assert len(windows) == 3
    starts = [window[0] for window in windows.values()]
    ends = [window[1] for window in windows.values()]
    # Every fetch had started before the first one finished: the windows overlap.
    assert max(starts) <= min(ends)


# --------------------------------------------------------------------------- #
# Failure isolation
# --------------------------------------------------------------------------- #
def _boom(message: str) -> Any:
    """An async fetcher that raises, standing in for a broken integration."""

    async def fetch(location: base.Location, travel_date: datetime.date) -> Any:
        raise RuntimeError(message)

    return fetch


def test_one_source_raising_is_isolated_and_reported(
    captured_envelopes: Mapping[str, base.NormalizedSignals],
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_sources(
        monkeypatch,
        captured_envelopes,
        overrides={"nws": _boom("forecast stream reset")},
    )
    model = _Model(_model_body(json.dumps(_answer(captured_envelopes["tomtom"]))))
    monkeypatch.setattr(fusion, "_send_chat_completion", model)

    assessment = asyncio.run(orchestrator.assess(LOCATION, TODAY))

    statuses = {source.source: source for source in assessment.sources}
    assert statuses["nws"].status == config.STATUS_ERROR
    assert statuses["nws"].error_code == base.ErrorCode.UNEXPECTED_ERROR
    assert "forecast stream reset" in (statuses["nws"].error or "")
    assert statuses["tomtom"].status == config.STATUS_OK
    assert statuses["aviationstack"].status == config.STATUS_OK
    # Real signals survived, so the answer is partial rather than an error.
    assert assessment.status == config.STATUS_PARTIAL
    assert model.calls == 1

    record = _record(store, assessment)
    assert record["sources"]["nws"]["status"] == config.STATUS_ERROR
    assert record["sources"]["nws"]["signals"] == []


def test_a_malformed_envelope_is_reported_not_crashed(
    captured_envelopes: Mapping[str, base.NormalizedSignals],
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def not_an_envelope(
        location: base.Location, travel_date: datetime.date
    ) -> Any:
        return {"source": "nws"}  # missing status/signals/evidence: a bug

    _install_sources(monkeypatch, captured_envelopes, overrides={"nws": not_an_envelope})
    monkeypatch.setattr(
        fusion,
        "_send_chat_completion",
        _Model(_model_body(json.dumps(_answer(captured_envelopes["tomtom"])))),
    )

    assessment = asyncio.run(orchestrator.assess(LOCATION, TODAY))

    nws = next(source for source in assessment.sources if source.source == "nws")
    assert nws.status == config.STATUS_ERROR
    assert nws.error_code == base.ErrorCode.UNEXPECTED_PAYLOAD_SHAPE
    assert "no status" in (nws.error or "")
    assert assessment.status == config.STATUS_PARTIAL


def test_all_sources_failing_is_an_error_and_never_calls_the_model(
    captured_envelopes: Mapping[str, base.NormalizedSignals],
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_sources(
        monkeypatch,
        captured_envelopes,
        overrides={
            "aviationstack": _boom("aviationstack down"),
            "nws": _boom("nws down"),
            "tomtom": _boom("tomtom down"),
        },
    )
    model = _Model(_model_body("{}"))
    monkeypatch.setattr(fusion, "_send_chat_completion", model)

    assessment = asyncio.run(orchestrator.assess(LOCATION, TODAY))

    assert assessment.status == config.STATUS_ERROR
    assert assessment.risk_level == "unknown"
    assert assessment.risks == []
    assert model.calls == 0
    assert any("without usable signals" in item for item in assessment.uncertainty)

    record = _record(store, assessment)
    assert record["fusion_prompt"] is None
    assert record["fusion_response"] is None
    assert record["fusion_metadata"]["usage_reported"] is False
    assert record["fusion_metadata"]["prompt_tokens"] is None


def test_fusion_http_failure_downgrades_to_partial_not_error(
    captured_envelopes: Mapping[str, base.NormalizedSignals],
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sources worked, fusion did not: ``partial`` with an explicit reason."""
    _install_sources(monkeypatch, captured_envelopes)
    monkeypatch.setattr(
        fusion,
        "_send_chat_completion",
        _Model(
            fusion._Attempt(
                429,
                json.dumps({"error": {"message": "rate limit exceeded"}}),
                4.0,
                None,
                None,
            )
        ),
    )

    assessment = asyncio.run(orchestrator.assess(LOCATION, TODAY))

    assert assessment.status == config.STATUS_PARTIAL
    assert assessment.risk_level == "unknown"
    assert any("429" in item for item in assessment.uncertainty)
    assert any(
        "source data was collected" in item for item in assessment.uncertainty
    )

    record = _record(store, assessment)
    assert record["fusion_response"]["status_code"] == 429
    assert record["fusion_metadata"]["total_tokens"] is None
    assert record["sources"]["tomtom"]["signals"], "the envelopes are kept"


def test_a_persist_failure_is_surfaced_instead_of_returned(
    captured_envelopes: Mapping[str, base.NormalizedSignals],
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_sources(monkeypatch, captured_envelopes)
    monkeypatch.setattr(
        fusion,
        "_send_chat_completion",
        _Model(_model_body(json.dumps(_answer(captured_envelopes["tomtom"])))),
    )
    # A regular file where the directory should be: mkdir can never succeed.
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(config, "ASSESSMENT_DIR", blocker / "assessments")

    with pytest.raises(orchestrator.AssessmentStoreError) as caught:
        asyncio.run(orchestrator.assess(LOCATION, TODAY))

    assert "could not be written" in str(caught.value)


def test_arguments_are_validated_before_anything_is_fetched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_sources(monkeypatch, {})

    with pytest.raises(ValueError):
        asyncio.run(orchestrator.assess("JFK-LAX", TODAY))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        asyncio.run(
            orchestrator.assess(
                LOCATION, datetime.datetime.combine(TODAY, datetime.time())
            )
        )
    assert calls == {}





