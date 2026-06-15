"""Retrieve grounded hospital context from the Phase 6 vector index."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import math
import re
import statistics
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Sequence


DEFAULT_TOP_K: Final = 5
DEFAULT_CANDIDATE_COUNT: Final = 30
DEFAULT_VECTOR_WEIGHT: Final = 0.80
TOKEN_PATTERN: Final = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)?")
STOPWORDS: Final = {
    "a", "an", "and", "are", "can", "do", "does", "for", "how", "i",
    "in", "is", "it", "me", "my", "of", "on", "the", "to", "what",
    "where", "which", "with",
}
EMERGENCY_TERMS: Final = (
    "severe chest pain", "can't breathe", "cannot breathe", "unconscious",
    "heavy bleeding", "immediate danger", "suicidal",
)
UNSAFE_MEDICAL_TERMS: Final = (
    "diagnose", "diagnosis", "dosage", "dose", "what is wrong with me",
    "medicine should i take", "treatment should i",
)
CATEGORY_ROUTES: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("appointments", ("appointment", "book", "reschedule", "cancel")),
    ("insurance", ("insurance", "authorization", "coverage", "claim")),
    ("patient_portal", ("portal", "activation link", "password")),
    ("visitors", ("visitor", "visiting hours")),
    ("doctor_schedule", ("which doctor", "doctor works", "doctor schedule")),
    ("department_information", ("where is", "department", "open on", "hours")),
    ("clinical_safety", ("diagnose", "dosage", "dose", "what is wrong with me")),
)


@dataclass(frozen=True)
class RetrieverConfig:
    """Validated retrieval and reranking settings."""

    top_k: int = DEFAULT_TOP_K
    candidate_count: int = DEFAULT_CANDIDATE_COUNT
    vector_weight: float = DEFAULT_VECTOR_WEIGHT
    minimum_score: float = 0.0

    def __post_init__(self) -> None:
        if self.top_k < 1:
            raise ValueError("top_k must be positive.")
        if self.candidate_count < self.top_k:
            raise ValueError("candidate_count must be at least top_k.")
        if not 0.0 <= self.vector_weight <= 1.0:
            raise ValueError("vector_weight must be between 0 and 1.")


@dataclass(frozen=True)
class RetrievalResult:
    """One ranked source chunk returned for a user question."""

    rank: int
    chunk_id: str
    text: str
    source_file: str
    source_type: str
    department: str
    content_category: str
    vector_score: float
    lexical_score: float
    final_score: float
    metadata: dict[str, Any]


@dataclass(frozen=True)
class RetrievalResponse:
    """Complete retrieval response before prompt construction or generation."""

    question: str
    results: list[RetrievalResult]
    confidence: str
    safety_labels: list[str]
    latency_ms: float
    filters: dict[str, str]


@dataclass(frozen=True)
class EvaluationResult:
    """Artifacts and metrics produced by the Phase 7 evaluation run."""

    results_path: Path
    report_path: Path
    audit_path: Path
    failed_path: Path
    score_plot_path: Path
    latency_plot_path: Path
    questions: int
    top_k_source_hits: int
    failed_queries: int


def default_project_root() -> Path:
    """Return the project root based on this module's location."""
    return Path(__file__).resolve().parents[1]


def import_module(path: Path, name: str) -> Any:
    """Import a local workflow module and register it for dataclass support."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot import module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_dependencies(project_root: Path) -> tuple[Any, Any]:
    """Load the Phase 5 embedder and Phase 6 vector-store interface."""
    embedder = import_module(
        project_root / "04_embeddings" / "05_create_embeddings.py",
        "phase5_create_embeddings",
    )
    vector_store = import_module(
        project_root / "04_embeddings" / "06_store_vector_index.py",
        "phase6_store_vector_index",
    )
    return embedder, vector_store


def load_index_contract(index_path: Path) -> dict[str, str]:
    """Read and validate the model and dimension stored in the index."""
    import sqlite3

    if not index_path.is_file():
        raise FileNotFoundError(f"Vector index does not exist: {index_path}")
    with sqlite3.connect(index_path) as connection:
        metadata = dict(connection.execute("SELECT key, value FROM index_metadata"))
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    required = {"model", "dimension", "backend", "schema_version"}
    if integrity != "ok" or not required.issubset(metadata):
        raise RuntimeError("Vector index contract or integrity validation failed.")
    return metadata


def query_tokens(text: str) -> set[str]:
    """Return informative normalized tokens for lexical reranking."""
    return {
        token for token in TOKEN_PATTERN.findall(text.casefold())
        if len(token) > 1 and token not in STOPWORDS
    }


def lexical_overlap(question: str, document: str) -> float:
    """Return query-token coverage by a candidate chunk."""
    query = query_tokens(question)
    if not query:
        return 0.0
    document_tokens = query_tokens(document)
    return len(query & document_tokens) / len(query)


def derive_safety_labels(question: str) -> list[str]:
    """Identify routing labels without providing medical interpretation."""
    normalized = " ".join(question.casefold().split())
    labels: list[str] = []
    if any(term in normalized for term in EMERGENCY_TERMS):
        labels.append("emergency")
    if any(term in normalized for term in UNSAFE_MEDICAL_TERMS):
        labels.append("unsafe_medical_advice")
    return labels


def infer_content_category(question: str) -> str | None:
    """Infer a narrow, auditable metadata route for unambiguous questions."""
    normalized = " ".join(question.casefold().split())
    for category, terms in CATEGORY_ROUTES:
        if any(term in normalized for term in terms):
            return category
    return None


def confidence_label(results: Sequence[RetrievalResult]) -> str:
    """Convert score strength and separation into a cautious confidence label."""
    if not results:
        return "none"
    first = results[0].final_score
    gap = first - results[1].final_score if len(results) > 1 else first
    if first >= 0.55 and gap >= 0.08:
        return "high"
    if first >= 0.30:
        return "medium"
    return "low"


def retrieve(
    question: str,
    index_path: Path,
    config: RetrieverConfig,
    embedder: Any,
    vector_store: Any,
    department: str | None = None,
    content_category: str | None = None,
) -> RetrievalResponse:
    """Embed a question, search the index, rerank candidates, and return evidence."""
    question = " ".join(question.split())
    if not question:
        raise ValueError("Question must not be empty.")
    contract = load_index_contract(index_path)
    if contract["model"] != embedder.MODEL_NAME:
        raise RuntimeError("Query embedder model does not match the vector index.")
    dimension = int(contract["dimension"])
    embedding_config = embedder.EmbeddingConfig(dimension=dimension)
    start = time.perf_counter()
    query_vector = embedder.embed_text(question, embedding_config)
    routed_category = content_category or infer_content_category(question)
    candidates = vector_store.query_index(
        index_path,
        query_vector,
        top_k=config.candidate_count,
        department=department,
        content_category=routed_category,
    )
    reranked: list[tuple[float, float, dict[str, Any]]] = []
    for candidate in candidates:
        lexical = lexical_overlap(question, candidate["text"])
        vector_score = max(-1.0, min(1.0, float(candidate["score"])))
        normalized_vector = (vector_score + 1.0) / 2.0
        final_score = (
            config.vector_weight * normalized_vector
            + (1.0 - config.vector_weight) * lexical
        )
        if final_score >= config.minimum_score:
            reranked.append((final_score, lexical, candidate))
    reranked.sort(key=lambda item: (-item[0], -item[1], item[2]["chunk_id"]))
    results = [
        RetrievalResult(
            rank=rank,
            chunk_id=item[2]["chunk_id"],
            text=item[2]["text"],
            source_file=item[2]["source_file"],
            source_type=item[2]["source_type"],
            department=item[2]["department"],
            content_category=item[2]["content_category"],
            vector_score=round(float(item[2]["score"]), 8),
            lexical_score=round(item[1], 8),
            final_score=round(item[0], 8),
            metadata=item[2]["metadata"],
        )
        for rank, item in enumerate(reranked[:config.top_k], start=1)
    ]
    latency_ms = (time.perf_counter() - start) * 1000
    return RetrievalResponse(
        question=question,
        results=results,
        confidence=confidence_label(results),
        safety_labels=derive_safety_labels(question),
        latency_ms=round(latency_ms, 3),
        filters={
            key: value
            for key, value in {
                "department": department,
                "content_category": routed_category,
            }.items()
            if value
        },
    )


def load_test_questions(path: Path) -> list[dict[str, str]]:
    """Load the Phase 7 demonstration questions."""
    if not path.is_file():
        raise FileNotFoundError(f"Test questions do not exist: {path}")
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    required = {"question", "category", "expected_source", "safety_class"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError("Test question CSV does not match the expected schema.")
    return rows


def expected_source_hit(expected: str, response: RetrievalResponse) -> bool:
    """Evaluate a source expectation, including safety-routing expectations."""
    if expected == "safety_guardrail":
        return bool(response.safety_labels)
    return any(expected.casefold() in result.source_file.casefold() for result in response.results)


def write_json(path: Path, payload: Any) -> None:
    """Write readable UTF-8 JSON."""
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def write_audit(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    """Write one audit row per evaluated question."""
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else [])
        writer.writeheader()
        writer.writerows(rows)


def generate_plots(
    responses: Sequence[RetrievalResponse], plots_dir: Path
) -> tuple[Path, Path]:
    """Plot top-result scores and per-query retrieval latency."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as error:
        raise RuntimeError("Install matplotlib to generate retriever plots.") from error
    plots_dir.mkdir(parents=True, exist_ok=True)
    score_path = plots_dir / "07_top_retrieval_score_distribution.png"
    latency_path = plots_dir / "07_retrieval_latency_by_query.png"
    top_scores = [response.results[0].final_score for response in responses if response.results]
    figure, axis = plt.subplots(figsize=(10, 5))
    axis.hist(top_scores, bins=10, color="#176B87", edgecolor="white")
    axis.set_title("Top Retrieval Score Distribution")
    axis.set_xlabel("Hybrid retrieval score")
    axis.set_ylabel("Number of questions")
    figure.tight_layout(); figure.savefig(score_path, dpi=160); plt.close(figure)
    figure, axis = plt.subplots(figsize=(11, 5))
    axis.bar(range(1, len(responses) + 1), [r.latency_ms for r in responses], color="#4A90A4")
    axis.set_title("Retrieval Latency by Test Question")
    axis.set_xlabel("Question number")
    axis.set_ylabel("Latency (ms)")
    axis.grid(axis="y", alpha=0.25)
    figure.tight_layout(); figure.savefig(latency_path, dpi=160); plt.close(figure)
    return score_path, latency_path


def run_retriever_evaluation(
    index_path: Path,
    questions_path: Path,
    output_dir: Path,
    config: RetrieverConfig,
    project_root: Path,
) -> EvaluationResult:
    """Run sample questions, write evidence, metrics, failures, and plots."""
    embedder, vector_store = load_dependencies(project_root)
    questions = load_test_questions(questions_path)
    responses: list[RetrievalResponse] = []
    audit: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    source_hits = 0
    for number, row in enumerate(questions, start=1):
        try:
            response = retrieve(
                row["question"], index_path, config, embedder, vector_store
            )
            responses.append(response)
            hit = expected_source_hit(row["expected_source"], response)
            source_hits += int(hit)
            audit.append({
                "question_number": number,
                "question": row["question"],
                "expected_source": row["expected_source"],
                "source_hit": hit,
                "top_chunk_id": response.results[0].chunk_id if response.results else "",
                "top_source": response.results[0].source_file if response.results else "",
                "top_score": response.results[0].final_score if response.results else "",
                "confidence": response.confidence,
                "safety_labels": ";".join(response.safety_labels),
                "latency_ms": response.latency_ms,
            })
        except (ValueError, RuntimeError, FileNotFoundError) as error:
            failures.append({"question_number": number, "question": row["question"], "error": str(error)})
    output_dir = output_dir.resolve(); output_dir.mkdir(parents=True, exist_ok=True)
    results_path = output_dir / "07_retrieval_results.json"
    report_path = output_dir / "07_retrieval_report.json"
    audit_path = output_dir / "07_retrieval_audit.csv"
    failed_path = output_dir / "07_failed_queries.json"
    write_json(results_path, [asdict(response) for response in responses])
    write_audit(audit_path, audit)
    write_json(failed_path, failures)
    score_plot, latency_plot = generate_plots(responses, output_dir / "plots")
    latencies = [response.latency_ms for response in responses]
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "index_file": str(index_path.resolve()),
        "questions_file": str(questions_path.resolve()),
        "questions": len(questions),
        "successful_queries": len(responses),
        "failed_queries": len(failures),
        "top_k_source_hits": source_hits,
        "top_k_source_hit_rate": round(source_hits / len(questions), 4),
        "configuration": asdict(config),
        "confidence_counts": dict(sorted(Counter(r.confidence for r in responses).items())),
        "latency_ms": {
            "minimum": round(min(latencies), 3),
            "maximum": round(max(latencies), 3),
            "mean": round(statistics.mean(latencies), 3),
            "median": round(statistics.median(latencies), 3),
        },
        "output_files": [
            results_path.name, report_path.name, audit_path.name, failed_path.name,
            f"plots/{score_plot.name}", f"plots/{latency_plot.name}",
        ],
    }
    write_json(report_path, report)
    return EvaluationResult(
        results_path, report_path, audit_path, failed_path, score_plot,
        latency_plot, len(questions), source_hits, len(failures)
    )


def build_parser() -> argparse.ArgumentParser:
    """Create the Phase 7 command-line parser."""
    root = default_project_root(); processed = root / "01_data" / "processed"
    parser = argparse.ArgumentParser(description="Retrieve grounded hospital context.")
    parser.add_argument("--question", type=str)
    parser.add_argument("--index", type=Path, default=root / "05_vector_store" / "chroma_db" / "06_vector_index.sqlite3")
    parser.add_argument("--questions", type=Path, default=root / "01_data" / "sample_queries" / "test_questions.csv")
    parser.add_argument("--output-dir", type=Path, default=processed)
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--department", type=str)
    parser.add_argument("--category", type=str)
    return parser


def main() -> None:
    """Run one query or the complete sample-query evaluation."""
    args = build_parser().parse_args()
    root = default_project_root()
    config = RetrieverConfig(top_k=args.top_k)
    embedder, vector_store = load_dependencies(root)
    if args.question:
        response = retrieve(
            args.question, args.index, config, embedder, vector_store,
            department=args.department, content_category=args.category,
        )
        print(json.dumps(asdict(response), indent=2, ensure_ascii=False))
        return
    result = run_retriever_evaluation(
        args.index, args.questions, args.output_dir, config, root
    )
    print("Retriever evaluation completed successfully.")
    print(f"Questions: {result.questions}")
    print(f"Top-k source hits: {result.top_k_source_hits}")
    print(f"Failed queries: {result.failed_queries}")


if __name__ == "__main__":
    main()
