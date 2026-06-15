"""Store privacy-conscious thumbs feedback for chatbot answers.

Phase 19 turns Streamlit chat turns into a safe feedback log. The logger keeps
only review-friendly fields, validates thumbs-up/down ratings, sanitizes free
text comments, writes append-ready JSONL, and produces summary artifacts for
answer-quality follow-up.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, Iterable, Sequence


PHASE_ID: Final = "19"
PHASE_NAME: Final = "feedback_logger"
MODULE_VERSION: Final = "1.0"
VALID_RATINGS: Final = {"thumbs_up", "thumbs_down"}
VALID_REASON_TAGS: Final = {
    "helpful",
    "clear",
    "fast",
    "wrong_source",
    "unclear",
    "missing_detail",
    "safety_concern",
    "emergency_routing",
}
SENSITIVE_PATTERNS: Final = (
    ("email", re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)),
    ("phone", re.compile(r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b")),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("mrn", re.compile(r"\b(?:MRN|medical record number)\s*[:#-]?\s*[A-Z0-9-]{5,}\b", re.IGNORECASE)),
)


@dataclass(frozen=True)
class FeedbackLoggerConfig:
    """Filesystem contract for Phase 19."""

    project_root: Path
    transcript_path: Path
    output_dir: Path
    plots_dir: Path

    @classmethod
    def from_project_root(cls, project_root: Path) -> "FeedbackLoggerConfig":
        return cls(
            project_root=project_root,
            transcript_path=project_root / "01_data" / "processed" / "15_streamlit_transcript.json",
            output_dir=project_root / "01_data" / "processed",
            plots_dir=project_root / "01_data" / "processed" / "plots",
        )


@dataclass(frozen=True)
class FeedbackRecord:
    """One sanitized user-feedback event."""

    feedback_id: str
    turn_id: str
    request_id: str
    rating: str
    reason_tags: list[str]
    sanitized_comment: str
    question_category: str
    answer_mode: str
    safety_flag: bool
    guardrail_action: str
    source_count: int
    created_at_utc: str
    feedback_timestamp_utc: str


@dataclass(frozen=True)
class FeedbackLoggerResult:
    """Paths and headline metrics produced by Phase 19."""

    feedback_json_path: Path
    feedback_jsonl_path: Path
    report_path: Path
    audit_path: Path
    failed_path: Path
    rating_plot_path: Path
    reason_plot_path: Path
    total_feedback: int
    failed_feedback: int


def resolve_project_root(start: Path | None = None) -> Path:
    """Find the project root from the workspace, project, or notebook folder."""

    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "01_data").is_dir() and (candidate / "09_evaluation").is_dir():
            return candidate
        nested = candidate / "hospital_patient_helpdesk_chatbot"
        if (nested / "01_data").is_dir() and (nested / "09_evaluation").is_dir():
            return nested
    raise FileNotFoundError("Could not locate hospital_patient_helpdesk_chatbot project root.")


def read_transcript(path: Path) -> list[dict[str, object]]:
    """Read the Phase 15 Streamlit transcript."""

    if not path.exists():
        raise FileNotFoundError(f"Streamlit transcript not found: {path}")
    transcript = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(transcript, list) or not transcript:
        raise ValueError("Streamlit transcript must be a non-empty JSON list.")
    return transcript


def sanitize_comment(comment: str) -> tuple[str, list[str]]:
    """Redact sensitive identifiers from optional feedback comments."""

    sanitized = " ".join(comment.split())[:500]
    redactions: list[str] = []
    for label, pattern in SENSITIVE_PATTERNS:
        if pattern.search(sanitized):
            redactions.append(label)
            sanitized = pattern.sub(f"[REDACTED_{label.upper()}]", sanitized)
    return sanitized, redactions


def infer_question_category(turn: dict[str, object]) -> str:
    """Infer a compact category from response sources or question text."""

    response = turn.get("response", {})
    if isinstance(response, dict):
        sources = response.get("sources", [])
        if isinstance(sources, list) and sources:
            first_source = sources[0]
            if isinstance(first_source, dict) and first_source.get("content_category"):
                return str(first_source["content_category"])
        mode = str(response.get("mode", ""))
        if mode:
            return mode
    question = str(turn.get("question", "")).casefold()
    if "appointment" in question:
        return "appointments"
    if "department" in question:
        return "department_information"
    if "chest pain" in question or "emergency" in question:
        return "emergency"
    return "unknown"


def sample_feedback_for_turn(turn: dict[str, object], sequence: int) -> dict[str, object]:
    """Create deterministic demo feedback from the Phase 15 transcript."""

    response = turn.get("response", {})
    mode = str(response.get("mode", "")) if isinstance(response, dict) else ""
    if mode == "emergency":
        return {
            "rating": "thumbs_up",
            "reason_tags": ["emergency_routing", "clear"],
            "comment": "Emergency routing was clear and did not try to diagnose.",
        }
    if sequence == 2:
        return {
            "rating": "thumbs_up",
            "reason_tags": ["helpful", "clear"],
            "comment": "The department location was easy to understand.",
        }
    return {
        "rating": "thumbs_down",
        "reason_tags": ["missing_detail"],
        "comment": "Useful answer, but I wanted clearer next steps for scheduling.",
    }


def create_feedback_record(turn: dict[str, object], payload: dict[str, object], sequence: int) -> FeedbackRecord:
    """Validate and sanitize one feedback payload."""

    rating = str(payload.get("rating", "")).strip()
    if rating not in VALID_RATINGS:
        raise ValueError(f"Unsupported feedback rating: {rating}")

    reason_tags = [str(tag).strip() for tag in payload.get("reason_tags", []) if str(tag).strip()]
    invalid_tags = sorted(set(reason_tags).difference(VALID_REASON_TAGS))
    if invalid_tags:
        raise ValueError(f"Unsupported feedback reason tags: {', '.join(invalid_tags)}")

    comment, redactions = sanitize_comment(str(payload.get("comment", "")))
    if redactions and "safety_concern" not in reason_tags:
        reason_tags = [*reason_tags, "safety_concern"]

    response = turn.get("response", {})
    response_dict = response if isinstance(response, dict) else {}
    sources = response_dict.get("sources", [])
    source_count = len(sources) if isinstance(sources, list) else 0

    return FeedbackRecord(
        feedback_id=f"{PHASE_ID}_FB_{sequence:03d}",
        turn_id=str(turn.get("turn_id", f"TURN-{sequence:03d}")),
        request_id=str(response_dict.get("request_id", "")),
        rating=rating,
        reason_tags=reason_tags,
        sanitized_comment=comment,
        question_category=infer_question_category(turn),
        answer_mode=str(response_dict.get("mode", "")),
        safety_flag=bool(response_dict.get("safety_flag", False)),
        guardrail_action=str(response_dict.get("guardrail_action", "")),
        source_count=source_count,
        created_at_utc=str(turn.get("created_at_utc", "")),
        feedback_timestamp_utc=datetime.now(timezone.utc).isoformat(),
    )


def build_sample_feedback(transcript: Sequence[dict[str, object]]) -> tuple[list[FeedbackRecord], list[dict[str, str]]]:
    """Create deterministic sample feedback records and collect failures."""

    records: list[FeedbackRecord] = []
    failures: list[dict[str, str]] = []
    for sequence, turn in enumerate(transcript, start=1):
        try:
            records.append(create_feedback_record(turn, sample_feedback_for_turn(turn, sequence), sequence))
        except ValueError as error:
            failures.append({"turn_id": str(turn.get("turn_id", sequence)), "error": str(error)})
    return records, failures


def write_json(data: object, path: Path) -> None:
    """Write formatted UTF-8 JSON."""

    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def write_jsonl(records: Sequence[FeedbackRecord], path: Path) -> None:
    """Write append-friendly JSON Lines feedback log."""

    with path.open("w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")


def write_audit_csv(records: Sequence[FeedbackRecord], path: Path) -> None:
    """Write spreadsheet-friendly feedback audit."""

    fieldnames = [
        "feedback_id",
        "turn_id",
        "request_id",
        "rating",
        "reason_tags",
        "question_category",
        "answer_mode",
        "safety_flag",
        "guardrail_action",
        "source_count",
        "feedback_timestamp_utc",
    ]
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            payload = asdict(record)
            payload["reason_tags"] = ";".join(record.reason_tags)
            writer.writerow({key: payload[key] for key in fieldnames})


def render_rating_plot(records: Sequence[FeedbackRecord], output_path: Path) -> None:
    """Plot feedback rating counts."""

    import matplotlib.pyplot as plt

    counts = Counter(record.rating for record in records)
    labels = ["thumbs_up", "thumbs_down"]
    figure, axis = plt.subplots(figsize=(7, 4.5))
    bars = axis.bar(labels, [counts[label] for label in labels], color=["#54A24B", "#E45756"])
    axis.set_title("Phase 19 Feedback Ratings")
    axis.set_xlabel("Rating")
    axis.set_ylabel("Count")
    axis.bar_label(bars)
    figure.tight_layout()
    figure.savefig(output_path, dpi=160)
    plt.close(figure)


def render_reason_plot(records: Sequence[FeedbackRecord], output_path: Path) -> None:
    """Plot feedback reason tag counts."""

    import matplotlib.pyplot as plt

    counts = Counter(tag for record in records for tag in record.reason_tags)
    labels = sorted(counts)
    figure, axis = plt.subplots(figsize=(9, 4.8))
    bars = axis.bar(labels, [counts[label] for label in labels], color="#4C78A8")
    axis.set_title("Phase 19 Feedback Reason Tags")
    axis.set_xlabel("Reason tag")
    axis.set_ylabel("Count")
    axis.tick_params(axis="x", rotation=25)
    axis.bar_label(bars)
    figure.tight_layout()
    figure.savefig(output_path, dpi=160)
    plt.close(figure)


def create_report(records: Sequence[FeedbackRecord], failures: Sequence[dict[str, str]], config: FeedbackLoggerConfig) -> dict[str, object]:
    """Create summary metrics for Phase 19."""

    rating_counts = Counter(record.rating for record in records)
    reason_counts = Counter(tag for record in records for tag in record.reason_tags)
    mode_counts = Counter(record.answer_mode for record in records)
    safety_feedback = sum(record.safety_flag for record in records)
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "phase": PHASE_ID,
        "module": PHASE_NAME,
        "module_version": MODULE_VERSION,
        "transcript_file": str(config.transcript_path.resolve()),
        "total_feedback": len(records),
        "failed_feedback": len(failures),
        "rating_counts": dict(sorted(rating_counts.items())),
        "reason_tag_counts": dict(sorted(reason_counts.items())),
        "answer_mode_counts": dict(sorted(mode_counts.items())),
        "safety_feedback_count": safety_feedback,
        "thumbs_up_rate": round(rating_counts["thumbs_up"] / len(records), 4) if records else 0.0,
        "output_files": [
            "19_feedback_log.json",
            "19_feedback_log.jsonl",
            "19_feedback_report.json",
            "19_feedback_audit.csv",
            "19_failed_feedback_records.json",
            "plots/19_feedback_ratings.png",
            "plots/19_feedback_reason_tags.png",
        ],
    }


def log_feedback(config: FeedbackLoggerConfig | None = None) -> FeedbackLoggerResult:
    """Run Phase 19 and write all numbered feedback artifacts."""

    resolved_config = config or FeedbackLoggerConfig.from_project_root(resolve_project_root())
    resolved_config.output_dir.mkdir(parents=True, exist_ok=True)
    resolved_config.plots_dir.mkdir(parents=True, exist_ok=True)

    transcript = read_transcript(resolved_config.transcript_path)
    records, failures = build_sample_feedback(transcript)

    feedback_json_path = resolved_config.output_dir / "19_feedback_log.json"
    feedback_jsonl_path = resolved_config.output_dir / "19_feedback_log.jsonl"
    report_path = resolved_config.output_dir / "19_feedback_report.json"
    audit_path = resolved_config.output_dir / "19_feedback_audit.csv"
    failed_path = resolved_config.output_dir / "19_failed_feedback_records.json"
    rating_plot_path = resolved_config.plots_dir / "19_feedback_ratings.png"
    reason_plot_path = resolved_config.plots_dir / "19_feedback_reason_tags.png"

    write_json([asdict(record) for record in records], feedback_json_path)
    write_jsonl(records, feedback_jsonl_path)
    write_json(create_report(records, failures, resolved_config), report_path)
    write_audit_csv(records, audit_path)
    write_json(list(failures), failed_path)
    render_rating_plot(records, rating_plot_path)
    render_reason_plot(records, reason_plot_path)

    return FeedbackLoggerResult(
        feedback_json_path=feedback_json_path,
        feedback_jsonl_path=feedback_jsonl_path,
        report_path=report_path,
        audit_path=audit_path,
        failed_path=failed_path,
        rating_plot_path=rating_plot_path,
        reason_plot_path=reason_plot_path,
        total_feedback=len(records),
        failed_feedback=len(failures),
    )


def iter_output_paths(result: FeedbackLoggerResult) -> Iterable[Path]:
    """Yield generated files in review order."""

    yield result.feedback_json_path
    yield result.feedback_jsonl_path
    yield result.report_path
    yield result.audit_path
    yield result.failed_path
    yield result.rating_plot_path
    yield result.reason_plot_path


def parse_args() -> argparse.Namespace:
    """Parse CLI options."""

    parser = argparse.ArgumentParser(description="Create privacy-conscious Phase 19 feedback logs.")
    parser.add_argument("--project-root", type=Path, default=None)
    return parser.parse_args()


def main() -> None:
    """CLI entry point."""

    args = parse_args()
    project_root = args.project_root.resolve() if args.project_root else resolve_project_root()
    result = log_feedback(FeedbackLoggerConfig.from_project_root(project_root))
    print("Phase 19 feedback logging completed successfully.")
    print(f"Total feedback records: {result.total_feedback}")
    print(f"Failed feedback records: {result.failed_feedback}")
    for output_path in iter_output_paths(result):
        print(f"- {output_path}")


if __name__ == "__main__":
    main()
