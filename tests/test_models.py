"""Contract tests for the shared Pydantic models (``backend/models.py``).

Two properties are worth pinning down here, because the rest of the pipeline and the
frontend both depend on them:

* ``EvidenceRef.value`` is a **string on the wire**. A reasoning model routinely emits
  the native JSON scalar it read (``0.96``, ``32``, ``true``) instead of the string the
  schema asked for; it is coerced once, on ingest, and objects/arrays are rejected
  rather than flattened into something that looks like evidence.
* ``AssessmentResponse.assessment_id`` is a pipeline-minted UUID4, unique per response,
  including for an explicit error state (that is what makes an error traceable).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from backend.models import AssessmentResponse, EvidenceRef


def _ref(value: Any) -> EvidenceRef:
    """An evidence reference carrying ``value`` exactly as a model might emit it."""
    return EvidenceRef(source="tomtom", evidence_id="tomtom-evt-1", value=value)


@pytest.mark.parametrize(
    "raw, expected",
    [
        (32, "32"),
        (0.96, "0.96"),
        (79.0, "79.0"),
        (True, "true"),
        (False, "false"),
        (None, "null"),
        ("Closed", "Closed"),
        ("", ""),
    ],
)
def test_evidence_value_is_stringified_from_a_native_scalar(
    raw: Any, expected: str
) -> None:
    assert _ref(raw).value == expected


def test_evidence_value_keeps_a_float_as_written_not_zero_padded() -> None:
    """``str()``, not ``format()``: ``0.96`` must not become ``0.960000``."""
    assert _ref(0.96).value == "0.96"


def test_evidence_value_accepts_the_same_scalar_through_json() -> None:
    """The coercion runs before validation, so it also applies to parsed JSON."""
    payload = '{"source": "nws", "evidence_id": "nws-fc-1", "value": 79.0}'
    assert EvidenceRef.model_validate_json(payload).value == "79.0"


def test_evidence_value_rejects_an_object_and_names_it() -> None:
    with pytest.raises(ValidationError) as caught:
        _ref({"distance_km": 0.96, "road_class": "motorway"})

    text = str(caught.value)
    assert "value" in text
    assert "must be a scalar" in text
    assert "distance_km" in text


def test_evidence_value_rejects_an_array() -> None:
    with pytest.raises(ValidationError) as caught:
        _ref([0.96, 1.29])

    assert "must be a scalar" in str(caught.value)


def test_evidence_value_defaults_to_null_when_the_key_is_absent() -> None:
    """An omitted value is still a string on the wire (``null``), not ``None``."""
    assert EvidenceRef(source="nws").value == "null"


def test_assessment_id_is_a_unique_uuid_per_response() -> None:
    first = AssessmentResponse(status="error")
    second = AssessmentResponse(status="error")

    assert uuid.UUID(first.assessment_id)
    assert first.assessment_id != second.assessment_id


def test_assessment_id_survives_a_json_round_trip() -> None:
    original = AssessmentResponse(status="partial", risk_level="unknown")
    restored = AssessmentResponse.model_validate_json(
        json.dumps(original.model_dump(mode="json"))
    )

    assert restored.assessment_id == original.assessment_id
