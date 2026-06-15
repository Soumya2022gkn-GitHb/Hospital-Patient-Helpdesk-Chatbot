"""Test Streamlit UI helper behavior without launching a browser."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "08_app" / "15_streamlit_app.py"
SPEC = importlib.util.spec_from_file_location("phase15_streamlit_app", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
app = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = app
SPEC.loader.exec_module(app)


def sample_response() -> dict:
    return {
        "request_id": "REQ-ABCDEF123456",
        "answer": "Radiology is on the lower ground floor. [S1]",
        "mode": "grounded_answer",
        "citations": ["[S1]"],
        "sources": [
            {
                "citation": "[S1]",
                "source_file": "department_info.csv",
                "department": "Radiology",
                "score": 0.75,
            }
        ],
        "retrieval_confidence": "high",
        "safety_flag": False,
        "guardrail_action": "pass",
        "risk_level": "low",
        "triggered_rules": [],
        "provider": "offline",
        "model": "offline-grounded-v1",
        "latency_ms": 4.2,
    }


def test_source_badges_are_compact() -> None:
    badges = app.source_badges(sample_response()["sources"])
    assert badges == ["[S1] department_info.csv (Radiology, score 0.750)"]


def test_safety_banner_for_pass_and_override() -> None:
    assert "passed" in app.safety_banner(sample_response()).casefold()
    response = sample_response()
    response.update({"guardrail_action": "override", "risk_level": "high"})
    assert "override" in app.safety_banner(response).casefold()


def test_response_markdown_includes_sources() -> None:
    rendered = app.response_to_markdown(sample_response())
    assert "Radiology" in rendered
    assert "**Sources:**" in rendered
    assert "[S1]" in rendered


def test_transcript_rows_flatten_turns() -> None:
    turn = app.ChatTurn(
        turn_id="TURN-001",
        question="Where is radiology?",
        response=sample_response(),
        created_at_utc="2026-06-15T00:00:00+00:00",
    )
    rows = app.transcript_rows([turn])
    assert rows[0]["turn_id"] == "TURN-001"
    assert rows[0]["citation_count"] == 1
    assert rows[0]["source_count"] == 1


def test_streamlit_dependency_is_available_for_ui() -> None:
    pytest.importorskip("streamlit")
