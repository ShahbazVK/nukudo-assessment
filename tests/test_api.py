"""HTTP-level tests for the API layer (``backend/main.py``).

These exercise the real FastAPI routes and the real orchestrator, with the same three
seams as ``tests/test_assess.py``: the three ``fetch_signals`` entry points (replaced
with coroutines returning the replayed real captures), the fusion socket, and the
config paths. Nothing is written into the repo's ``logs/`` directory.

What is asserted is the status-code contract, because that is what the frontend reads:

* an unsupported route is ``422`` and spends nothing;
* a completed assessment is ``200`` -- including ``status="error"``, which is a
  truthful envelope rather than an HTTP failure;
* a record that cannot be written is ``500`` (no untraceable answer);
* an unknown or malformed id is ``404``.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any, Dict, Mapping

import pytest
from fastapi.testclient import TestClient

import config
from backend import assess as orchestrator
from backend import fusion
from backend.main import app
from data_sources import base

TODAY = datetime.date.today()
UNKNOWN_ID = "00000000-0000-4000-8000-000000000000"


def _model_body(content: str) -> Dict[str, Any]:
    """A provider-shaped body with a usage block, as Groq returns it."""
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
            "prompt_tokens": 1200,
            "completion_tokens": 300,
            "total_tokens": 1500,
            "completion_tokens_details": {"reasoning_tokens": 120},
        },
    }


def _answer(envelope: Mapping[str, Any]) -> Dict[str, Any]:
    """A complete model answer citing one real evidence entry."""
    first = list(envelope.get("evidence") or ())[0]
    return {
        "risk_level": "moderate",
        "confidence": 0.5,
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
                        "value": 32,
                    }
                ],
            }
        ],
        "recommended_actions": ["Leave extra time for the airport transfer."],
        "uncertainty": ["Only one signal was cited in this test."],
    }


class _Model:
    """The fusion socket: one scripted provider body."""

    def __init__(self, body: Any) -> None:
        self.body = body
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
        return fusion._Attempt(200, json.dumps(self.body), 6.0, None, None)


@pytest.fixture()
def client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    captured_envelopes: Mapping[str, base.NormalizedSignals],
) -> TestClient:
    """The app with real endpoints, replayed real envelopes and isolated paths."""
    monkeypatch.setattr(config, "FUSION_MODEL_PROVIDER", "openai_compatible")
    monkeypatch.setattr(config, "FUSION_PROVIDER_WARNING", None)
    monkeypatch.setattr(config, "FUSION_MODEL_NAME", "test-model-1")
    monkeypatch.setattr(config, "FUSION_MODEL_BASE_URL", "https://fusion.test/v1")
    monkeypatch.setattr(config, "FUSION_MODEL_API_KEY", "test-key-0123456789abcdef")
    monkeypatch.setattr(config, "FUSION_TRANSPORT_RETRIES", 0)
    monkeypatch.setattr(config, "RAW_RESPONSE_DIR", tmp_path / "raw")
    monkeypatch.setattr(config, "ASSESSMENT_DIR", tmp_path / "assessments")
    monkeypatch.setattr(config, "LOG_RAW_RESPONSES", True)

    def make_fetcher(source: str) -> Any:
        async def fetch(location: base.Location, travel_date: datetime.date) -> Any:
            return captured_envelopes[source]

        return fetch

    for source, module in orchestrator._source_modules():
        monkeypatch.setattr(module, "fetch_signals", make_fetcher(source))
    monkeypatch.setattr(
        fusion,
        "_send_chat_completion",
        _Model(_model_body(json.dumps(_answer(captured_envelopes["tomtom"])))),
    )
    return TestClient(app)


# --------------------------------------------------------------------------- #
# Route validation: 422 before anything is fetched
# --------------------------------------------------------------------------- #
def test_an_unsupported_airport_is_rejected_with_422(client: TestClient) -> None:
    response = client.post(
        "/assess",
        json={"origin": "ZZZ", "destination": "LAX", "travel_date": TODAY.isoformat()},
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "ZZZ" in detail
    assert "not a supported airport" in detail


def test_a_city_name_resolves_to_its_airport(client: TestClient) -> None:
    response = client.post(
        "/assess",
        json={
            "origin": "San Francisco",
            "destination": "Los Angeles",
            "travel_date": TODAY.isoformat(),
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["origin"] == "SFO"
    assert body["destination"] == "LAX"


def test_an_ambiguous_city_names_the_candidates_instead_of_guessing(
    client: TestClient,
) -> None:
    """``New York`` serves JFK and LGA: the answer is the choices, not a coin flip."""
    response = client.post(
        "/assess",
        json={
            "origin": "New York",
            "destination": "LAX",
            "travel_date": TODAY.isoformat(),
        },
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "more than one airport" in detail
    assert "JFK" in detail and "LGA" in detail


def test_the_same_airport_twice_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/assess",
        json={"origin": "JFK", "destination": "jfk", "travel_date": TODAY.isoformat()},
    )

    assert response.status_code == 422
    assert "same airport" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# The happy path, and the record behind it
# --------------------------------------------------------------------------- #
def test_assess_returns_an_envelope_and_evidence_serves_its_record(
    client: TestClient,
) -> None:
    response = client.post(
        "/assess",
        json={"origin": "JFK", "destination": "LAX", "travel_date": TODAY.isoformat()},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["assessment_id"]
    assert body["schema_version"] == config.SCHEMA_VERSION
    assert body["risk_level"] == "moderate"
    assert body["sources"]
    assert body["risks"][0]["evidence"][0]["value"] == "32"

    evidence = client.get("/evidence/{0}".format(body["assessment_id"]))
    assert evidence.status_code == 200, evidence.text
    record = evidence.json()
    assert record["assessment_id"] == body["assessment_id"]
    assert record["assessment"]["risk_level"] == "moderate"
    assert set(record["sources"]) == {"aviationstack", "nws", "tomtom"}
    assert record["fusion_prompt"]["messages"][1]["role"] == "user"
    assert record["fusion_response"]["status_code"] == 200
    assert record["fusion_metadata"]["prompt_tokens"] == 1200
    assert record["fusion_metadata"]["reasoning_tokens"] == 120
    assert record["assessment_record_path"].endswith(
        "{0}.json".format(body["assessment_id"])
    )


def test_a_successful_assessment_carries_no_fusion_error_code(
    client: TestClient,
) -> None:
    """``None`` is the success case: no failure to classify, nothing invented."""
    response = client.post(
        "/assess",
        json={"origin": "JFK", "destination": "LAX", "travel_date": TODAY.isoformat()},
    )

    assert response.status_code == 200, response.text
    assert response.json()["fusion_error_code"] is None


def test_a_rate_limited_fusion_call_survives_as_a_stable_code(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 429 the frontend branches on is a *field*, not a sentence to match.

    Collection succeeded and only the reasoning step was refused, so the envelope is
    ``partial`` -- with the risk level left ``unknown`` and the cause named in a code.
    """
    provider_body = json.dumps({"error": {"message": "rate limit exceeded"}})

    async def rate_limited(
        payload: Mapping[str, Any], *, url: str, api_key: str, timeout: float
    ) -> fusion._Attempt:
        return fusion._Attempt(429, provider_body, 11.0, None, None)

    monkeypatch.setattr(fusion, "_send_chat_completion", rate_limited)

    response = client.post(
        "/assess",
        json={"origin": "JFK", "destination": "LAX", "travel_date": TODAY.isoformat()},
    )

    assert response.status_code == 200, response.text
    envelope = response.json()
    assert envelope["status"] == config.STATUS_PARTIAL
    assert envelope["fusion_error_code"] == "rate_limited"
    assert envelope["risk_level"] == "unknown"
    assert envelope["risks"] == []
    # The sources were still collected, so their status is recorded alongside the code.
    assert {source["status"] for source in envelope["sources"]} == {config.STATUS_OK}


# --------------------------------------------------------------------------- #
# Evidence lookups: 404 for anything that is not a stored record
# --------------------------------------------------------------------------- #
def test_evidence_for_an_unknown_id_is_404(client: TestClient) -> None:
    response = client.get("/evidence/{0}".format(UNKNOWN_ID))

    assert response.status_code == 404
    assert "No assessment record" in response.json()["detail"]


def test_evidence_for_a_malformed_id_is_404(client: TestClient) -> None:
    """A non-UUID id cannot name a record, and must not be used as a path."""
    response = client.get("/evidence/not-a-uuid")

    assert response.status_code == 404
    assert "not a valid assessment id" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# Reading one persisted assessment by id (what the pre-run demo buttons call)
# --------------------------------------------------------------------------- #
def test_assessment_by_id_returns_the_persisted_envelope(client: TestClient) -> None:
    """The stored envelope comes back as stored -- no re-run, no re-shaping."""
    created = client.post(
        "/assess",
        json={"origin": "JFK", "destination": "LAX", "travel_date": TODAY.isoformat()},
    ).json()
    assessment_id = created["assessment_id"]

    response = client.get("/assessment/{0}".format(assessment_id))

    assert response.status_code == 200, response.text
    stored = response.json()
    assert stored["assessment_id"] == assessment_id
    for field in (
        "status",
        "origin",
        "destination",
        "travel_date",
        "risk_level",
        "confidence",
        "summary",
        "recommended_actions",
        "uncertainty",
    ):
        assert stored[field] == created[field], field
    assert stored["fusion_error_code"] is None
    assert [item["source"] for item in stored["sources"]] == [
        item["source"] for item in created["sources"]
    ]


def test_assessment_by_id_for_an_unknown_id_is_404(client: TestClient) -> None:
    response = client.get("/assessment/{0}".format(UNKNOWN_ID))

    assert response.status_code == 404
    assert "No assessment record" in response.json()["detail"]


@pytest.mark.parametrize("bad_id", ["not-a-uuid", "8aebdd49-884c", "8aebdd49-884c-4ecb"])
def test_assessment_by_id_for_a_malformed_id_is_404(
    client: TestClient, bad_id: str
) -> None:
    """A malformed id names nothing: 404, and it is never used to build a path."""
    response = client.get("/assessment/{0}".format(bad_id))

    assert response.status_code == 404
    assert "not a valid assessment id" in response.json()["detail"]


def test_a_traversal_looking_id_is_404_and_names_no_path(client: TestClient) -> None:
    """``..%2F`` is normalised away before routing; either layer rejecting it is a 404."""
    response = client.get("/assessment/..%2Fescape")

    assert response.status_code == 404
    assert "logs" not in json.dumps(response.json())


def test_a_record_without_an_assessment_is_500_not_a_blank_envelope(
    client: TestClient,
) -> None:
    """An empty envelope would read as a real assessment with nothing in it."""
    orphan_id = "00000000-0000-4000-8000-0000000000ab"
    path = Path(config.ASSESSMENT_DIR) / "{0}.json".format(orphan_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"assessment_id": orphan_id, "record_version": 1}),
        encoding="utf-8",
    )

    response = client.get("/assessment/{0}".format(orphan_id))

    assert response.status_code == 500
    assert "carries no assessment object" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# A record that cannot be written is a 500, not a silent answer
# --------------------------------------------------------------------------- #
def test_an_unwritable_store_is_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(config, "ASSESSMENT_DIR", blocker / "assessments")

    response = client.post(
        "/assess",
        json={"origin": "JFK", "destination": "LAX", "travel_date": TODAY.isoformat()},
    )

    assert response.status_code == 500
    assert "could not be written" in response.json()["detail"]

