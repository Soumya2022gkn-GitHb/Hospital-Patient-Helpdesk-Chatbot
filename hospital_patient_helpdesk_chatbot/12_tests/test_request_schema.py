"""Test canonical API request-schema validation."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "07_backend" / "13_request_schema.py"
SPEC = importlib.util.spec_from_file_location("phase13_request_schema", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
schema = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = schema
SPEC.loader.exec_module(schema)


def test_valid_request_is_normalized() -> None:
    result = schema.validate_request_payload(
        {
            "question": "  Where   is cardiology?  ",
            "department": " Cardiology ",
            "session_id": "session-001",
            "unknown": "ignored",
        }
    )
    assert result.valid is True
    assert result.normalized_request.question == "Where is cardiology?"
    assert result.normalized_request.department == "Cardiology"
    assert result.rejected_fields == ["unknown"]
    assert result.warnings


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"question": "x"}, "at least 2 characters"),
        ({"question": "A" * 1001}, "must not exceed 1000 characters"),
        ({"question": "My SSN is 123-45-6789"}, "sensitive identifiers"),
        ({"question": "Where is billing?", "language": "invalid-language-code"}, "language"),
        ({"question": "Where is billing?", "urgency": "soon"}, "urgency"),
    ],
)
def test_invalid_requests_are_rejected(payload: dict, expected: str) -> None:
    result = schema.validate_request_payload(payload)
    assert result.valid is False
    assert any(expected in error for error in result.errors)


def test_sensitive_identifiers_can_be_allowed_for_internal_review() -> None:
    config = schema.RequestValidationConfig(allow_sensitive_identifiers=True)
    result = schema.validate_request_payload(
        {"question": "My SSN is 123-45-6789"}, config
    )
    assert result.valid is True


def test_optional_pydantic_model_is_dependency_gated() -> None:
    pytest.importorskip("pydantic")
    model = schema.create_pydantic_request_model()
    request = model(question="Where is cardiology?").to_chat_request()
    assert request.question == "Where is cardiology?"
