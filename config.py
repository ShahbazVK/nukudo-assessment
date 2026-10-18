"""Central configuration for the Travel Disruption Assessor.

Every secret is read from an environment variable. A local ``.env`` file (loaded
with python-dotenv) is supported for development, but real environment variables
always win. Never hardcode a credential in this file or anywhere else.

This module must not import any project module, so it can be imported from the
backend, the data sources, and scripts without circular imports.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
PROJECT_ROOT: Path = Path(__file__).resolve().parent
LOG_DIR: Path = PROJECT_ROOT / "logs"
RAW_RESPONSE_DIR: Path = LOG_DIR / "raw"
# One JSON record per assessment: the fused answer, the three source envelopes, the
# exact fusion prompt/response, and the token usage. This is what
# ``GET /evidence/{assessment_id}`` serves.
ASSESSMENT_DIR: Path = LOG_DIR / "assessments"

# Load `.env` from the project root if it exists; real env vars take precedence.
load_dotenv(dotenv_path=PROJECT_ROOT / ".env", override=False)


# --------------------------------------------------------------------------- #
# Environment helpers
# --------------------------------------------------------------------------- #
def _get_env(name: str, default: Optional[str] = None) -> Optional[str]:
    """Return an environment variable, treating empty strings as unset."""
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _get_bool_env(name: str, default: bool) -> bool:
    """Parse a boolean environment variable ("1/true/yes/on" => True)."""
    raw = _get_env(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _get_float_env(name: str, default: float) -> float:
    """Parse a float environment variable, falling back to ``default``."""
    raw = _get_env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _get_int_env(name: str, default: int) -> int:
    """Parse an int environment variable, falling back to ``default``."""
    raw = _get_env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# --------------------------------------------------------------------------- #
# App metadata and output contract
# --------------------------------------------------------------------------- #
APP_NAME: str = "Travel Disruption Assessor"
APP_VERSION: str = "0.1.0"
SCHEMA_VERSION: str = "0.1.0"

RISK_LEVELS: Tuple[str, ...] = ("low", "moderate", "high", "severe", "unknown")
STATUS_OK: str = "ok"
STATUS_PARTIAL: str = "partial"
STATUS_ERROR: str = "error"
# A source that was reached successfully but legitimately has no data for this
# query (e.g. Aviationstack free-tier plan limit, no records for the date). It is
# *not* a failure of the source, and it is never silently treated as "no risk".
STATUS_UNAVAILABLE: str = "unavailable"

TRAVEL_DATE_FORMAT: str = "%Y-%m-%d"


# --------------------------------------------------------------------------- #
# Credentials
# Empty/None when not configured. Never substitute a default key.
# --------------------------------------------------------------------------- #
AVIATIONSTACK_API_KEY: Optional[str] = _get_env("AVIATIONSTACK_API_KEY")
TOMTOM_API_KEY: Optional[str] = _get_env("TOMTOM_API_KEY")
DEEPSEEK_API_KEY: Optional[str] = _get_env("DEEPSEEK_API_KEY")

# NWS needs no API key, but rejects requests without a descriptive User-Agent
# identifying the app and a contact address.
NWS_USER_AGENT: str = (
    _get_env("NWS_USER_AGENT", "(travel-disruption-assessor, you@example.com)")
    or "(travel-disruption-assessor, you@example.com)"
)


# --------------------------------------------------------------------------- #
# Data source endpoints and identifiers
# --------------------------------------------------------------------------- #
AVIATIONSTACK_BASE_URL: str = (
    _get_env("AVIATIONSTACK_BASE_URL", "https://api.aviationstack.com/v1")
    or "https://api.aviationstack.com/v1"
)
NWS_BASE_URL: str = _get_env("NWS_BASE_URL", "https://api.weather.gov") or (
    "https://api.weather.gov"
)
TOMTOM_BASE_URL: str = _get_env("TOMTOM_BASE_URL", "https://api.tomtom.com") or (
    "https://api.tomtom.com"
)
GDELT_BASE_URL: str = (
    _get_env("GDELT_BASE_URL", "https://api.gdeltproject.org/api/v2/doc/doc")
    or "https://api.gdeltproject.org/api/v2/doc/doc"
)
GDELT_ENABLED: bool = _get_bool_env("GDELT_ENABLED", False)

SOURCE_AVIATIONSTACK: str = "aviationstack"
SOURCE_NWS: str = "nws"
SOURCE_TOMTOM: str = "tomtom"
SOURCE_GDELT: str = "gdelt"
SOURCE_DEEPSEEK: str = "deepseek"

# Required credential per source, used by `missing_credentials()`/`is_configured()`.
REQUIRED_CREDENTIALS: Dict[str, str] = {
    SOURCE_AVIATIONSTACK: "AVIATIONSTACK_API_KEY",
    SOURCE_TOMTOM: "TOMTOM_API_KEY",
    SOURCE_DEEPSEEK: "DEEPSEEK_API_KEY",
}


# --------------------------------------------------------------------------- #
# AI fusion layer
#
# The fusion model is deliberately **not** tied to DeepSeek's own API: fusion calls an
# OpenAI-compatible ``/chat/completions`` endpoint, so a free hosted model, a local
# server, or DeepSeek itself all work. `DEEPSEEK_BASE_URL` is the historical name for
# that endpoint and is still read; `FUSION_MODEL_BASE_URL` overrides it. `DEEPSEEK_*`
# settings below are kept so an existing `.env` keeps working.
# --------------------------------------------------------------------------- #
DEEPSEEK_BASE_URL: str = _get_env("DEEPSEEK_BASE_URL", "https://api.deepseek.com") or (
    "https://api.deepseek.com"
)
DEEPSEEK_MODEL: str = _get_env("DEEPSEEK_MODEL", "deepseek-flash") or "deepseek-flash"
DEEPSEEK_TIMEOUT_SECONDS: float = _get_float_env("DEEPSEEK_TIMEOUT_SECONDS", 30.0)
DEEPSEEK_MAX_RETRIES: int = _get_int_env("DEEPSEEK_MAX_RETRIES", 1)
# Low temperature: we want consistent severity reasoning, not creative writing.
DEEPSEEK_TEMPERATURE: float = _get_float_env("DEEPSEEK_TEMPERATURE", 0.1)

# Which auth/request shape fusion uses. `deepseek` selects DeepSeek's own API,
# `openai_compatible` the generic OpenAI-compatible path that hosted free models expose.
FUSION_MODEL_PROVIDERS: Tuple[str, ...] = ("deepseek", "openai_compatible")
FUSION_MODEL_PROVIDER_DEFAULT: str = "openai_compatible"


def normalize_fusion_provider(raw: Optional[str]) -> Tuple[str, Optional[str]]:
    """Return ``(provider, warning)`` for a raw ``FUSION_MODEL_PROVIDER`` value.

    A typo must not take the app down at import time, and it must not be silently
    swallowed either: the configured default is used and the warning is surfaced by
    :func:`fusion_readiness` and in the logs.
    """
    if raw is None or not raw.strip():
        return FUSION_MODEL_PROVIDER_DEFAULT, None
    candidate = raw.strip().lower()
    if candidate in FUSION_MODEL_PROVIDERS:
        return candidate, None
    return FUSION_MODEL_PROVIDER_DEFAULT, (
        "FUSION_MODEL_PROVIDER={0!r} is not one of {1}; falling back to {2!r}.".format(
            raw.strip(), ", ".join(FUSION_MODEL_PROVIDERS), FUSION_MODEL_PROVIDER_DEFAULT
        )
    )


FUSION_MODEL_PROVIDER, FUSION_PROVIDER_WARNING = normalize_fusion_provider(
    _get_env("FUSION_MODEL_PROVIDER")
)
# Empty until a model is chosen. Fusion must refuse to run on an empty name rather than
# guess one: the model is a deployment choice, not something to invent in code.
FUSION_MODEL_NAME: str = _get_env("FUSION_MODEL_NAME", "") or ""
FUSION_MODEL_BASE_URL: str = (
    _get_env("FUSION_MODEL_BASE_URL", DEEPSEEK_BASE_URL) or DEEPSEEK_BASE_URL
)
# The key for the fusion endpoint. Falls back to the DeepSeek variable so an existing
# `.env` keeps working. It is never defaulted to a placeholder and never printed.
FUSION_MODEL_API_KEY: Optional[str] = _get_env("FUSION_MODEL_API_KEY") or DEEPSEEK_API_KEY

# Request shape for the fusion call. One retry by default, and retries are only ever
# spent on a transport error: a re-generated assessment is a *different* assessment, so
# a malformed or invalid answer is reported as an error rather than re-rolled until it
# happens to validate.
FUSION_TIMEOUT_SECONDS: float = _get_float_env(
    "FUSION_TIMEOUT_SECONDS", DEEPSEEK_TIMEOUT_SECONDS
)
FUSION_TRANSPORT_RETRIES: int = max(
    0, _get_int_env("FUSION_TRANSPORT_RETRIES", DEEPSEEK_MAX_RETRIES)
)
FUSION_MODEL_TEMPERATURE: float = _get_float_env(
    "FUSION_MODEL_TEMPERATURE", DEEPSEEK_TEMPERATURE
)
# Prompt size guard. One live JFK->LAX query returned 147 TomTom incidents; sending all
# of them costs more context than it buys, so each source is capped at this many signals
# (ranked by severity, then distance to the airport) and the cut is disclosed in both the
# prompt and the response's uncertainty list.
FUSION_MAX_SIGNALS_PER_SOURCE: int = max(
    1, _get_int_env("FUSION_MAX_SIGNALS_PER_SOURCE", 25)
)

# A key that is obviously a placeholder (the shipped `.env` holds one) must not be spent
# on a real request, and "present but fake" must not be reported as ready. This is a
# documented heuristic, not validation: provider key formats vary and are not ours to
# assume.
_PLACEHOLDER_KEY_MARKERS: Tuple[str, ...] = (
    "skip",
    "changeme",
    "change_me",
    "placeholder",
    "your_",
    "replace_me",
    "xxx",
)


# --------------------------------------------------------------------------- #
# Runtime / HTTP behaviour
# --------------------------------------------------------------------------- #
HTTP_TIMEOUT_SECONDS: float = _get_float_env("HTTP_TIMEOUT_SECONDS", 10.0)
HTTP_MAX_RETRIES: int = _get_int_env("HTTP_MAX_RETRIES", 2)

# Retry backoff for transport failures and retryable HTTP statuses (429/5xx):
# delay = min(BASE * 2**attempt, MAX), and a `Retry-After` header wins when present.
HTTP_BACKOFF_BASE_SECONDS: float = _get_float_env("HTTP_BACKOFF_BASE_SECONDS", 0.5)
HTTP_BACKOFF_MAX_SECONDS: float = _get_float_env("HTTP_BACKOFF_MAX_SECONDS", 4.0)

# Raw API responses are logged before processing, for evidence traceability.
LOG_RAW_RESPONSES: bool = _get_bool_env("LOG_RAW_RESPONSES", True)
LOG_LEVEL: str = (_get_env("LOG_LEVEL", "INFO") or "INFO").upper()

# Evidence display limits: raw bodies are stored in full on disk, then truncated
# only when they are surfaced (GET /evidence) or quoted in a message.
EVIDENCE_BODY_MAX_CHARS: int = _get_int_env("EVIDENCE_BODY_MAX_CHARS", 4000)
EVIDENCE_VALUE_MAX_CHARS: int = _get_int_env("EVIDENCE_VALUE_MAX_CHARS", 500)


# --------------------------------------------------------------------------- #
# Servers
# --------------------------------------------------------------------------- #
API_HOST: str = _get_env("API_HOST", "127.0.0.1") or "127.0.0.1"
API_PORT: int = _get_int_env("API_PORT", 8000)
BACKEND_URL: str = _get_env("BACKEND_URL", "http://127.0.0.1:8000") or (
    "http://127.0.0.1:8000"
)
FRONTEND_ORIGINS: List[str] = [
    "http://localhost:8501",
    "http://127.0.0.1:8501",
]


# --------------------------------------------------------------------------- #
# Introspection helpers
# Used to communicate uncertainty explicitly - never to supply fallback values.
# --------------------------------------------------------------------------- #
def missing_credentials() -> List[str]:
    """Return the names of required-but-unset environment variables."""
    return [
        env_var
        for env_var in REQUIRED_CREDENTIALS.values()
        if _get_env(env_var) is None
    ]


def is_configured(source: str) -> bool:
    """Whether the credential required by ``source`` is present (keyless => True)."""
    env_var = REQUIRED_CREDENTIALS.get(source)
    if env_var is None:
        return True
    return _get_env(env_var) is not None


def looks_like_placeholder_key(key: Optional[str]) -> bool:
    """Whether a key is obviously a placeholder rather than a credential.

    Deliberately conservative: only explicit marker words count, because key formats are
    provider-specific and a false positive here would block a real key.
    """
    if not key:
        return False
    lowered = key.lower()
    return any(marker in lowered for marker in _PLACEHOLDER_KEY_MARKERS)


def fusion_readiness() -> Dict[str, object]:
    """Whether the fusion model can be called, and every reason it cannot.

    Never returns the key itself, only whether one is present: this is consumed by
    diagnostics endpoints and logs. ``ready`` is true only when a model name and a
    non-placeholder key are both configured.
    """
    reasons: List[str] = []
    if FUSION_PROVIDER_WARNING is not None:
        reasons.append(FUSION_PROVIDER_WARNING)
    if not FUSION_MODEL_NAME:
        reasons.append(
            "FUSION_MODEL_NAME is empty: the fusion model has not been chosen yet."
        )
    if not FUSION_MODEL_API_KEY:
        reasons.append(
            "No fusion API key is configured (FUSION_MODEL_API_KEY, falling back to "
            "DEEPSEEK_API_KEY)."
        )
    elif looks_like_placeholder_key(FUSION_MODEL_API_KEY):
        reasons.append(
            "The configured fusion API key looks like a placeholder, so no request will "
            "be made with it."
        )
    return {
        "ready": not reasons,
        "reasons": reasons,
        "provider": FUSION_MODEL_PROVIDER,
        "model": FUSION_MODEL_NAME or None,
        "base_url": FUSION_MODEL_BASE_URL,
        "api_key_present": FUSION_MODEL_API_KEY is not None,
    }


def as_dict() -> Dict[str, object]:
    """Return non-secret settings for diagnostics endpoints and log records."""
    return {
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "schema_version": SCHEMA_VERSION,
        "deepseek_model": DEEPSEEK_MODEL,
        "deepseek_base_url": DEEPSEEK_BASE_URL,
        "fusion_provider": FUSION_MODEL_PROVIDER,
        "fusion_model": FUSION_MODEL_NAME or None,
        "fusion_base_url": FUSION_MODEL_BASE_URL,
        "fusion_ready": fusion_readiness()["ready"],
        "fusion_timeout_seconds": FUSION_TIMEOUT_SECONDS,
        "fusion_max_signals_per_source": FUSION_MAX_SIGNALS_PER_SOURCE,
        "http_timeout_seconds": HTTP_TIMEOUT_SECONDS,
        "http_max_retries": HTTP_MAX_RETRIES,
        "log_raw_responses": LOG_RAW_RESPONSES,
        "log_level": LOG_LEVEL,
        "gdelt_enabled": GDELT_ENABLED,
        "sources_configured": {
            source: is_configured(source) for source in REQUIRED_CREDENTIALS
        },
    }
