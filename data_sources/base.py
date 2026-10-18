"""Shared data source contract: normalized envelope, error-state vocabulary, raw
response evidence logging, and a provider-agnostic async HTTP helper.

Every integration in ``data_sources/`` builds on this module. It is deliberately
*not* a source module: it exposes shared types and helpers rather than a single
``fetch_signals`` entry point. Rule 4's "exactly one public entry point" applies to
vendor integrations, not to shared libraries, so the helpers below (``get_json``,
``ok_signals``, ``unavailable_signals``, ``load_raw_response``, ...) are public on
purpose, and ``iata_lookup`` is exempt for the same reason. Each concrete source
module (``aviationstack``, ``nws``, ``tomtom``, ``gdelt``) exposes exactly one
public function:

    async def fetch_signals(location: Location, date: datetime.date) -> NormalizedSignals

What this module guarantees (see ``.clinerules`` for the rules it implements):

* Rule 2 - every request carries an explicit timeout; any non-2xx status is an
  error; transport failures, rate limits, malformed and unexpected payloads are
  converted into error states. No ``httpx`` exception, ``KeyError`` or
  ``TypeError`` escapes this module; unexpected exceptions become
  ``ErrorCode.UNEXPECTED_ERROR``.
* Rule 3 - nothing is ever fabricated. A failed or empty call produces an
  envelope with ``status`` of ``"error"``/``"unavailable"``, a machine-readable
  ``error_code`` and a human-readable ``error``. An empty signal list is never
  presented as a clean result. When part of a query succeeds and part fails,
  ``partial_signals()`` reports the real signals *and* the failures; ``partial`` is
  never a stand-in for either ``ok`` or ``unavailable``.
* Rule 5 - the raw response (status, headers, verbatim body, sha256, latency) is
  written to ``config.RAW_RESPONSE_DIR`` *before* the body is parsed. If that
  write fails, or raw logging is switched off, the call is reported as failed
  instead of returning untraceable data.
* Rule 7 - credentials come from ``config`` (environment variables) and are
  scrubbed from every URL, header, log record and error message produced here.

Raw evidence files are named ``<source>_<utc timestamp>_<random>.json`` and are
recorded relative to the project root (e.g.
``logs/raw/nws_20261016T190712Z_ab12cd34.json``) so they can be surfaced safely
through ``GET /evidence/{assessment_id}``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import hashlib
import inspect
import json
import logging
import time
import uuid
from pathlib import Path
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Type,
    TypedDict,
    cast,
)

import httpx

import config

# ``Any`` is used only where a value is genuinely opaque: raw unparsed API
# payloads, and free-form normalized ``metrics`` maps handed to the AI layer.

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Error-state vocabulary - the contract every source module shares
# --------------------------------------------------------------------------- #
class ErrorCode:
    """Machine-readable reasons a source produced no usable signals.

    Every failure mode in the project maps to exactly one of these strings, so
    the fusion layer can reason about *why* a signal is missing without having to
    parse prose. The envelope ``status`` is derived from the code by
    :func:`status_for_error_code`.
    """

    # -- configuration / build-time ---------------------------------------- #
    MISSING_CREDENTIAL: str = "missing_credential"
    NOT_IMPLEMENTED: str = "not_implemented"

    # -- transport --------------------------------------------------------- #
    TIMEOUT: str = "timeout"
    NETWORK_ERROR: str = "network_error"

    # -- HTTP status ------------------------------------------------------- #
    UNAUTHORIZED: str = "unauthorized"          # 401
    FORBIDDEN: str = "forbidden"                # 403
    NOT_FOUND: str = "not_found"                # 404
    RATE_LIMITED: str = "rate_limited"          # 429
    CLIENT_ERROR: str = "client_error"          # any other 4xx
    SERVER_ERROR: str = "server_error"          # any 5xx

    # -- payload shape ----------------------------------------------------- #
    MALFORMED_PAYLOAD: str = "malformed_payload"
    UNEXPECTED_PAYLOAD_SHAPE: str = "unexpected_payload_shape"

    # -- evidence layer ---------------------------------------------------- #
    LOGGING_FAILED: str = "logging_failed"
    LOGGING_DISABLED: str = "logging_disabled"

    # -- genuinely no data (envelope status becomes "unavailable") --------- #
    NO_DATA_FOR_QUERY: str = "no_data_for_query"
    PLAN_LIMIT: str = "plan_limit"

    # -- catch-all (never silently swallowed) ------------------------------ #
    UNEXPECTED_ERROR: str = "unexpected_error"

    @classmethod
    def all_codes(cls: Type["ErrorCode"]) -> Tuple[str, ...]:
        """Every declared code, for contract tests and documentation."""
        return tuple(
            value
            for name, value in vars(cls).items()
            if name.isupper() and isinstance(value, str)
        )


# Codes that mean "the source worked, but there is no data for this query". They
# map to config.STATUS_UNAVAILABLE rather than config.STATUS_ERROR, so a free-tier
# plan limit is never mistaken for a source outage (and never for "no risk").
_UNAVAILABLE_ERROR_CODES: frozenset = frozenset(
    {
        ErrorCode.NO_DATA_FOR_QUERY,
        ErrorCode.PLAN_LIMIT,
        ErrorCode.NOT_IMPLEMENTED,
    }
)

# Statuses worth retrying: transient throttling and server-side faults. Client
# 4xx errors are not retried - repeating them wastes quota and cannot succeed.
_RETRYABLE_STATUS_CODES: frozenset = frozenset(
    {408, 425, 429, 500, 502, 503, 504, 522, 524}
)


def status_for_error_code(error_code: Optional[str]) -> str:
    """Map an error code to the envelope ``status`` it implies."""
    if error_code is None:
        return config.STATUS_ERROR
    if error_code in _UNAVAILABLE_ERROR_CODES:
        return config.STATUS_UNAVAILABLE
    return config.STATUS_ERROR


def error_code_for_status(status_code: int) -> str:
    """Map an HTTP status code onto the shared error-code vocabulary."""
    if status_code == 401:
        return ErrorCode.UNAUTHORIZED
    if status_code == 403:
        return ErrorCode.FORBIDDEN
    if status_code == 404:
        return ErrorCode.NOT_FOUND
    if status_code == 429:
        return ErrorCode.RATE_LIMITED
    if 400 <= status_code < 500:
        return ErrorCode.CLIENT_ERROR
    if status_code >= 500:
        return ErrorCode.SERVER_ERROR
    return ErrorCode.UNEXPECTED_ERROR


def is_retryable_status(status_code: int) -> bool:
    """Whether an HTTP status is worth retrying."""
    return status_code in _RETRYABLE_STATUS_CODES
# --------------------------------------------------------------------------- #
# Time helpers (every timestamp in the envelope is ISO 8601 UTC)
# --------------------------------------------------------------------------- #
def utc_now() -> datetime.datetime:
    """Timezone-aware current UTC time."""
    return datetime.datetime.now(datetime.timezone.utc)


def utc_now_iso() -> str:
    """UTC now as ISO 8601, e.g. ``2026-10-16T19:07:12.345Z``."""
    return utc_now().isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso_date(value: Optional[datetime.date]) -> Optional[str]:
    """Format a date as ``YYYY-MM-DD`` (``None`` passes through unchanged)."""
    return value.isoformat() if value is not None else None
# --------------------------------------------------------------------------- #
# Secret redaction - no record, log line or message leaves here with a key in it
# --------------------------------------------------------------------------- #
_REDACTED: str = "***REDACTED***"

# Query-string and header keys whose values are credentials.
_SECRET_KEYS: Tuple[str, ...] = (
    "access_key",
    "access_token",
    "api_key",
    "api-key",
    "apikey",
    "auth",
    "authorization",
    "client_secret",
    "cookie",
    "key",
    "password",
    "proxy-authorization",
    "set-cookie",
    "subscription-key",
    "subscription_key",
    "token",
    "x-api-key",
)


def _secret_values() -> Tuple[str, ...]:
    """Credential values known to ``config``, read fresh on every call.

    Used to scrub secrets a provider may have echoed into a URL, header or error
    message. Callers only ever receive redacted text from this module.
    """
    values: List[str] = []
    for env_var in config.REQUIRED_CREDENTIALS.values():
        value = getattr(config, env_var, None)
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    return tuple(values)


def redact_text(text: str) -> str:
    """Replace any known credential value embedded in free-form text."""
    redacted = text
    for secret in _secret_values():
        if secret in redacted:
            redacted = redacted.replace(secret, _REDACTED)
    return redacted


def redact_url(url: str) -> str:
    """Redact secret query parameters (``?key=``, ``?access_key=``) and values."""
    safe_url = redact_text(url)
    try:
        parsed = httpx.URL(safe_url)
    except (httpx.InvalidURL, ValueError):
        return safe_url
    items = list(parsed.params.multi_items())
    if not items:
        return safe_url
    if not any(key.lower() in _SECRET_KEYS for key, _ in items):
        return safe_url
    base = str(parsed).split("?", 1)[0]
    query = "&".join(
        "{0}={1}".format(key, _REDACTED if key.lower() in _SECRET_KEYS else value)
        for key, value in items
    )
    return "{0}?{1}".format(base, query)


def redact_headers(headers: Mapping[str, str]) -> Dict[str, str]:
    """Return HTTP headers with credential-bearing values replaced."""
    safe: Dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in _SECRET_KEYS:
            safe[key] = _REDACTED
        else:
            safe[key] = redact_text(str(value))
    return safe


def redact_params(params: Mapping[str, Any]) -> Dict[str, Any]:
    """Return query parameters with credential-bearing values replaced."""
    safe: Dict[str, Any] = {}
    for key, value in params.items():
        if key.lower() in _SECRET_KEYS:
            safe[key] = _REDACTED
        else:
            safe[key] = redact_text(str(value))
    return safe
# --------------------------------------------------------------------------- #
# Text helpers
# --------------------------------------------------------------------------- #
def truncate_text(text: str, max_chars: int) -> str:
    """Truncate ``text`` to ``max_chars``, marking that truncation happened."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    removed = len(text) - max_chars
    return "{0}...[{1} more characters truncated]".format(text[:max_chars], removed)


def sha256_text(text: str) -> str:
    """SHA-256 of a response body, so stored evidence can be verified later."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _stringify(value: Any, max_chars: int) -> Optional[str]:
    """Render an evidence value as text without losing structure."""
    if value is None:
        return None
    if isinstance(value, str):
        return truncate_text(value, max_chars)
    if isinstance(value, (int, float, bool)):
        return str(value)
    try:
        rendered = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        rendered = repr(value)
    return truncate_text(rendered, max_chars)
# --------------------------------------------------------------------------- #
# Route input - airport-to-airport (IATA codes)
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class Endpoint:
    """One end of the route. The IATA code is the identifier.

    Coordinates are optional and are only ever *carried* by this module - it does
    not geocode. A source that needs lat/lon (e.g. NWS) must be handed them, or
    report the gap as an explicit error/unavailable state. It must never invent a
    location.
    """

    iata: str
    name: Optional[str] = None
    city: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None

    def __post_init__(self) -> None:
        cleaned = (self.iata or "").strip().upper()
        if not cleaned:
            raise ValueError("Endpoint.iata must be a non-empty IATA code")
        object.__setattr__(self, "iata", cleaned)
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError(
                "Endpoint latitude and longitude must be provided together for "
                "{0}".format(cleaned)
            )

    @property
    def has_coordinates(self) -> bool:
        """Whether this endpoint carries usable coordinates."""
        return self.latitude is not None and self.longitude is not None

    def to_dict(self) -> Dict[str, Any]:
        """Plain-dict form, stored in the envelope's ``location`` field."""
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class Location:
    """The route a source is asked about: origin and destination airports."""

    origin: Endpoint
    destination: Endpoint

    @classmethod
    def from_iata(
        cls: Type["Location"],
        origin_iata: str,
        destination_iata: str,
        *,
        origin_name: Optional[str] = None,
        destination_name: Optional[str] = None,
    ) -> "Location":
        """Build a route from IATA codes (the only input the UI accepts)."""
        return cls(
            origin=Endpoint(iata=origin_iata, name=origin_name),
            destination=Endpoint(iata=destination_iata, name=destination_name),
        )

    @property
    def route_label(self) -> str:
        """Short human-readable route label, e.g. ``JFK -> SFO``."""
        return "{0} -> {1}".format(self.origin.iata, self.destination.iata)

    @property
    def route_iata(self) -> Tuple[str, str]:
        """``(origin_iata, destination_iata)`` tuple for vendor parameters."""
        return (self.origin.iata, self.destination.iata)

    def to_dict(self) -> Dict[str, Any]:
        """Plain-dict form embedded in every envelope's ``location`` field."""
        return {
            "origin": self.origin.to_dict(),
            "destination": self.destination.to_dict(),
            "route_label": self.route_label,
        }
# --------------------------------------------------------------------------- #
# Normalized envelope types - the one shape every `fetch_signals` returns
# --------------------------------------------------------------------------- #
class EvidenceEntry(TypedDict):
    """A single traceable observation: where a value came from and what it was."""

    id: str
    source: str
    field: Optional[str]
    value: Optional[str]
    observed_at: Optional[str]
    raw_response_path: Optional[str]
    note: Optional[str]


class SignalEntry(TypedDict):
    """One normalized signal unit contributed by a source.

    ``kind`` is a normalized category (e.g. ``flight_delay``, ``weather_alert``,
    ``traffic_congestion``) chosen by the source module; ``severity`` is mapped
    onto ``config.RISK_LEVELS`` by the source, never left as a vendor string.
    """

    id: str
    kind: str
    summary: str
    severity: Optional[str]
    confidence: Optional[float]
    observed_at: Optional[str]
    effective_at: Optional[str]
    metrics: Dict[str, Any]
    evidence_ids: List[str]


class NormalizedSignals(TypedDict):
    """The normalized result of one data source for one query.

    Returned by every ``fetch_signals``. ``status`` is one of
    ``config.STATUS_OK`` / ``config.STATUS_PARTIAL`` / ``config.STATUS_UNAVAILABLE``
    / ``config.STATUS_ERROR``. ``ok`` and ``partial`` carry signals; ``partial`` also
    carries ``error_code`` and adds the ``error_codes`` list (see
    :class:`PartialSignals`). ``unavailable``/``error`` carry no signals at all:
    ``error`` and ``error_code`` explain exactly why, and a missing signal is
    reported rather than substituted.
    """

    source: str
    status: str
    error: Optional[str]
    error_code: Optional[str]
    retrieved_at: str
    latency_ms: Optional[float]
    travel_date: Optional[str]
    location: Optional[Dict[str, Any]]
    records: int
    signals: List[SignalEntry]
    evidence: List[EvidenceEntry]
    raw_response_path: Optional[str]
    raw_response_paths: List[str]
    warnings: List[str]


class PartialSignals(NormalizedSignals):
    """An envelope where some sub-queries succeeded and some failed.

    Structurally identical to :class:`NormalizedSignals` plus ``error_codes``: one
    code per failed sub-query, in the order the failures were reported (duplicates
    kept, so ``len(error_codes)`` equals the number of failures). ``error_code``
    mirrors ``error_codes[0]`` and ``error`` is one combined message naming every
    failure, so a caller that only understands the base shape still reads *why* the
    result is incomplete instead of mistaking it for ``ok``.
    """

    error_codes: List[str]


class SubQueryFailure(TypedDict, total=False):
    """One failed part of a partly-successful call, as passed to ``partial_signals``.

    ``error_code`` and ``error`` are required in practice; they are validated at
    runtime with a ``ValueError`` rather than trusted. ``scope`` optionally names
    the part that failed (an IATA code, an endpoint, a page window) so the combined
    envelope message can point at it.
    """

    scope: Optional[str]
    error_code: str
    error: str
# --------------------------------------------------------------------------- #
# Normalization helpers - validate source output before it reaches the envelope
# --------------------------------------------------------------------------- #
def _validated_severity(severity: Optional[str]) -> Optional[str]:
    """Normalize a severity onto ``config.RISK_LEVELS``, or raise if unknown.

    Sources must map vendor scales onto our own levels. An unmappable value is a
    source bug: raising here keeps an unknown label out of the assessment.
    """
    if severity is None:
        return None
    normalized = severity.strip().lower()
    if normalized not in config.RISK_LEVELS:
        raise ValueError(
            "severity {0!r} is not one of: {1}".format(
                severity, ", ".join(config.RISK_LEVELS)
            )
        )
    return normalized


def _validated_confidence(confidence: Optional[float]) -> Optional[float]:
    """Coerce confidence to a float probability in ``[0.0, 1.0]``."""
    if confidence is None:
        return None
    try:
        value = float(confidence)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "confidence {0!r} is not numeric".format(confidence)
        ) from exc
    if not 0.0 <= value <= 1.0:
        raise ValueError("confidence {0!r} is outside [0.0, 1.0]".format(confidence))
    return value


def _assign_ids(items: List[Dict[str, Any]], prefix: str) -> None:
    """Give every entry a stable, citable id (``sig_1``, ``ev_1``, ...)."""
    for index, item in enumerate(items, start=1):
        if not item.get("id"):
            item["id"] = "{0}_{1}".format(prefix, index)


def make_evidence(
    source: str,
    *,
    field: Optional[str] = None,
    value: Any = None,
    observed_at: Optional[str] = None,
    raw_response_path: Optional[str] = None,
    note: Optional[str] = None,
    evidence_id: Optional[str] = None,
) -> EvidenceEntry:
    """Build one evidence entry pointing at the raw response it came from."""
    if not source.strip():
        raise ValueError("evidence 'source' must be a non-empty data source id")
    return cast(
        EvidenceEntry,
        {
            "id": evidence_id or "",
            "source": source,
            "field": field,
            "value": _stringify(value, config.EVIDENCE_VALUE_MAX_CHARS),
            "observed_at": observed_at,
            "raw_response_path": raw_response_path,
            "note": note,
        },
    )


def make_signal(
    kind: str,
    summary: str,
    *,
    severity: Optional[str] = None,
    confidence: Optional[float] = None,
    observed_at: Optional[str] = None,
    effective_at: Optional[str] = None,
    metrics: Optional[Mapping[str, Any]] = None,
    evidence_ids: Optional[Iterable[str]] = None,
    signal_id: Optional[str] = None,
) -> SignalEntry:
    """Build one normalized signal: a finding, never a raw vendor record.

    ``metrics`` carries the numbers the AI layer reasons over (delay minutes, wind
    gusts, ETA delta). ``evidence_ids`` link the finding back to the evidence
    entries, which in turn link to the raw response file on disk.
    """
    if not kind.strip():
        raise ValueError("signal 'kind' must be a non-empty normalized category")
    if not summary.strip():
        raise ValueError("signal 'summary' must be a non-empty description")
    return cast(
        SignalEntry,
        {
            "id": signal_id or "",
            "kind": kind.strip(),
            "summary": summary.strip(),
            "severity": _validated_severity(severity),
            "confidence": _validated_confidence(confidence),
            "observed_at": observed_at,
            "effective_at": effective_at,
            "metrics": dict(metrics or {}),
            "evidence_ids": list(evidence_ids or []),
        },
    )
# --------------------------------------------------------------------------- #
# Envelope builders - the only sanctioned way to produce a NormalizedSignals
# --------------------------------------------------------------------------- #
def _validate_evidence_links(
    signals: List[SignalEntry], evidence: List[EvidenceEntry]
) -> None:
    """Every ``evidence_ids`` reference must resolve to a real evidence entry."""
    known = {entry.get("id") for entry in evidence}
    for signal in signals:
        for evidence_id in signal.get("evidence_ids", []):
            if evidence_id not in known:
                raise ValueError(
                    "signal {0!r} cites unknown evidence id {1!r}".format(
                        signal.get("id"), evidence_id
                    )
                )


def _base_envelope(
    *,
    source: str,
    status: str,
    signals: Sequence[SignalEntry],
    evidence: Sequence[EvidenceEntry],
    error: Optional[str],
    error_code: Optional[str],
    location: Optional[Location],
    travel_date: Optional[datetime.date],
    raw_response_path: Optional[str],
    raw_response_paths: Sequence[str],
    latency_ms: Optional[float],
    warnings: Sequence[str],
    retrieved_at: Optional[str],
    error_codes: Sequence[str] = (),
) -> NormalizedSignals:
    """Assemble and validate the envelope.

    The invariants enforced here are what keep a fake or ambiguous result from
    reaching the fusion layer: ``ok`` requires at least one real signal and no
    error; ``partial`` requires at least one real signal *and* at least one recorded
    failure (``error`` + ``error_code``, with the full list in ``error_codes``);
    ``unavailable``/``error`` require an explicit reason and carry no signals at all.
    """
    if not source or not source.strip():
        raise ValueError("envelope 'source' must be a non-empty data source id")
    if status not in (
        config.STATUS_OK,
        config.STATUS_PARTIAL,
        config.STATUS_UNAVAILABLE,
        config.STATUS_ERROR,
    ):
        raise ValueError(
            "envelope 'status' must be one of {0}, {1}, {2}, {3}".format(
                config.STATUS_OK,
                config.STATUS_PARTIAL,
                config.STATUS_UNAVAILABLE,
                config.STATUS_ERROR,
            )
        )
    if error_codes and status != config.STATUS_PARTIAL:
        raise ValueError(
            "'error_codes' is only meaningful on a 'partial' envelope; every other "
            "status carries a single 'error_code' by design"
        )

    signal_list = cast(List[SignalEntry], [dict(item) for item in signals])
    evidence_list = cast(List[EvidenceEntry], [dict(item) for item in evidence])

    if status == config.STATUS_OK:
        if error or error_code:
            raise ValueError(
                "an 'ok' envelope must not carry an error/error_code; use "
                "failure_signals() or unavailable_signals() instead"
            )
        if not signal_list:
            raise ValueError(
                "an 'ok' envelope must contain at least one signal; a source with "
                "no data must return unavailable_signals(), never an empty 'ok'"
            )
    elif status == config.STATUS_PARTIAL:
        if not signal_list:
            raise ValueError(
                "a 'partial' envelope must contain at least one real signal; a call "
                "that produced nothing is unavailable_signals() or failure_signals(), "
                "never 'partial'"
            )
        if not error or not error_code:
            raise ValueError(
                "a 'partial' envelope requires both 'error' and 'error_code' "
                "describing the sub-queries that failed; partial is earned, not a "
                "default"
            )
    else:
        if not error or not error_code:
            raise ValueError(
                "a non-'ok' envelope requires both 'error' and 'error_code'"
            )
        if signal_list:
            raise ValueError(
                "a non-'ok' envelope must not carry signals; report the missing "
                "signal explicitly instead of mixing it with partial data"
            )

    _assign_ids(cast(List[Dict[str, Any]], signal_list), "sig")
    _assign_ids(cast(List[Dict[str, Any]], evidence_list), "ev")
    _validate_evidence_links(signal_list, evidence_list)

    envelope: Dict[str, Any] = {
        "source": source.strip(),
        "status": status,
        "error": error,
        "error_code": error_code,
        "retrieved_at": retrieved_at or utc_now_iso(),
        "latency_ms": latency_ms,
        "travel_date": iso_date(travel_date),
        "location": location.to_dict() if location is not None else None,
        "records": len(signal_list),
        "signals": signal_list,
        "evidence": evidence_list,
        "raw_response_path": raw_response_path,
        "raw_response_paths": list(raw_response_paths),
        "warnings": [str(item) for item in warnings],
    }
    if error_codes:
        # Only partial envelopes carry this key, so ok/unavailable/error payloads
        # keep the exact shape every existing consumer (and test) already expects.
        envelope["error_codes"] = list(error_codes)
    return cast(NormalizedSignals, envelope)


def ok_signals(
    *,
    source: str,
    signals: Sequence[SignalEntry],
    location: Optional[Location] = None,
    travel_date: Optional[datetime.date] = None,
    evidence: Sequence[EvidenceEntry] = (),
    raw_response_path: Optional[str] = None,
    raw_response_paths: Sequence[str] = (),
    latency_ms: Optional[float] = None,
    warnings: Sequence[str] = (),
    retrieved_at: Optional[str] = None,
) -> NormalizedSignals:
    """Build a successful envelope. Raises ``ValueError`` if ``signals`` is empty."""
    return _base_envelope(
        source=source,
        status=config.STATUS_OK,
        signals=signals,
        evidence=evidence,
        error=None,
        error_code=None,
        location=location,
        travel_date=travel_date,
        raw_response_path=raw_response_path,
        raw_response_paths=raw_response_paths,
        latency_ms=latency_ms,
        warnings=warnings,
        retrieved_at=retrieved_at,
    )


def failure_signals(
    *,
    source: str,
    error_code: str,
    error: str,
    location: Optional[Location] = None,
    travel_date: Optional[datetime.date] = None,
    raw_response_path: Optional[str] = None,
    raw_response_paths: Sequence[str] = (),
    latency_ms: Optional[float] = None,
    warnings: Sequence[str] = (),
    status: Optional[str] = None,
    retrieved_at: Optional[str] = None,
) -> NormalizedSignals:
    """Build an error/unavailable envelope with an explicit, traceable reason.

    ``status`` defaults to whatever :func:`status_for_error_code` implies for the
    code, and may be overridden when a code legitimately means "no data for this
    query" rather than "the call failed".
    """
    if not error or not error.strip():
        raise ValueError("failure_signals requires a non-empty 'error' message")
    if not error_code or not error_code.strip():
        raise ValueError("failure_signals requires a non-empty 'error_code'")
    return _base_envelope(
        source=source,
        status=status or status_for_error_code(error_code),
        signals=(),
        evidence=(),
        error=error.strip(),
        error_code=error_code,
        location=location,
        travel_date=travel_date,
        raw_response_path=raw_response_path,
        raw_response_paths=raw_response_paths,
        latency_ms=latency_ms,
        warnings=warnings,
        retrieved_at=retrieved_at,
    )


def unavailable_signals(
    *,
    source: str,
    error_code: str,
    error: str,
    location: Optional[Location] = None,
    travel_date: Optional[datetime.date] = None,
    raw_response_path: Optional[str] = None,
    raw_response_paths: Sequence[str] = (),
    latency_ms: Optional[float] = None,
    warnings: Sequence[str] = (),
    retrieved_at: Optional[str] = None,
) -> NormalizedSignals:
    """Build an envelope asserting "no data for this query" - not a failure.

    Used for free-tier plan limits, dates outside a vendor's window, or a vendor
    that knows nothing about this route. It is never a stand-in for "no risk": the
    fusion layer sees ``status="unavailable"`` plus a reason and must reflect the
    gap in its uncertainty output.
    """
    return failure_signals(
        source=source,
        error_code=error_code,
        error=error,
        location=location,
        travel_date=travel_date,
        raw_response_path=raw_response_path,
        raw_response_paths=raw_response_paths,
        latency_ms=latency_ms,
        warnings=warnings,
        status=config.STATUS_UNAVAILABLE,
        retrieved_at=retrieved_at,
    )


def partial_signals(
    *,
    source: str,
    signals: Sequence[SignalEntry],
    failures: Sequence[SubQueryFailure],
    location: Optional[Location] = None,
    travel_date: Optional[datetime.date] = None,
    evidence: Sequence[EvidenceEntry] = (),
    raw_response_path: Optional[str] = None,
    raw_response_paths: Sequence[str] = (),
    latency_ms: Optional[float] = None,
    warnings: Sequence[str] = (),
    retrieved_at: Optional[str] = None,
) -> PartialSignals:
    """Build an envelope where some sub-queries succeeded and some failed.

    A partial envelope means some sub-queries succeeded and some failed. The fusion
    layer must treat partial sources as weaker evidence than ok sources and stronger
    than unavailable sources. The error_codes list the failures; the signals list the
    successes. Neither is inferred from the other.

    ``partial`` is earned, never a default. It requires **at least one real signal**
    and **at least one recorded failure**, and every signal must cite at least one
    resolvable evidence id - partial does not mean giving up on traceability. Any
    violation raises ``ValueError`` instead of producing an ambiguous envelope.

    ``error_code`` repeats the first failure's code and ``error`` is one combined
    message naming every failure, so a consumer that only understands the base
    ``NormalizedSignals`` shape still sees *why* the result is incomplete. The full
    list is in ``error_codes``. ``scope`` optionally labels the sub-query that failed
    (an IATA code, an endpoint, a paging window) so the message points at it.
    """
    signal_list = list(signals)
    if not signal_list:
        raise ValueError(
            "partial_signals requires at least one real signal: a call in which "
            "nothing succeeded is unavailable_signals() or failure_signals(), and an "
            "empty 'partial' would be a failure wearing a partial label"
        )
    if not failures:
        raise ValueError(
            "partial_signals requires at least one failed sub-query (error_code + "
            "error): when everything succeeded use ok_signals(), the only status that "
            "means 'complete'"
        )

    normalized: List[SubQueryFailure] = []
    for index, failure in enumerate(failures, start=1):
        if not isinstance(failure, Mapping):
            raise ValueError(
                "partial_signals failure #{0} must be a mapping with 'error_code' and "
                "'error', not {1}".format(index, type(failure).__name__)
            )
        code = str(failure.get("error_code") or "").strip()
        message = str(failure.get("error") or "").strip()
        if not code or not message:
            raise ValueError(
                "partial_signals failure #{0} needs both 'error_code' and 'error'; a "
                "partial result must say which sub-query failed and why".format(index)
            )
        normalized.append(
            {
                "scope": str(failure.get("scope") or "").strip() or None,
                "error_code": code,
                "error": message,
            }
        )

    for signal in signal_list:
        if not signal.get("evidence_ids"):
            raise ValueError(
                "signal {0!r} in a partial envelope cites no evidence; a partial "
                "result is still fully traceable, so every signal must reference at "
                "least one evidence entry".format(
                    signal.get("id") or signal.get("summary")
                )
            )

    details = "; ".join(
        "{0}: {1} - {2}".format(item["scope"], item["error_code"], item["error"])
        if item.get("scope")
        else "{0} - {1}".format(item["error_code"], item["error"])
        for item in normalized
    )
    error = (
        "Partial result: {0} sub-quer{1} failed alongside the {2} signal(s) from the "
        "parts that succeeded, so this is real but incomplete data. Failures - {3}"
    ).format(
        len(normalized),
        "y" if len(normalized) == 1 else "ies",
        len(signal_list),
        details,
    )

    envelope = _base_envelope(
        source=source,
        status=config.STATUS_PARTIAL,
        signals=signal_list,
        evidence=evidence,
        error=error,
        error_code=normalized[0]["error_code"],
        error_codes=[item["error_code"] for item in normalized],
        location=location,
        travel_date=travel_date,
        raw_response_path=raw_response_path,
        raw_response_paths=raw_response_paths,
        latency_ms=latency_ms,
        warnings=list(warnings)
        + [
            "Partial result: treat these signals as weaker evidence than a fully "
            "successful source and stronger than an unavailable one, and account for "
            "the reduced confidence. Never present this as 'ok'."
        ],
        retrieved_at=retrieved_at,
    )
    return cast(PartialSignals, envelope)


def missing_credential_signals(
    source: str,
    *,
    location: Optional[Location] = None,
    travel_date: Optional[datetime.date] = None,
    warnings: Sequence[str] = (),
) -> Optional[NormalizedSignals]:
    """Return an error envelope when ``source`` needs a credential that is unset.

    Returns ``None`` when the source needs no key or its key is present, so a
    source module can start with::

        blocked = base.missing_credential_signals(config.SOURCE_TOMTOM)
        if blocked is not None:
            return blocked

    The message names the missing environment variable, and no key is ever
    substituted (rule 7).
    """
    env_var = config.REQUIRED_CREDENTIALS.get(source)
    if env_var is None:
        return None
    value = getattr(config, env_var, None)
    if isinstance(value, str) and value.strip():
        return None
    return failure_signals(
        source=source,
        error_code=ErrorCode.MISSING_CREDENTIAL,
        error=(
            "Environment variable {0} is not set, so {1} signals cannot be fetched. "
            "No default credential and no substitute source were used."
        ).format(env_var, source),
        location=location,
        travel_date=travel_date,
        warnings=list(warnings)
        + ["Set {0} in .env to enable this source.".format(env_var)],
    )


def has_usable_signals(signals: NormalizedSignals) -> bool:
    """Whether an envelope contributes real signals to the fusion step.

    ``partial`` envelopes do contribute signals: they are real, evidenced findings
    about the parts of the query that succeeded. This predicate answers "are there
    signals to read?", not "is the picture complete?" - the fusion layer must weigh
    ``status`` itself, because ``partial`` is weaker evidence than ``ok``.
    """
    return signals.get("status") in (
        config.STATUS_OK,
        config.STATUS_PARTIAL,
    ) and bool(signals.get("signals"))


def to_source_status(signals: NormalizedSignals) -> Dict[str, Any]:
    """Project an envelope onto the fields of ``backend.models.SourceStatus``.

    Keeps the API layer from re-deriving status semantics per source, so an
    "unavailable" source can never be reported as a failure (or vice versa), and a
    "partial" source is reported as partial with both the surviving record count and
    the full list of failed sub-query codes.
    """
    error_code = signals["error_code"]
    error_codes = list(signals.get("error_codes") or ())  # type: ignore[call-overload]
    if not error_codes and error_code:
        error_codes = [error_code]
    return {
        "source": signals["source"],
        "status": signals["status"],
        "error": signals["error"],
        "error_code": error_code,
        "error_codes": error_codes,
        "latency_ms": signals["latency_ms"],
        "records": signals["records"],
        "raw_response_path": signals["raw_response_path"],
        "consulted_at": signals["retrieved_at"],
    }
# --------------------------------------------------------------------------- #
# HTTP result - the outcome of one logical request, retries included
# --------------------------------------------------------------------------- #
class _RawLoggingError(RuntimeError):
    """Raised internally when raw evidence cannot be persisted (rule 5)."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclasses.dataclass(frozen=True)
class HttpResult:
    """Outcome of one logical HTTP request, including every retry attempt.

    ``payload`` is only meaningful when ``ok`` is True. On any failure the caller
    builds an error envelope from ``error_code`` / ``error`` and must not read
    partial data. ``raw_paths`` lists the evidence record written for every
    attempt, so a retry storm is auditable.
    """

    ok: bool
    method: str
    url: str
    status_code: Optional[int]
    latency_ms: float
    attempts: int
    payload: Optional[Any]
    raw_path: Optional[str]
    raw_paths: Tuple[str, ...]
    error: Optional[str]
    error_code: Optional[str]
    response_headers: Dict[str, str]
    body_preview: Optional[str]
    content_type: Optional[str]

    @property
    def failure_status(self) -> str:
        """Envelope status this result implies (``"ok"`` when it succeeded)."""
        if self.ok:
            return config.STATUS_OK
        return status_for_error_code(self.error_code)
# --------------------------------------------------------------------------- #
# Async HTTP client - provider-agnostic
# --------------------------------------------------------------------------- #
def _merged_headers(headers: Optional[Mapping[str, str]]) -> Dict[str, str]:
    """Caller headers plus a default ``User-Agent`` identifying this app."""
    merged: Dict[str, str] = dict(headers or {})
    if not any(key.lower() == "user-agent" for key in merged):
        merged["User-Agent"] = "{0}/{1}".format(config.APP_NAME, config.APP_VERSION)
    return merged


async def _close_client(client: httpx.AsyncClient) -> None:
    """Close a client we opened ourselves.

    httpx renamed ``AsyncClient.close()`` to ``aclose()`` in 0.28, so both spellings
    are supported to keep the wrapper usable on either version.
    """
    aclose = getattr(client, "aclose", None)
    if callable(aclose):
        await aclose()
        return
    legacy_close = getattr(client, "close", None)  # pragma: no cover - httpx < 0.28
    if callable(legacy_close):
        maybe_awaitable = legacy_close()
        if inspect.isawaitable(maybe_awaitable):
            await maybe_awaitable


def _backoff_delay(attempt_index: int) -> float:
    """Exponential backoff between attempts (``attempt_index`` is zero-based)."""
    base = max(0.0, config.HTTP_BACKOFF_BASE_SECONDS)
    cap = max(base, config.HTTP_BACKOFF_MAX_SECONDS)
    return min(base * (2 ** attempt_index), cap)


def _retry_delay(response: Optional[httpx.Response], attempt_index: int) -> float:
    """Delay before the next attempt, honouring a numeric ``Retry-After`` header."""
    if response is not None:
        raw = response.headers.get("retry-after")
        if raw:
            try:
                requested = float(raw.strip())
            except ValueError:
                requested = None  # HTTP-date form: fall back to our own backoff
            if requested is not None:
                cap = max(0.0, config.HTTP_BACKOFF_MAX_SECONDS)
                return max(0.0, min(requested, cap))
    return _backoff_delay(attempt_index)


def _body_text(response: httpx.Response) -> str:
    """Response body as text, never raising on an unexpected charset."""
    try:
        return response.text
    except (UnicodeDecodeError, LookupError):
        return response.content.decode("utf-8", errors="replace")


def _missing_keys(payload: Any, expect_keys: Optional[Sequence[str]]) -> List[str]:
    """Keys the caller declared as required that the payload does not contain."""
    if not expect_keys:
        return []
    if not isinstance(payload, Mapping):
        return list(expect_keys)
    return [key for key in expect_keys if key not in payload]


def _http_failure_result(
    *,
    url: str,
    latency_ms: float,
    attempts: int,
    error_code: str,
    error: str,
    status_code: Optional[int],
    response_headers: Mapping[str, str],
    body_preview: Optional[str],
    content_type: Optional[str],
    raw_paths: Sequence[str],
) -> HttpResult:
    """Build the failed result shared by every error path in :func:`get_json`."""
    return HttpResult(
        ok=False,
        method="GET",
        url=url,
        status_code=status_code,
        latency_ms=latency_ms,
        attempts=attempts,
        payload=None,
        raw_path=raw_paths[-1] if raw_paths else None,
        raw_paths=tuple(raw_paths),
        error=redact_text(error),
        error_code=error_code,
        response_headers=dict(response_headers),
        body_preview=body_preview,
        content_type=content_type,
    )


async def get_json(
    source: str,
    url: str,
    *,
    params: Optional[Mapping[str, Any]] = None,
    headers: Optional[Mapping[str, str]] = None,
    timeout: Optional[float] = None,
    max_retries: Optional[int] = None,
    expect_keys: Optional[Sequence[str]] = None,
    context: Optional[Mapping[str, Any]] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> HttpResult:
    """GET ``url`` and return parsed JSON, with raw evidence logged per attempt.

    Provider-agnostic: no vendor names, no vendor parameters. It always:

    * applies an explicit timeout (``config.HTTP_TIMEOUT_SECONDS`` by default);
    * retries only transport failures and retryable statuses (429/5xx), honouring
      ``Retry-After``, and never retries a 400/401/403/404;
    * writes the raw exchange to the evidence directory *before* parsing;
    * returns an error state instead of raising for network, HTTP or payload-shape
      failures - read ``ok``, ``error_code`` and ``error``.

    ``ValueError`` is raised only for a programming error in the arguments; any
    unexpected exception becomes ``ErrorCode.UNEXPECTED_ERROR``.
    """
    if not source or not source.strip():
        raise ValueError("get_json requires a non-empty 'source' id")
    effective_timeout = float(
        timeout if timeout is not None else config.HTTP_TIMEOUT_SECONDS
    )
    if effective_timeout <= 0:
        raise ValueError("timeout must be greater than 0 seconds")
    retries = int(max_retries if max_retries is not None else config.HTTP_MAX_RETRIES)
    if retries < 0:
        raise ValueError("max_retries must be 0 or greater")
    max_attempts = retries + 1

    request_params: Dict[str, Any] = dict(params or {})
    request_url = (
        url
        if not request_params
        else "{0}?{1}".format(url, httpx.QueryParams(request_params))
    )
    redacted_url = redact_url(request_url)
    request_headers = _merged_headers(headers)

    owns_client = client is None
    active_client = client or httpx.AsyncClient(
        timeout=httpx.Timeout(effective_timeout), follow_redirects=True
    )

    started_at = time.monotonic()
    raw_paths: List[str] = []
    attempts = 0
    last_status: Optional[int] = None
    last_headers: Dict[str, str] = {}
    last_body_preview: Optional[str] = None
    last_content_type: Optional[str] = None
    last_error_code: Optional[str] = None
    last_error: Optional[str] = None

    def _elapsed_ms() -> float:
        return round((time.monotonic() - started_at) * 1000.0, 3)

    try:
        for attempt in range(1, max_attempts + 1):
            attempts = attempt
            attempt_started = time.monotonic()
            response: Optional[httpx.Response] = None
            transport_error: Optional[BaseException] = None
            try:
                response = await active_client.get(
                    url, params=request_params or None, headers=request_headers
                )
            except httpx.TimeoutException as exc:
                transport_error = exc
            except httpx.HTTPError as exc:
                transport_error = exc
            except Exception as exc:  # defensive: convert, never re-raise
                transport_error = exc

            attempt_latency_ms = round((time.monotonic() - attempt_started) * 1000.0, 3)
            body_text = _body_text(response) if response is not None else None

            # Rule 5: persist the raw exchange before any parsing or status logic.
            record = _raw_record(
                source=source,
                url=redacted_url,
                request_params=request_params,
                request_headers=request_headers,
                response=response,
                body_text=body_text,
                latency_ms=attempt_latency_ms,
                attempt=attempt,
                max_attempts=max_attempts,
                transport_error=transport_error,
                context=context,
            )
            try:
                raw_paths.append(_write_raw_record(source=source, record=record))
            except _RawLoggingError as exc:
                # Untraceable data is never returned as if it were traceable.
                return _http_failure_result(
                    url=redacted_url,
                    latency_ms=_elapsed_ms(),
                    attempts=attempt,
                    error_code=exc.error_code,
                    error=str(exc),
                    status_code=response.status_code if response is not None else None,
                    response_headers=last_headers,
                    body_preview=None,
                    content_type=last_content_type,
                    raw_paths=raw_paths,
                )

            if response is not None:
                last_status = response.status_code
                last_headers = redact_headers(dict(response.headers))
                last_content_type = response.headers.get("content-type")
                last_body_preview = truncate_text(redact_text(body_text or ""), 300)

            if response is None:
                is_timeout = isinstance(transport_error, httpx.TimeoutException)
                last_error_code = (
                    ErrorCode.TIMEOUT if is_timeout else ErrorCode.NETWORK_ERROR
                )
                last_error = (
                    "{0} calling {1} (timeout {2:g}s, attempt {3}/{4}): {5}: {6}"
                ).format(
                    "Timeout" if is_timeout else "Transport error",
                    redacted_url,
                    effective_timeout,
                    attempt,
                    max_attempts,
                    type(transport_error).__name__,
                    redact_text(str(transport_error)),
                )
                if attempt < max_attempts:
                    await asyncio.sleep(_retry_delay(None, attempt - 1))
                    continue
                break

            if not 200 <= response.status_code < 300:
                last_error_code = error_code_for_status(response.status_code)
                last_error = (
                    "HTTP {0} ({1}) from {2} on attempt {3}/{4}. Body preview: {5}"
                ).format(
                    response.status_code,
                    response.reason_phrase,
                    redacted_url,
                    attempt,
                    max_attempts,
                    last_body_preview,
                )
                if is_retryable_status(response.status_code) and attempt < max_attempts:
                    await asyncio.sleep(_retry_delay(response, attempt - 1))
                    continue
                break

            try:
                payload = response.json()
            except (json.JSONDecodeError, ValueError) as exc:
                last_error_code = ErrorCode.MALFORMED_PAYLOAD
                last_error = (
                    "Response from {0} was not valid JSON (attempt {1}/{2}): {3}. "
                    "Body preview: {4}"
                ).format(redacted_url, attempt, max_attempts, exc, last_body_preview)
                break

            missing = _missing_keys(payload, expect_keys)
            if missing:
                last_error_code = ErrorCode.UNEXPECTED_PAYLOAD_SHAPE
                last_error = (
                    "Response from {0} is missing expected key(s) {1} (attempt "
                    "{2}/{3}). The payload was not read and no field was guessed."
                ).format(redacted_url, ", ".join(missing), attempt, max_attempts)
                break

            return HttpResult(
                ok=True,
                method="GET",
                url=redacted_url,
                status_code=response.status_code,
                latency_ms=_elapsed_ms(),
                attempts=attempt,
                payload=payload,
                raw_path=raw_paths[-1] if raw_paths else None,
                raw_paths=tuple(raw_paths),
                error=None,
                error_code=None,
                response_headers=last_headers,
                body_preview=last_body_preview,
                content_type=last_content_type,
            )
    except Exception as exc:  # defensive: convert surprises into an error state
        return _http_failure_result(
            url=redacted_url,
            latency_ms=_elapsed_ms(),
            attempts=max(attempts, 1),
            error_code=ErrorCode.UNEXPECTED_ERROR,
            error="Unexpected {0} calling {1}: {2}".format(
                type(exc).__name__, redacted_url, exc
            ),
            status_code=last_status,
            response_headers=last_headers,
            body_preview=last_body_preview,
            content_type=last_content_type,
            raw_paths=raw_paths,
        )
    finally:
        if owns_client:
            await _close_client(active_client)

    # The loop only falls through when every attempt failed (or was not retried).
    return _http_failure_result(
        url=redacted_url,
        latency_ms=_elapsed_ms(),
        attempts=max(attempts, 1),
        error_code=last_error_code or ErrorCode.UNEXPECTED_ERROR,
        error=last_error or "Request failed without a stated reason.",
        status_code=last_status,
        response_headers=last_headers,
        body_preview=last_body_preview,
        content_type=last_content_type,
        raw_paths=raw_paths,
    )


# --------------------------------------------------------------------------- #
# Raw evidence records (rule 5: written before anything is parsed)
# --------------------------------------------------------------------------- #
def _slug(value: str) -> str:
    """Filesystem-safe slug for a data source id."""
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in value.strip().lower())
    return cleaned.strip("-") or "source"


def _timestamp_slug() -> str:
    """Compact UTC timestamp for evidence filenames."""
    return utc_now().strftime("%Y%m%dT%H%M%SZ")


def _raw_record(
    *,
    source: str,
    url: str,
    request_params: Mapping[str, Any],
    request_headers: Mapping[str, str],
    response: Optional[httpx.Response],
    body_text: Optional[str],
    latency_ms: float,
    attempt: int,
    max_attempts: int,
    transport_error: Optional[BaseException],
    context: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Build the evidence record for one request attempt.

    ``response.body_text`` is stored verbatim (never re-serialized), with its
    byte length and SHA-256 so the stored evidence can be verified independently.
    ``response`` is ``None`` when the request never got an answer, in which case
    the transport error is recorded instead.
    """
    return {
        "record_version": 1,
        "source": source,
        "retrieved_at": utc_now_iso(),
        "attempt": attempt,
        "max_attempts": max_attempts,
        "raw_logged_before_parsing": True,
        "request": {
            "method": "GET",
            "url": url,
            "params": redact_params(request_params),
            "headers": dict(request_headers),
        },
        "response": None
        if response is None
        else {
            "status_code": response.status_code,
            "reason_phrase": response.reason_phrase,
            "content_type": response.headers.get("content-type"),
            "headers": redact_headers(dict(response.headers)),
            "body_text": body_text,
            "body_chars": len(body_text) if body_text is not None else None,
            "body_sha256": sha256_text(body_text) if body_text is not None else None,
        },
        "error": None
        if transport_error is None
        else {
            "type": type(transport_error).__name__,
            "message": redact_text(str(transport_error)),
        },
        "latency_ms": latency_ms,
        "context": dict(context or {}),
    }
# --------------------------------------------------------------------------- #
# Raw evidence persistence
# --------------------------------------------------------------------------- #
def raw_evidence_dir() -> Path:
    """Directory holding raw response evidence (created lazily on write)."""
    return Path(config.RAW_RESPONSE_DIR)


def _write_raw_record(*, source: str, record: Mapping[str, Any]) -> str:
    """Persist one evidence record and return its project-relative path.

    Raises :class:`_RawLoggingError` when evidence cannot be stored, which the
    caller converts into a failed :class:`HttpResult`. Untraceable data is never
    handed back as if it were traceable.
    """
    if not config.LOG_RAW_RESPONSES:
        raise _RawLoggingError(
            ErrorCode.LOGGING_DISABLED,
            (
                "Raw response logging is disabled (LOG_RAW_RESPONSES=false), so the "
                "response cannot be traced back to its source. .clinerules rule 5 "
                "requires logging raw responses before processing them."
            ),
        )

    directory = raw_evidence_dir()
    filename = "{0}_{1}_{2}.json".format(
        _slug(source), _timestamp_slug(), uuid.uuid4().hex[:8]
    )
    path = directory / filename
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(record, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except OSError as exc:
        raise _RawLoggingError(
            ErrorCode.LOGGING_FAILED,
            "Could not write raw response evidence to {0}: {1}".format(path, exc),
        ) from exc

    try:
        return str(path.relative_to(config.PROJECT_ROOT))
    except ValueError:
        # Evidence stored outside the project tree (custom RAW_RESPONSE_DIR): keep
        # the absolute path rather than pretending it is project-relative.
        return str(path)


def write_raw_record(*, source: str, record: Mapping[str, Any]) -> str:
    """Persist an arbitrary evidence record (public seam for non-HTTP callers).

    The HTTP fetchers use :func:`_write_raw_record` directly; other layers that must
    also log "before parsing" evidence -- the fusion model call, where the stored
    record *is* the audit trail of the AI's reasoning -- go through this wrapper so
    naming and error behaviour stay identical. Returns the stored path and raises
    ``_RawLoggingError`` when nothing can be written.
    """
    return _write_raw_record(source=source, record=record)


# --------------------------------------------------------------------------- #
# Evidence reader (backs GET /evidence/{assessment_id})
# --------------------------------------------------------------------------- #
def _resolve_raw_path(raw_path: str) -> Path:
    """Resolve a stored evidence path, refusing anything outside the evidence dir."""
    if not raw_path or not raw_path.strip():
        raise ValueError("raw_path must be a non-empty string")
    candidate = Path(raw_path.strip())
    if not candidate.is_absolute():
        candidate = Path(config.PROJECT_ROOT) / candidate
    resolved = candidate.resolve()
    evidence_root = raw_evidence_dir().resolve()
    if resolved != evidence_root and evidence_root not in resolved.parents:
        raise ValueError(
            "raw_path {0!r} is outside the raw evidence directory".format(raw_path)
        )
    return resolved


def load_raw_response(
    raw_path: str, *, max_chars: Optional[int] = None
) -> Dict[str, Any]:
    """Load one stored evidence record, truncating the body for display only.

    The file on disk is never modified: truncation applies to the returned copy,
    and ``body_truncated`` says whether it happened. Raises ``FileNotFoundError``
    when the record is missing and ``ValueError`` when the path escapes
    ``config.RAW_RESPONSE_DIR`` (so the debug endpoint cannot read arbitrary files).
    """
    limit = config.EVIDENCE_BODY_MAX_CHARS if max_chars is None else max_chars
    path = _resolve_raw_path(raw_path)
    if not path.is_file():
        raise FileNotFoundError("No raw evidence record at {0}".format(raw_path))

    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            "Raw evidence record {0} could not be read: {1}".format(raw_path, exc)
        ) from exc
    if not isinstance(record, dict):
        raise ValueError(
            "Raw evidence record {0} is not a JSON object".format(raw_path)
        )

    record["raw_response_path"] = raw_path
    record["body_truncated"] = False
    response = record.get("response")
    if isinstance(response, dict):
        body = response.get("body_text")
        if isinstance(body, str) and limit > 0 and len(body) > limit:
            response["body_text"] = truncate_text(body, limit)
            record["body_truncated"] = True
    return record
# --------------------------------------------------------------------------- #
# Failure mapping - HttpResult -> envelope
# --------------------------------------------------------------------------- #
def signals_from_failure(
    result: HttpResult,
    *,
    source: str,
    location: Optional[Location] = None,
    travel_date: Optional[datetime.date] = None,
    status: Optional[str] = None,
    warnings: Sequence[str] = (),
) -> NormalizedSignals:
    """Convert a failed :class:`HttpResult` into the normalized error envelope.

    This is how a source module reports a failure without inventing anything: the
    error text and code come straight from the HTTP layer, the raw evidence path
    is carried along, and no signals are produced. ``status`` can be overridden
    when the failure actually means "no data for this query".
    """
    if result.ok:
        raise ValueError(
            "signals_from_failure requires a failed HttpResult (ok=False); "
            "successful results must go through ok_signals()"
        )
    return failure_signals(
        source=source,
        error_code=result.error_code or ErrorCode.UNEXPECTED_ERROR,
        error=result.error
        or "Request to {0} failed without a stated reason.".format(result.url),
        location=location,
        travel_date=travel_date,
        raw_response_path=result.raw_path,
        raw_response_paths=result.raw_paths,
        latency_ms=result.latency_ms,
        warnings=warnings,
        status=status,
    )
__all__ = [
    # types
    "Endpoint",
    "EvidenceEntry",
    "HttpResult",
    "Location",
    "NormalizedSignals",
    "PartialSignals",
    "SignalEntry",
    "SubQueryFailure",
    # error-state vocabulary
    "ErrorCode",
    "error_code_for_status",
    "is_retryable_status",
    "status_for_error_code",
    # envelope builders
    "failure_signals",
    "has_usable_signals",
    "make_evidence",
    "make_signal",
    "missing_credential_signals",
    "ok_signals",
    "partial_signals",
    "signals_from_failure",
    "to_source_status",
    "unavailable_signals",
    # http + evidence
    "get_json",
    "load_raw_response",
    "raw_evidence_dir",
    "redact_headers",
    "redact_params",
    "redact_text",
    "redact_url",
    "sha256_text",
    "truncate_text",
    "write_raw_record",
    # time
    "iso_date",
    "utc_now",
    "utc_now_iso",
]

