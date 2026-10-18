"""Configuration surface tests, especially the fusion-model settings.

Fusion is not implemented yet, but the surface it will read is: which provider shape to
use, which model name, which endpoint and which key. These tests pin the *properties* of
that surface (defaults, accepted values, fallbacks, "never leak the key") rather than the
literal strings, so they stay true once a real free model and key are filled in.
"""

from __future__ import annotations

import json
import os
from typing import Any, List, Optional

import pytest

import config

SECRET_NAMES = (
    "AVIATIONSTACK_API_KEY",
    "TOMTOM_API_KEY",
    "DEEPSEEK_API_KEY",
    "FUSION_MODEL_API_KEY",
)


def _secret_values() -> List[str]:
    """Every configured credential value, read from config (never printed)."""
    values: List[str] = []
    for name in SECRET_NAMES:
        value = getattr(config, name, None)
        if value:
            values.append(value)
    return values


# --------------------------------------------------------------------------- #
# Provider selection
# --------------------------------------------------------------------------- #
def test_the_provider_default_is_the_generic_openai_compatible_path() -> None:
    """Fusion targets an OpenAI-compatible endpoint unless told otherwise."""
    assert config.FUSION_MODEL_PROVIDER_DEFAULT == "openai_compatible"
    assert config.FUSION_MODEL_PROVIDERS == ("deepseek", "openai_compatible")
    assert config.FUSION_MODEL_PROVIDER in config.FUSION_MODEL_PROVIDERS


@pytest.mark.parametrize(
    "raw, expected, warned",
    [
        (None, "openai_compatible", False),
        ("", "openai_compatible", False),
        ("   ", "openai_compatible", False),
        ("deepseek", "deepseek", False),
        ("  DeepSeek  ", "deepseek", False),
        ("OPENAI_COMPATIBLE", "openai_compatible", False),
        ("opencode_zen", "openai_compatible", True),
    ],
)
def test_provider_parsing_normalizes_and_reports_typos(
    raw: Optional[str], expected: str, warned: bool
) -> None:
    """A typo falls back to a working default and says so; it never raises at import."""
    provider, warning = config.normalize_fusion_provider(raw)

    assert provider == expected
    assert (warning is not None) is warned
    if warned:
        assert raw is not None and raw.strip() in warning


def test_an_invalid_provider_would_be_surfaced_as_a_readiness_reason() -> None:
    """The warning is not swallowed: readiness repeats it for diagnostics."""
    readiness = config.fusion_readiness()

    assert isinstance(readiness["provider"], str)
    assert readiness["provider"] in config.FUSION_MODEL_PROVIDERS
    if config.FUSION_PROVIDER_WARNING is not None:
        assert config.FUSION_PROVIDER_WARNING in readiness["reasons"]

# --------------------------------------------------------------------------- #
# Endpoint, key and model name
# --------------------------------------------------------------------------- #
def test_the_fusion_endpoint_falls_back_to_the_deepseek_variable() -> None:
    """`DEEPSEEK_BASE_URL` remains the historical name for the same endpoint."""
    if os.getenv("FUSION_MODEL_BASE_URL"):
        pytest.skip("FUSION_MODEL_BASE_URL is explicitly set in this environment")
    assert config.FUSION_MODEL_BASE_URL == config.DEEPSEEK_BASE_URL
    assert config.FUSION_MODEL_BASE_URL


def test_the_fusion_key_falls_back_to_the_deepseek_key() -> None:
    if os.getenv("FUSION_MODEL_API_KEY"):
        pytest.skip("FUSION_MODEL_API_KEY is explicitly set in this environment")
    assert config.FUSION_MODEL_API_KEY == config.DEEPSEEK_API_KEY


def test_the_model_name_is_never_invented() -> None:
    """An unset model stays empty; fusion must refuse rather than pick one in code."""
    assert isinstance(config.FUSION_MODEL_NAME, str)
    if not os.getenv("FUSION_MODEL_NAME"):
        assert config.FUSION_MODEL_NAME == ""
        assert config.fusion_readiness()["model"] is None


@pytest.mark.parametrize(
    "key, placeholder",
    [
        (None, False),
        ("", False),
        ("skip_for_now_or_fill_later", True),
        ("CHANGEME", True),
        ("your_api_key_here", True),
        ("placeholder", True),
        ("abc123def456", False),
        ("9f8e7d6c5b4a39281706f5e4d3c2b1a0", False),
    ],
)
def test_placeholder_keys_are_recognised(key: Optional[str], placeholder: bool) -> None:
    assert config.looks_like_placeholder_key(key) is placeholder


# --------------------------------------------------------------------------- #
# Readiness and the diagnostics projection
# --------------------------------------------------------------------------- #
def test_readiness_is_honest_and_never_includes_the_key() -> None:
    readiness = config.fusion_readiness()

    assert set(readiness) == {
        "ready",
        "reasons",
        "provider",
        "model",
        "base_url",
        "api_key_present",
    }
    assert readiness["ready"] == (not readiness["reasons"])
    assert isinstance(readiness["base_url"], str) and readiness["base_url"]
    assert isinstance(readiness["api_key_present"], bool)

    dumped = json.dumps(readiness)
    for secret in _secret_values():
        assert secret not in dumped


def test_the_shipped_placeholder_key_blocks_readiness() -> None:
    """`skip_for_now_or_fill_later` is present but unusable, and readiness says so."""
    if not config.FUSION_MODEL_API_KEY:
        pytest.skip("no fusion key is configured at all")
    if not config.looks_like_placeholder_key(config.FUSION_MODEL_API_KEY):
        pytest.skip("a real fusion key is configured")

    readiness = config.fusion_readiness()

    assert readiness["ready"] is False
    assert readiness["api_key_present"] is True
    assert any("placeholder" in reason for reason in readiness["reasons"])


def test_as_dict_exposes_the_fusion_surface_without_secrets() -> None:
    settings: Any = config.as_dict()

    assert settings["fusion_provider"] == config.FUSION_MODEL_PROVIDER
    assert settings["fusion_base_url"] == config.FUSION_MODEL_BASE_URL
    assert settings["fusion_model"] == (config.FUSION_MODEL_NAME or None)
    assert settings["fusion_ready"] == config.fusion_readiness()["ready"]

    dumped = json.dumps(settings)
    for secret in _secret_values():
        assert secret not in dumped

