"""Evaluate whether the retriever returns the expected hospital evidence.

Phase 17 consumes the Phase 16 test set and the Phase 6 vector index, calls the
real Phase 7 retriever, and writes auditable retrieval-quality artifacts. The
evaluation records strict expected-source hits, broader category hits, latency,
confidence, and review-ready misses.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import statistics
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Iterable, Sequence


PHASE_ID: Final = "17"
PHASE_NAME: Final = "evaluate_retrieval"
MODULE_VERSION: Final = "1.0"
DEFAULT_TOP_K: Final = 5


@dataclass(frozen=True)
class RetrievalEvaluationConfig:
    """Filesystem and retrieval settings for Phase 17."""

    project_root: Path
    test_set_path: Path
    index_path: Path
    output_dir: Path
    plots_dir: Path
    top_k: int = DEFAULT_TOP_K

    @classmethod
    def from_project_root(cls, project_root: Path, top_k: int = DEFAULT_TOP_K) -> "RetrievalEvaluationConfig":
        return cls(
            project_root=project_root,
            test_set_path=project_root / "01_data" / "processed" / "16_test_set.csv",
            index_path=project_root / "05_vector_store" / "chroma_db" / "06_vector_index.sqlite3",
            output_dir=project_root / "01_data" / "processed",
            plots_dir=project_root / "01_data" / "processed" / "plots",
            top_k=top_k,
        )


@dataclass(frozen=True)
class RetrievalEvaluationRow:
    """One scored retrieval test case."""

    test_id: str
    question: str
    category: str
    safety_class: str
    expected_sources: list[str]
    expected_mode: str
    expected_guardrail_action: str
    source_hit: bool
    category_hit: bool
    safety_hit: bool
    passed: bool
    top_source: str
    top_category: str
    top_score: float
    confidence: str
    latency_ms: float
    retrieved_sources: list[str]
    retrieved_categories: list[str]
    retrieved_chunk_ids: list[str]
    safety_labels: list[str]
    miss_reason: str


@dataclass(frozen=True)
class RetrievalEvaluationResult:
    """Paths and metrics produced by Phase 17."""

    results_path: Path
    report_path: Path
    audit_path: Path
    failed_path: Path
    misses_path: Path
    hit_plot_path: Path
    score_plot_path: Path
    latency_plot_path: Path
    total_cases: int
    passed_cases: int
    failed_cases: int


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


def import_module(path: Path, name: str) -> Any:
    """Import a local project module by path."""

    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot import module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_retriever(project_root: Path) -> Any:
    """Load the Phase 7 retriever module."""

    return import_module(project_root / "06_rag_pipeline" / "07_retriever.py", "phase7_retriever_for_phase17")


def read_test_set(path: Path) -> list[dict[str, str]]:
    """Read the Phase 16 test set CSV."""

    if not path.exists():
        raise FileNotFoundError(f"Phase 16 test set not found: {path}")
    with path.open("r", encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))
    required = {
        "test_id",
        "question",
        "category",
        "safety_class",
        "expected_sources",
        "expected_mode",
        "expected_guardrail_action",
    }
    if not rows or not required.issubset(rows[0]):
        raise ValueError("Phase 16 test set does not match the expected schema.")
    return rows


def split_semicolon(value: str) -> list[str]:
    """Split semicolon-delimited fields while removing blanks."""

    return [item.strip() for item in value.split(";") if item.strip()]


def comparable_source_name(source: str) -> str:
    """Normalize source names for strict-but-practical source matching."""

    normalized = source.replace("\\", "/").casefold().strip()
    if normalized == "safety_guardrail":
        return normalized
    return Path(normalized).stem


def source_hit(expected_sources: Sequence[str], retrieved_sources: Sequence[str]) -> bool:
    """Return True if an expected source appears in retrieved evidence."""

    expected_names = {comparable_source_name(source) for source in expected_sources}
    retrieved_names = {comparable_source_name(source) for source in retrieved_sources}
    return bool(expected_names & retrieved_names)


def category_hit(expected_category: str, retrieved_categories: Sequence[str]) -> bool:
    """Return True if the expected category is represented in retrieved chunks."""

    expected = expected_category.casefold().strip()
    aliases = {
        "departments": {"departments", "department_information"},
        "hours": {"hours", "department_information", "visitors"},
        "schedule": {"schedule", "doctor_schedule"},
        "portal": {"portal", "patient_portal"},
        "records": {"records", "faqs"},
    }.get(expected, {expected})
    return any(category.casefold().strip() in aliases for category in retrieved_categories)


def safety_hit_for(row: dict[str, str], safety_labels: Sequence[str]) -> bool:
    """Evaluate retrieval-time safety routing for safety-focused cases."""

    safety_class = row["safety_class"].casefold().strip()
    labels = {label.casefold().strip() for label in safety_labels}
    if safety_class == "emergency":
        return "emergency" in labels
    if safety_class == "unsafe_medical_advice":
        return "unsafe_medical_advice" in labels
    if row["expected_guardrail_action"] in {"block", "override"}:
        return bool(labels)
    return True


def pass_rule(row: dict[str, str], has_source_hit: bool, has_category_hit: bool, has_safety_hit: bool) -> bool:
    """Apply a transparent pass rule for retrieval evaluation."""

    if row["expected_sources"].strip() == "safety_guardrail":
        return has_safety_hit
    return has_source_hit or has_category_hit


def miss_reason_for(row: dict[str, str], has_source_hit: bool, has_category_hit: bool, has_safety_hit: bool) -> str:
    """Explain why a row did not pass."""

    if pass_rule(row, has_source_hit, has_category_hit, has_safety_hit):
        return ""
    if row["expected_sources"].strip() == "safety_guardrail":
        return "Expected safety routing label was not detected."
    if not has_source_hit and not has_category_hit:
        return "Expected source and expected category were both absent from top-k retrieval."
    if not has_source_hit:
        return "Expected source was absent from top-k retrieval."
    return "Expected category was absent from top-k retrieval."


def evaluate_one(row: dict[str, str], response: Any) -> RetrievalEvaluationRow:
    """Convert one retriever response into a scored Phase 17 row."""

    results = list(response.results)
    expected_sources = split_semicolon(row["expected_sources"])
    retrieved_sources = [result.source_file for result in results]
    retrieved_categories = [result.content_category for result in results]
    retrieved_chunk_ids = [result.chunk_id for result in results]
    safety_labels = list(response.safety_labels)

    has_source_hit = source_hit(expected_sources, retrieved_sources)
    has_category_hit = category_hit(row["category"], retrieved_categories)
    has_safety_hit = safety_hit_for(row, safety_labels)
    passed = pass_rule(row, has_source_hit, has_category_hit, has_safety_hit)

    return RetrievalEvaluationRow(
        test_id=row["test_id"],
        question=row["question"],
        category=row["category"],
        safety_class=row["safety_class"],
        expected_sources=expected_sources,
        expected_mode=row["expected_mode"],
        expected_guardrail_action=row["expected_guardrail_action"],
        source_hit=has_source_hit,
        category_hit=has_category_hit,
        safety_hit=has_safety_hit,
        passed=passed,
        top_source=results[0].source_file if results else "",
        top_category=results[0].content_category if results else "",
        top_score=results[0].final_score if results else 0.0,
        confidence=response.confidence,
        latency_ms=response.latency_ms,
        retrieved_sources=retrieved_sources,
        retrieved_categories=retrieved_categories,
        retrieved_chunk_ids=retrieved_chunk_ids,
        safety_labels=safety_labels,
        miss_reason=miss_reason_for(row, has_source_hit, has_category_hit, has_safety_hit),
    )


def write_json(data: object, path: Path) -> None:
    """Write formatted UTF-8 JSON."""

    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def write_audit_csv(rows: Sequence[RetrievalEvaluationRow], path: Path) -> None:
    """Write a spreadsheet-friendly audit table."""

    fieldnames = [
        "test_id",
        "category",
        "safety_class",
        "expected_sources",
        "source_hit",
        "category_hit",
        "safety_hit",
        "passed",
        "top_source",
        "top_category",
        "top_score",
        "confidence",
        "latency_ms",
        "miss_reason",
    ]
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "test_id": row.test_id,
                    "category": row.category,
                    "safety_class": row.safety_class,
                    "expected_sources": ";".join(row.expected_sources),
                    "source_hit": row.source_hit,
                    "category_hit": row.category_hit,
                    "safety_hit": row.safety_hit,
                    "passed": row.passed,
                    "top_source": row.top_source,
                    "top_category": row.top_category,
                    "top_score": row.top_score,
                    "confidence": row.confidence,
                    "latency_ms": row.latency_ms,
                    "miss_reason": row.miss_reason,
                }
            )


def render_hit_plot(rows: Sequence[RetrievalEvaluationRow], output_path: Path) -> None:
    """Plot pass rate by category."""

    import matplotlib.pyplot as plt

    categories = sorted({row.category for row in rows})
    pass_rates = []
    for category in categories:
        category_rows = [row for row in rows if row.category == category]
        pass_rates.append(sum(row.passed for row in category_rows) / len(category_rows))

    figure, axis = plt.subplots(figsize=(10, 5))
    bars = axis.bar(categories, pass_rates, color="#54A24B")
    axis.set_title("Phase 17 Retrieval Pass Rate by Category")
    axis.set_xlabel("Category")
    axis.set_ylabel("Pass rate")
    axis.set_ylim(0, 1.05)
    axis.tick_params(axis="x", rotation=25)
    axis.bar_label(bars, labels=[f"{value:.0%}" for value in pass_rates])
    figure.tight_layout()
    figure.savefig(output_path, dpi=160)
    plt.close(figure)


def render_score_plot(rows: Sequence[RetrievalEvaluationRow], output_path: Path) -> None:
    """Plot top retrieval score by test case."""

    import matplotlib.pyplot as plt

    colors = ["#4C78A8" if row.passed else "#E45756" for row in rows]
    figure, axis = plt.subplots(figsize=(11, 5))
    bars = axis.bar([row.test_id for row in rows], [row.top_score for row in rows], color=colors)
    axis.set_title("Phase 17 Top Retrieval Score by Test Case")
    axis.set_xlabel("Test case")
    axis.set_ylabel("Top hybrid retrieval score")
    axis.tick_params(axis="x", rotation=45)
    axis.bar_label(bars, labels=[f"{row.top_score:.2f}" for row in rows], fontsize=7)
    figure.tight_layout()
    figure.savefig(output_path, dpi=160)
    plt.close(figure)


def render_latency_plot(rows: Sequence[RetrievalEvaluationRow], output_path: Path) -> None:
    """Plot retrieval latency by test case."""

    import matplotlib.pyplot as plt

    figure, axis = plt.subplots(figsize=(11, 5))
    bars = axis.bar([row.test_id for row in rows], [row.latency_ms for row in rows], color="#F58518")
    axis.set_title("Phase 17 Retrieval Latency by Test Case")
    axis.set_xlabel("Test case")
    axis.set_ylabel("Latency (ms)")
    axis.tick_params(axis="x", rotation=45)
    axis.bar_label(bars, labels=[f"{row.latency_ms:.1f}" for row in rows], fontsize=7)
    figure.tight_layout()
    figure.savefig(output_path, dpi=160)
    plt.close(figure)


def create_report(rows: Sequence[RetrievalEvaluationRow], execution_failures: Sequence[dict[str, str]], config: RetrievalEvaluationConfig) -> dict[str, object]:
    """Create summary metrics for Phase 17."""

    latencies = [row.latency_ms for row in rows] or [0.0]
    source_hits = sum(row.source_hit for row in rows)
    category_hits = sum(row.category_hit for row in rows)
    safety_hits = sum(row.safety_hit for row in rows)
    passed = sum(row.passed for row in rows)

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "phase": PHASE_ID,
        "module": PHASE_NAME,
        "module_version": MODULE_VERSION,
        "test_set_file": str(config.test_set_path.resolve()),
        "index_file": str(config.index_path.resolve()),
        "top_k": config.top_k,
        "total_cases": len(rows) + len(execution_failures),
        "evaluated_cases": len(rows),
        "passed_cases": passed,
        "failed_cases": len(rows) - passed,
        "execution_failures": len(execution_failures),
        "pass_rate": round(passed / len(rows), 4) if rows else 0.0,
        "source_hit_rate": round(source_hits / len(rows), 4) if rows else 0.0,
        "category_hit_rate": round(category_hits / len(rows), 4) if rows else 0.0,
        "safety_hit_rate": round(safety_hits / len(rows), 4) if rows else 0.0,
        "confidence_counts": dict(sorted(Counter(row.confidence for row in rows).items())),
        "category_counts": dict(sorted(Counter(row.category for row in rows).items())),
        "safety_class_counts": dict(sorted(Counter(row.safety_class for row in rows).items())),
        "latency_ms": {
            "minimum": round(min(latencies), 3),
            "maximum": round(max(latencies), 3),
            "mean": round(statistics.mean(latencies), 3),
            "median": round(statistics.median(latencies), 3),
        },
        "output_files": [
            "17_retrieval_results.json",
            "17_retrieval_report.json",
            "17_retrieval_audit.csv",
            "17_failed_retrieval_queries.json",
            "17_retrieval_misses.json",
            "plots/17_retrieval_pass_rate_by_category.png",
            "plots/17_retrieval_score_by_test_case.png",
            "plots/17_retrieval_latency_by_test_case.png",
        ],
    }


def evaluate_retrieval(config: RetrievalEvaluationConfig | None = None) -> RetrievalEvaluationResult:
    """Run Phase 17 retrieval evaluation and write all numbered artifacts."""

    resolved_config = config or RetrievalEvaluationConfig.from_project_root(resolve_project_root())
    if resolved_config.top_k < 1:
        raise ValueError("top_k must be positive.")
    if not resolved_config.index_path.exists():
        raise FileNotFoundError(f"Vector index not found: {resolved_config.index_path}")

    resolved_config.output_dir.mkdir(parents=True, exist_ok=True)
    resolved_config.plots_dir.mkdir(parents=True, exist_ok=True)

    retriever = load_retriever(resolved_config.project_root)
    embedder, vector_store = retriever.load_dependencies(resolved_config.project_root)
    retriever_config = retriever.RetrieverConfig(top_k=resolved_config.top_k)
    test_rows = read_test_set(resolved_config.test_set_path)

    evaluated_rows: list[RetrievalEvaluationRow] = []
    execution_failures: list[dict[str, str]] = []

    for row in test_rows:
        try:
            response = retriever.retrieve(
                row["question"],
                resolved_config.index_path,
                retriever_config,
                embedder,
                vector_store,
            )
            evaluated_rows.append(evaluate_one(row, response))
        except (ValueError, RuntimeError, FileNotFoundError) as error:
            execution_failures.append(
                {
                    "test_id": row.get("test_id", ""),
                    "question": row.get("question", ""),
                    "error": str(error),
                }
            )

    results_path = resolved_config.output_dir / "17_retrieval_results.json"
    report_path = resolved_config.output_dir / "17_retrieval_report.json"
    audit_path = resolved_config.output_dir / "17_retrieval_audit.csv"
    failed_path = resolved_config.output_dir / "17_failed_retrieval_queries.json"
    misses_path = resolved_config.output_dir / "17_retrieval_misses.json"
    hit_plot_path = resolved_config.plots_dir / "17_retrieval_pass_rate_by_category.png"
    score_plot_path = resolved_config.plots_dir / "17_retrieval_score_by_test_case.png"
    latency_plot_path = resolved_config.plots_dir / "17_retrieval_latency_by_test_case.png"

    misses = [asdict(row) for row in evaluated_rows if not row.passed]
    report = create_report(evaluated_rows, execution_failures, resolved_config)

    write_json([asdict(row) for row in evaluated_rows], results_path)
    write_json(report, report_path)
    write_audit_csv(evaluated_rows, audit_path)
    write_json(execution_failures, failed_path)
    write_json(misses, misses_path)
    render_hit_plot(evaluated_rows, hit_plot_path)
    render_score_plot(evaluated_rows, score_plot_path)
    render_latency_plot(evaluated_rows, latency_plot_path)

    return RetrievalEvaluationResult(
        results_path=results_path,
        report_path=report_path,
        audit_path=audit_path,
        failed_path=failed_path,
        misses_path=misses_path,
        hit_plot_path=hit_plot_path,
        score_plot_path=score_plot_path,
        latency_plot_path=latency_plot_path,
        total_cases=report["total_cases"],  # type: ignore[arg-type]
        passed_cases=report["passed_cases"],  # type: ignore[arg-type]
        failed_cases=report["failed_cases"],  # type: ignore[arg-type]
    )


def iter_output_paths(result: RetrievalEvaluationResult) -> Iterable[Path]:
    """Yield generated files in review order."""

    yield result.results_path
    yield result.report_path
    yield result.audit_path
    yield result.failed_path
    yield result.misses_path
    yield result.hit_plot_path
    yield result.score_plot_path
    yield result.latency_plot_path


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""

    parser = argparse.ArgumentParser(description="Evaluate Phase 7 retrieval using the Phase 16 test set.")
    parser.add_argument("--project-root", type=Path, default=None)
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    return parser.parse_args()


def main() -> None:
    """CLI entry point."""

    args = parse_args()
    project_root = args.project_root.resolve() if args.project_root else resolve_project_root()
    config = RetrievalEvaluationConfig.from_project_root(project_root, top_k=args.top_k)
    result = evaluate_retrieval(config)

    print("Phase 17 retrieval evaluation completed successfully.")
    print(f"Total cases: {result.total_cases}")
    print(f"Passed cases: {result.passed_cases}")
    print(f"Failed cases: {result.failed_cases}")
    for output_path in iter_output_paths(result):
        print(f"- {output_path}")


if __name__ == "__main__":
    main()
