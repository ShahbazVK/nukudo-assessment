"""The assessment orchestrator: fan out to every source, fuse, persist, return.

What this module does
---------------------
One call to :func:`assess` is one assessment:

1. Every source in :data:`_SOURCE_MODULES` is fetched **concurrently** with
   ``asyncio.gather``. Each fetch is a real ``fetch_signals`` call; nothing here
   knows a vendor's payload shape.
2. A source that raises, or returns something that is not a ``NormalizedSignals``
   envelope, is converted into an explicit ``error`` envelope naming that source
   and the reason. One broken integration never takes the other two down, and it
   never disappears from the result either.
3. The envelopes go to :func:`backend.fusion.fuse_with_trace`, which returns the
   validated assessment *and* the exact prompt, raw answer and token usage.
4. The whole thing is written to ``logs/assessments/{assessment_id}.json``
   **before** the response is returned: the assessment, the three envelopes, the
   fusion prompt, the fusion response, and the usage metadata. If that write
   fails, the call raises :class:`AssessmentStoreError` instead of returning an
   assessment nobody can look up. Untraceable output is not a deliverable
   (``.clinerules`` rule 5).

Honest status, decided here rather than guessed
-----------------------------------------------
* Every source failed -> ``status="error"``. Absent evidence is *not* calm.
* Sources produced signals but fusion failed -> ``status="partial"``: the
  collection worked, the reasoning step did not, and the response says so. The
  risk level stays ``unknown``; nothing is filled in.
* Otherwise the status is fusion's own (``ok`` only when every source was clean,
  ``partial`` when any source was degraded).
* ``fusion_error_code`` carries fusion's own stable reason (``rate_limited``,
  ``timeout``, ``invalid_response``, ...) whenever it failed, and ``None`` when it
  succeeded -- including a ``partial`` that is only degraded *sources*. The
  orchestrator enforces that invariant so the frontend can branch on the code
  instead of on prose.

Boundaries
----------
* No vendor parsing and no prompt construction: this module composes the layers.
* No retries and no caching: one request, three fetches, one fusion call.
* No fabricated substitutions: a failed source is reported as failed.
* Backend only: nothing here imports Streamlit (``.clinerules`` rule 6).
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

import config
from backend import fusion
from backend.fusion import FusionResult
from backend.models import AssessmentResponse
from data_sources import aviationstack, base, nws, tomtom

__all__ = [
    "AssessmentNotFoundError",
    "AssessmentRecordError",
    "AssessmentStoreError",
    "assess",
    "load_assessment",
]

logger = logging.getLogger(__name__)


def _source_modules() -> Tuple[Tuple[str, Any], ...]:
    """The ``(source id, module)`` pairs this orchestrator fans out to.

    Each value is a source module exposing the single ``fetch_signals`` entry point;
    ``Any`` is used for the module object because importing it as a type would be a
    fiction (modules are not instances of a declared protocol in Python).

    Resolved per call rather than captured at import time, so a test (or a future
    feature flag) can substitute a fetcher and actually be exercised. ``gdelt`` is
    deliberately absent: it is an unimplemented optional source, and asking it for
    signals would only produce a ``not_implemented`` error envelope.
    """
    return (
        (config.SOURCE_AVIATIONSTACK, aviationstack),
        (config.SOURCE_NWS, nws),
        (config.SOURCE_TOMTOM, tomtom),
    )


class AssessmentStoreError(RuntimeError):
    """The assessment record could not be written, so no assessment is returned."""


class AssessmentNotFoundError(FileNotFoundError):
    """No record exists for the requested id (unknown or not a UUID)."""


class AssessmentRecordError(RuntimeError):
    """A record exists but could not be read back as a JSON object."""


@dataclasses.dataclass(frozen=True)
class _SourceOutcome:
    """One source's envelope and how long the fetch actually took."""

    source: str
    envelope: base.NormalizedSignals
    latency_ms: float


def _failure_envelope(
    source: str,
    error_code: str,
    error: str,
    location: base.Location,
    travel_date: datetime.date,
) -> base.NormalizedSignals:
    """An error envelope for a failure the orchestrator itself detected."""
    return base.failure_signals(
        source=source,
        error_code=error_code,
        error=error,
        location=location,
        travel_date=travel_date,
        warnings=[
            "The other sources are unaffected; this gap is stated in the assessment's "
            "sources and uncertainty."
        ],
    )


def _envelope_problem(source: str, envelope: Any) -> Optional[str]:
    """Why ``envelope`` is not a usable ``NormalizedSignals`` for ``source``, if it is not.

    A source module promises this shape; checking it here means a future integration
    bug surfaces as a named, reported failure instead of a ``KeyError`` inside fusion.
    """
    if not isinstance(envelope, Mapping):
        return "returned {0} instead of a NormalizedSignals envelope".format(
            type(envelope).__name__
        )
    if envelope.get("source") != source:
        return "returned an envelope for source {0!r} instead of {1!r}".format(
            envelope.get("source"), source
        )
    if not isinstance(envelope.get("status"), str) or not envelope.get("status"):
        return "returned an envelope with no status"
    for key in ("signals", "evidence"):
        if not isinstance(envelope.get(key), list):
            return "returned an envelope whose {0!r} is not a list".format(key)
    return None


async def _fetch_source(
    source: str,
    module: Any,
    location: base.Location,
    travel_date: datetime.date,
) -> _SourceOutcome:
    """Fetch one source, converting *any* failure into an explicit error envelope.

    The source modules already turn vendor problems into error states; this wrapper
    exists for the two failure modes they cannot report about themselves -- a raised
    exception (a bug) and a return value that is not the promised envelope (also a
    bug). Neither is allowed to become an exception out of the request, and neither
    is allowed to vanish from the response.
    """
    started = time.perf_counter()
    error_code = base.ErrorCode.UNEXPECTED_PAYLOAD_SHAPE
    try:
        envelope: Any = await module.fetch_signals(location, travel_date)
    except asyncio.CancelledError:  # pragma: no cover - shutdown, not a data problem
        raise
    except Exception as exc:  # noqa: BLE001 - one broken source must not fail the rest
        logger.exception("assess: %s.fetch_signals raised", source)
        envelope = None
        error_code = base.ErrorCode.UNEXPECTED_ERROR
        problem = "raised {0}: {1}".format(
            type(exc).__name__, base.redact_text(str(exc)) or "no message"
        )
    else:
        problem = _envelope_problem(source, envelope)

    latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
    if problem:
        return _SourceOutcome(
            source=source,
            envelope=_failure_envelope(
                source=source,
                error_code=error_code,
                error=(
                    "{0}.fetch_signals {1}. No signals from this source were used, "
                    "and the assessment reports the gap instead of filling it."
                ).format(source, problem),
                location=location,
                travel_date=travel_date,
            ),
            latency_ms=latency_ms,
        )
    return _SourceOutcome(
        source=source, envelope=envelope, latency_ms=latency_ms
    )


def _reconcile_status(
    assessment: AssessmentResponse, envelopes: Sequence[base.NormalizedSignals]
) -> AssessmentResponse:
    """Make the overall status agree with what the collection layer actually got.

    Two cases fusion cannot decide on its own:

    * no source produced a usable signal -> ``error``. The risk level is ``unknown``
      and the reason is stated; an empty picture is never reported as calm.
    * sources did produce signals but fusion failed -> ``partial``. Collection worked,
      reasoning did not; the uncertainty list says exactly that, and the source
      envelopes are still recorded with the assessment.
    """
    usable = any(base.has_usable_signals(envelope) for envelope in envelopes)
    if not usable:
        if assessment.status != config.STATUS_ERROR:
            assessment.status = config.STATUS_ERROR
            assessment.uncertainty.append(
                "No source returned a usable signal, so this is not an assessment of "
                "risk: nothing was checked successfully, and that is not the same as "
                "nothing being wrong."
            )
        return assessment
    if assessment.status == config.STATUS_ERROR:
        assessment.status = config.STATUS_PARTIAL
        assessment.uncertainty.append(
            "The source data was collected, but the fusion step did not produce an "
            "assessment, so no risk level is reported. The source envelopes are "
            "recorded with this assessment and can be reviewed directly."
        )
    return assessment


async def assess(
    location: base.Location, travel_date: datetime.date
) -> AssessmentResponse:
    """Fetch every source concurrently, fuse, persist the record, return the answer.

    Raises ``ValueError`` only for a programming error in the arguments (matching the
    ``data_sources`` convention) and :class:`AssessmentStoreError` when the record
    cannot be written -- an assessment nobody can look up is not returned.
    """
    if not isinstance(location, base.Location):
        raise ValueError(
            "assess requires a data_sources.base.Location, got {0}".format(
                type(location).__name__
            )
        )
    if isinstance(travel_date, datetime.datetime) or not isinstance(
        travel_date, datetime.date
    ):
        raise ValueError(
            "assess requires a datetime.date travel date, got {0}".format(
                type(travel_date).__name__
            )
        )

    started = time.perf_counter()
    specs = _source_modules()
    outcomes = await asyncio.gather(
        *(
            _fetch_source(source, module, location, travel_date)
            for source, module in specs
        ),
        return_exceptions=True,
    )

    collected: List[_SourceOutcome] = []
    for (source, _module), outcome in zip(specs, outcomes):
        if isinstance(outcome, BaseException):
            if isinstance(outcome, asyncio.CancelledError):  # pragma: no cover
                raise outcome
            # _fetch_source already converts failures; this is the belt-and-braces
            # path for a bug in the wrapper itself.
            logger.error(
                "assess: the %s fetch wrapper failed: %s", source, outcome,
                exc_info=outcome,
            )
            collected.append(
                _SourceOutcome(
                    source=source,
                    envelope=_failure_envelope(
                        source=source,
                        error_code=base.ErrorCode.UNEXPECTED_ERROR,
                        error=(
                            "The {0} fetch wrapper failed with {1}. No signals from "
                            "this source were used.".format(
                                source, type(outcome).__name__
                            )
                        ),
                        location=location,
                        travel_date=travel_date,
                    ),
                    latency_ms=0.0,
                )
            )
        else:
            collected.append(outcome)

    envelopes = [item.envelope for item in collected]
    latencies = {item.source: item.latency_ms for item in collected}

    fusion_started = time.perf_counter()
    result = await fusion.fuse_with_trace(envelopes, location, travel_date)
    fusion_ms = round((time.perf_counter() - fusion_started) * 1000.0, 2)
    assessment = _reconcile_status(result.assessment, envelopes)
    # Fusion classified *why* it failed (it is the only layer that sees the provider's
    # status code and the transport exception). Keep the field honest here: a
    # successful assessment never carries a failure code, whatever a future path does.
    if assessment.status == config.STATUS_OK:
        assessment.fusion_error_code = None
    total_ms = round((time.perf_counter() - started) * 1000.0, 2)

    record = _build_record(
        assessment=assessment,
        envelopes=envelopes,
        latencies=latencies,
        result=result,
        total_ms=total_ms,
        fusion_ms=fusion_ms,
        location=location,
        travel_date=travel_date,
    )
    path = _persist(record)

    logger.info(
        "assess: %s %s risk=%s status=%s total_ms=%.1f fusion_ms=%.1f sources=%s "
        "assessment_id=%s record=%s",
        location.origin.iata,
        location.destination.iata,
        assessment.risk_level,
        assessment.status,
        total_ms,
        fusion_ms,
        ",".join(
            "{0}:{1}/{2:.0f}ms".format(item.source, item.envelope["status"], item.latency_ms)
            for item in collected
        ),
        assessment.assessment_id,
        _display_path(path),
    )
    return assessment


def _fusion_metadata(result: FusionResult) -> Dict[str, Any]:
    """Token usage and call shape for the record, with missing values left missing.

    ``prompt_tokens`` / ``completion_tokens`` / ``reasoning_tokens`` / ``total_tokens``
    are ``None`` when the provider did not report them (or when no call was made).
    ``usage_reported`` distinguishes "the provider said zero" from "no usage block".
    """
    exchange = result.exchange
    usage: Dict[str, Any] = dict(exchange.usage) if exchange is not None else {}
    return {
        "model": (
            exchange.model
            if exchange is not None
            else (config.FUSION_MODEL_NAME or None)
        ),
        "latency_ms": exchange.latency_ms if exchange is not None else None,
        "attempts": exchange.attempts if exchange is not None else 0,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "reasoning_tokens": usage.get("reasoning_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "usage_reported": bool(usage),
        "transport_error": (
            exchange.transport_error if exchange is not None else None
        ),
        "raw_response_paths": (
            list(exchange.raw_response_paths) if exchange is not None else []
        ),
    }


def _build_record(
    *,
    assessment: AssessmentResponse,
    envelopes: Sequence[base.NormalizedSignals],
    latencies: Mapping[str, float],
    result: FusionResult,
    total_ms: float,
    fusion_ms: float,
    location: base.Location,
    travel_date: datetime.date,
) -> Dict[str, Any]:
    """The stored evidence record: answer, inputs, prompt, answer text, and usage.

    ``fusion_prompt`` / ``fusion_response`` are separate fields on purpose: the
    prompt is the evidence for the reasoning, and the response is the raw thing the
    provider sent. Neither is re-serialized into a lossy summary.
    """
    exchange = result.exchange
    return {
        "record_version": 1,
        "assessment_id": assessment.assessment_id,
        "persisted_at": base.utc_now_iso(),
        "request": {
            "origin": location.origin.iata,
            "destination": location.destination.iata,
            "route_label": location.route_label,
            "travel_date": travel_date.isoformat(),
        },
        "latency": {
            "total_ms": total_ms,
            "fusion_ms": fusion_ms,
            "sources": dict(latencies),
        },
        "assessment": assessment.model_dump(mode="json"),
        "sources": {
            str(envelope["source"]): envelope for envelope in envelopes
        },
        "fusion_prompt": (
            None
            if exchange is None
            else {
                "model": exchange.model,
                "messages": [dict(message) for message in exchange.prompt_messages],
            }
        ),
        "fusion_response": (
            None
            if exchange is None
            else {
                "status_code": exchange.status_code,
                "attempts": exchange.attempts,
                "body_text": exchange.response_body,
            }
        ),
        "fusion_metadata": _fusion_metadata(result),
    }


def _persist(record: Mapping[str, Any]) -> Path:
    """Write the record atomically, or raise :class:`AssessmentStoreError`.

    Written to a temporary file and renamed so a reader never sees a half-written
    record; a failure here is surfaced, never swallowed (rule 5).
    """
    directory = Path(config.ASSESSMENT_DIR)
    path = directory / "{0}.json".format(record["assessment_id"])
    temporary = path.with_name(path.name + ".tmp")
    try:
        directory.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(str(temporary), str(path))
    except OSError as exc:
        raise AssessmentStoreError(
            "The assessment record {0} could not be written ({1}: {2}). The "
            "assessment is not returned, because it would not be traceable.".format(
                _display_path(path),
                type(exc).__name__,
                base.redact_text(str(exc)),
            )
        ) from exc
    return path


def _display_path(path: Path) -> str:
    """Path relative to the project root when possible, absolute otherwise."""
    try:
        return str(path.resolve().relative_to(Path(config.PROJECT_ROOT).resolve()))
    except ValueError:
        return str(path)


def _canonical_assessment_id(raw: str) -> str:
    """Canonical UUID string for ``raw``, or ``ValueError`` if it is not a UUID.

    Canonicalising before building a path is what keeps ``..`` and absolute paths
    out of ``GET /evidence/{assessment_id}``.
    """
    try:
        return str(uuid.UUID(str(raw).strip()))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(
            "{0!r} is not a valid assessment id (a UUID is required)".format(raw)
        ) from exc


def load_assessment(assessment_id: str) -> Dict[str, Any]:
    """Load one stored assessment record for serving over the API.

    Raises :class:`AssessmentNotFoundError` for an unknown or non-UUID id and
    :class:`AssessmentRecordError` when a record exists but cannot be read as a JSON
    object. The stored file is never modified: the raw fusion body is truncated in
    the returned copy only, and ``fusion_response_body_truncated`` says whether that
    happened.
    """
    try:
        canonical = _canonical_assessment_id(assessment_id)
    except ValueError as exc:
        raise AssessmentNotFoundError(str(exc)) from exc
    path = Path(config.ASSESSMENT_DIR) / "{0}.json".format(canonical)
    if not path.is_file():
        raise AssessmentNotFoundError(
            "No assessment record for id {0}".format(canonical)
        )
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssessmentRecordError(
            "Assessment record {0} could not be read: {1}".format(
                canonical, exc
            )
        ) from exc
    if not isinstance(record, dict):
        raise AssessmentRecordError(
            "Assessment record {0} is not a JSON object".format(canonical)
        )

    record["assessment_id"] = canonical
    record["assessment_record_path"] = _display_path(path)
    record["fusion_response_body_truncated"] = False
    response = record.get("fusion_response")
    if isinstance(response, dict):
        body = response.get("body_text")
        limit = config.EVIDENCE_BODY_MAX_CHARS
        if isinstance(body, str) and limit > 0 and len(body) > limit:
            response["body_text"] = base.truncate_text(body, limit)
            record["fusion_response_body_truncated"] = True
    return record



