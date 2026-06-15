"""Persist Phase 5 embeddings in a portable SQLite cosine vector index."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sqlite3
import struct
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


SCHEMA_VERSION = "1.0"
BACKEND_NAME = "sqlite_exact_cosine"


@dataclass(frozen=True)
class IndexResult:
    """Paths and counts produced by one index build."""

    index_path: Path
    manifest_path: Path
    report_path: Path
    audit_path: Path
    failed_path: Path
    department_plot_path: Path
    source_plot_path: Path
    input_embeddings: int
    indexed_vectors: int
    failed_records: int


def default_project_root() -> Path:
    """Return the project root based on this module's location."""
    return Path(__file__).resolve().parents[1]


def load_json_list(path: Path, label: str) -> list[dict[str, Any]]:
    """Load a JSON list and reject missing or malformed inputs."""
    if not path.is_file():
        raise FileNotFoundError(f"{label} does not exist: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise ValueError(f"{label} must be a JSON list of objects.")
    return payload


def validate_inputs(
    embeddings: Sequence[dict[str, Any]], chunks: Sequence[dict[str, Any]]
) -> tuple[int, str]:
    """Validate one-to-one identity, checksums, dimensions, and model consistency."""
    if not embeddings:
        raise ValueError("Embedding input is empty.")
    chunk_map = {str(chunk["chunk_id"]): chunk for chunk in chunks}
    if len(chunk_map) != len(chunks):
        raise ValueError("Duplicate Phase 4 chunk IDs were found.")
    dimensions = {int(record["dimension"]) for record in embeddings}
    models = {str(record["model"]) for record in embeddings}
    if len(dimensions) != 1 or len(models) != 1:
        raise ValueError("All vectors must use one dimension and one embedding model.")
    dimension = dimensions.pop()
    model = models.pop()
    ids: list[str] = []
    for record in embeddings:
        chunk_id = str(record["chunk_id"])
        ids.append(chunk_id)
        if chunk_id not in chunk_map:
            raise ValueError(f"Missing Phase 4 text for {chunk_id}.")
        vector = record.get("embedding")
        if not isinstance(vector, list) or len(vector) != dimension:
            raise ValueError(f"Invalid vector dimension for {chunk_id}.")
        checksum = hashlib.sha256(str(chunk_map[chunk_id]["text"]).encode("utf-8")).hexdigest()
        if checksum != record.get("text_sha256"):
            raise ValueError(f"Text checksum mismatch for {chunk_id}.")
    if len(ids) != len(set(ids)) or set(ids) != set(chunk_map):
        raise ValueError("Embedding and chunk IDs do not have one-to-one coverage.")
    return dimension, model


def vector_to_blob(vector: Sequence[float]) -> bytes:
    """Encode a vector as compact little-endian float32 bytes."""
    return struct.pack(f"<{len(vector)}f", *vector)


def blob_to_vector(blob: bytes, dimension: int) -> tuple[float, ...]:
    """Decode a float32 vector stored by :func:`vector_to_blob`."""
    return struct.unpack(f"<{dimension}f", blob)


def create_schema(connection: sqlite3.Connection) -> None:
    """Create the metadata and vector tables plus filter indexes."""
    connection.executescript(
        """
        PRAGMA journal_mode=DELETE;
        PRAGMA foreign_keys=ON;
        CREATE TABLE index_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE vectors (
            chunk_id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            text TEXT NOT NULL,
            source_file TEXT NOT NULL,
            source_type TEXT NOT NULL,
            department TEXT NOT NULL,
            content_category TEXT NOT NULL,
            model TEXT NOT NULL,
            dimension INTEGER NOT NULL,
            text_sha256 TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            embedding BLOB NOT NULL
        );
        CREATE INDEX idx_vectors_department ON vectors(department);
        CREATE INDEX idx_vectors_category ON vectors(content_category);
        CREATE INDEX idx_vectors_source_type ON vectors(source_type);
        """
    )


def build_index(
    index_path: Path,
    embeddings: Sequence[dict[str, Any]],
    chunks: Sequence[dict[str, Any]],
    dimension: int,
    model: str,
) -> list[dict[str, Any]]:
    """Build a complete index in a temporary file and replace it atomically."""
    chunk_map = {str(chunk["chunk_id"]): chunk for chunk in chunks}
    index_path.parent.mkdir(parents=True, exist_ok=True)
    audit: list[dict[str, Any]] = []
    with tempfile.NamedTemporaryFile(suffix=".sqlite3", dir=index_path.parent, delete=False) as handle:
        temporary_path = Path(handle.name)
    try:
        connection = sqlite3.connect(temporary_path)
        try:
            create_schema(connection)
            metadata = {
                "schema_version": SCHEMA_VERSION,
                "backend": BACKEND_NAME,
                "model": model,
                "dimension": str(dimension),
                "created_at_utc": datetime.now(timezone.utc).isoformat(),
            }
            connection.executemany(
                "INSERT INTO index_metadata(key, value) VALUES (?, ?)", metadata.items()
            )
            for position, record in enumerate(embeddings, start=1):
                chunk = chunk_map[str(record["chunk_id"])]
                retrieval = dict(chunk["retrieval_metadata"])
                connection.execute(
                    """INSERT INTO vectors VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        record["chunk_id"], record["document_id"], chunk["text"],
                        record["source_file"], record["source_type"],
                        record["department"], record["content_category"], model,
                        dimension, record["text_sha256"],
                        json.dumps(retrieval, ensure_ascii=False, sort_keys=True),
                        vector_to_blob(record["embedding"]),
                    ),
                )
                audit.append({
                    "position": position,
                    "chunk_id": record["chunk_id"],
                    "department": record["department"],
                    "content_category": record["content_category"],
                    "source_type": record["source_type"],
                    "dimension": dimension,
                    "text_sha256": record["text_sha256"],
                    "status": "indexed",
                })
            connection.commit()
            result = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise RuntimeError(f"SQLite integrity check failed: {result}")
        finally:
            connection.close()
        temporary_path.replace(index_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return audit


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Return cosine similarity, accepting normalized or unnormalized vectors."""
    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0


def query_index(
    index_path: Path,
    query_vector: Sequence[float],
    top_k: int = 5,
    department: str | None = None,
    content_category: str | None = None,
) -> list[dict[str, Any]]:
    """Run exact cosine search with optional metadata filters."""
    if top_k < 1:
        raise ValueError("top_k must be positive.")
    with sqlite3.connect(index_path) as connection:
        dimension = int(connection.execute(
            "SELECT value FROM index_metadata WHERE key='dimension'"
        ).fetchone()[0])
        if len(query_vector) != dimension:
            raise ValueError(f"Query dimension must be {dimension}.")
        clauses: list[str] = []
        parameters: list[str] = []
        if department:
            clauses.append("department = ?"); parameters.append(department)
        if content_category:
            clauses.append("content_category = ?"); parameters.append(content_category)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = connection.execute(
            "SELECT chunk_id, text, source_file, source_type, department, "
            "content_category, metadata_json, embedding FROM vectors" + where,
            parameters,
        ).fetchall()
    ranked = [
        {
            "chunk_id": row[0], "text": row[1], "source_file": row[2],
            "source_type": row[3], "department": row[4],
            "content_category": row[5], "metadata": json.loads(row[6]),
            "score": round(cosine_similarity(query_vector, blob_to_vector(row[7], dimension)), 8),
        }
        for row in rows
    ]
    return sorted(ranked, key=lambda item: (-item["score"], item["chunk_id"]))[:top_k]


def validate_index(index_path: Path, expected_count: int, dimension: int) -> None:
    """Validate persisted count, dimensions, checksums, and SQLite integrity."""
    with sqlite3.connect(index_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        rows = connection.execute(
            "SELECT text, text_sha256, length(embedding) FROM vectors"
        ).fetchall()
    if count != expected_count or integrity != "ok":
        raise RuntimeError("Persisted index count or integrity validation failed.")
    expected_bytes = dimension * 4
    for text, checksum, byte_count in rows:
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != checksum:
            raise RuntimeError("Persisted text checksum validation failed.")
        if byte_count != expected_bytes:
            raise RuntimeError("Persisted vector byte length validation failed.")


def write_json(path: Path, payload: Any) -> None:
    """Write readable UTF-8 JSON."""
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def write_audit(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    """Write a human-readable index audit CSV."""
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else [])
        writer.writeheader(); writer.writerows(rows)


def generate_plots(
    embeddings: Sequence[dict[str, Any]], plots_dir: Path
) -> tuple[Path, Path]:
    """Plot index composition by department and source type."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as error:
        raise RuntimeError("Install matplotlib to generate vector-index plots.") from error
    plots_dir.mkdir(parents=True, exist_ok=True)
    department_path = plots_dir / "06_vectors_by_department.png"
    source_path = plots_dir / "06_vectors_by_source_type.png"
    department_counts = Counter(str(item["department"]) for item in embeddings).most_common(10)
    figure, axis = plt.subplots(figsize=(10, 6))
    axis.barh([x for x, _ in reversed(department_counts)], [y for _, y in reversed(department_counts)], color="#4A90A4")
    axis.set_title("Indexed Vectors by Department"); axis.set_xlabel("Number of vectors")
    axis.grid(axis="x", alpha=0.25); figure.tight_layout(); figure.savefig(department_path, dpi=160); plt.close(figure)
    source_counts = sorted(Counter(str(item["source_type"]) for item in embeddings).items())
    figure, axis = plt.subplots(figsize=(10, 5))
    axis.bar([x for x, _ in source_counts], [y for _, y in source_counts], color="#176B87")
    axis.set_title("Indexed Vectors by Source Type"); axis.set_ylabel("Number of vectors")
    axis.grid(axis="y", alpha=0.25); figure.tight_layout(); figure.savefig(source_path, dpi=160); plt.close(figure)
    return department_path, source_path


def run_index_storage(
    embeddings_path: Path,
    chunks_path: Path,
    index_dir: Path,
    output_dir: Path,
) -> IndexResult:
    """Build, validate, test-query, report, and persist the Phase 6 index."""
    embeddings = load_json_list(embeddings_path.resolve(), "Embedding input")
    chunks = load_json_list(chunks_path.resolve(), "Enriched chunk input")
    dimension, model = validate_inputs(embeddings, chunks)
    index_dir = index_dir.resolve(); output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    index_path = index_dir / "06_vector_index.sqlite3"
    manifest_path = output_dir / "06_vector_index_manifest.json"
    report_path = output_dir / "06_vector_index_report.json"
    audit_path = output_dir / "06_vector_index_audit.csv"
    failed_path = output_dir / "06_failed_index_records.json"
    audit = build_index(index_path, embeddings, chunks, dimension, model)
    validate_index(index_path, len(embeddings), dimension)
    smoke_results = query_index(index_path, embeddings[0]["embedding"], top_k=3)
    if not smoke_results or smoke_results[0]["chunk_id"] != embeddings[0]["chunk_id"]:
        raise RuntimeError("Index smoke query did not return the source vector first.")
    department_plot, source_plot = generate_plots(embeddings, output_dir / "plots")
    failed: list[dict[str, Any]] = []
    manifest = {
        "backend": BACKEND_NAME, "schema_version": SCHEMA_VERSION,
        "index_file": str(index_path), "model": model, "dimension": dimension,
        "vector_count": len(embeddings), "distance_metric": "cosine",
        "metadata_filters": ["department", "content_category", "source_type"],
    }
    write_json(manifest_path, manifest); write_audit(audit_path, audit); write_json(failed_path, failed)
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "embeddings_input": str(embeddings_path.resolve()),
        "chunks_input": str(chunks_path.resolve()),
        **manifest,
        "failed_records": 0,
        "index_size_bytes": index_path.stat().st_size,
        "smoke_query": [{"chunk_id": item["chunk_id"], "score": item["score"]} for item in smoke_results],
        "output_files": [
            "05_vector_store/chroma_db/06_vector_index.sqlite3",
            manifest_path.name, report_path.name, audit_path.name, failed_path.name,
            f"plots/{department_plot.name}", f"plots/{source_plot.name}",
        ],
    }
    write_json(report_path, report)
    return IndexResult(index_path, manifest_path, report_path, audit_path, failed_path,
                       department_plot, source_plot, len(embeddings), len(embeddings), 0)


def build_parser() -> argparse.ArgumentParser:
    """Create the Phase 6 command-line parser."""
    root = default_project_root(); processed = root / "01_data" / "processed"
    parser = argparse.ArgumentParser(description="Store embeddings in a persistent vector index.")
    parser.add_argument("--embeddings", type=Path, default=processed / "05_embeddings.json")
    parser.add_argument("--chunks", type=Path, default=processed / "04_enriched_chunks.json")
    parser.add_argument("--index-dir", type=Path, default=root / "05_vector_store" / "chroma_db")
    parser.add_argument("--output-dir", type=Path, default=processed)
    return parser


def print_result(result: IndexResult) -> None:
    """Print the build summary and artifact inventory."""
    print("Vector index storage completed successfully.")
    print(f"Input embeddings: {result.input_embeddings}")
    print(f"Indexed vectors: {result.indexed_vectors}")
    print(f"Failed records: {result.failed_records}")
    print("\nOutput files:")
    for path in (result.index_path, result.manifest_path, result.report_path,
                 result.audit_path, result.failed_path, result.department_plot_path,
                 result.source_plot_path):
        print(f"- {path} ({path.stat().st_size:,} bytes)")


def main() -> None:
    """Run Phase 6 from command-line arguments."""
    args = build_parser().parse_args()
    print_result(run_index_storage(args.embeddings, args.chunks, args.index_dir, args.output_dir))


if __name__ == "__main__":
    main()
