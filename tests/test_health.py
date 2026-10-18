"""The API's liveness endpoint, exercised in-process (no network, no live sources).

Config gained a fusion-model surface this turn, so this pins that ``/health`` still
answers and that the new settings are visible there *without* any credential value.
"""

from __future__ import annotations

import json
from typing import Any, List

from fastapi.testclient import TestClient

import config
from backend.main import app

SECRET_NAMES = (
    "AVIATIONSTACK_API_KEY",
    "TOMTOM_API_KEY",
    "DEEPSEEK_API_KEY",
    "FUSION_MODEL_API_KEY",
)


def _secret_values() -> List[str]:
    """Every configured credential value, read from config (never printed)."""
    return [
        value
        for value in (getattr(config, name, None) for name in SECRET_NAMES)
        if value
    ]


def test_health_reports_the_fusion_and_source_surfaces() -> None:
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    body: Any = response.json()
    assert body["status"] == config.STATUS_OK
    assert body["components"]["api_layer"] == "ok"
    # Implemented this turn; fusion is reported as usable only when it really is.
    assert body["components"]["data_sources"] == "ok"
    ready = config.fusion_readiness()["ready"]
    assert body["components"]["ai_fusion"] == ("ok" if ready else "unconfigured")

    settings = body["settings"]
    assert settings["fusion_provider"] == config.FUSION_MODEL_PROVIDER
    assert settings["fusion_base_url"] == config.FUSION_MODEL_BASE_URL
    assert settings["fusion_model"] == (config.FUSION_MODEL_NAME or None)
    assert settings["fusion_ready"] == ready
    assert settings["schema_version"] == config.SCHEMA_VERSION


def test_health_never_echoes_a_credential() -> None:
    response = TestClient(app).get("/health")

    for secret in _secret_values():
        assert secret not in response.text
    # A key marker that would only appear if config were serialized wholesale.
    assert "api_key" not in json.dumps(response.json()["settings"]).lower()
