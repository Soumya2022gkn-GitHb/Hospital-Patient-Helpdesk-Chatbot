"""Create deterministic vector embeddings for enriched hospital text chunks.

The default provider is an offline feature-hashing embedder. It keeps approved
hospital text on the local machine, requires no model download or API key, and
produces reproducible L2-normalized vectors for development and testing.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any, Final, Iterable, Sequence


MODEL_NAME: Final = "local-hashing-embedding-v1"
DEFAULT_DIMENSION: Final = 384
DEFAULT_BATCH_SIZE: Final = 32
TOKEN_PATTERN: Final = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)?")


@dataclass(frozen=True)
class EmbeddingConfig:
    """Validated settings for deterministic local embedding generation."""

    dimension: int = DEFAULT_DIMENSION
    batch_size: int = DEFAULT_BATCH_SIZE
    include_bigrams: bool = True

    def __post_init__(self) -> None:
        if self.dimension < 32:
            raise ValueError("Embedding dimension must be at least 32.")
        if self.batch_size < 1:
            raise ValueError("Batch size must be positive.")


@dataclass(frozen=True)
class EmbeddingRecord:
    """One vector and its traceable Phase 4 identity."""

    chunk_id: str
    document_id: str
    source_file: str
    source_type: str
    department: str
    content_category: str
    model: str
    dimension: int
    text_sha256: str
    vector_norm: float
    embedding: list[float]


@dataclass(frozen=True)
class EmbeddingResult:
    """Paths and counts returned by one embedding run."""

    embeddings_path: Path
    manifest_path: Path
    report_path: Path
    audit_path: Path
    failed_path: Path
    norm_plot_path: Path
    similarity_plot_path: Path
    input_chunks: int
    embeddings_created: int
    failed_embeddings: int


def default_project_root() -> Path:
    """Return the project root based on this module's location."""
    return Path(__file__).resolve().parents[1]


def load_enriched_chunks(path: Path) -> list[dict[str, Any]]:
    """Load and validate the Phase 4 enriched chunk contract."""
    if not path.is_file():
        raise FileNotFoundError(f"Enriched chunk input does not exist: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Enriched chunk input must be a JSON list.")
    required = {"chunk_id", "document_id", "text", "source_file", "source_type", "retrieval_metadata"}
    for index, chunk in enumerate(payload, start=1):
        if not isinstance(chunk, dict) or not required.issubset(chunk):
            raise ValueError(f"Chunk {index} does not match the Phase 4 schema.")
        if not isinstance(chunk["retrieval_metadata"], dict):
            raise ValueError(f"Chunk {index} has invalid retrieval metadata.")
    return payload


def normalize_text(text: str) -> str:
    """Normalize whitespace and case without changing the stored source text."""
    return " ".join(text.casefold().split())


def text_features(text: str, include_bigrams: bool = True) -> list[str]:
    """Create word and optional adjacent-word features for hashing."""
    tokens = TOKEN_PATTERN.findall(normalize_text(text))
    features = [f"word:{token}" for token in tokens]
    if include_bigrams:
        features.extend(
            f"bigram:{left}_{right}" for left, right in zip(tokens, tokens[1:])
        )
    return features


def feature_location(feature: str, dimension: int) -> tuple[int, float]:
    """Map a feature to a stable vector index and signed contribution."""
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=16).digest()
    index = int.from_bytes(digest[:8], "little") % dimension
    sign = 1.0 if digest[8] & 1 else -1.0
    return index, sign


def l2_normalize(vector: Sequence[float]) -> tuple[list[float], float]:
    """Return an L2-normalized vector and its original norm."""
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        raise ValueError("Cannot normalize an empty embedding vector.")
    return [round(value / norm, 8) for value in vector], norm


def embed_text(text: str, config: EmbeddingConfig) -> list[float]:
    """Embed text with signed feature hashing and log-scaled frequencies."""
    counts = Counter(text_features(text, config.include_bigrams))
    if not counts:
        raise ValueError("Text contains no embeddable tokens.")
    vector = [0.0] * config.dimension
    for feature, frequency in counts.items():
        index, sign = feature_location(feature, config.dimension)
        vector[index] += sign * (1.0 + math.log(frequency))
    normalized, _ = l2_normalize(vector)
    return normalized


def batched(values: Sequence[dict[str, Any]], size: int) -> Iterable[Sequence[dict[str, Any]]]:
    """Yield fixed-size input batches without copying the full collection."""
    for start in range(0, len(values), size):
        yield values[start:start + size]


def create_embedding_records(
    chunks: Sequence[dict[str, Any]], config: EmbeddingConfig
) -> tuple[list[EmbeddingRecord], list[dict[str, Any]], list[dict[str, Any]]]:
    """Embed every valid chunk while isolating and recording failures."""
    records: list[EmbeddingRecord] = []
    audit: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for batch_number, batch in enumerate(batched(chunks, config.batch_size), start=1):
        for chunk in batch:
            chunk_id = str(chunk["chunk_id"])
            text = str(chunk["text"])
            try:
                vector = embed_text(text, config)
                metadata = chunk["retrieval_metadata"]
                record = EmbeddingRecord(
                    chunk_id=chunk_id,
                    document_id=str(chunk["document_id"]),
                    source_file=str(chunk["source_file"]),
                    source_type=str(chunk["source_type"]),
                    department=str(metadata.get("department", "general")),
                    content_category=str(metadata.get("content_category", "general")),
                    model=MODEL_NAME,
                    dimension=config.dimension,
                    text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    vector_norm=round(math.sqrt(sum(value * value for value in vector)), 8),
                    embedding=vector,
                )
                records.append(record)
                audit.append({
                    "chunk_id": record.chunk_id,
                    "batch_number": batch_number,
                    "character_count": len(text),
                    "feature_count": len(text_features(text, config.include_bigrams)),
                    "dimension": record.dimension,
                    "vector_norm": record.vector_norm,
                    "text_sha256": record.text_sha256,
                    "status": "created",
                })
            except (TypeError, ValueError) as error:
                failures.append({"chunk_id": chunk_id, "error": str(error)})
    return records, audit, failures


def validate_embeddings(
    chunks: Sequence[dict[str, Any]], records: Sequence[EmbeddingRecord], config: EmbeddingConfig
) -> None:
    """Validate identity coverage, dimensions, finite values, and unit norms."""
    expected_ids = {str(chunk["chunk_id"]) for chunk in chunks}
    record_ids = [record.chunk_id for record in records]
    if len(record_ids) != len(set(record_ids)):
        raise RuntimeError("Duplicate embedding chunk IDs were generated.")
    if set(record_ids) != expected_ids:
        raise RuntimeError("Embeddings do not cover every Phase 4 chunk.")
    for record in records:
        if len(record.embedding) != config.dimension:
            raise RuntimeError(f"Incorrect vector dimension for {record.chunk_id}.")
        if not all(math.isfinite(value) for value in record.embedding):
            raise RuntimeError(f"Non-finite vector value for {record.chunk_id}.")
        norm = math.sqrt(sum(value * value for value in record.embedding))
        if not math.isclose(norm, 1.0, abs_tol=1e-6):
            raise RuntimeError(f"Vector is not normalized for {record.chunk_id}: {norm}")


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Return cosine similarity for normalized vectors."""
    return sum(a * b for a, b in zip(left, right))


def sampled_similarities(records: Sequence[EmbeddingRecord], limit: int = 500) -> list[float]:
    """Return deterministic pair similarities for diagnostics."""
    similarities: list[float] = []
    for left_index, left in enumerate(records):
        for right in records[left_index + 1:]:
            similarities.append(cosine_similarity(left.embedding, right.embedding))
            if len(similarities) >= limit:
                return similarities
    return similarities


def write_json(path: Path, payload: Any) -> None:
    """Write readable UTF-8 JSON."""
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def write_audit(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    """Write one audit row per successfully embedded chunk."""
    fieldnames = list(rows[0]) if rows else []
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def generate_plots(records: Sequence[EmbeddingRecord], plots_dir: Path) -> tuple[Path, Path]:
    """Create vector norm and sampled cosine-similarity diagnostics."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as error:
        raise RuntimeError("Install matplotlib to generate embedding plots.") from error
    plots_dir.mkdir(parents=True, exist_ok=True)
    norm_path = plots_dir / "05_embedding_norm_distribution.png"
    similarity_path = plots_dir / "05_cosine_similarity_distribution.png"

    norms = [math.sqrt(sum(value * value for value in record.embedding)) for record in records]
    norm_deviations = [(norm - 1.0) * 100_000_000 for norm in norms]
    figure, axis = plt.subplots(figsize=(10, 5))
    axis.hist(norm_deviations, bins=12, color="#176B87", edgecolor="white")
    axis.axvline(0.0, color="#B33A3A", linestyle="--", label="Exact unit norm")
    axis.set_title("Embedding Norm Deviation from 1.0")
    axis.set_xlabel("Deviation from unit norm (x 1e-8)")
    axis.set_ylabel("Number of vectors")
    axis.legend()
    figure.tight_layout()
    figure.savefig(norm_path, dpi=160)
    plt.close(figure)

    similarities = sampled_similarities(records)
    figure, axis = plt.subplots(figsize=(10, 5))
    axis.hist(similarities, bins=20, color="#4A90A4", edgecolor="white")
    axis.set_title("Sampled Pairwise Cosine Similarity")
    axis.set_xlabel("Cosine similarity")
    axis.set_ylabel("Number of chunk pairs")
    figure.tight_layout()
    figure.savefig(similarity_path, dpi=160)
    plt.close(figure)
    return norm_path, similarity_path


def run_embedding_creation(
    input_path: Path, output_dir: Path, config: EmbeddingConfig
) -> EmbeddingResult:
    """Run loading, embedding, validation, reporting, plotting, and writing."""
    chunks = load_enriched_chunks(input_path.resolve())
    records, audit, failures = create_embedding_records(chunks, config)
    if failures:
        raise RuntimeError(f"Embedding failed for {len(failures)} chunks.")
    validate_embeddings(chunks, records, config)

    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    plots_dir = output_dir / "plots"
    embeddings_path = output_dir / "05_embeddings.json"
    manifest_path = output_dir / "05_embedding_manifest.json"
    report_path = output_dir / "05_embedding_report.json"
    audit_path = output_dir / "05_embedding_audit.csv"
    failed_path = output_dir / "05_failed_embeddings.json"

    write_json(embeddings_path, [asdict(record) for record in records])
    write_json(manifest_path, [
        {key: value for key, value in asdict(record).items() if key != "embedding"}
        for record in records
    ])
    write_audit(audit_path, audit)
    write_json(failed_path, failures)
    norm_plot, similarity_plot = generate_plots(records, plots_dir)

    similarities = sampled_similarities(records)
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "input_file": str(input_path.resolve()),
        "input_chunks": len(chunks),
        "embeddings_created": len(records),
        "failed_embeddings": len(failures),
        "provider": "local_feature_hashing",
        "model": MODEL_NAME,
        "configuration": asdict(config),
        "vector_norm_statistics": {
            "minimum": min(record.vector_norm for record in records),
            "maximum": max(record.vector_norm for record in records),
            "mean": round(mean(record.vector_norm for record in records), 8),
        },
        "sampled_cosine_similarity": {
            "pairs": len(similarities),
            "minimum": round(min(similarities), 6),
            "maximum": round(max(similarities), 6),
            "mean": round(mean(similarities), 6),
            "median": round(median(similarities), 6),
        },
        "output_files": [
            embeddings_path.name,
            manifest_path.name,
            report_path.name,
            audit_path.name,
            failed_path.name,
            f"plots/{norm_plot.name}",
            f"plots/{similarity_plot.name}",
        ],
    }
    write_json(report_path, report)
    return EmbeddingResult(
        embeddings_path, manifest_path, report_path, audit_path, failed_path,
        norm_plot, similarity_plot, len(chunks), len(records), len(failures)
    )


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line argument parser."""
    project_root = default_project_root()
    processed_dir = project_root / "01_data" / "processed"
    parser = argparse.ArgumentParser(description="Create local embeddings for hospital chunks.")
    parser.add_argument("--input", type=Path, default=processed_dir / "04_enriched_chunks.json")
    parser.add_argument("--output-dir", type=Path, default=processed_dir)
    parser.add_argument("--dimension", type=int, default=DEFAULT_DIMENSION)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--no-bigrams", action="store_true")
    return parser


def print_result(result: EmbeddingResult) -> None:
    """Print a concise run summary and generated paths."""
    print("Embedding creation completed successfully.")
    print(f"Input chunks: {result.input_chunks}")
    print(f"Embeddings created: {result.embeddings_created}")
    print(f"Failed embeddings: {result.failed_embeddings}")
    print("\nOutput files:")
    for path in (
        result.embeddings_path, result.manifest_path, result.report_path,
        result.audit_path, result.failed_path, result.norm_plot_path,
        result.similarity_plot_path,
    ):
        print(f"- {path} ({path.stat().st_size:,} bytes)")


def main() -> None:
    """Run embedding creation from command-line arguments."""
    args = build_parser().parse_args()
    config = EmbeddingConfig(
        dimension=args.dimension,
        batch_size=args.batch_size,
        include_bigrams=not args.no_bigrams,
    )
    print_result(run_embedding_creation(args.input, args.output_dir, config))


if __name__ == "__main__":
    main()
