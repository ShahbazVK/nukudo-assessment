"""Frontend render tests. No network, no backend, no real data.

``streamlit.testing.v1.AppTest`` executes ``frontend/app.py`` in-process.
``frontend.api_client.assess`` and ``.get_evidence`` are replaced with in-process
fakes, so no test can reach the backend or a third-party API.

The payloads below are **UI fixtures**: shaped exactly like the response contract so
the renderer can be exercised, and asserted only as *rendering* (does the severe pill
appear, is the rate-limit banner shown). Nothing here claims to be evidence, and
nothing here is ever shown to a user. Behaviour that depends on real data lives in
``tests/test_assess_live.py`` and ``tests/test_api.py``, which call the real stack.
"""

from __future__ import annotations

import dataclasses
import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from streamlit.testing.v1 import AppTest

import frontend.api_client as api_client
import frontend.demo_assessments as demo_assessments
from backend.models import AssessmentResponse

APP_PATH: Path = Path(__file__).resolve().parents[1] / "frontend" / "app.py"


def _assessment(**overrides: Any) -> AssessmentResponse:
    """A contract-shaped assessment for rendering tests."""
    payload: Dict[str, Any] = {
        "status": "ok",
        "origin": "JFK",
        "destination": "LAX",
        "travel_date": datetime.date.today().isoformat(),
        "risk_level": "severe",
        "confidence": 0.84,
        "confidence_label": "high",
        "summary": "Severe road closures near both airports.",
        "risks": [
            {
                "risk": "Severe road closures around JFK and LAX",
                "severity": "severe",
                "confidence": 0.8,
                "confidence_label": "high",
                "evidence": [
                    {
                        "source": "tomtom",
                        "evidence_id": "tomtom-incidents-0",
                        "field": "incident_distance_km",
                        "value": "0.96",
                        "observed_at": "2026-10-17T09:00:00Z",
                    },
                    {
                        "source": "nws",
                        "evidence_id": "nws-alerts-0",
                        "field": "short_forecast",
                        "value": "Thunderstorms",
                    },
                ],
            }
        ],
        "recommended_actions": ["Leave two hours earlier", "Carry a charged phone"],
        "uncertainty": ["Ground incidents are reported in real time only."],
        "sources": [
            {"source": "nws", "status": "ok", "records": 3},
            {"source": "aviationstack", "status": "ok", "records": 2},
            {"source": "tomtom", "status": "ok", "records": 4},
        ],
    }
    payload.update(overrides)
    return AssessmentResponse.model_validate(payload)


def _record(response: AssessmentResponse) -> api_client.EvidenceRecord:
    """The persisted record ``GET /evidence/{id}`` would return for ``response``."""
    path = "logs/assessments/{0}.json".format(response.assessment_id)
    return api_client.EvidenceRecord.model_validate(
        {
            "assessment_id": response.assessment_id,
            "record_version": 1,
            "persisted_at": "2026-10-17T09:00:05Z",
            "request": {
                "origin": "JFK",
                "destination": "LAX",
                "travel_date": datetime.date.today().isoformat(),
            },
            "assessment": response.model_dump(mode="json"),
            "sources": {
                "nws": {"source": "nws", "status": "ok", "signals": [{"signal": "x"}]},
                "aviationstack": {
                    "source": "aviationstack",
                    "status": "ok",
                    "signals": [],
                },
                "tomtom": {"source": "tomtom", "status": "ok", "signals": []},
            },
            "fusion_prompt": {
                "model": "test-model",
                "messages": [
                    {"role": "system", "content": "You are a risk assessor."},
                    {"role": "user", "content": "Here are the signals."},
                ],
            },
            "fusion_response": {
                "status_code": 200,
                "attempts": 1,
                "body_text": '{"risk_level": "severe"}',
            },
            "fusion_metadata": {
                "model": "test-model",
                "prompt_tokens": 4888,
                "completion_tokens": 2056,
                "total_tokens": 6944,
                "latency_ms": 4312,
            },
            "assessment_record_path": path,
            "fusion_response_body_truncated": False,
        }
    )


def _patch_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response: Optional[AssessmentResponse] = None,
    record: Optional[api_client.EvidenceRecord] = None,
    stored: Optional[AssessmentResponse] = None,
    error: Optional[api_client.AssessError] = None,
    stored_error: Optional[api_client.AssessError] = None,
) -> Dict[str, List[Any]]:
    """Replace the network functions; record calls so tests can assert they happened."""
    calls: Dict[str, List[Any]] = {"assess": [], "assessment": [], "evidence": []}

    async def fake_assess(
        origin: str, destination: str, travel_date: datetime.date
    ) -> AssessmentResponse:
        calls["assess"].append((origin, destination, travel_date))
        if error is not None:
            raise error
        assert response is not None, "test bug: patched assess with no response"
        return response

    async def fake_get_assessment(assessment_id: str) -> AssessmentResponse:
        calls["assessment"].append(assessment_id)
        if stored_error is not None:
            raise stored_error
        assert stored is not None, "test bug: patched get_assessment with no response"
        return stored

    async def fake_evidence(assessment_id: str) -> api_client.EvidenceRecord:
        calls["evidence"].append(assessment_id)
        assert record is not None, "test bug: patched get_evidence with no record"
        return record

    monkeypatch.setattr(api_client, "assess", fake_assess)
    monkeypatch.setattr(api_client, "get_assessment", fake_get_assessment)
    monkeypatch.setattr(api_client, "get_evidence", fake_evidence)
    return calls


def _open() -> AppTest:
    """Render the app once, asserting it did not raise."""
    app = AppTest.from_file(str(APP_PATH), default_timeout=30)
    app.run()
    assert not app.exception, [element.value for element in app.exception]
    return app


def _submit(app: AppTest, origin: str, destination: str) -> None:
    """Fill the form and press the button, the way a user would."""
    app.text_input[0].set_value(origin)
    app.text_input[1].set_value(destination)
    submit_index = [button.label for button in app.button].index("Assess trip")
    app.button[submit_index].click()
    app.run()
    assert not app.exception, [element.value for element in app.exception]


def _markdown(app: AppTest) -> List[str]:
    return [element.value for element in app.markdown]


def _plain(text: str) -> str:
    """Undo the backslash escapes the app adds so model text cannot restyle a page."""
    return text.replace("\\", "")


def test_form_renders_with_todays_date_and_empty_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First load: the collapsed demo section, then two empty fields and today's date."""
    calls = _patch_client(monkeypatch)
    app = _open()

    assert [field.value for field in app.text_input] == ["", ""]
    assert app.date_input[0].value == datetime.date.today()
    labels = [button.label for button in app.button]
    # The demo buttons render above the form's own button, and none is marked loaded.
    assert labels[-1] == "Assess trip"
    assert [label.split(" · ")[0] for label in labels[:-1]] == [
        "JFK → LAX",
        "SFO → JFK",
        "DEN → MIA",
    ]
    assert not any(label.startswith("✓") for label in labels)
    assert app.session_state["response"] is None
    assert _markdown(app) == []
    assert calls == {"assess": [], "assessment": [], "evidence": []}


@pytest.mark.parametrize(
    ("origin", "destination", "fragment"),
    [
        ("ZZZ", "LAX", "not a supported US airport"),
        ("", "LAX", "Origin is required"),
        ("JFK", "JFK", "same airport"),
    ],
)
def test_invalid_codes_are_rejected_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
    destination: str,
    fragment: str,
) -> None:
    """A typo costs a keystroke, not three upstream calls and a model call."""
    calls = _patch_client(monkeypatch, response=_assessment())
    app = _open()
    _submit(app, origin, destination)

    assert calls["assess"] == []
    assert len(app.error) == 1
    assert fragment in app.error[0].value
    assert app.session_state["response"] is None


def test_successful_submit_renders_badge_risks_actions_and_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 10-second read: risk level, confidence, summary, evidence, actions, sources."""
    response = _assessment()
    calls = _patch_client(monkeypatch, response=response, record=_record(response))
    app = _open()
    _submit(app, "jfk", "lax")

    assert calls["assess"] == [("JFK", "LAX", datetime.date.today())]
    markdown = _markdown(app)

    assert any("severe risk" in item for item in markdown), "risk badge missing"
    assert any("**Confidence 0.84**" in item for item in markdown)
    assert any("High confidence" in item for item in markdown)
    assert response.summary in markdown
    assert any("Severe road closures around JFK and LAX" in item for item in markdown)
    assert any("SEVERE" in item for item in markdown), "severity chip missing"
    assert any(
        "Source: **TomTom**" in item and "0.96" in item for item in markdown
    ), "traceable evidence line missing"
    assert [box.label for box in app.checkbox] == [
        "Leave two hours earlier",
        "Carry a charged phone",
    ]
    assert any("NWS · ok" in item for item in markdown)
    assert any("Aviationstack · ok" in item for item in markdown)
    assert any("TomTom · ok" in item for item in markdown)
    assert isinstance(app.session_state["elapsed_seconds"], float)



def test_a_capped_source_lists_every_raw_record_on_its_own_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prompt-cap disclosure: the sentence, a subheading, then one bullet per path.

    The path list is not truncated: a reader who wants to verify the withheld signals
    can open every one of them.
    """
    paths = [
        "logs/raw/nws_20261017T124823Z_ee0a5c8d.json",
        "logs/raw/nws_20261017T124823Z_2f492482.json",
        "logs/raw/nws_20261017T124823Z_f749e074.json",
        "logs/raw/nws_20261017T124824Z_f7976375.json",
        "logs/raw/nws_20261017T124824Z_cfec2278.json",
    ]
    sentence = (
        "nws: 3 of 5 signals were included in the assessment (the other 2 were "
        "withheld for prompt size, ranked by severity then distance, and are not "
        "reflected here)."
    )
    response = _assessment(uncertainty=[sentence + " Raw records: " + ", ".join(paths)])
    _patch_client(monkeypatch, response=response, record=_record(response))
    app = _open()
    _submit(app, "JFK", "LAX")

    markdown = [_plain(item) for item in _markdown(app)]
    assert any(item.startswith("- nws: 3 of 5 signals") for item in markdown), (
        "the sentence keeps its bullet"
    )
    assert not any(
        "logs/raw/" in item for item in markdown if item.startswith("- nws:")
    ), "the paths are no longer concatenated onto the sentence"
    assert "**Raw records:**" in markdown, "paths need a subheading"
    path_bullets = [item for item in markdown if item.startswith("- `logs/raw/")]
    assert path_bullets == [
        "- `{0}`".format(path) for path in paths
    ], "one monospace bullet per path, in order, none truncated"


def test_a_plain_uncertainty_item_is_still_a_single_bullet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No marker, no newline: the rendering is exactly what it was before."""
    note = "Ground incidents are reported in real time only."
    response = _assessment(uncertainty=[note])
    _patch_client(monkeypatch, response=response, record=_record(response))
    app = _open()
    _submit(app, "JFK", "LAX")

    markdown = [_plain(item) for item in _markdown(app)]
    assert "- " + note in markdown
    assert not any("Raw records" in item for item in markdown)
    assert not any("\n" in item for item in markdown)


def test_an_uncertainty_item_with_a_newline_gets_one_bullet_per_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A newline separates statements, so each statement is a bullet of its own."""
    lines = [
        "tomtom: no ground incidents were returned for either airport.",
        "The road picture around both airports is therefore unverified.",
    ]
    response = _assessment(uncertainty=["\n".join(lines)])
    _patch_client(monkeypatch, response=response, record=_record(response))
    app = _open()
    _submit(app, "JFK", "LAX")

    markdown = [_plain(item) for item in _markdown(app)]
    assert "- " + lines[0] in markdown
    assert "- " + lines[1] in markdown
    assert not any("\n" in item for item in markdown), "no line break leaks into one bullet"
    assert not any("Raw records" in item for item in markdown)


def test_partial_status_shows_banner_and_keeps_the_assessment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A degraded source is announced, and the rest of the answer is still rendered."""
    response = _assessment(
        status="partial",
        sources=[
            {"source": "nws", "status": "ok", "records": 3},
            {
                "source": "aviationstack",
                "status": "error",
                "error": "The Aviationstack key is not configured.",
                "error_code": "missing_credential",
                "error_codes": ["missing_credential"],
                "records": 0,
            },
            {"source": "tomtom", "status": "ok", "records": 4},
        ],
    )
    _patch_client(monkeypatch, response=response, record=_record(response))
    app = _open()
    _submit(app, "JFK", "LAX")

    assert len(app.warning) == 1
    assert "Some sources or the AI assessment were unavailable" in app.warning[0].value
    assert any("severe risk" in item for item in _markdown(app))
    assert any("Aviationstack · error" in item for item in _markdown(app))
    captions = _plain(" ".join(element.value for element in app.caption))
    assert "code: missing_credential" in captions
    assert "The Aviationstack key is not configured." in captions


def test_error_status_shows_could_not_assess_and_no_badge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No risk badge on an error: there is no risk level to show, so none is shown."""
    response = _assessment(
        status="error",
        risk_level="unknown",
        confidence=None,
        confidence_label=None,
        summary="All 3 sources failed, so no assessment could be produced.",
        risks=[],
        recommended_actions=[],
        uncertainty=[
            "nws: The NWS request timed out.",
            "aviationstack: The Aviationstack key is not configured.",
        ],
        sources=[
            {"source": "nws", "status": "error", "error_code": "timeout"},
            {"source": "aviationstack", "status": "error", "error_code": "missing_credential"},
            {"source": "tomtom", "status": "error", "error_code": "timeout"},
        ],
    )
    calls = _patch_client(monkeypatch, response=response, record=_record(response))
    app = _open()
    _submit(app, "JFK", "LAX")

    assert [element.value for element in app.subheader] == ["Could not assess"]
    markdown = _markdown(app)
    assert not any("risk</span>" in item for item in markdown), "no badge on error"
    assert "All 3 sources failed, so no assessment could be produced." in markdown
    assert any("The NWS request timed out." in item for item in markdown)
    assert any("NWS · error" in item for item in markdown)
    assert calls["evidence"] == [response.assessment_id]


def test_rate_limited_fusion_shows_the_free_tier_banner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An older backend (no code, only the 429 sentence) still gets the right banner."""
    response = _assessment(
        status="partial",
        risk_level="unknown",
        confidence=None,
        confidence_label=None,
        summary="No assessment was produced.",
        risks=[],
        recommended_actions=[],
        uncertainty=[
            "The fusion model returned HTTP 429, so no assessment was produced.",
            "Rate limiting is expected on the free tier: retry after about a minute.",
        ],
    )
    assert response.fusion_error_code is None, "this test is the pre-field fallback"
    _patch_client(monkeypatch, response=response, record=_record(response))
    app = _open()
    _submit(app, "JFK", "LAX")

    assert len(app.warning) == 1
    banner = app.warning[0].value
    assert "one call per minute" in banner
    assert "Show raw evidence and AI reasoning" in banner
    # The source signals are still on the page: only the reasoning step failed.
    assert any("NWS · ok" in item for item in _markdown(app))
    assert any("unknown risk" in item for item in _markdown(app))


def test_the_rate_limit_banner_follows_the_fusion_error_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The code decides, not the prose: this text carries no 429 wording at all."""
    response = _assessment(
        status="partial",
        risk_level="unknown",
        confidence=None,
        confidence_label=None,
        summary="No assessment was produced.",
        risks=[],
        recommended_actions=[],
        uncertainty=["Nothing was reasoned over for this assessment."],
        fusion_error_code="rate_limited",
    )
    _patch_client(monkeypatch, response=response, record=_record(response))
    app = _open()
    _submit(app, "JFK", "LAX")

    assert len(app.warning) == 1
    assert "one call per minute" in app.warning[0].value


def test_another_fusion_error_code_does_not_claim_the_rate_limit_banner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``invalid_response`` is a different failure, so it gets the generic banner."""
    response = _assessment(
        status="partial",
        risk_level="unknown",
        confidence=None,
        confidence_label=None,
        summary="No assessment was produced.",
        risks=[],
        recommended_actions=[],
        uncertainty=["The fusion model returned an answer that failed validation."],
        fusion_error_code="invalid_response",
    )
    _patch_client(monkeypatch, response=response, record=_record(response))
    app = _open()
    _submit(app, "JFK", "LAX")

    assert len(app.warning) == 1
    banner = app.warning[0].value
    assert "one call per minute" not in banner
    assert "were unavailable" in banner


def test_a_partial_assessment_without_a_fusion_failure_shows_no_rate_limit_banner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A degraded *source* is not a rate-limited model: no banner claims otherwise."""
    response = _assessment(
        status="partial",
        fusion_error_code=None,
        risks=[],
        sources=[
            {"source": "nws", "status": "ok", "records": 3},
            {"source": "aviationstack", "status": "unavailable", "records": 0},
            {"source": "tomtom", "status": "ok", "records": 4},
        ],
    )
    _patch_client(monkeypatch, response=response, record=_record(response))
    app = _open()
    _submit(app, "JFK", "LAX")

    assert len(app.warning) == 1
    assert "one call per minute" not in app.warning[0].value
    assert "were unavailable" in app.warning[0].value


def test_evidence_drawer_renders_envelopes_prompt_and_raw_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The drawer is the anti-black-box: three envelopes plus the exact prompt/answer."""
    response = _assessment()
    record = _record(response)
    calls = _patch_client(monkeypatch, response=response, record=record)
    app = _open()
    _submit(app, "JFK", "LAX")

    assert [tab.label for tab in app.tabs] == [
        "NWS",
        "Aviationstack",
        "TomTom",
        "AI reasoning",
    ]
    assert len(app.json) == 3, "one envelope per source"
    assert record.assessment_record_path is not None
    captions = [element.value for element in app.caption]
    assert any("Read from " + record.assessment_record_path in item for item in captions)
    code = "\n".join(element.value for element in app.code)
    assert "You are a risk assessor." in code, "fusion prompt not shown"
    assert '{"risk_level": "severe"}' in code, "raw model answer not shown"
    assert "prompt 4888" in code or any("prompt 4888" in item for item in captions)
    assert calls["evidence"] == [response.assessment_id]

    # One widget rerun must not re-fetch the same record.
    app.checkbox[0].check()
    app.run()
    assert not app.exception
    assert calls["evidence"] == [response.assessment_id]


def test_backend_failure_shows_the_backend_message_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 500 is reported with the real reason, not 'something went wrong'."""
    error = api_client.AssessError(
        "The assessment record logs/assessments/x.json could not be written "
        "(OSError: No space left on device).",
        status_code=500,
    )
    calls = _patch_client(monkeypatch, error=error)
    app = _open()
    _submit(app, "JFK", "LAX")

    assert len(app.error) == 1
    assert "No space left on device" in app.error[0].value
    assert "HTTP 500" in app.error[0].value
    assert _markdown(app) == []
    assert calls["evidence"] == [], "no evidence lookup when no assessment exists"




# --------------------------------------------------------------------------- #
# Pre-run demo buttons: read a stored assessment, never run a new one
# --------------------------------------------------------------------------- #
def _demo_button_index(app: AppTest, origin: str, destination: str) -> int:
    """Find a demo button by its route, ignoring whatever date label it carries."""
    prefix = "{0} → {1} · ".format(origin, destination)
    labels = [button.label.lstrip("✓ ") for button in app.button]
    return next(index for index, label in enumerate(labels) if label.startswith(prefix))


def test_a_pre_run_button_loads_the_stored_assessment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The click reads one id and renders exactly those facts: no source, no model."""
    entry = demo_assessments.DEMO_ASSESSMENTS[0]
    stored = _assessment(
        assessment_id=entry.assessment_id,
        origin=entry.origin,
        destination=entry.destination,
        travel_date=entry.travel_date,
        summary="Pre-run summary, read back from the store.",
    )
    calls = _patch_client(monkeypatch, stored=stored, record=_record(stored))
    app = _open()

    app.button[_demo_button_index(app, entry.origin, entry.destination)].click()
    app.run()
    assert not app.exception, [element.value for element in app.exception]

    assert calls["assessment"] == [entry.assessment_id]
    assert calls["assess"] == [], "a demo load must not start a new assessment"
    assert calls["evidence"] == [entry.assessment_id]

    markdown = _markdown(app)
    assert any("Pre-run summary, read back from the store." in item for item in markdown)
    assert any("severe risk" in item for item in markdown)
    assert app.session_state["response"].assessment_id == entry.assessment_id
    # The load measured no latency of its own, so none is claimed.
    assert app.session_state["elapsed_seconds"] is None

    # The loaded button is marked, and the form is untouched.
    loaded = [button.label for button in app.button if button.label.startswith("✓ ")]
    assert len(loaded) == 1
    assert "{0} → {1}".format(entry.origin, entry.destination) in loaded[0]
    assert app.text_input[0].value == ""
    assert app.text_input[1].value == ""


def test_switching_between_two_pre_run_assessments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second click replaces the first: one assessment on screen at a time."""
    first = demo_assessments.DEMO_ASSESSMENTS[0]
    second = demo_assessments.DEMO_ASSESSMENTS[1]
    stored = {
        first.assessment_id: _assessment(
            assessment_id=first.assessment_id,
            origin=first.origin,
            destination=first.destination,
            summary="Summary for the first demo route.",
        ),
        second.assessment_id: _assessment(
            assessment_id=second.assessment_id,
            origin=second.origin,
            destination=second.destination,
            summary="Summary for the second demo route.",
        ),
    }
    served: List[str] = []

    async def fake_get_assessment(assessment_id: str) -> AssessmentResponse:
        served.append(assessment_id)
        return stored[assessment_id]

    async def fake_evidence(assessment_id: str) -> api_client.EvidenceRecord:
        return _record(stored[assessment_id])

    async def fail_assess(*args: Any, **kwargs: Any) -> AssessmentResponse:
        raise AssertionError("a demo load must not start a new assessment")

    monkeypatch.setattr(api_client, "get_assessment", fake_get_assessment)
    monkeypatch.setattr(api_client, "get_evidence", fake_evidence)
    monkeypatch.setattr(api_client, "assess", fail_assess)
    app = _open()

    app.button[_demo_button_index(app, first.origin, first.destination)].click()
    app.run()
    app.button[_demo_button_index(app, second.origin, second.destination)].click()
    app.run()
    assert not app.exception, [element.value for element in app.exception]

    assert served == [first.assessment_id, second.assessment_id]
    markdown = " ".join(_markdown(app))
    assert "Summary for the second demo route." in markdown
    assert "Summary for the first demo route." not in markdown
    assert app.session_state["response"].assessment_id == second.assessment_id


def test_a_missing_pre_run_assessment_is_reported_not_faked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 404 on the id is shown verbatim: no envelope is invented to fill the page."""
    entry = demo_assessments.DEMO_ASSESSMENTS[0]
    error = api_client.AssessError(
        "No assessment record for id {0}".format(entry.assessment_id),
        status_code=404,
    )
    _patch_client(monkeypatch, stored_error=error)
    app = _open()

    app.button[_demo_button_index(app, entry.origin, entry.destination)].click()
    app.run()
    assert not app.exception, [element.value for element in app.exception]

    assert len(app.error) == 1
    assert "No assessment record for id" in app.error[0].value
    assert app.session_state["response"] is None
    assert _markdown(app) == []


def test_demo_entries_are_identifiers_only() -> None:
    """An entry names a stored assessment; it carries no result that could be rendered."""
    for entry in demo_assessments.DEMO_ASSESSMENTS:
        assert {field.name for field in dataclasses.fields(entry)} == {
            "assessment_id",
            "origin",
            "destination",
            "travel_date",
        }
        assert entry.assessment_id
        assert entry.travel_date
