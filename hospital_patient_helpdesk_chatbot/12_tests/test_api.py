"""Test request validation and guarded chat responses."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "07_backend" / "12_api_main.py"
SPEC = importlib.util.spec_from_file_location("phase12_api_main", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
api_main = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = api_main
SPEC.loader.exec_module(api_main)


@pytest.fixture(scope="module")
def service():
    return api_main.ChatService(PROJECT_ROOT, api_main.ApiConfig(provider="offline"))


def test_request_normalization() -> None:
    request = api_main.ChatRequest.from_mapping(
        {"question": "  Where   is cardiology?  "}
    )
    assert request.question == "Where is cardiology?"


@pytest.mark.parametrize("question", ["", " ", "x", "a" * 1001])
def test_invalid_questions_are_rejected(question: str) -> None:
    with pytest.raises(ValueError):
        api_main.ChatRequest.from_mapping({"question": question})


def test_health_is_ready(service) -> None:
    health = service.health()
    assert health["status"] == "ready"
    assert health["index_ready"] is True
    assert health["provider"] == "offline"


def test_grounded_chat_response(service) -> None:
    response = service.chat(
        api_main.ChatRequest.from_mapping({"question": "Where is cardiology?"})
    )
    assert response["mode"] == "grounded_answer"
    assert response["guardrail_action"] == "pass"
    assert response["safety_flag"] is False
    assert response["citations"]
    assert response["sources"]


def test_emergency_chat_response(service) -> None:
    response = service.chat(
        api_main.ChatRequest.from_mapping(
            {"question": "I have severe chest pain. What is wrong with me?"}
        )
    )
    assert response["mode"] == "emergency"
    assert response["guardrail_action"] == "override"
    assert response["risk_level"] == "critical"
    assert response["citations"] == []
    assert response["sources"] == []
    assert "emergency" in response["answer"].casefold()


def test_fastapi_contract_when_dependencies_are_available() -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("pydantic")
    application = api_main.create_app(
        PROJECT_ROOT, api_main.ApiConfig(provider="offline")
    )
    paths = application.openapi()["paths"]
    assert "/chat" in paths
    assert "/health" in paths
    assert "post" in paths["/chat"]
