"""Split cleaned hospital documents into retrieval-sized text chunks.

This module is the reusable equivalent of
``13_notebooks/03_chunk_documents.ipynb``. It uses deterministic,
boundary-aware character chunking and preserves source and cleaning metadata.
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
from typing import Any, Final, Sequence


DEFAULT_CHUNK_SIZE: Final = 800
DEFAULT_CHUNK_OVERLAP: Final = 120
DEFAULT_MINIMUM_CHUNK_SIZE: Final = 80
BREAKPOINT_PATTERNS: Final = (
    re.compile(r"\n\n"),
    re.compile(r"(?<=[.!?])\s+"),
    re.compile(r"(?<=[;:])\s+"),
    re.compile(r"\s+"),
)


@dataclass(frozen=True)
class ChunkingConfig:
    """Validated settings controlling chunk length and overlap."""

    chunk_size: int = DEFAULT_CHUNK_SIZE
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP
    minimum_chunk_size: int = DEFAULT_MINIMUM_CHUNK_SIZE

    def __post_init__(self) -> None:
        if self.chunk_size <= 0:
            raise ValueError("chunk_size must be greater than zero.")
        if self.chunk_overlap < 0:
            raise ValueError("chunk_overlap cannot be negative.")
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap must be smaller than chunk_size.")
        if not 0 < self.minimum_chunk_size <= self.chunk_size:
            raise ValueError("minimum_chunk_size must be between 1 and chunk_size.")


@dataclass(frozen=True)
class TextChunk:
    """One retrieval unit with inherited source provenance."""

    chunk_id: str
    document_id: str
    text: str
    source_file: str
    source_type: str
    category: str
    record_index: int
    chunk_index: int
    chunk_count: int
    character_start: int
    character_end: int
    character_count: int
    metadata: dict[str, Any]


@dataclass(frozen=True)
class RejectedChunk:
    """A candidate chunk excluded from output with its reason."""

    document_id: str
    chunk_index: int
    reason: str
    character_count: int


@dataclass(frozen=True)
class ChunkingResult:
    """Paths and statistics produced by one chunking run."""

    chunks_path: Path
    report_path: Path
    audit_path: Path
    rejected_path: Path
    length_plot_path: Path
    source_plot_path: Path
    input_documents: int
    chunks_created: int
    rejected_chunks: int


def default_project_root() -> Path:
    """Return the project root based on this module's location."""
    return Path(__file__).resolve().parents[1]


def load_cleaned_documents(path: Path) -> list[dict[str, Any]]:
    """Load and validate the Phase 2 cleaned-document collection."""
    if not path.is_file():
        raise FileNotFoundError(f"Cleaned input does not exist: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Cleaned input must be a JSON list.")

    required_fields = {
        "document_id",
        "text",
        "source_file",
        "source_type",
        "category",
        "record_index",
        "metadata",
        "cleaning",
    }
    for index, document in enumerate(payload, start=1):
        if not isinstance(document, dict) or not required_fields.issubset(document):
            raise ValueError(f"Document {index} does not match the Phase 2 schema.")
    return payload


def find_preferred_end(text: str, start: int, limit: int, minimum_end: int) -> int:
    """Find the strongest available text boundary before a hard limit."""
    window = text[start:limit]
    minimum_relative_end = max(1, minimum_end - start)
    for pattern in BREAKPOINT_PATTERNS:
        candidates = [match.end() for match in pattern.finditer(window)]
        valid_candidates = [position for position in candidates if position >= minimum_relative_end]
        if valid_candidates:
            return start + valid_candidates[-1]
    return limit


def split_text(text: str, config: ChunkingConfig) -> list[tuple[str, int, int]]:
    """Split text at preferred boundaries while retaining exact source offsets."""
    if not text.strip():
        return []

    chunks: list[tuple[str, int, int]] = []
    start = 0
    text_length = len(text)

    while start < text_length:
        hard_end = min(start + config.chunk_size, text_length)
        if hard_end == text_length:
            end = text_length
        else:
            preferred_minimum = start + max(
                config.minimum_chunk_size,
                int(config.chunk_size * 0.60),
            )
            end = find_preferred_end(text, start, hard_end, preferred_minimum)

        raw_chunk = text[start:end]
        left_trim = len(raw_chunk) - len(raw_chunk.lstrip())
        right_trimmed = raw_chunk.rstrip()
        chunk_start = start + left_trim
        chunk_end = start + len(right_trimmed)
        chunk_text = text[chunk_start:chunk_end]

        if chunk_text:
            chunks.append((chunk_text, chunk_start, chunk_end))
        if end >= text_length:
            break

        next_start = max(0, end - config.chunk_overlap)
        if (
            0 < next_start < text_length
            and text[next_start - 1].isalnum()
            and text[next_start].isalnum()
        ):
            following_boundary = re.search(r"\s+", text[next_start:end])
            if following_boundary is not None:
                next_start += following_boundary.end()
        if next_start <= start:
            next_start = end
        start = next_start

    return chunks


def chunk_document(
    document: dict[str, Any],
    config: ChunkingConfig,
) -> tuple[list[TextChunk], list[RejectedChunk]]:
    """Split one document and attach inherited and chunk-specific metadata."""
    candidates = split_text(str(document["text"]), config)
    accepted_candidates: list[tuple[str, int, int]] = []
    rejected: list[RejectedChunk] = []

    for index, (text, start, end) in enumerate(candidates, start=1):
        if len(text) < config.minimum_chunk_size and len(candidates) > 1:
            rejected.append(
                RejectedChunk(
                    document_id=str(document["document_id"]),
                    chunk_index=index,
                    reason=(
                        f"chunk shorter than {config.minimum_chunk_size} characters"
                    ),
                    character_count=len(text),
                )
            )
        else:
            accepted_candidates.append((text, start, end))

    chunk_count = len(accepted_candidates)
    chunks = [
        TextChunk(
            chunk_id=f"{document['document_id']}-chunk-{index:03d}",
            document_id=str(document["document_id"]),
            text=text,
            source_file=str(document["source_file"]),
            source_type=str(document["source_type"]),
            category=str(document["category"]),
            record_index=int(document["record_index"]),
            chunk_index=index,
            chunk_count=chunk_count,
            character_start=start,
            character_end=end,
            character_count=len(text),
            metadata={
                **dict(document["metadata"]),
                "cleaning": dict(document["cleaning"]),
            },
        )
        for index, (text, start, end) in enumerate(accepted_candidates, start=1)
    ]
    return chunks, rejected


def chunk_documents(
    documents: Sequence[dict[str, Any]],
    config: ChunkingConfig,
) -> tuple[list[TextChunk], list[RejectedChunk], list[dict[str, Any]]]:
    """Chunk all documents and create one audit record per source document."""
    chunks: list[TextChunk] = []
    rejected: list[RejectedChunk] = []
    audit: list[dict[str, Any]] = []

    for document in documents:
        document_chunks, document_rejections = chunk_document(document, config)
        chunks.extend(document_chunks)
        rejected.extend(document_rejections)
        audit.append(
            {
                "document_id": document["document_id"],
                "source_file": document["source_file"],
                "source_type": document["source_type"],
                "document_character_count": len(str(document["text"])),
                "chunks_created": len(document_chunks),
                "chunks_rejected": len(document_rejections),
                "minimum_chunk_length": min(
                    (chunk.character_count for chunk in document_chunks),
                    default=0,
                ),
                "maximum_chunk_length": max(
                    (chunk.character_count for chunk in document_chunks),
                    default=0,
                ),
            }
        )
    return chunks, rejected, audit


def validate_chunks(
    documents: Sequence[dict[str, Any]],
    chunks: Sequence[TextChunk],
    config: ChunkingConfig,
) -> None:
    """Validate identifiers, lengths, offsets, and source coverage."""
    if not chunks:
        raise RuntimeError("No chunks were created.")
    chunk_ids = [chunk.chunk_id for chunk in chunks]
    if len(chunk_ids) != len(set(chunk_ids)):
        raise RuntimeError("Duplicate chunk IDs were generated.")
    if any(chunk.character_count > config.chunk_size for chunk in chunks):
        raise RuntimeError("A chunk exceeds the configured chunk size.")
    if any(not chunk.text.strip() for chunk in chunks):
        raise RuntimeError("An empty chunk was generated.")
    covered_document_ids = {chunk.document_id for chunk in chunks}
    expected_document_ids = {str(document["document_id"]) for document in documents}
    if covered_document_ids != expected_document_ids:
        missing = sorted(expected_document_ids - covered_document_ids)
        raise RuntimeError(f"Documents missing from chunk output: {missing}")
    for chunk in chunks:
        if chunk.character_end - chunk.character_start != chunk.character_count:
            raise RuntimeError(f"Invalid offsets for chunk: {chunk.chunk_id}")


def write_json(path: Path, payload: Any) -> None:
    """Write readable UTF-8 JSON."""
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def write_audit(path: Path, audit: Sequence[dict[str, Any]]) -> None:
    """Write one chunking audit row per source document."""
    fieldnames = [
        "document_id",
        "source_file",
        "source_type",
        "document_character_count",
        "chunks_created",
        "chunks_rejected",
        "minimum_chunk_length",
        "maximum_chunk_length",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(audit)


def percentile(values: Sequence[int], percentage: float) -> float:
    """Calculate a linearly interpolated percentile without extra dependencies."""
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentage
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def generate_plots(
    chunks: Sequence[TextChunk],
    plots_dir: Path,
    config: ChunkingConfig,
) -> tuple[Path, Path]:
    """Generate PNG diagnostics for chunk length and source-type volume."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as error:
        raise RuntimeError("Install matplotlib to generate chunking plots.") from error

    plots_dir.mkdir(parents=True, exist_ok=True)
    length_plot_path = plots_dir / "03_chunk_length_distribution.png"
    source_plot_path = plots_dir / "03_chunks_by_source_type.png"

    lengths = [chunk.character_count for chunk in chunks]
    figure, axis = plt.subplots(figsize=(9, 5))
    axis.hist(lengths, bins=12, color="#176B87", edgecolor="white")
    axis.axvline(config.chunk_size, color="#B33A3A", linestyle="--", label="Chunk limit")
    axis.set_title("Chunk Length Distribution")
    axis.set_xlabel("Characters per chunk")
    axis.set_ylabel("Number of chunks")
    axis.grid(axis="y", alpha=0.25)
    axis.legend()
    figure.tight_layout()
    figure.savefig(length_plot_path, dpi=160)
    plt.close(figure)

    source_counts = Counter(chunk.source_type for chunk in chunks)
    labels = list(sorted(source_counts))
    values = [source_counts[label] for label in labels]
    figure, axis = plt.subplots(figsize=(9, 5))
    axis.bar(labels, values, color="#4A90A4")
    axis.set_title("Chunks by Source Type")
    axis.set_xlabel("Source type")
    axis.set_ylabel("Number of chunks")
    axis.grid(axis="y", alpha=0.25)
    figure.tight_layout()
    figure.savefig(source_plot_path, dpi=160)
    plt.close(figure)
    return length_plot_path, source_plot_path


def run_chunking(
    input_path: Path,
    output_dir: Path,
    config: ChunkingConfig,
) -> ChunkingResult:
    """Run loading, chunking, validation, reporting, plotting, and output writing."""
    documents = load_cleaned_documents(input_path.resolve())
    chunks, rejected, audit = chunk_documents(documents, config)
    validate_chunks(documents, chunks, config)

    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    plots_dir = output_dir / "plots"
    chunks_path = output_dir / "03_text_chunks.json"
    report_path = output_dir / "03_chunking_report.json"
    audit_path = output_dir / "03_chunking_audit.csv"
    rejected_path = output_dir / "03_rejected_chunks.json"

    write_json(chunks_path, [asdict(chunk) for chunk in chunks])
    write_json(rejected_path, [asdict(chunk) for chunk in rejected])
    write_audit(audit_path, audit)
    length_plot_path, source_plot_path = generate_plots(chunks, plots_dir, config)

    lengths = [chunk.character_count for chunk in chunks]
    documents_split = sum(item["chunks_created"] > 1 for item in audit)
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "input_file": str(input_path.resolve()),
        "input_documents": len(documents),
        "chunks_created": len(chunks),
        "rejected_chunks": len(rejected),
        "documents_split_into_multiple_chunks": documents_split,
        "configuration": asdict(config),
        "chunk_length_statistics": {
            "minimum": min(lengths),
            "maximum": max(lengths),
            "mean": round(sum(lengths) / len(lengths), 2),
            "median": round(percentile(lengths, 0.50), 2),
            "p95": round(percentile(lengths, 0.95), 2),
        },
        "chunks_by_source_type": dict(
            sorted(Counter(chunk.source_type for chunk in chunks).items())
        ),
        "output_files": [
            chunks_path.name,
            report_path.name,
            audit_path.name,
            rejected_path.name,
            f"plots/{length_plot_path.name}",
            f"plots/{source_plot_path.name}",
        ],
    }
    write_json(report_path, report)

    return ChunkingResult(
        chunks_path=chunks_path,
        report_path=report_path,
        audit_path=audit_path,
        rejected_path=rejected_path,
        length_plot_path=length_plot_path,
        source_plot_path=source_plot_path,
        input_documents=len(documents),
        chunks_created=len(chunks),
        rejected_chunks=len(rejected),
    )


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line argument parser."""
    processed_dir = default_project_root() / "01_data" / "processed"
    parser = argparse.ArgumentParser(
        description="Split cleaned hospital documents into RAG chunks."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=processed_dir / "02_cleaned_documents.json",
        help="Phase 2 cleaned JSON input.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=processed_dir,
        help="Directory for chunks, reports, audits, and plots.",
    )
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    parser.add_argument("--chunk-overlap", type=int, default=DEFAULT_CHUNK_OVERLAP)
    parser.add_argument(
        "--minimum-chunk-size",
        type=int,
        default=DEFAULT_MINIMUM_CHUNK_SIZE,
    )
    return parser


def print_result(result: ChunkingResult) -> None:
    """Print a concise chunking summary and output paths."""
    print("Document chunking completed successfully.")
    print(f"Input documents: {result.input_documents}")
    print(f"Chunks created: {result.chunks_created}")
    print(f"Rejected chunks: {result.rejected_chunks}")
    print("\nOutput files:")
    for path in (
        result.chunks_path,
        result.report_path,
        result.audit_path,
        result.rejected_path,
        result.length_plot_path,
        result.source_plot_path,
    ):
        print(f"- {path} ({path.stat().st_size:,} bytes)")


def main() -> None:
    """Run document chunking from command-line arguments."""
    args = build_parser().parse_args()
    config = ChunkingConfig(
        chunk_size=args.chunk_size,
        chunk_overlap=args.chunk_overlap,
        minimum_chunk_size=args.minimum_chunk_size,
    )
    result = run_chunking(args.input, args.output_dir, config)
    print_result(result)


if __name__ == "__main__":
    main()
