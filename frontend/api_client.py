"""Typed HTTP client for the FastAPI backend.

This module is the ONLY place the frontend talks to the network
(``.clinerules`` rule 6). Streamlit code lives in ``frontend/app.py`` and never
imports ``httpx``.

Three calls, three typed results:

* :func:`assess` -> :class:`backend.models.AssessmentResponse`. A ``200`` whose
  body says ``status="error"`` is *not* an exception: the envelope is a truthful
  answer about what could not be checked, and the UI renders it.
* :func:`get_assessment` -> :class:`backend.models.AssessmentResponse` for an id
  already persisted by this backend (the demo buttons use it; it never re-runs
  collection or fusion).
* :func:`get_evidence` -> :class:`EvidenceRecord`, the persisted record behind an
  assessment id.

Everything that is a failure -- unreachable backend, timeout, non-2xx, a body that
is not JSON, a body that no longer matches the contract -- raises
:class:`AssessError`, carrying the HTTP status (``None`` when no response was
received) and the backend's own message. Nothing is retried, nothing is cached
and nothing is substituted: the UI shows what actually happened.
"""

from __future__ import annotations

import datetime
import os
from typing import Any, Dict, Optional, Type, TypeVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend.models import AssessmentResponse

DEFAULT_BACKEND_URL: str = "http://127.0.0.1:8000"
# One assessment = three source fetches + one model call. The model alone has been
# measured at ~5s, so the client outwaits the backend instead of racing it.
DEFAULT_TIMEOUT_SECONDS: float = 60.0

ModelT = TypeVar("ModelT", bound=BaseModel)


class AssessError(RuntimeError):
    """A backend call failed. ``status_code`` is ``None`` when nothing answered."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: Optional[int] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(detail)
        self.detail: str = detail
        self.status_code: Optional[int] = status_code
        # The verbatim backend body when it was JSON, so the UI can show it.
        self.body: Optional[Dict[str, Any]] = body

    def __str__(self) -> str:
        if self.status_code is None:
            return self.detail
        return "{0} (HTTP {1})".format(self.detail, self.status_code)


class EvidenceRecord(BaseModel):
    """The stored record served by ``GET /evidence/{assessment_id}``.

    Mirrors ``backend.assess.load_assessment``'s output. ``extra="allow"`` is
    deliberate: a field this frontend does not render yet (or one added to the
    record later) is kept on the model rather than silently dropped, so the
    evidence drawer can show the record as it was written.
    """

    model_config = ConfigDict(extra="allow")

    assessment_id: str
    record_version: Optional[int] = Field(
        None, description="Version of the on-disk record layout."
    )
    persisted_at: Optional[str] = Field(None, description="When the record was written.")
    request: Dict[str, Any] = Field(default_factory=dict)
    latency: Dict[str, Any] = Field(default_factory=dict)
    assessment: AssessmentResponse
    sources: Dict[str, Dict[str, Any]] = Field(
        default_factory=dict, description="Source id -> normalized envelope."
    )
    fusion_prompt: Optional[Dict[str, Any]] = Field(
        None,
        description="The exact messages sent to the model; None if no call was made.",
    )
    fusion_response: Optional[Dict[str, Any]] = Field(
        None, description="Status code, attempts and raw body of the model answer."
    )
    fusion_metadata: Dict[str, Any] = Field(
        default_factory=dict, description="Token usage and call shape."
    )
    assessment_record_path: Optional[str] = None
    fusion_response_body_truncated: bool = Field(
        False, description="True when the served body was truncated for display."
    )


def backend_url() -> str:
    """Return the configured backend base URL (from the environment)."""
    return os.getenv("BACKEND_URL", DEFAULT_BACKEND_URL).rstrip("/")


def _backend_detail(body: Optional[Any], status_code: int) -> str:
    """Extract the backend's own explanation from an error body, or state the code."""
    if isinstance(body, dict):
        for key in ("detail", "summary", "message"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return "The backend returned HTTP {0} with a JSON body but no message.".format(
            status_code
        )
    return "The backend returned HTTP {0} with a body that is not JSON.".format(
        status_code
    )


async def _request_json(
    method: str,
    path: str,
    *,
    payload: Optional[Dict[str, Any]] = None,
    base_url: Optional[str] = None,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """Perform one backend request and return its JSON object, or raise AssessError."""
    url = "{0}{1}".format((base_url or backend_url()).rstrip("/"), path)
    try:
        async with httpx.AsyncClient(
            timeout=timeout or DEFAULT_TIMEOUT_SECONDS
        ) as client:
            response = await client.request(method, url, json=payload)
    except httpx.TimeoutException as exc:
        raise AssessError(
            "The backend at {0} did not answer in time. It may still be waiting on "
            "an upstream source.".format(url)
        ) from exc
    except httpx.HTTPError as exc:
        raise AssessError(
            "Could not reach the backend at {0} ({1}). Is it running?".format(
                url, type(exc).__name__
            )
        ) from exc

    try:
        body: Any = response.json()
    except ValueError:
        body = None

    if not 200 <= response.status_code < 300:
        raise AssessError(
            _backend_detail(body, response.status_code),
            status_code=response.status_code,
            body=body if isinstance(body, dict) else None,
        )
    if not isinstance(body, dict):
        raise AssessError(
            "The backend returned HTTP {0} with a body that is not a JSON "
            "object.".format(response.status_code),
            status_code=response.status_code,
        )
    return body


def _validated(model: Type[ModelT], body: Dict[str, Any], what: str) -> ModelT:
    """Validate a response body against the contract, reporting drift as AssessError."""
    try:
        return model.model_validate(body)
    except ValidationError as exc:
        first = (exc.errors()[0].get("loc") or ("body",))[0]
        raise AssessError(
            "The backend returned {0} that does not match the response contract "
            "({1} validation error(s); first at {2!r}).".format(
                what, exc.error_count(), first
            ),
            status_code=200,
            body=body,
        ) from exc


async def assess(
    origin: str,
    destination: str,
    travel_date: datetime.date,
    *,
    base_url: Optional[str] = None,
    timeout: Optional[float] = None,
) -> AssessmentResponse:
    """``POST /assess`` for one route and date.

    Raises :class:`AssessError` when the *request* failed. An assessment that says
    ``status="error"`` comes back as a normal return value: that envelope is the
    answer, and it names what could not be checked.
    """
    body = await _request_json(
        "POST",
        "/assess",
        payload={
            "origin": origin,
            "destination": destination,
            "travel_date": travel_date.isoformat(),
        },
        base_url=base_url,
        timeout=timeout,
    )
    return _validated(AssessmentResponse, body, "an assessment")


async def get_assessment(
    assessment_id: str,
    *,
    base_url: Optional[str] = None,
    timeout: Optional[float] = None,
) -> AssessmentResponse:
    """``GET /assessment/{assessment_id}``: the stored envelope for an id.

    Reading is not assessing: the backend returns exactly what it persisted, so this
    costs no source calls and no model call. A backend that does not know the id
    answers 404, which arrives here as :class:`AssessError` with that status.
    """
    body = await _request_json(
        "GET",
        "/assessment/{0}".format(quote(assessment_id, safe="")),
        base_url=base_url,
        timeout=timeout,
    )
    return _validated(AssessmentResponse, body, "a stored assessment")


async def get_evidence(
    assessment_id: str,
    *,
    base_url: Optional[str] = None,
    timeout: Optional[float] = None,
) -> EvidenceRecord:
    """``GET /evidence/{assessment_id}``: the stored record behind an assessment."""
    body = await _request_json(
        "GET",
        "/evidence/{0}".format(quote(assessment_id, safe="")),
        base_url=base_url,
        timeout=timeout,
    )
    return _validated(EvidenceRecord, body, "an evidence record")

