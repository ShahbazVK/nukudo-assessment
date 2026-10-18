"""Unit tests for the fusion layer (``backend.fusion``).

What is real here
-----------------
The envelopes come from ``captured_envelopes`` (see ``tests/conftest.py``): vendor
payloads captured live and replayed through the **real** ``fetch_signals``
implementations, so every signal, metric, evidence id and raw path is produced by our
own code from a payload a vendor actually served. No envelope is hand-written.

Exactly two things are substituted:

* ``fusion._send_chat_completion`` -- the single socket seam. Prompt construction,
  evidence logging (before parsing), retry policy, JSON / Pydantic / semantic
  validation and the merged response all run for real.
* ``config`` values and ``config.RAW_RESPONSE_DIR`` -- so no test needs a credential
  and no test writes evidence into the repo's ``logs/`` directory.

Where an envelope is *not* a vendor result, the test says so: the status-error case
uses ``base.failure_signals`` (a real code path, an availability outcome) and
``test_missing_evidence_ids_returns_error_without_calling_the_model`` deliberately
injects a collecting-layer defect into a real envelope to exercise the guard for it.

The model's own output is the one thing that must be canned: these tests check that we
reject bad output as thoroughly as we accept good output, so the reply is written out
per test rather than fetched.
"""

from __future__ import annotations

import asyncio
import datetime
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import pytest

import config
from backend import fusion
from data_sources import base

ORIGIN = "JFK"
DESTINATION = "LAX"
LOCATION = base.Location.from_iata(ORIGIN, DESTINATION)
TODAY = datetime.date.today()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _body(content: str) -> Dict[str, Any]:
    """A chat-completions response body carrying ``content`` (real provider shape)."""
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": "test-model-1",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1234, "completion_tokens": 321, "total_tokens": 1555},
    }


def _json_body(payload: Any) -> Dict[str, Any]:
    """A chat-completions body whose content is ``payload`` serialized as JSON."""
    return _body(json.dumps(payload))


class _FakeModel:
    """Stands in for one ``POST``, replaying scripted replies and recording the ask."""

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.payloads: List[Dict[str, Any]] = []
        self.urls: List[str] = []
        self.api_keys: List[str] = []
        self.timeouts: List[float] = []

    @property
    def calls(self) -> int:
        return len(self.payloads)

    async def __call__(
        self,
        payload: Mapping[str, Any],
        *,
        url: str,
        api_key: str,
        timeout: float,
    ) -> fusion._Attempt:
        self.payloads.append(json.loads(json.dumps(dict(payload))))
        self.urls.append(url)
        self.api_keys.append(api_key)
        self.timeouts.append(timeout)
        reply = self.replies[min(self.calls - 1, len(self.replies) - 1)]
        if callable(reply):
            reply = reply()
        if isinstance(reply, fusion._Attempt):
            return reply
        if isinstance(reply, str):
            return fusion._Attempt(200, reply, 7.5, None, None)
        return fusion._Attempt(200, json.dumps(reply), 7.5, None, None)


def _install(monkeypatch: pytest.MonkeyPatch, model: _FakeModel) -> _FakeModel:
    """Route the module's only socket call at ``model``."""
    monkeypatch.setattr(fusion, "_send_chat_completion", model)
    return model


def _fuse(
    signals: Sequence[Mapping[str, Any]], travel_date: Optional[datetime.date] = None
) -> Any:
    return asyncio.run(fusion.fuse(signals, LOCATION, travel_date or TODAY))


def _records(directory: Path) -> List[Dict[str, Any]]:
    """Every evidence record written for a call, oldest first."""
    paths = sorted(
        directory.glob("*.json"), key=lambda item: (item.stat().st_mtime_ns, item.name)
    )
    return [json.loads(path.read_text(encoding="utf-8")) for path in paths]


def _squash(text: str) -> str:
    """Collapse whitespace so assertions survive the prompt's line wrapping."""
    return " ".join(text.split())



def _prompt_payload(user: str) -> Dict[str, Any]:
    """The JSON evidence block embedded in the user prompt, parsed back."""
    start = user.index('{"route"')
    end = user.index("\n", start)
    return json.loads(user[start:end])



def _first_evidence(envelope: Mapping[str, Any]) -> Mapping[str, Any]:
    """The first citable evidence entry a real envelope carries."""
    entries = list(envelope.get("evidence") or ())
    assert entries, "the replayed envelope should carry evidence"
    return entries[0]


def _risk(evidence_id: str, source: str, **overrides: Any) -> Dict[str, Any]:
    """One well-formed risk citing a real evidence entry."""
    risk: Dict[str, Any] = {
        "risk": "Road closure on the JFK approach road",
        "severity": "high",
        "confidence": 0.6,
        "evidence": [
            {
                "source": source,
                "evidence_id": evidence_id,
                "field": "event_descriptions",
                "value": "Closed",
            }
        ],
    }
    risk.update(overrides)
    return risk


def _assessment(evidence_id: str, source: str, **overrides: Any) -> Dict[str, Any]:
    """A complete, internally consistent model answer (every model-owned key)."""
    payload: Dict[str, Any] = {
        "risk_level": "moderate",
        "confidence": 0.62,
        "summary": (
            "One closure sits on an approach road to JFK and the weather window is "
            "clear. TomTom reports current conditions only, which caps confidence."
        ),
        "risks": [_risk(evidence_id, source)],
        "recommended_actions": [
            "Allow 30 extra minutes to reach JFK.",
            "Check the airport access road again two hours before departure.",
        ],
        "uncertainty": [
            "TomTom describes the road network now, not on the travel date."
        ],
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture()
def evidence_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A configured model and an isolated evidence directory, both real code paths."""
    directory = tmp_path / "raw"
    monkeypatch.setattr(config, "FUSION_MODEL_PROVIDER", "openai_compatible")
    monkeypatch.setattr(config, "FUSION_PROVIDER_WARNING", None)
    monkeypatch.setattr(config, "FUSION_MODEL_NAME", "test-model-1")
    monkeypatch.setattr(config, "FUSION_MODEL_BASE_URL", "https://fusion.test/v1")
    monkeypatch.setattr(config, "FUSION_MODEL_API_KEY", "test-key-0123456789abcdef")
    monkeypatch.setattr(config, "FUSION_TRANSPORT_RETRIES", 1)
    monkeypatch.setattr(config, "HTTP_BACKOFF_BASE_SECONDS", 0.0)
    monkeypatch.setattr(config, "RAW_RESPONSE_DIR", directory)
    monkeypatch.setattr(config, "LOG_RAW_RESPONSES", True)
    assert config.fusion_readiness()["ready"] is True
    return directory


@pytest.fixture()
def tomtom_only(
    captured_envelopes: Dict[str, base.NormalizedSignals]
) -> List[base.NormalizedSignals]:
    """Just the replayed TomTom envelope: real signals, real evidence, one source."""
    return [captured_envelopes["tomtom"]]



# --------------------------------------------------------------------------- #
# Prompt construction
# --------------------------------------------------------------------------- #
def test_prompt_carries_route_date_and_every_source(
    captured_envelopes: Dict[str, base.NormalizedSignals]
) -> None:
    """Each source appears with the status it really reported, not a summary of it."""
    messages = fusion.build_messages(
        list(captured_envelopes.values()), LOCATION, TODAY, today=TODAY
    )

    assert [message["role"] for message in messages] == ["system", "user"]
    user = messages[1]["content"]
    assert "ROUTE: JFK -> LAX" in user
    assert "TRAVEL DATE: {0}".format(TODAY.isoformat()) in user

    payload = _prompt_payload(user)
    assert payload["route"]["origin"] == ORIGIN
    assert payload["route"]["days_from_now"] == 0
    assert [block["source"] for block in payload["sources"]] == [
        "aviationstack",
        "nws",
        "tomtom",
    ]
    for block in payload["sources"]:
        envelope = captured_envelopes[block["source"]]
        assert block["status"] == envelope["status"]
        expected_codes = list(envelope.get("error_codes") or ())
        if not expected_codes and envelope.get("error_code"):
            expected_codes = [envelope["error_code"]]
        assert block["error_codes"] == expected_codes
        assert block["signals_included"] == min(
            len(envelope["signals"]), config.FUSION_MAX_SIGNALS_PER_SOURCE
        )
        assert block["signal_count"] == len(envelope["signals"])
        # Both counts are always present, so "0 excluded" is stated, not inferred
        # from a missing key.
        assert block["signals_excluded_by_cap"] == (
            len(envelope["signals"]) - block["signals_included"]
        )
        if envelope["status"] != config.STATUS_OK:
            assert block["error"], "a degraded source must state why"


def test_prompt_states_the_rules_that_prevent_false_calm(
    captured_envelopes: Dict[str, base.NormalizedSignals]
) -> None:
    """The four distinctions the brief asks for are in the prompt, not assumed."""
    system = _squash(fusion.SYSTEM_PROMPT)
    assert '"Checked and clear" is evidence; "could not check" is not.' in system
    assert 'road_class "unclassified" means the vendor published no road class' in (
        system
    )
    assert "severity null means ungraded, never harmless" in system
    assert "Never invent evidence ids or source names." in system
    assert "Never fill a gap with general knowledge about airports" in system
    assert "below 0.40 = low, 0.40 to 0.69 = medium, 0.70 and above = high" in system
    assert "no prose, no markdown and no code fences" in system

    user = _squash(
        fusion.build_messages(
            list(captured_envelopes.values()), LOCATION, TODAY, today=TODAY
        )[1]["content"]
    )
    assert "cite only evidence ids present in the input" in user
    assert "no markdown, no code fences, no commentary" in user


def test_prompt_publishes_the_schema_the_response_is_validated_against() -> None:
    """The prompt's schema is generated from the response model, model-owned fields only."""
    schema = fusion._prompt_schema()
    assert set(schema["properties"]) == set(fusion._MODEL_OWNED_FIELDS)
    assert set(schema["required"]) == set(fusion._MODEL_OWNED_FIELDS)
    for owned_by_pipeline in fusion._PIPELINE_OWNED_FIELDS:
        assert owned_by_pipeline not in schema["properties"]
    # A trimmed sub-model the model never fills must not drag its definition along.
    assert "SourceStatus" not in json.dumps(schema)


def test_prompt_discloses_withheld_signals_and_the_response_repeats_it(
    captured_envelopes: Dict[str, base.NormalizedSignals]
) -> None:
    """A capped source is capped out loud: in the prompt, and in ``uncertainty``."""
    envelopes = list(captured_envelopes.values())
    user = fusion.build_messages(envelopes, LOCATION, TODAY, today=TODAY)[1]["content"]

    capped = [
        envelope
        for envelope in envelopes
        if len(envelope["signals"]) > config.FUSION_MAX_SIGNALS_PER_SOURCE
    ]
    assert capped, "the captured TomTom board is larger than the cap"
    for envelope in capped:
        withheld = len(envelope["signals"]) - config.FUSION_MAX_SIGNALS_PER_SOURCE
        assert "PROMPT SIZE LIMIT (disclosed, not silent)" in user
        assert "{0}: {1} of {2} signals shown".format(
            envelope["source"], config.FUSION_MAX_SIGNALS_PER_SOURCE, len(envelope["signals"])
        ) in user
        assert "{0} withheld".format(withheld) in user
    assert "do not treat their absence as calm" in user

    notes = fusion._truncation_notes(envelopes)
    assert notes and all("were withheld for prompt size" in note for note in notes)
    assert all(envelope["source"] in note for note, envelope in zip(notes, capped))


def test_unavailable_source_is_shown_as_missing_evidence_not_as_calm() -> None:
    """A real availability outcome (``failure_signals``) is carried through verbatim."""
    envelope = base.failure_signals(
        source="aviationstack",
        error_code=base.ErrorCode.PLAN_LIMIT,
        error="Free-tier keys cannot read this date.",
        location=LOCATION,
        travel_date=TODAY,
        warnings=["Plan limit reached."],
    )

    block = fusion._source_block(envelope)
    assert block["status"] == config.STATUS_UNAVAILABLE
    assert block["error_codes"] == [base.ErrorCode.PLAN_LIMIT]
    assert block["error"] == "Free-tier keys cannot read this date."
    assert block["signals"] == []
    assert block.get("sample") is None

    user = fusion.build_messages([envelope], LOCATION, TODAY, today=TODAY)[1]["content"]
    assert '"status":"unavailable"' in user
    assert base.ErrorCode.PLAN_LIMIT in user
    assert "Free-tier keys cannot read this date." in user


# --------------------------------------------------------------------------- #
# A valid answer: merged, validated, labelled, logged
# --------------------------------------------------------------------------- #
def test_valid_answer_becomes_a_validated_assessment_with_pipeline_fields_winning(
    evidence_dir: Path,
    tomtom_only: List[base.NormalizedSignals],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model's own ``status``/``origin``/``confidence_label`` never reach the result."""
    envelope = tomtom_only[0]
    entry = _first_evidence(envelope)
    payload = _assessment(
        str(entry["id"]),
        str(entry["source"]),
        # Deliberately wrong pipeline-owned values: they must be discarded, not trusted.
        status="ok",
        origin="ZZZ",
        destination="ZZZ",
        schema_version="0.0.0",
        confidence_label="high",
        fusion_error_code="rate_limited",
    )
    model = _install(monkeypatch, _FakeModel(_json_body(payload)))

    assessment = _fuse(tomtom_only)

    assert model.calls == 1
    assert model.urls == ["https://fusion.test/v1/chat/completions"]
    assert model.payloads[0]["model"] == "test-model-1"
    assert model.payloads[0]["temperature"] == config.FUSION_MODEL_TEMPERATURE
    assert model.payloads[0]["response_format"] == {"type": "json_object"}
    assert [message["role"] for message in model.payloads[0]["messages"]] == [
        "system",
        "user",
    ]

    assert assessment.status == config.STATUS_OK
    assert assessment.origin == ORIGIN
    assert assessment.destination == DESTINATION
    assert assessment.travel_date == TODAY.isoformat()
    assert assessment.schema_version == config.SCHEMA_VERSION
    assert assessment.risk_level == "moderate"
    assert assessment.confidence == pytest.approx(0.62)
    # A model that claims its own answer was rate-limited is ignored, not believed.
    assert assessment.fusion_error_code is None
    # Derived from the number, so the model cannot call 0.62 "high".
    assert assessment.confidence_label == "medium"
    assert len(assessment.risks) == 1
    assert assessment.risks[0].evidence[0].evidence_id == entry["id"]
    assert assessment.risks[0].evidence[0].source == entry["source"]
    assert assessment.recommended_actions == payload["recommended_actions"]
    assert [source.source for source in assessment.sources] == ["tomtom"]
    assert assessment.sources[0].status == config.STATUS_OK
    assert assessment.missing_credentials == config.missing_credentials()
    assert assessment.generated_at and assessment.generated_at.endswith("Z")


def test_prompt_and_raw_answer_are_logged_before_parsing(
    evidence_dir: Path,
    tomtom_only: List[base.NormalizedSignals],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stored record is the audit trail: exact prompt, exact body, no key."""
    entry = _first_evidence(tomtom_only[0])
    model = _install(
        monkeypatch, _FakeModel(_json_body(_assessment(str(entry["id"]), str(entry["source"]))))
    )

    _fuse(tomtom_only)

    paths = sorted(evidence_dir.glob("*.json"))
    assert len(paths) == 1
    record = json.loads(paths[0].read_text(encoding="utf-8"))

    assert record["source"] == fusion.FUSION_LOG_SOURCE
    assert record["raw_logged_before_parsing"] is True
    assert record["attempt"] == 1
    assert record["max_attempts"] == 1 + config.FUSION_TRANSPORT_RETRIES
    assert record["request"]["messages"] == model.payloads[0]["messages"]
    assert _squash(record["request"]["messages"][0]["content"]).startswith(
        "You are the signal-fusion layer of a travel disruption assessor."
    )
    assert record["request"]["headers"]["Authorization"] == "Bearer <redacted>"
    assert record["request"]["prompt_chars_total"] == sum(
        record["request"]["prompt_chars"].values()
    )
    assert record["request"]["prompt_estimated_tokens"] > 1000
    assert record["request"]["url"] == "https://fusion.test/v1/chat/completions"

    body = json.loads(record["response"]["body_text"])
    assert record["response"]["status_code"] == 200
    assert body["choices"][0]["message"]["content"] == json.dumps(
        _assessment(str(entry["id"]), str(entry["source"]))
    )
    assert record["response"]["body_sha256"]
    assert record["error"] is None
    assert record["context"]["origin"] == ORIGIN
    assert record["context"]["travel_date"] == TODAY.isoformat()
    assert [source["source"] for source in record["context"]["sources"]] == ["tomtom"]
    assert record["context"]["evidence_available"] == len(tomtom_only[0]["evidence"])

    # The credential is never written, in any field.
    written = paths[0].read_text(encoding="utf-8")
    assert "test-key-0123456789abcdef" not in written
    assert "Bearer <redacted>" in written


def test_withheld_signals_are_disclosed_in_the_returned_uncertainty(
    evidence_dir: Path,
    tomtom_only: List[base.NormalizedSignals],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A capped prompt must not read as a complete one in the response either."""
    entry = _first_evidence(tomtom_only[0])
    _install(
        monkeypatch, _FakeModel(_json_body(_assessment(str(entry["id"]), str(entry["source"]))))
    )

    assessment = _fuse(tomtom_only)

    withheld = len(tomtom_only[0]["signals"]) - config.FUSION_MAX_SIGNALS_PER_SOURCE
    assert withheld > 0
    notes = [note for note in assessment.uncertainty if "withheld for prompt size" in note]
    assert len(notes) == 1
    assert "tomtom: {0} of {1} signals were included".format(
        config.FUSION_MAX_SIGNALS_PER_SOURCE, len(tomtom_only[0]["signals"])
    ) in notes[0]
    assert "the other {0}".format(withheld) in notes[0]
    # The model's own uncertainty is kept, alongside the pipeline's disclosure.
    assert "TomTom describes the road network now" in " ".join(assessment.uncertainty)


# --------------------------------------------------------------------------- #
# Refusals that must happen before (or instead of) asking the model
# --------------------------------------------------------------------------- #
def _forbidden(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("the model must not be called here")


def test_not_ready_returns_an_error_naming_what_is_unset(
    tomtom_only: List[base.NormalizedSignals], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unconfigured model is an error state, not an invitation to improvise."""
    monkeypatch.setattr(config, "FUSION_MODEL_NAME", "")
    monkeypatch.setattr(fusion, "_send_chat_completion", _forbidden)

    assessment = _fuse(tomtom_only)

    assert assessment.status == config.STATUS_ERROR
    assert assessment.risk_level == "unknown"
    assert assessment.confidence is None
    assert assessment.risks == []
    assert assessment.fusion_error_code == "not_ready"
    assert "not configured" in (assessment.summary or "")
    assert any("FUSION_MODEL_NAME is empty" in text for text in assessment.uncertainty)


def test_placeholder_key_returns_an_error_without_a_request(
    tomtom_only: List[base.NormalizedSignals], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key-shaped placeholder must not be spent on a live call."""
    monkeypatch.setattr(config, "FUSION_MODEL_API_KEY", "your_api_key_here")
    monkeypatch.setattr(fusion, "_send_chat_completion", _forbidden)

    assessment = _fuse(tomtom_only)

    assert assessment.status == config.STATUS_ERROR
    assert any("placeholder" in text for text in assessment.uncertainty)


def test_no_usable_signals_returns_an_error_rather_than_reasoning_over_nothing(
    evidence_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two real availability outcomes, zero signals: "not checked" is not "clear"."""
    envelopes = [
        base.unavailable_signals(
            source="aviationstack",
            error_code=base.ErrorCode.PLAN_LIMIT,
            error="Free-tier keys cannot read this date.",
            location=LOCATION,
            travel_date=TODAY,
        ),
        base.unavailable_signals(
            source="tomtom",
            error_code=base.ErrorCode.NO_DATA_FOR_QUERY,
            error="No incidents reported for these points.",
            location=LOCATION,
            travel_date=TODAY,
        ),
    ]
    monkeypatch.setattr(fusion, "_send_chat_completion", _forbidden)

    assessment = _fuse(envelopes)

    assert assessment.status == config.STATUS_ERROR
    assert "nothing to assess" in (assessment.summary or "")
    assert "not a clear result" in (assessment.summary or "")
    assert assessment.fusion_error_code == "no_signals"
    assert any("not evidence of calm" in text for text in assessment.uncertainty)
    assert any("aviationstack: unavailable (plan_limit)" in text for text in assessment.uncertainty)
    assert [source.status for source in assessment.sources] == [
        config.STATUS_UNAVAILABLE,
        config.STATUS_UNAVAILABLE,
    ]


def test_missing_evidence_ids_returns_error_without_calling_the_model(
    evidence_dir: Path,
    tomtom_only: List[base.NormalizedSignals],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Signals nobody can cite are a collecting-layer bug; refuse to assess them."""
    # A deliberate defect injected into a real envelope -- not a source result. No live
    # capture can produce it, and the guard exists precisely because it would be a bug.
    defective = dict(tomtom_only[0])
    defective["evidence"] = []
    monkeypatch.setattr(fusion, "_send_chat_completion", _forbidden)

    assessment = _fuse([defective])

    assert assessment.status == config.STATUS_ERROR
    assert "no evidence ids" in (assessment.summary or "")
    assert assessment.fusion_error_code == "no_evidence"
    assert any("bug in the collecting layer" in text for text in assessment.uncertainty)


# --------------------------------------------------------------------------- #
# Bad model output is rejected, never repaired and never re-rolled
# --------------------------------------------------------------------------- #
def test_omitted_required_key_returns_an_error(
    evidence_dir: Path,
    tomtom_only: List[base.NormalizedSignals],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _first_evidence(tomtom_only[0])
    payload = _assessment(str(entry["id"]), str(entry["source"]))
    payload.pop("risks")
    model = _install(monkeypatch, _FakeModel(_json_body(payload)))

    assessment = _fuse(tomtom_only)

    assert model.calls == 1
    assert assessment.status == config.STATUS_ERROR
    assert "omitted required fields" in (assessment.summary or "")
    assert "Missing required keys: risks" in " ".join(assessment.uncertainty)


def test_json_wrapped_in_a_code_fence_is_rejected_not_unwrapped(
    evidence_dir: Path,
    tomtom_only: List[base.NormalizedSignals],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lenient parsing would hide a model that ignores the output contract."""
    entry = _first_evidence(tomtom_only[0])
    fenced = "```json\n{0}\n```".format(
        json.dumps(_assessment(str(entry["id"]), str(entry["source"])))
    )
    model = _install(monkeypatch, _FakeModel(_body(fenced)))

    assessment = _fuse(tomtom_only)

    assert model.calls == 1
    assert assessment.status == config.STATUS_ERROR
    assert "did not return a JSON object" in (assessment.summary or "")
    assert any("not JSON" in text for text in assessment.uncertainty)


def test_prose_before_the_json_is_rejected(
    evidence_dir: Path,
    tomtom_only: List[base.NormalizedSignals],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _first_evidence(tomtom_only[0])
    prose = "Here is my assessment: {0}".format(
        json.dumps(_assessment(str(entry["id"]), str(entry["source"])))
    )
    _install(monkeypatch, _FakeModel(_body(prose)))

    assessment = _fuse(tomtom_only)

    assert assessment.status == config.STATUS_ERROR
    assert any("not JSON" in text for text in assessment.uncertainty)
    assert assessment.fusion_error_code == "invalid_response"


def test_schema_violation_is_reported_as_a_validation_error(
    evidence_dir: Path,
    tomtom_only: List[base.NormalizedSignals],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pydantic is the last line: an out-of-range confidence is not clamped."""
    entry = _first_evidence(tomtom_only[0])
    _install(
        monkeypatch,
        _FakeModel(
            _json_body(
                _assessment(str(entry["id"]), str(entry["source"]), confidence=1.4)
            )
        ),
    )

    assessment = _fuse(tomtom_only)

    assert assessment.status == config.STATUS_ERROR
    assert "failed validation" in (assessment.summary or "")
    errors = " ".join(assessment.uncertainty)
    assert "confidence" in errors
    assert "less than or equal to 1" in errors
    assert assessment.confidence is None
    assert assessment.fusion_error_code == "invalid_response"


def test_invented_evidence_id_is_an_error_not_a_dropped_citation(
    evidence_dir: Path,
    tomtom_only: List[base.NormalizedSignals],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dropping the citation would leave a claim whose support we quietly removed."""
    entry = _first_evidence(tomtom_only[0])
    _install(
        monkeypatch,
        _FakeModel(_json_body(_assessment("ev_made_up_999", str(entry["source"])))),
    )

    assessment = _fuse(tomtom_only)

    assert assessment.status == config.STATUS_ERROR
    assert assessment.risks == []
    assert "internally inconsistent" in (assessment.summary or "")
    problems = " ".join(assessment.uncertainty)
    assert "ev_made_up_999" in problems
    assert "does not exist in the input" in problems


def test_evidence_attributed_to_the_wrong_source_is_an_error(
    evidence_dir: Path,
    tomtom_only: List[base.NormalizedSignals],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An id that exists but is credited to someone else is still a false citation."""
    entry = _first_evidence(tomtom_only[0])
    assert entry["source"] == "tomtom"
    _install(
        monkeypatch, _FakeModel(_json_body(_assessment(str(entry["id"]), "nws")))
    )

    assessment = _fuse(tomtom_only)

    assert assessment.status == config.STATUS_ERROR
    assert "belongs to 'tomtom' but was attributed to 'nws'" in " ".join(
        assessment.uncertainty
    )


def test_severe_level_with_zero_risks_is_an_error(
    evidence_dir: Path,
    tomtom_only: List[base.NormalizedSignals],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The brief's example contradiction: alarm with nothing behind it."""
    payload = _assessment("", "tomtom", risk_level="severe", risks=[])
    _install(monkeypatch, _FakeModel(_json_body(payload)))

    assessment = _fuse(tomtom_only)

    assert assessment.status == config.STATUS_ERROR
    assert "was reported with no cited risks" in " ".join(assessment.uncertainty)


def test_unknown_level_with_risks_is_an_error(
    evidence_dir: Path,
    tomtom_only: List[base.NormalizedSignals],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mirror image: "unknown" cannot coexist with concrete findings."""
    entry = _first_evidence(tomtom_only[0])
    payload = _assessment(str(entry["id"]), str(entry["source"]), risk_level="unknown")
    _install(monkeypatch, _FakeModel(_json_body(payload)))

    assessment = _fuse(tomtom_only)

    assert assessment.status == config.STATUS_ERROR
    assert "'unknown' was reported alongside 1 risk(s)" in " ".join(
        assessment.uncertainty
    )


def test_severity_outside_the_vocabulary_is_an_error(
    evidence_dir: Path,
    tomtom_only: List[base.NormalizedSignals],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A made-up severity must not reach the frontend as if it were graded."""
    entry = _first_evidence(tomtom_only[0])
    payload = _assessment(str(entry["id"]), str(entry["source"]))
    payload["risks"][0]["severity"] = "catastrophic"
    _install(monkeypatch, _FakeModel(_json_body(payload)))

    assessment = _fuse(tomtom_only)

    assert assessment.status == config.STATUS_ERROR
    assert "severity 'catastrophic' is not one of" in " ".join(
        assessment.uncertainty
    )


def test_risk_without_evidence_is_an_error(
    evidence_dir: Path,
    tomtom_only: List[base.NormalizedSignals],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule 3 enforced structurally: no claim without a citation."""
    payload = _assessment("", "tomtom", risks=[_risk("", "tomtom", evidence=[])])
    _install(monkeypatch, _FakeModel(_json_body(payload)))

    assessment = _fuse(tomtom_only)

    assert assessment.status == config.STATUS_ERROR
    assert "no evidence cited" in " ".join(assessment.uncertainty)


# --------------------------------------------------------------------------- #
# Transport, logging and programming errors
# --------------------------------------------------------------------------- #
def _transport_failure() -> fusion._Attempt:
    """A connection that never produced an answer (the one retryable case)."""
    return fusion._Attempt(
        status_code=None,
        body_text=None,
        latency_ms=3.0,
        transport_error="ConnectError",
        transport_error_message="connection refused",
    )


def test_transport_error_is_retried_once_then_reported(
    evidence_dir: Path,
    tomtom_only: List[base.NormalizedSignals],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One retry is allowed because no answer arrived; both attempts are recorded."""
    model = _install(monkeypatch, _FakeModel(_transport_failure(), _transport_failure()))

    assessment = _fuse(tomtom_only)

    assert model.calls == 2
    assert assessment.status == config.STATUS_ERROR
    assert "could not be reached" in (assessment.summary or "")
    assert assessment.fusion_error_code == "transport_error"
    assert "after 2 attempt(s): ConnectError: connection refused" in " ".join(
        assessment.uncertainty
    )

    records = _records(evidence_dir)
    assert [record["attempt"] for record in records] == [1, 2]
    assert all(record["response"] is None for record in records)
    assert records[0]["error"]["type"] == "ConnectError"
    # Same prompt both times: the retry re-asks the identical question.
    assert records[0]["request"]["messages"] == records[1]["request"]["messages"]


def test_transport_retry_that_succeeds_returns_the_assessment(
    evidence_dir: Path,
    tomtom_only: List[base.NormalizedSignals],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry only happens when nothing arrived, so a good answer is never re-rolled."""
    entry = _first_evidence(tomtom_only[0])
    reply = _json_body(_assessment(str(entry["id"]), str(entry["source"])))
    model = _install(monkeypatch, _FakeModel(_transport_failure(), reply))

    assessment = _fuse(tomtom_only)

    assert model.calls == 2
    assert assessment.status == config.STATUS_OK
    assert len(_records(evidence_dir)) == 2


def test_non_200_answer_is_reported_and_not_retried(
    evidence_dir: Path,
    tomtom_only: List[base.NormalizedSignals],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 429 is an answer, not a transport failure: retrying would spend quota blindly."""
    body = json.dumps({"error": {"message": "rate limit exceeded", "type": "rate_limit"}})
    model = _install(
        monkeypatch, _FakeModel(fusion._Attempt(429, body, 11.0, None, None))
    )

    assessment = _fuse(tomtom_only)

    assert model.calls == 1
    assert assessment.status == config.STATUS_ERROR
    assert "HTTP 429" in (assessment.summary or "")
    assert assessment.fusion_error_code == "rate_limited"
    assert "rate limit exceeded" in " ".join(assessment.uncertainty)
    record = _records(evidence_dir)[0]
    assert record["response"]["status_code"] == 429
    assert record["response"]["body_text"] == body


def test_a_5xx_answer_is_coded_provider_error(
    evidence_dir: Path,
    tomtom_only: List[base.NormalizedSignals],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 503 is the provider failing, not a quota answer: the code says which."""
    _install(
        monkeypatch,
        _FakeModel(fusion._Attempt(503, '{"error": "unavailable"}', 9.0, None, None)),
    )

    assessment = _fuse(tomtom_only)

    assert assessment.fusion_error_code == "provider_error"
    assert "HTTP 503" in (assessment.summary or "")


def test_a_timeout_is_coded_timeout_not_a_generic_transport_error(
    evidence_dir: Path,
    tomtom_only: List[base.NormalizedSignals],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exception *type* separates \"too slow\" from \"could not connect\"."""
    slow = fusion._Attempt(None, None, 30.0, "ReadTimeout", "read timed out")
    _install(monkeypatch, _FakeModel(slow, slow))

    assessment = _fuse(tomtom_only)

    assert assessment.fusion_error_code == "timeout"


def test_an_unknown_error_code_is_a_programming_error() -> None:
    """A typo'd code must fail loudly here, not reach the frontend as a silent miss."""
    with pytest.raises(ValueError) as caught:
        fusion._error_response(
            location=LOCATION,
            travel_date=TODAY,
            summary="A failure with no valid code.",
            uncertainty=[],
            error_code="rate-limted",
        )

    assert "unknown fusion error code" in str(caught.value)
    assert "rate_limited" in str(caught.value)


def test_unwritable_logging_stops_the_assessment(
    evidence_dir: Path,
    tomtom_only: List[base.NormalizedSignals],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule 5: with no record of prompt and answer, there is no assessment to return."""
    entry = _first_evidence(tomtom_only[0])
    _install(
        monkeypatch,
        _FakeModel(_json_body(_assessment(str(entry["id"]), str(entry["source"])))),
    )
    monkeypatch.setattr(config, "LOG_RAW_RESPONSES", False)

    assessment = _fuse(tomtom_only)

    assert assessment.status == config.STATUS_ERROR
    assert "could not be recorded" in (assessment.summary or "")
    assert any("rule 5" in text for text in assessment.uncertainty)
    assert list(evidence_dir.glob("*.json")) == []


@pytest.mark.parametrize(
    "signals, location, travel_date",
    [
        ([{"signals": []}], LOCATION, TODAY),
        ("jfk-lax", LOCATION, TODAY),
        ([], "JFK-LAX", TODAY),
        ([], LOCATION, "2026-10-17"),
        ([], LOCATION, datetime.datetime(2026, 10, 17, 12, 0)),
    ],
    ids=["no-source-id", "not-a-sequence", "not-a-location", "not-a-date", "datetime"],
)
def test_programming_errors_raise_value_error(
    signals: Any, location: Any, travel_date: Any
) -> None:
    """Bad arguments are a bug in the caller, so they raise like ``data_sources`` does."""
    with pytest.raises(ValueError):
        asyncio.run(fusion.fuse(signals, location, travel_date))


def test_fuse_with_trace_returns_the_prompt_the_body_and_the_usage(
    evidence_dir: Path,
    tomtom_only: List[base.NormalizedSignals],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The orchestrator gets the exchange without re-parsing the evidence file.

    ``fuse`` stays the plain entry point; ``fuse_with_trace`` is the same call plus the
    exact messages sent, the raw body received and the provider's usage counters --
    including reasoning tokens, which are billed against the same budget.
    """
    entry = _first_evidence(tomtom_only[0])
    body = _json_body(_assessment(str(entry["id"]), str(entry["source"])))
    body["usage"]["completion_tokens_details"] = {"reasoning_tokens": 111}
    model = _install(monkeypatch, _FakeModel(body))

    result = asyncio.run(fusion.fuse_with_trace(tomtom_only, LOCATION, TODAY))

    assert result.assessment.risk_level == "moderate"
    exchange = result.exchange
    assert exchange is not None
    assert [message["role"] for message in exchange.prompt_messages] == [
        "system",
        "user",
    ]
    # The traced prompt is byte-for-byte what was posted.
    assert exchange.prompt_messages[1]["content"] == model.payloads[0]["messages"][1][
        "content"
    ]
    assert exchange.status_code == 200
    assert exchange.response_body == json.dumps(body)
    assert exchange.latency_ms == 7.5
    assert exchange.attempts == 1
    assert exchange.raw_response_paths
    assert exchange.usage == {
        "prompt_tokens": 1234,
        "completion_tokens": 321,
        "total_tokens": 1555,
        "reasoning_tokens": 111,
    }


def test_fuse_with_trace_has_no_exchange_when_no_request_was_made(
    captured_envelopes: Dict[str, base.NormalizedSignals],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not ready means no prompt exists, so none is invented for the record."""
    monkeypatch.setattr(config, "FUSION_MODEL_API_KEY", None, raising=False)

    result = asyncio.run(
        fusion.fuse_with_trace(list(captured_envelopes.values()), LOCATION, TODAY)
    )

    assert result.assessment.status == config.STATUS_ERROR
    assert result.exchange is None


@pytest.mark.parametrize(
    "body",
    [
        None,
        "",
        "not json",
        "[]",
        '{"choices": []}',
        '{"usage": null}',
        '{"usage": {"prompt_tokens": "12", "total_tokens": true}}',
    ],
    ids=["none", "empty", "not-json", "list", "no-usage", "null-usage", "wrong-types"],
)
def test_usage_extraction_never_invents_a_count(body: Optional[str]) -> None:
    """A body without a usable ``usage`` block yields nothing, not zeros."""
    assert fusion._usage_from_body(body) == {}


def test_usage_extraction_reads_both_reasoning_shapes() -> None:
    nested = '{"usage": {"prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11, "completion_tokens_details": {"reasoning_tokens": 4}}}'
    flat = '{"usage": {"prompt_tokens": 5, "reasoning_tokens": 4}}'

    assert fusion._usage_from_body(nested)["reasoning_tokens"] == 4
    assert fusion._usage_from_body(flat) == {"prompt_tokens": 5, "reasoning_tokens": 4}

