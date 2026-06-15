"""Test canonical API response-schema validation."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "07_backend" / "14_response_schema.py"
SPEC = importlib.util.spec_from_file_location("phase14_response_schema", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
schema = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = schema
SPEC.loader.exec_module(schema)


def valid_payload() -> dict:
    return {
        "request_id": "REQ-ABCDEF123456",
        "answer": "Radiology is on the lower ground floor. [S1]",
        "mode": "grounded_answer",
        "citations": ["[S1]"],
        "sources": [{"citation": "[S1]", "source_file": "department_info.csv", "score": 0.75}],
        "retrieval_confidence": "high",
        "safety_flag": False,
        "guardrail_action": "pass",
        "risk_level": "low",
        "triggered_rules": [],
        "provider": "offline",
        "model": "offline-grounded-v1",
        "latency_ms": 4.2,
        "timestamp_utc": "2026-06-15T19:33:44.156495+00:00",
    }


def test_valid_response_passes() -> None:
    result = schema.validate_response_payload(valid_payload())
    assert result.valid is True
    assert result.normalized_response.answer.startswith("Radiology")
    assert result.normalized_response.sources[0].citation == "[S1]"


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        ({"answer": ""}, "answer must not be empty"),
        ({"citations": ["[S9]"]}, "citations not present"),
        ({"citations": []}, "grounded answers with sources"),
        ({"latency_ms": -1}, "latency_ms must not be negative"),
        ({"retrieval_confidence": "certain"}, "retrieval_confidence"),
    ],
)
def test_invalid_responses_are_rejected(updates: dict, expected: str) -> None:
    payload = valid_payload()
    payload.update(updates)
    result = schema.validate_response_payload(payload)
    assert result.valid is False
    assert any(expected in error for error in result.errors)


def test_safety_flag_consistency() -> None:
    payload = valid_payload()
    payload.update(
        {
            "mode": "unsafe_medical_advice",
            "citations": [],
            "sources": [],
            "safety_flag": True,
            "guardrail_action": "override",
            "risk_level": "high",
            "triggered_rules": ["GR-003_UNSAFE_MEDICAL_REQUEST"],
        }
    )
    result = schema.validate_response_payload(payload)
    assert result.valid is True


def test_optional_pydantic_response_model_is_dependency_gated() -> None:
    pytest.importorskip("pydantic")
    model = schema.create_pydantic_response_model()
    response = model(**valid_payload())
    assert response.request_id == "REQ-ABCDEF123456"
