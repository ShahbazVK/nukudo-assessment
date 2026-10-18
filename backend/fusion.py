"""AI fusion: turn normalized signals into one evidence-backed assessment.

What this module does
---------------------
Given the ``NormalizedSignals`` envelopes a caller already fetched (one per data
source), a route and a travel date, :func:`fuse` asks an OpenAI-compatible chat
model for a structured assessment and validates the answer into
:class:`backend.models.AssessmentResponse`.

The prompt's job is *reasoning*, not summarization. The model is told to weigh each
signal against this route and this date (``distance_km`` / ``place`` /
``road_class`` / ``road_numbers`` for road incidents, ``delay_minutes`` against the
flight, ``airports`` / ``period_name`` for weather, ``sample_size`` /
``page_coverage`` for flight cohorts), to distinguish "checked and clear" from
"could not check", to cite at least one evidence id per risk, to explain its
confidence, and to say nothing the input does not support.

Boundaries
----------
* It does **not** call the data sources. A caller fans out and passes the envelopes in.
* It does **not** persist assessments or serve evidence; that is the API layer.
* It never fabricates: with no usable signals it returns an error state instead of
  asking the model to reason over nothing.

Contract
--------
``async def fuse(signals, location, travel_date) -> AssessmentResponse``

``async def fuse_with_trace(signals, location, travel_date) -> FusionResult``

``fuse`` is the plain entry point. ``fuse_with_trace`` performs the same call and
also returns the *exact* prompt that was sent, the raw body that came back, and the
token usage the provider reported, so the orchestrator can persist the reasoning
alongside the answer (and report what it cost) without re-parsing a log file.

Every runtime failure -- not ready, transport error, non-2xx answer, unparseable
body, invalid or contradictory JSON, an invented evidence id -- comes back as a
well-formed ``AssessmentResponse`` with ``status="error"``, ``risk_level="unknown"``,
a stable ``fusion_error_code`` (see :data:`FUSION_ERROR_CODES`) and the reason in
``uncertainty``. Fusion never raises for a data or model problem;
it only raises ``ValueError`` for a programming error in its arguments, matching the
``data_sources`` convention. No mocked or substituted model output is ever returned.

Configuration (see ``config.fusion_readiness``)
-----------------------------------------------
``FUSION_MODEL_PROVIDER``, ``FUSION_MODEL_NAME``, ``FUSION_MODEL_BASE_URL``,
``FUSION_MODEL_API_KEY`` (falling back to ``DEEPSEEK_*``) select the endpoint.
``FUSION_TIMEOUT_SECONDS``, ``FUSION_TRANSPORT_RETRIES``,
``FUSION_MODEL_TEMPERATURE`` and ``FUSION_MAX_SIGNALS_PER_SOURCE`` shape the request.
An empty model name, a missing key or a placeholder key means "not ready": fusion
returns an error state naming exactly what is unset and makes no request.

Deliberate decisions
--------------------
* **Pipeline-owned fields are never taken from the model.** ``assessment_id``,
  ``status``, ``schema_version``, ``origin``, ``destination``, ``travel_date``,
  ``sources``, ``missing_credentials`` and ``generated_at`` are derived from the
  envelopes, the clock and the pipeline itself. A model cannot be the authority on
  what a source returned; that is evidence. If the model returns them anyway they are
  discarded, not trusted.
* **``confidence_label`` is derived, not requested.** It is banded from the model's
  numeric ``confidence`` (< 0.40 low, 0.40-0.69 medium, >= 0.70 high) so the label
  cannot contradict the number. The prompt states the same bands.
* **One retry, transport errors only.** A retried generation is a *different*
  assessment, so a malformed or invalid answer is an error state -- never silently
  re-rolled, never parsed leniently (no code-fence stripping, no partial recovery).
* **Prompt size is capped per source and the cut is disclosed.** Signals are ranked
  by severity then distance to the airport, so the cap keeps the *most relevant*
  signals rather than an arbitrary slice. Each source block carries
  ``signals_included`` and ``signals_excluded_by_cap`` (both always present, even
  when zero), the prompt says how many were withheld, and the response repeats it in
  ``uncertainty``. The reason for the cut is the prompt budget -- never a source
  limitation -- and it must be reported as such, because a silent truncation would
  read as "nothing else happened".
* **Evidence integrity is enforced after parsing.** Every ``evidence_id`` in the
  output must exist in the input, must belong to the source it is attributed to, and
  every risk must cite at least one. ``road_class="unclassified"`` and
  ``severity=null`` mean *unknown*, never *minor*, and the prompt says so.
* **The whole exchange is logged before parsing.** Prompt and raw body go to
  ``logs/raw/fusion_<timestamp>_<id>.json`` (``.clinerules`` rule 5). If the record
  cannot be written, the assessment is an error state: untraceable reasoning is not
  reported as a result.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import json
import logging
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import httpx
from pydantic import ValidationError

import config
from backend.models import AssessmentResponse, SourceStatus
from data_sources import base

__all__ = [
    "FUSION_ERROR_CODES",
    "FusionExchange",
    "FusionResult",
    "fuse",
    "fuse_with_trace",
]

logger = logging.getLogger(__name__)

FUSION_LOG_SOURCE: str = "fusion"

#: ``AssessmentResponse`` fields the pipeline owns; the model must not author them.
_PIPELINE_OWNED_FIELDS: Tuple[str, ...] = (
    "assessment_id",
    "status",
    "schema_version",
    "origin",
    "destination",
    "travel_date",
    "sources",
    "missing_credentials",
    "generated_at",
    "confidence_label",
    "fusion_error_code",
)

#: Why fusion produced no assessment, as stable codes on
#: ``AssessmentResponse.fusion_error_code`` (``None`` means fusion succeeded).
#:
#: The classification happens *here* because this is the only layer that knows the
#: cause: at the provider seam the HTTP status and the transport exception type are
#: still structured values, whereas a caller downstream would have to match prose to
#: tell a 429 from a 500. Renaming a code is a contract change for the frontend.
FUSION_ERROR_CODES: Tuple[str, ...] = (
    "not_ready",  # model name/key missing or placeholder; no request made
    "no_signals",  # nothing usable to reason over; no request made
    "no_evidence",  # signals carried no citable evidence ids; no request made
    "raw_logging_failed",  # the prompt/response record could not be written
    "unexpected_error",  # fusion itself raised before a verdict
    "timeout",  # the provider did not answer before the timeout, twice
    "transport_error",  # another transport failure (DNS, refused, reset)
    "rate_limited",  # the provider answered 429
    "provider_error",  # the provider answered any other non-200 status
    "invalid_response",  # 200, but unparseable / incomplete / invalid / contradictory
)

#: Fields the model must return. Presence is checked before validation so a model that
#: silently omits a section is an error instead of a partially-filled answer.
_MODEL_OWNED_FIELDS: Tuple[str, ...] = (
    "risk_level",
    "confidence",
    "summary",
    "risks",
    "recommended_actions",
    "uncertainty",
)

_SEVERITY_RANK: Dict[str, int] = {
    "severe": 0,
    "high": 1,
    "moderate": 2,
    "low": 3,
    "unknown": 4,
}

#: Confidence bands for the derived ``confidence_label``. Stated in the prompt as well,
#: so the model can aim its number at the label the pipeline will print.
_CONFIDENCE_BANDS: Tuple[Tuple[float, str], ...] = (
    (0.40, "low"),
    (0.70, "medium"),
    (1.01, "high"),
)

#: Metrics kept in the envelope and the evidence trail but *not* sent to the model: they
#: explain how we graded a signal rather than what happened, and together they were the
#: bulk of a 147-incident prompt.
_PROMPT_OMIT_METRIC_KEYS: Tuple[str, ...] = (
    "road_class_basis",
    "vendor_incident_id",
    "severity_source",
    "magnitude_label",
    "delay_seconds",
    "radius_km",
    "date_window",
    "travel_date",
)

#: Aviationstack cohort context, hoisted into one ``sample`` block per source instead of
#: being repeated on all 100 flight signals.
_SAMPLE_METRIC_KEYS: Tuple[str, ...] = (
    "sample_size",
    "returned_records",
    "total_available",
    "page_coverage",
    "sample_is_thin",
)

#: Caps that keep one noisy source from crowding out the others in the prompt.
_SUMMARY_MAX_CHARS: int = 300
_METRIC_STRING_MAX_CHARS: int = 200
_LIST_METRIC_MAX_ITEMS: int = 5
_WARNINGS_MAX_ITEMS: int = 4


# --------------------------------------------------------------------------- #
# Confidence labelling (derived by the pipeline, never authored by the model)
# --------------------------------------------------------------------------- #
def _confidence_label(score: Optional[float]) -> Optional[str]:
    """Band a numeric confidence onto ``low`` / ``medium`` / ``high``.

    The label is derived here rather than requested from the model so the printed
    label can never contradict the printed number.
    """
    if score is None:
        return None
    for ceiling, label in _CONFIDENCE_BANDS:
        if score < ceiling:
            return label
    return _CONFIDENCE_BANDS[-1][1]  # pragma: no cover - last band is open-ended


# --------------------------------------------------------------------------- #
# Prompt input: compact projection of the envelopes
# --------------------------------------------------------------------------- #
def _severity_rank(signal: Mapping[str, Any]) -> int:
    """Sort key for ``None`` severities: ungraded ranks below every known band."""
    severity = signal.get("severity")
    if isinstance(severity, str) and severity in _SEVERITY_RANK:
        return _SEVERITY_RANK[severity]
    return len(_SEVERITY_RANK)


def _distance_km(signal: Mapping[str, Any]) -> float:
    """``distance_km`` from a signal, or ``inf`` when the source does not say."""
    value = (signal.get("metrics") or {}).get("distance_km")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return float("inf")
    return float(value)


def _select_signals(signals: Sequence[Any]) -> Tuple[List[Any], int]:
    """Return ``(kept, omitted_count)`` for one source, ranked by relevance.

    Severity first, then distance to the airport: an incident two kilometres from the
    terminal outranks an equally graded one fifteen kilometres away, and an ungraded
    incident still beats nothing. Ties keep the source's own order, so the projection
    is deterministic.
    """
    limit = config.FUSION_MAX_SIGNALS_PER_SOURCE
    ordered = sorted(signals, key=lambda s: (_severity_rank(s), _distance_km(s)))
    if len(ordered) <= limit:
        return ordered, 0
    return ordered[:limit], len(ordered) - limit


def _compact_metrics(metrics: Mapping[str, Any]) -> Dict[str, Any]:
    """Keep the metric fields a reasoning model can use, drop the audit-only ones."""
    compact: Dict[str, Any] = {}
    for key, value in metrics.items():
        if key in _PROMPT_OMIT_METRIC_KEYS or key in _SAMPLE_METRIC_KEYS:
            continue
        if value is None:
            continue
        if isinstance(value, bool) or isinstance(value, (int, float)):
            compact[key] = value
        elif isinstance(value, str):
            if value.strip():
                compact[key] = base.truncate_text(value, _METRIC_STRING_MAX_CHARS)
        elif isinstance(value, (list, tuple)):
            if not value or len(value) > _LIST_METRIC_MAX_ITEMS:
                continue
            if any(isinstance(item, (list, tuple, dict)) for item in value):
                continue
            compact[key] = [
                base.truncate_text(str(item), _METRIC_STRING_MAX_CHARS)
                for item in value
                if item is not None
            ]
        # Nested objects are deliberately dropped: no source uses one in a way the
        # model needs, and they inflate the prompt faster than anything else.
    return compact


def _compact_signal(signal: Mapping[str, Any]) -> Dict[str, Any]:
    """One signal as the model sees it: the sentence, the grade, the facts, the ids."""
    compact: Dict[str, Any] = {
        "kind": signal.get("kind"),
        "severity": signal.get("severity"),
        "confidence": signal.get("confidence"),
        "summary": base.truncate_text(
            str(signal.get("summary") or ""), _SUMMARY_MAX_CHARS
        ),
        "metrics": _compact_metrics(signal.get("metrics") or {}),
        "evidence_ids": list(signal.get("evidence_ids") or ()),
    }
    if signal.get("effective_at"):
        compact["effective_at"] = signal["effective_at"]
    return compact


def _sample_block(signals: Sequence[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """The flight-cohort context that applies to every signal from one source.

    Aviationstack adds ``sample_size`` / ``total_available`` / ``page_coverage`` to each
    flight signal; hoisting them keeps the prompt readable and puts the "9 of 173"
    caveat next to the source status instead of burying it in the first flight.
    """
    for signal in signals:
        metrics = signal.get("metrics") or {}
        sample = {
            key: metrics[key]
            for key in _SAMPLE_METRIC_KEYS
            if metrics.get(key) is not None
        }
        if sample:
            return sample
    return None


def _source_block(envelope: Mapping[str, Any]) -> Dict[str, Any]:
    """One source as the model sees it: status first, then the signals themselves.

    ``status`` + ``error_code`` come before the signals on purpose: "could not check"
    has to be visible before the model reads (or fails to read) findings, otherwise an
    empty list reads as "nothing happened".
    """
    all_signals: Sequence[Mapping[str, Any]] = envelope.get("signals") or ()
    kept, omitted = _select_signals(all_signals)
    error_codes = list(envelope.get("error_codes") or ())
    if not error_codes and envelope.get("error_code"):
        error_codes = [envelope["error_code"]]
    block: Dict[str, Any] = {
        "source": envelope.get("source"),
        "status": envelope.get("status"),
        "records": envelope.get("records"),
        "error": envelope.get("error"),
        "error_codes": error_codes,
        "consulted_at": envelope.get("retrieved_at"),
        "signal_count": len(all_signals),
        "signals_included": len(kept),
        # Always present, even when zero: the model must be able to say "3 of 147"
        # rather than infer completeness from a missing key.
        "signals_excluded_by_cap": omitted,
    }
    sample = _sample_block(all_signals)
    if sample:
        block["sample"] = sample
    warnings = [
        base.truncate_text(str(warning), _METRIC_STRING_MAX_CHARS)
        for warning in (envelope.get("warnings") or ())[:_WARNINGS_MAX_ITEMS]
    ]
    if warnings:
        block["warnings"] = warnings
    block["signals"] = [_compact_signal(signal) for signal in kept]
    return block


def _days_from_now(travel_date: datetime.date, today: datetime.date) -> int:
    """Whole days between ``today`` and ``travel_date`` (negative for the past)."""
    return (travel_date - today).days


def _prompt_schema() -> Dict[str, Any]:
    """The JSON Schema the model must satisfy: model-owned fields only.

    Generated from ``AssessmentResponse`` so the prompt cannot drift from the model it
    is validated against, then trimmed of pipeline-owned properties (the pipeline sets
    those and would overwrite anything the model wrote).
    """
    schema = AssessmentResponse.model_json_schema()
    schema["properties"] = {
        name: definition
        for name, definition in schema.get("properties", {}).items()
        if name in _MODEL_OWNED_FIELDS
    }
    # ``AssessmentResponse`` gives most fields defaults, but the pipeline treats every
    # model-owned key as required: a silently omitted section is a different failure
    # from an empty one, and the model should know which keys it must return.
    schema["required"] = list(_MODEL_OWNED_FIELDS)
    # Drop definitions no remaining property references (e.g. ``SourceStatus``).
    rendered = json.dumps(schema.get("properties", {}))
    for def_name in list(schema.get("$defs", {})):
        if def_name in rendered:
            continue
        schema["$defs"].pop(def_name)
    if not schema.get("$defs"):
        schema.pop("$defs", None)
    return schema


SYSTEM_PROMPT: str = """You are the signal-fusion layer of a travel disruption assessor. Each request \
gives you one airline route, one travel date, and the normalized signals that live data \
sources returned for it. You return one JSON object: an assessment a traveller can act \
on and an engineer can audit back to the raw vendor records.

Your value is judgement, not summarization. Follow these rules.

1. Relevance over raw severity. A vendor severity label is not a trip impact. For every
   risk you report, state why it matters for THIS route and THIS date, using the facts in
   the signal:
   - Road incidents: distance_km, place, from_place/to_place, road_class, road_numbers,
     on_numbered_route, magnitude_of_delay, event_descriptions, start_time/end_time.
     A closure on the road that feeds the terminal is not the same as a closure on a side
     street 12 km away. road_class "unclassified" means the vendor published no road
     class: it is UNKNOWN, never minor. severity null means ungraded, never harmless.
     metrics.scope says which airport ("origin" or "destination") an incident sits near.
   - Flights: delay_minutes against the scheduled block time, cancelled, diverted,
     departure vs arrival delay, and the cohort context in "sample".
   - Weather: airports, period_name, travel_date_overlap, short_forecast, wind_max_mph,
     precipitation_probability, and any active alert.
   Do not report a risk because its label is high; report it because of what it does to
   this trip.

2. "Checked and clear" is evidence; "could not check" is not. A source with status "ok"
   and a clear result tells you the conditions were actually checked. A source with
   status "unavailable" or "error" (or codes such as no_data_for_query, plan_limit,
   timeout, missing_credential) tells you nothing about conditions: that is missing
   evidence, not good news. "partial" means real signals for part of the route and
   silence for the rest. Reflect this in what you say and in your confidence.

3. Only claim what a signal supports. Every risk must cite at least one evidence id from
   the input, copied verbatim, and that evidence must actually support the claim. If no
   signal supports a risk, do not report it. Never fill a gap with general knowledge
   about airports, airlines, weather or traffic: the reader will assume every statement
   is backed by the input. Never invent evidence ids or source names.

4. Be explicit about what you do not know. "uncertainty" holds the limitations you
   relied on: sources that were missing, partial or errored; thin or partial samples;
   real-time-only or stale data; signals that conflict; a travel date the sources cannot
   cover; anything you had to assume.

5. Calibrate. "confidence" is your confidence in the overall risk_level, from 0.0 to 1.0.
   Lower it when a source is unavailable or errored, when a sample is thin or coverage is
   partial, when signals disagree, when the travel date is far from now, or when the only
   support is a single weak signal. Raise it when independent sources agree and coverage
   is complete. The pipeline derives confidence_label from your number with these bands:
   below 0.40 = low, 0.40 to 0.69 = medium, 0.70 and above = high. State the main driver
   of your confidence in "summary".

Output contract: return exactly one JSON object, with no prose, no markdown and no code
fences. Required keys:
- "risk_level": one of low, moderate, high, severe, unknown. Use "unknown" only when the
  evidence cannot support any level.
- "confidence": a number from 0.0 to 1.0.
- "summary": two to four plain sentences covering what matters for this trip, which
  sources were clear versus unavailable, and why your confidence is what it is.
- "risks": one entry per risk that matters for this trip. Each entry needs "risk",
  "severity" (one of the levels above), "confidence", and "evidence" with at least one
  object {"source", "evidence_id", "field", "value"} taken from the input. A risk with no
  supporting signal is an error, not a cautious guess.
- "recommended_actions": concrete, ordered actions tied to the risks you reported.
- "uncertainty": short strings, as described above.
Do not return status, schema_version, origin, destination, travel_date, sources,
missing_credentials, generated_at or confidence_label: the pipeline derives those from
the envelopes themselves and overwrites anything you write there."""


def _user_prompt(
    envelopes: Sequence[Mapping[str, Any]],
    location: base.Location,
    travel_date: datetime.date,
    *,
    today: Optional[datetime.date] = None,
) -> str:
    """Render the user message: route, source envelopes, schema, and the rules."""
    now = today or datetime.date.today()
    blocks = [_source_block(envelope) for envelope in envelopes]
    payload = {
        "route": {
            "origin": location.origin.iata,
            "destination": location.destination.iata,
            "travel_date": travel_date.isoformat(),
            "days_from_now": _days_from_now(travel_date, now),
        },
        "sources": blocks,
    }

    omitted = [
        "  {0}: {1} of {2} signals shown ({3} withheld, ranked by severity then "
        "distance)".format(
            block["source"],
            block["signals_included"],
            block["signal_count"],
            block["signals_excluded_by_cap"],
        )
        for block in blocks
        if block.get("signals_excluded_by_cap")
    ]
    omitted_note = ""
    if omitted:
        omitted_note = (
            "\nPROMPT SIZE LIMIT (disclosed, not silent):\n"
            + "\n".join(omitted)
            + "\nThose withheld signals are real but were not shown to you. Do not claim "
            "anything about them and do not treat their absence as calm; if it matters, "
            "say the picture is incomplete.\n"
        )

    return (
        "ROUTE: {origin} -> {destination}\n"
        "TRAVEL DATE: {date} ({days} days from today, {today} UTC)\n"
        "SOURCES: {sources}\n\n"
        "INPUT (JSON) -- the complete evidence set. Each source carries its own status, "
        "error codes, records and signals; each signal carries a summary, its mapped "
        "severity (null = ungraded), the source's confidence, the relevance metrics, and "
        "the evidence ids you must cite.\n"
        "{payload}\n"
        "{omitted}\n"
        "RETURN ONE JSON OBJECT MATCHING THIS SCHEMA (JSON Schema, model-owned fields "
        "only):\n"
        "{schema}\n\n"
        "Final rules: cite only evidence ids present in the input above, give every risk "
        "at least one evidence entry, keep every severity inside the allowed levels, and "
        "return nothing but the JSON object -- no markdown, no code fences, no commentary."
    ).format(
        origin=location.origin.iata,
        destination=location.destination.iata,
        date=travel_date.isoformat(),
        days=_days_from_now(travel_date, now),
        today=now.isoformat(),
        sources=", ".join(str(block["source"]) for block in blocks) or "none",
        payload=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        omitted=omitted_note,
        schema=json.dumps(_prompt_schema(), ensure_ascii=False, separators=(",", ":")),
    )


def build_messages(
    envelopes: Sequence[Mapping[str, Any]],
    location: base.Location,
    travel_date: datetime.date,
    *,
    today: Optional[datetime.date] = None,
) -> List[Dict[str, str]]:
    """The exact ``messages`` array sent to the model (exposed for tests and audit)."""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": _user_prompt(envelopes, location, travel_date, today=today),
        },
    ]


# --------------------------------------------------------------------------- #
# Model call: one HTTP attempt at a time, every attempt logged before parsing
# --------------------------------------------------------------------------- #
class _RawLoggingFailed(RuntimeError):
    """The prompt/response record could not be written, so nothing may be parsed."""


@dataclasses.dataclass(frozen=True)
class _Attempt:
    """What one ``POST`` produced: an answer, or the error that stopped it."""

    status_code: Optional[int]
    body_text: Optional[str]
    latency_ms: float
    transport_error: Optional[str]
    transport_error_message: Optional[str]


@dataclasses.dataclass(frozen=True)
class _Exchange:
    """The outcome of a call, including the evidence records written for it."""

    status_code: Optional[int]
    body_text: Optional[str]
    latency_ms: float
    attempts: int
    raw_paths: Tuple[str, ...]
    transport_error: Optional[str]
    transport_error_message: Optional[str]


#: Token counters read from the provider's ``usage`` block. Every one is optional:
#: a missing counter means the provider did not report it, never that it was zero.
_USAGE_TOKEN_KEYS: Tuple[str, ...] = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
)


@dataclasses.dataclass(frozen=True)
class FusionExchange:
    """The exact prompt that was sent and the raw answer that came back.

    ``usage`` holds only counters the provider actually reported (Groq and OpenAI
    put them under ``usage``; reasoning tokens may sit under
    ``usage.completion_tokens_details.reasoning_tokens``). Nothing is estimated here:
    a missing number stays missing, because an invented token count would misreport
    what the call cost.
    """

    model: str
    prompt_messages: Tuple[Dict[str, str], ...]
    status_code: Optional[int]
    response_body: Optional[str]
    latency_ms: float
    attempts: int
    raw_response_paths: Tuple[str, ...]
    transport_error: Optional[str]
    usage: Dict[str, Any]


@dataclasses.dataclass(frozen=True)
class FusionResult:
    """The assessment plus the exchange behind it (``None`` when no call was made)."""

    assessment: AssessmentResponse
    exchange: Optional[FusionExchange]


def _usage_from_body(body_text: Optional[str]) -> Dict[str, Any]:
    """Extract the provider's reported token counters from a completion body.

    Read-only and total: a body that is not JSON, not an object, or has no
    ``usage`` block yields an empty dict rather than an exception or a guess.
    """
    usage: Dict[str, Any] = {}
    if not body_text:
        return usage
    try:
        body = json.loads(body_text)
    except json.JSONDecodeError:
        return usage
    if not isinstance(body, dict):
        return usage
    raw = body.get("usage")
    if not isinstance(raw, dict):
        return usage
    for key in _USAGE_TOKEN_KEYS:
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            usage[key] = value
    reasoning = raw.get("reasoning_tokens")
    details = raw.get("completion_tokens_details")
    if reasoning is None and isinstance(details, dict):
        reasoning = details.get("reasoning_tokens")
    if isinstance(reasoning, int) and not isinstance(reasoning, bool):
        usage["reasoning_tokens"] = reasoning
    return usage


def _retry_delay_seconds(attempt_index: int) -> float:
    """Exponential backoff between transport retries, capped like ``base.get_json``."""
    delay = config.HTTP_BACKOFF_BASE_SECONDS * (2 ** max(0, attempt_index - 1))
    return max(0.0, min(delay, config.HTTP_BACKOFF_MAX_SECONDS))


async def _send_chat_completion(
    payload: Mapping[str, Any], *, url: str, api_key: str, timeout: float
) -> _Attempt:
    """Perform exactly one ``POST`` to the chat-completions endpoint.

    This is the only place a socket is opened, which makes it the seam the tests
    replace: everything else -- prompt construction, logging, retry policy, parsing,
    validation -- runs for real under test.
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer {0}".format(api_key),
    }
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=dict(payload), headers=headers)
    except httpx.HTTPError as exc:
        return _Attempt(
            status_code=None,
            body_text=None,
            latency_ms=round((time.perf_counter() - started) * 1000.0, 2),
            transport_error=type(exc).__name__,
            transport_error_message=base.redact_text(str(exc)),
        )
    return _Attempt(
        status_code=response.status_code,
        body_text=response.text,
        latency_ms=round((time.perf_counter() - started) * 1000.0, 2),
        transport_error=None,
        transport_error_message=None,
    )



def _exchange_record(
    *,
    payload: Mapping[str, Any],
    attempt: _Attempt,
    attempt_number: int,
    max_attempts: int,
    url: str,
    context: Mapping[str, Any],
) -> Dict[str, Any]:
    """Build the evidence record for one model call, prompt included.

    The prompt is stored verbatim because it *is* the evidence for the reasoning: a
    later reader must be able to see exactly what the model was asked and exactly what
    it answered. The API key is never part of the record.
    """
    messages = list(payload.get("messages") or ())
    prompt_chars = {
        str(message.get("role")): len(str(message.get("content") or ""))
        for message in messages
    }
    total_chars = sum(prompt_chars.values())
    return {
        "record_version": 1,
        "source": FUSION_LOG_SOURCE,
        "retrieved_at": base.utc_now_iso(),
        "attempt": attempt_number,
        "max_attempts": max_attempts,
        "raw_logged_before_parsing": True,
        "request": {
            "method": "POST",
            "url": url,
            "headers": {
                "Content-Type": "application/json",
                # Written literally: the bearer value must never reach disk.
                "Authorization": "Bearer <redacted>",
            },
            "provider": config.FUSION_MODEL_PROVIDER,
            "model": payload.get("model"),
            "temperature": payload.get("temperature"),
            "response_format": payload.get("response_format"),
            "messages": messages,
            "prompt_chars": prompt_chars,
            "prompt_chars_total": total_chars,
            # Rough and provider-independent (~4 characters per token). Recorded so
            # prompt growth is visible across runs, not to predict billing.
            "prompt_estimated_tokens": int(total_chars / 4),
        },
        "response": None
        if attempt.body_text is None
        else {
            "status_code": attempt.status_code,
            "body_text": attempt.body_text,
            "body_chars": len(attempt.body_text),
            "body_sha256": base.sha256_text(attempt.body_text),
        },
        "error": None
        if attempt.transport_error is None
        else {
            "type": attempt.transport_error,
            "message": attempt.transport_error_message,
        },
        "latency_ms": attempt.latency_ms,
        "context": dict(context),
    }


def _log_exchange(
    *,
    payload: Mapping[str, Any],
    attempt: _Attempt,
    attempt_number: int,
    max_attempts: int,
    url: str,
    context: Mapping[str, Any],
) -> str:
    """Persist one attempt's prompt + raw body, returning its stored path."""
    record = _exchange_record(
        payload=payload,
        attempt=attempt,
        attempt_number=attempt_number,
        max_attempts=max_attempts,
        url=url,
        context=context,
    )
    try:
        return base.write_raw_record(source=FUSION_LOG_SOURCE, record=record)
    except Exception as exc:  # noqa: BLE001 - any failure here must stop the parse
        raise _RawLoggingFailed(
            "The fusion prompt/response record could not be written ({0}: {1}); the "
            "assessment would not be traceable, so none is returned. .clinerules rule 5 "
            "requires logging before parsing.".format(
                type(exc).__name__, base.redact_text(str(exc))
            )
        ) from exc



async def _call_model(
    messages: Sequence[Mapping[str, str]],
    *,
    url: str,
    timeout: float,
    api_key: str,
    context: Mapping[str, Any],
) -> _Exchange:
    """POST the prompt, retrying once on a transport error and never on content.

    A retry is only spent when no answer arrived (connection reset, timeout, DNS). A
    bad answer is returned as-is for the caller to reject: re-generating until the
    output validates would be report-shopping, and the second answer would not be the
    assessment of record anyway.
    """
    payload: Dict[str, Any] = {
        "model": config.FUSION_MODEL_NAME,
        "messages": list(messages),
        "temperature": config.FUSION_MODEL_TEMPERATURE,
        # Ask for JSON at the provider level. Not every OpenAI-compatible server honours
        # it, so the parser stays strict regardless; this only reduces wasted calls.
        "response_format": {"type": "json_object"},
    }
    max_attempts = 1 + config.FUSION_TRANSPORT_RETRIES
    raw_paths: List[str] = []

    for attempt_number in range(1, max_attempts + 1):
        attempt = await _send_chat_completion(
            payload, url=url, api_key=api_key, timeout=timeout
        )
        raw_paths.append(
            _log_exchange(
                payload=payload,
                attempt=attempt,
                attempt_number=attempt_number,
                max_attempts=max_attempts,
                url=url,
                context=context,
            )
        )
        if attempt.transport_error is None:
            return _Exchange(
                status_code=attempt.status_code,
                body_text=attempt.body_text,
                latency_ms=attempt.latency_ms,
                attempts=attempt_number,
                raw_paths=tuple(raw_paths),
                transport_error=None,
                transport_error_message=None,
            )
        if attempt_number < max_attempts:
            delay = _retry_delay_seconds(attempt_number)
            logger.warning(
                "fusion: %s on attempt %d/%d (%s); retrying in %.2fs",
                attempt.transport_error,
                attempt_number,
                max_attempts,
                attempt.transport_error_message,
                delay,
            )
            await asyncio.sleep(delay)
            continue
        logger.error(
            "fusion: %s on attempt %d/%d; giving up",
            attempt.transport_error,
            attempt_number,
            max_attempts,
        )
        return _Exchange(
            status_code=None,
            body_text=None,
            latency_ms=attempt.latency_ms,
            attempts=attempt_number,
            raw_paths=tuple(raw_paths),
            transport_error=attempt.transport_error,
            transport_error_message=attempt.transport_error_message,
        )

    raise AssertionError(  # pragma: no cover - loop always returns
        "unreachable: the attempt loop returns on its last iteration"
    )


# --------------------------------------------------------------------------- #
# Parsing and validation: strict, with every rejection explained
# --------------------------------------------------------------------------- #
def _model_content(body_text: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(content, problem)`` from a chat-completions response body.

    Only ``choices[0].message.content`` is accepted. A provider that puts the answer in
    ``reasoning_content``, a tool call, or a bare top-level field has not followed the
    contract, and guessing would make the recorded evidence disagree with what was used.
    """
    if not body_text:
        return None, "the fusion endpoint returned an empty body"
    try:
        body = json.loads(body_text)
    except json.JSONDecodeError as exc:
        return None, "the fusion endpoint returned a body that is not JSON: {0}".format(
            exc
        )
    if not isinstance(body, dict):
        return None, "the fusion endpoint returned JSON that is not an object"
    if body.get("error"):
        return None, "the fusion endpoint returned an error object: {0}".format(
            base.truncate_text(base.redact_text(json.dumps(body["error"])), 300)
        )
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return None, "the fusion response had no 'choices' array"
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return None, "the fusion response had no 'choices[0].message' object"
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return None, "the fusion response had no text in 'choices[0].message.content'"
    return content, None


def _parse_json_object(content: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Parse the model's content as a JSON object, strictly.

    No code-fence stripping and no substring extraction: if the model wraps the object
    in prose or markdown, the answer does not meet the contract and the caller reports
    an error. Lenient parsing here would silently accept a model that ignored the output
    format, which is exactly the failure mode the strict schema is meant to expose.
    """
    text = content.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, (
            "the fusion model returned content that is not JSON ({0} at char {1}): "
            "{2}".format(exc.msg, exc.pos, base.truncate_text(text, 200))
        )
    if not isinstance(parsed, dict):
        return None, "the fusion model returned JSON that is not an object"
    return parsed, None



# --------------------------------------------------------------------------- #
# Evidence integrity and semantic checks (things Pydantic cannot know about)
# --------------------------------------------------------------------------- #
#: Levels that assert a real, actionable problem. Reporting one of these with nothing
#: cited is the contradiction the brief calls out ("severe" with zero risks).
_ESCALATED_LEVELS: Tuple[str, ...] = ("moderate", "high", "severe")


def _evidence_index(envelopes: Sequence[Mapping[str, Any]]) -> Dict[str, str]:
    """Map every citable evidence id to the source that produced it."""
    index: Dict[str, str] = {}
    for envelope in envelopes:
        source = str(envelope.get("source") or "")
        for entry in envelope.get("evidence") or ():
            if not isinstance(entry, Mapping):
                continue
            evidence_id = entry.get("id")
            if not evidence_id:
                continue
            index[str(evidence_id)] = str(entry.get("source") or source)
    return index


def _semantic_problems(
    assessment: AssessmentResponse, evidence_index: Mapping[str, str]
) -> List[str]:
    """Reasons the assessment is internally inconsistent or unsupported.

    Pydantic checks shape; these check meaning: a real risk level needs cited risks, and
    a cited evidence id must exist in the input. An invented id is an error, never
    silently dropped -- dropping it would leave a claim whose support we quietly removed.
    """
    problems: List[str] = []
    level = assessment.risk_level
    if level is not None and level not in config.RISK_LEVELS:
        problems.append(
            "risk_level {0!r} is not one of: {1}".format(
                level, ", ".join(config.RISK_LEVELS)
            )
        )
    if level in _ESCALATED_LEVELS and not assessment.risks:
        problems.append(
            "risk_level {0!r} was reported with no cited risks".format(level)
        )
    if level == "unknown" and assessment.risks:
        problems.append(
            "risk_level 'unknown' was reported alongside {0} risk(s)".format(
                len(assessment.risks)
            )
        )
    for position, risk in enumerate(assessment.risks, start=1):
        label = "risk #{0} ({1!r})".format(position, base.truncate_text(risk.risk, 80))
        if risk.severity not in config.RISK_LEVELS:
            problems.append(
                "{0}: severity {1!r} is not one of: {2}".format(
                    label, risk.severity, ", ".join(config.RISK_LEVELS)
                )
            )
        if not risk.evidence:
            problems.append("{0}: no evidence cited".format(label))
        for reference in risk.evidence:
            evidence_id = reference.evidence_id
            if not evidence_id:
                problems.append(
                    "{0}: an evidence entry had no evidence_id".format(label)
                )
                continue
            owner = evidence_index.get(evidence_id)
            if owner is None:
                problems.append(
                    "{0}: evidence_id {1!r} does not exist in the input".format(
                        label, evidence_id
                    )
                )
            elif reference.source and reference.source != owner:
                problems.append(
                    "{0}: evidence_id {1!r} belongs to {2!r} but was attributed to "
                    "{3!r}".format(label, evidence_id, owner, reference.source)
                )
    return problems



def _overall_status(sources: Sequence[SourceStatus]) -> str:
    """``ok`` only when every source came back clean; otherwise ``partial``."""
    if not sources:
        return config.STATUS_ERROR
    if all(source.status == config.STATUS_OK for source in sources):
        return config.STATUS_OK
    return config.STATUS_PARTIAL


def _chat_completions_url() -> str:
    """``{base_url}/chat/completions`` for the configured OpenAI-compatible endpoint."""
    return "{0}/chat/completions".format(config.FUSION_MODEL_BASE_URL.rstrip("/"))


def _transport_error_code(transport_error: Optional[str]) -> str:
    """Stable code for a transport failure, from the exception type name.

    ``httpx`` names every timeout ``*Timeout`` (``ReadTimeout``, ``ConnectTimeout``,
    ``PoolTimeout``, ``WriteTimeout``), so the type name -- not the message -- is what
    separates "the provider was too slow" from "the provider could not be reached".
    """
    return "timeout" if "Timeout" in (transport_error or "") else "transport_error"


def _error_response(
    *,
    location: base.Location,
    travel_date: datetime.date,
    summary: str,
    uncertainty: Sequence[str],
    error_code: str,
    sources: Sequence[SourceStatus] = (),
) -> AssessmentResponse:
    """A well-formed ``status="error"`` response: valid shape, no invented content.

    ``error_code`` is required so no failure path can be added without saying in one
    stable word *why* it failed; the frontend branches on that instead of on prose.
    """
    if error_code not in FUSION_ERROR_CODES:
        raise ValueError(
            "unknown fusion error code {0!r}; expected one of: {1}".format(
                error_code, ", ".join(FUSION_ERROR_CODES)
            )
        )
    return AssessmentResponse(
        status=config.STATUS_ERROR,
        fusion_error_code=error_code,
        origin=location.origin.iata,
        destination=location.destination.iata,
        travel_date=travel_date.isoformat(),
        risk_level="unknown",
        confidence=None,
        confidence_label=None,
        summary=summary,
        risks=[],
        recommended_actions=[],
        uncertainty=[text for text in uncertainty if text],
        sources=list(sources),
        missing_credentials=config.missing_credentials(),
        generated_at=base.utc_now_iso(),
    )


def _require_arguments(
    signals: Sequence[Any], location: base.Location, travel_date: datetime.date
) -> None:
    """Reject a programming error in the arguments (a data problem is handled later)."""
    if not isinstance(location, base.Location):
        raise ValueError(
            "location must be a data_sources.base.Location, got {0}".format(
                type(location).__name__
            )
        )
    if isinstance(travel_date, datetime.datetime) or not isinstance(
        travel_date, datetime.date
    ):
        raise ValueError(
            "travel_date must be a datetime.date, got {0}".format(
                type(travel_date).__name__
            )
        )
    if isinstance(signals, (str, bytes, Mapping)) or not isinstance(
        signals, (list, tuple)
    ):
        raise ValueError(
            "signals must be a sequence of NormalizedSignals envelopes, got {0}".format(
                type(signals).__name__
            )
        )
    for position, envelope in enumerate(signals, start=1):
        if not isinstance(envelope, Mapping):
            raise ValueError(
                "signals[{0}] is not a NormalizedSignals envelope ({1})".format(
                    position, type(envelope).__name__
                )
            )
        if not envelope.get("source"):
            raise ValueError("signals[{0}] has no 'source' id".format(position))



def _truncation_notes(envelopes: Sequence[Mapping[str, Any]]) -> List[str]:
    """Disclose, in the response itself, any signal the prompt did not carry.

    The prompt says this too, but the prompt is not what the reader sees: without this
    note a capped source would look like a complete one, and "we did not look" would
    read as "nothing there".
    """
    notes: List[str] = []
    for envelope in envelopes:
        all_signals = list(envelope.get("signals") or ())
        kept, omitted = _select_signals(all_signals)
        if not omitted:
            continue
        raw_paths = list(envelope.get("raw_response_paths") or ())
        if not raw_paths and envelope.get("raw_response_path"):
            raw_paths = [envelope["raw_response_path"]]
        notes.append(
            "{0}: {1} of {2} signals were included in the assessment (the other {3} "
            "were withheld for prompt size, ranked by severity then distance, and are "
            "not reflected here). Raw records: {4}".format(
                envelope.get("source"),
                len(kept),
                len(all_signals),
                omitted,
                ", ".join(raw_paths) if raw_paths else "not recorded",
            )
        )
    return notes


def _source_context(envelopes: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Compact per-source context for the evidence record (not sent to the model)."""
    context: List[Dict[str, Any]] = []
    for envelope in envelopes:
        all_signals = list(envelope.get("signals") or ())
        kept, omitted = _select_signals(all_signals)
        context.append(
            {
                "source": envelope.get("source"),
                "status": envelope.get("status"),
                "error_code": envelope.get("error_code"),
                "signals": len(all_signals),
                "signals_included": len(kept),
                "signals_excluded_by_cap": omitted,
                "evidence": len(list(envelope.get("evidence") or ())),
            }
        )
    return context



async def _run_fusion(
    signals: Sequence[Mapping[str, Any]],
    location: base.Location,
    travel_date: datetime.date,
    trace: List[FusionExchange],
) -> AssessmentResponse:
    """Fuse the given source envelopes into one validated :class:`AssessmentResponse`.

    ``signals`` is one ``NormalizedSignals`` envelope per source (``aviationstack``,
    ``nws``, ``tomtom``, and any future source) exactly as the fetchers returned it.
    Fusion does not fetch anything itself and does not persist the result.

    ``trace`` is an out-parameter: when a model call was made, its exact prompt,
    raw body and reported usage are appended to it (even if the answer is then
    rejected), so :func:`fuse_with_trace` can hand them to the caller. It is a list
    rather than a return value so the many early error returns below stay readable.

    Returns a valid assessment on success. On any runtime failure it returns
    ``status="error"`` with ``risk_level="unknown"`` and the reason in ``uncertainty``;
    it never raises for a data or model problem, and it never returns a fabricated
    assessment to fill a failed call.
    """
    _require_arguments(signals, location, travel_date)
    envelopes = list(signals)
    sources = [
        SourceStatus(**base.to_source_status(envelope)) for envelope in envelopes
    ]
    readiness = config.fusion_readiness()

    if not readiness["ready"]:
        return _error_response(
            location=location,
            travel_date=travel_date,
            sources=sources,
            error_code="not_ready",
            summary=(
                "The fusion model is not configured, so no assessment was produced. "
                "The signals were collected successfully; only the reasoning step is "
                "missing."
            ),
            uncertainty=[
                "Fusion is not ready: {0}".format(reason)
                for reason in readiness.get("reasons") or ["no reason was reported"]
            ],
        )

    signal_total = sum(
        len(list(envelope.get("signals") or ())) for envelope in envelopes
    )
    if signal_total == 0:
        degraded = [
            "{0}: {1}{2}{3}".format(
                envelope.get("source"),
                envelope.get("status"),
                " ({0})".format(envelope["error_code"])
                if envelope.get("error_code")
                else "",
                " - {0}".format(base.truncate_text(str(envelope["error"]), 200))
                if envelope.get("error")
                else "",
            )
            for envelope in envelopes
            if envelope.get("status") != config.STATUS_OK
        ]
        return _error_response(
            location=location,
            travel_date=travel_date,
            sources=sources,
            error_code="no_signals",
            summary=(
                "No source returned a usable signal, so there is nothing to assess. "
                "This is not a clear result: nothing was checked successfully."
            ),
            uncertainty=[
                "Every supplied source came back without usable signals; absent "
                "evidence is not evidence of calm.",
                *degraded,
            ],
        )

    evidence_index = _evidence_index(envelopes)
    if not evidence_index:
        return _error_response(
            location=location,
            travel_date=travel_date,
            sources=sources,
            error_code="no_evidence",
            summary=(
                "The signals carried no evidence ids, so no claim could be cited back "
                "to a source. No assessment was produced."
            ),
            uncertainty=[
                "The collected envelopes contain signals without evidence entries; "
                "that is a bug in the collecting layer, not a property of the sources.",
            ],
        )

    messages = build_messages(envelopes, location, travel_date)
    try:
        exchange = await _call_model(
            messages,
            url=_chat_completions_url(),
            timeout=config.FUSION_TIMEOUT_SECONDS,
            api_key=config.FUSION_MODEL_API_KEY or "",
            context={
                "origin": location.origin.iata,
                "destination": location.destination.iata,
                "travel_date": travel_date.isoformat(),
                "sources": _source_context(envelopes),
                "evidence_available": len(evidence_index),
            },
        )
    except _RawLoggingFailed as exc:
        return _error_response(
            location=location,
            travel_date=travel_date,
            sources=sources,
            error_code="raw_logging_failed",
            summary=(
                "The fusion call could not be recorded, so no assessment was returned: "
                "an untraceable assessment is not a deliverable."
            ),
            uncertainty=[str(exc)],
        )
    except Exception as exc:  # noqa: BLE001 - fusion must not raise on a model problem
        logger.exception("fusion: unexpected failure while calling the model")
        return _error_response(
            location=location,
            travel_date=travel_date,
            sources=sources,
            error_code="unexpected_error",
            summary=(
                "The fusion model call failed unexpectedly, so no assessment was "
                "produced."
            ),
            uncertainty=[
                "Fusion raised {0}: {1}".format(
                    type(exc).__name__, base.redact_text(str(exc))
                )
            ],
        )


    trace.append(
        FusionExchange(
            model=str(config.FUSION_MODEL_NAME),
            prompt_messages=tuple(dict(message) for message in messages),
            status_code=exchange.status_code,
            response_body=exchange.body_text,
            latency_ms=exchange.latency_ms,
            attempts=exchange.attempts,
            raw_response_paths=exchange.raw_paths,
            transport_error=exchange.transport_error,
            usage=_usage_from_body(exchange.body_text),
        )
    )

    if exchange.transport_error:
        return _error_response(
            location=location,
            travel_date=travel_date,
            sources=sources,
            error_code=_transport_error_code(exchange.transport_error),
            summary=(
                "The fusion model could not be reached, so no assessment was produced."
            ),
            uncertainty=[
                "Fusion transport error after {0} attempt(s): {1}: {2}".format(
                    exchange.attempts,
                    exchange.transport_error,
                    exchange.transport_error_message,
                ),
                "Prompt and error records: {0}".format(
                    ", ".join(exchange.raw_paths) or "none written"
                ),
            ],
        )

    if exchange.status_code != 200:
        preview = base.truncate_text(base.redact_text(exchange.body_text or ""), 300)
        return _error_response(
            location=location,
            travel_date=travel_date,
            sources=sources,
            # The provider's own status is the classification: 429 is a quota
            # answer, anything else non-200 is a provider failure.
            error_code=(
                "rate_limited" if exchange.status_code == 429 else "provider_error"
            ),
            summary=(
                "The fusion model returned HTTP {0}, so no assessment was "
                "produced.".format(exchange.status_code)
            ),
            uncertainty=[
                "Fusion endpoint answered {0}: {1}".format(
                    exchange.status_code, preview or "(empty body)"
                ),
                "Prompt and response record: {0}".format(
                    ", ".join(exchange.raw_paths) or "none written"
                ),
            ],
        )

    content, problem = _model_content(exchange.body_text)
    if problem:
        return _error_response(
            location=location,
            travel_date=travel_date,
            sources=sources,
            error_code="invalid_response",
            summary=(
                "The fusion model returned a response that could not be used, so no "
                "assessment was produced."
            ),
            uncertainty=[
                problem,
                "Prompt and response record: {0}".format(
                    ", ".join(exchange.raw_paths) or "none written"
                ),
            ],
        )

    parsed, problem = _parse_json_object(content or "")
    if problem:
        return _error_response(
            location=location,
            travel_date=travel_date,
            sources=sources,
            error_code="invalid_response",
            summary=(
                "The fusion model did not return a JSON object, so no assessment was "
                "produced. Returning prose here would hide the contract violation."
            ),
            uncertainty=[
                problem,
                "Prompt and response record: {0}".format(
                    ", ".join(exchange.raw_paths) or "none written"
                ),
            ],
        )

    missing = [name for name in _MODEL_OWNED_FIELDS if name not in parsed]
    if missing:
        return _error_response(
            location=location,
            travel_date=travel_date,
            sources=sources,
            error_code="invalid_response",
            summary=(
                "The fusion model omitted required fields, so no assessment was "
                "produced."
            ),
            uncertainty=[
                "Missing required keys: {0}".format(", ".join(missing)),
                "Keys returned: {0}".format(", ".join(sorted(parsed)) or "none"),
                "Prompt and response record: {0}".format(
                    ", ".join(exchange.raw_paths) or "none written"
                ),
            ],
        )


    # Pipeline-owned fields are dropped rather than trusted; Pydantic ignores unknown
    # extras, and everything model-authored that we keep is validated below.
    candidate: Dict[str, Any] = {
        key: value for key, value in parsed.items() if key not in _PIPELINE_OWNED_FIELDS
    }
    candidate.update(
        {
            "status": _overall_status(sources),
            "origin": location.origin.iata,
            "destination": location.destination.iata,
            "travel_date": travel_date.isoformat(),
            "sources": sources,
            "missing_credentials": config.missing_credentials(),
            "generated_at": base.utc_now_iso(),
        }
    )

    try:
        assessment = AssessmentResponse.model_validate(candidate)
    except ValidationError as exc:
        details = "; ".join(
            "{0}: {1}".format(
                ".".join(str(part) for part in error.get("loc", ())) or "(root)",
                error.get("msg", "invalid"),
            )
            for error in exc.errors()[:6]
        )
        return _error_response(
            location=location,
            travel_date=travel_date,
            sources=sources,
            error_code="invalid_response",
            summary=(
                "The fusion model returned an assessment that failed validation "
                "({0} problem(s)), so no assessment was produced.".format(
                    len(exc.errors())
                )
            ),
            uncertainty=[
                "Validation errors: {0}".format(
                    base.truncate_text(details, 600) or "none reported"
                ),
                "Prompt and response record: {0}".format(
                    ", ".join(exchange.raw_paths) or "none written"
                ),
            ],
        )

    problems = _semantic_problems(assessment, evidence_index)
    if problems:
        return _error_response(
            location=location,
            travel_date=travel_date,
            sources=sources,
            error_code="invalid_response",
            summary=(
                "The fusion model returned an internally inconsistent assessment, so "
                "no assessment was produced."
            ),
            uncertainty=[
                "Contradictions: {0}".format(
                    base.truncate_text("; ".join(problems), 800)
                ),
                "Prompt and response record: {0}".format(
                    ", ".join(exchange.raw_paths) or "none written"
                ),
            ],
        )

    # Derived last, from the number that survived validation, so label and value agree.
    assessment.confidence_label = _confidence_label(assessment.confidence)
    # Facts the model cannot know (what we withheld) belong in the response too.
    assessment.uncertainty.extend(_truncation_notes(envelopes))

    logger.info(
        "fusion: %s %s->%s %s risk=%s confidence=%s risks=%d sources=%s records=%s",
        config.FUSION_MODEL_NAME,
        location.origin.iata,
        location.destination.iata,
        travel_date.isoformat(),
        assessment.risk_level,
        assessment.confidence,
        len(assessment.risks),
        ",".join("{0}:{1}".format(item.source, item.status) for item in sources),
        ",".join(exchange.raw_paths),
    )
    return assessment


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #
async def fuse(
    signals: Sequence[Mapping[str, Any]],
    location: base.Location,
    travel_date: datetime.date,
) -> AssessmentResponse:
    """Fuse the envelopes into one validated assessment (see :func:`_run_fusion`)."""
    return await _run_fusion(signals, location, travel_date, [])


async def fuse_with_trace(
    signals: Sequence[Mapping[str, Any]],
    location: base.Location,
    travel_date: datetime.date,
) -> FusionResult:
    """Same call as :func:`fuse`, plus the exact prompt/answer/usage behind it.

    ``exchange`` is ``None`` when no request was made at all (not ready, no usable
    signals, no evidence ids): there is no prompt to report, and fabricating one
    would misrepresent what happened.
    """
    trace: List[FusionExchange] = []
    assessment = await _run_fusion(signals, location, travel_date, trace)
    return FusionResult(
        assessment=assessment, exchange=trace[-1] if trace else None
    )

