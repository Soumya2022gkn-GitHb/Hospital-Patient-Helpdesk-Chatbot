"""Clean normalized hospital documents while preserving source provenance.

This module is the reusable equivalent of
``13_notebooks/02_clean_documents.ipynb``. It reads the Phase 1 normalized
records and writes cleaned records plus quality-control artifacts.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Sequence


MINIMUM_TEXT_LENGTH: Final = 20
PAGE_NUMBER_PATTERN: Final = re.compile(
    r"^(?:page\s+)?\d+(?:\s+(?:of|/|-)\s+\d+)?$",
    flags=re.IGNORECASE,
)
CONTROL_CHARACTER_PATTERN: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
HORIZONTAL_WHITESPACE_PATTERN: Final = re.compile(r"[^\S\n]+")
EXCESSIVE_NEWLINES_PATTERN: Final = re.compile(r"\n{3,}")


@dataclass(frozen=True)
class CleanedDocument:
    """A cleaned document with its original ingestion provenance."""

    document_id: str
    text: str
    source_file: str
    source_type: str
    category: str
    record_index: int
    metadata: dict[str, Any]
    cleaning: dict[str, Any]


@dataclass(frozen=True)
class RejectedDocument:
    """A document excluded from downstream processing with a reason."""

    document_id: str
    source_file: str
    reason: str
    original_character_count: int
    cleaned_character_count: int


@dataclass(frozen=True)
class CleaningResult:
    """Paths and statistics produced by one cleaning run."""

    cleaned_documents_path: Path
    cleaning_report_path: Path
    cleaning_audit_path: Path
    rejected_documents_path: Path
    operations_plot_path: Path
    characters_plot_path: Path
    input_documents: int
    cleaned_documents: int
    rejected_documents: int
    characters_removed: int


def default_project_root() -> Path:
    """Return the project root based on this module's location."""
    return Path(__file__).resolve().parents[1]


def normalize_unicode(text: str) -> tuple[str, bool]:
    """Normalize compatibility characters without removing useful symbols."""
    normalized = unicodedata.normalize("NFKC", text)
    return normalized, normalized != text


def remove_control_characters(text: str) -> tuple[str, bool]:
    """Remove non-printing controls while retaining newlines and tabs."""
    cleaned = CONTROL_CHARACTER_PATTERN.sub("", text)
    return cleaned, cleaned != text


def remove_standalone_page_numbers(text: str) -> tuple[str, int]:
    """Remove lines containing only a page number or page-number label."""
    lines = text.splitlines()
    retained = [line for line in lines if not PAGE_NUMBER_PATTERN.fullmatch(line.strip())]
    return "\n".join(retained), len(lines) - len(retained)


def remove_adjacent_duplicate_lines(text: str) -> tuple[str, int]:
    """Remove repeated neighboring lines, commonly produced by headers."""
    retained: list[str] = []
    removed = 0
    previous_normalized: str | None = None
    for line in text.splitlines():
        normalized = " ".join(line.lower().split())
        if normalized and normalized == previous_normalized:
            removed += 1
            continue
        retained.append(line)
        previous_normalized = normalized if normalized else previous_normalized
    return "\n".join(retained), removed


def remove_repeated_prefix(text: str) -> tuple[str, bool]:
    """Remove a duplicated multi-word title at the beginning of a record."""
    words = text.split()
    maximum_phrase_length = min(12, len(words) // 2)
    for phrase_length in range(maximum_phrase_length, 1, -1):
        first = [word.casefold() for word in words[:phrase_length]]
        second = [word.casefold() for word in words[phrase_length : phrase_length * 2]]
        if first == second:
            return " ".join(words[phrase_length:]), True
    return text, False


def repair_wrapped_lines(text: str) -> tuple[str, int]:
    """Join layout-wrapped lines while retaining paragraph boundaries."""
    paragraphs: list[str] = []
    current_lines: list[str] = []
    joins = 0

    def flush() -> None:
        nonlocal joins
        if not current_lines:
            return
        joins += max(0, len(current_lines) - 1)
        paragraphs.append(" ".join(line.strip() for line in current_lines if line.strip()))
        current_lines.clear()

    for line in text.splitlines():
        if line.strip():
            current_lines.append(line)
        else:
            flush()
    flush()
    return "\n\n".join(paragraphs), joins


def normalize_whitespace(text: str) -> tuple[str, bool]:
    """Collapse extra spaces and excessive blank lines."""
    cleaned = HORIZONTAL_WHITESPACE_PATTERN.sub(" ", text)
    cleaned = "\n".join(line.strip() for line in cleaned.splitlines())
    cleaned = EXCESSIVE_NEWLINES_PATTERN.sub("\n\n", cleaned).strip()
    return cleaned, cleaned != text


def clean_text(
    text: str,
    *,
    repair_pdf_line_wraps: bool = False,
) -> tuple[str, list[str], dict[str, int]]:
    """Apply deterministic, auditable cleaning rules to one text value."""
    operations: list[str] = []
    metrics = {
        "page_number_lines_removed": 0,
        "duplicate_lines_removed": 0,
        "wrapped_lines_joined": 0,
    }

    cleaned, changed = normalize_unicode(text)
    if changed:
        operations.append("unicode_normalized")

    cleaned, changed = remove_control_characters(cleaned)
    if changed:
        operations.append("control_characters_removed")

    cleaned, count = remove_standalone_page_numbers(cleaned)
    if count:
        operations.append("page_numbers_removed")
        metrics["page_number_lines_removed"] = count

    cleaned, count = remove_adjacent_duplicate_lines(cleaned)
    if count:
        operations.append("duplicate_lines_removed")
        metrics["duplicate_lines_removed"] = count

    cleaned, changed = remove_repeated_prefix(cleaned)
    if changed:
        operations.append("repeated_prefix_removed")

    if repair_pdf_line_wraps:
        cleaned, count = repair_wrapped_lines(cleaned)
        if count:
            operations.append("wrapped_lines_joined")
            metrics["wrapped_lines_joined"] = count

    cleaned, changed = normalize_whitespace(cleaned)
    if changed:
        operations.append("whitespace_normalized")

    return cleaned, operations, metrics


def load_ingested_documents(path: Path) -> list[dict[str, Any]]:
    """Load and validate the Phase 1 JSON collection."""
    if not path.is_file():
        raise FileNotFoundError(f"Ingestion input does not exist: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Ingestion input must be a JSON list.")

    required_fields = {
        "document_id",
        "text",
        "source_file",
        "source_type",
        "category",
        "record_index",
        "metadata",
    }
    for index, document in enumerate(payload, start=1):
        if not isinstance(document, dict) or not required_fields.issubset(document):
            raise ValueError(f"Document {index} does not match the ingestion schema.")
    return payload


def clean_documents(
    documents: Sequence[dict[str, Any]],
    minimum_text_length: int = MINIMUM_TEXT_LENGTH,
) -> tuple[list[CleanedDocument], list[RejectedDocument], list[dict[str, Any]]]:
    """Clean documents and return accepted, rejected, and audit records."""
    cleaned_documents: list[CleanedDocument] = []
    rejected_documents: list[RejectedDocument] = []
    audit_records: list[dict[str, Any]] = []

    for document in documents:
        original_text = str(document["text"])
        cleaned_text, operations, metrics = clean_text(
            original_text,
            repair_pdf_line_wraps=document["source_type"] == "pdf",
        )
        original_count = len(original_text)
        cleaned_count = len(cleaned_text)

        if cleaned_count < minimum_text_length:
            rejected_documents.append(
                RejectedDocument(
                    document_id=str(document["document_id"]),
                    source_file=str(document["source_file"]),
                    reason=f"cleaned text shorter than {minimum_text_length} characters",
                    original_character_count=original_count,
                    cleaned_character_count=cleaned_count,
                )
            )
            status = "rejected"
        else:
            cleaning_metadata = {
                "original_character_count": original_count,
                "cleaned_character_count": cleaned_count,
                "characters_removed": original_count - cleaned_count,
                "operations": operations,
                **metrics,
            }
            cleaned_documents.append(
                CleanedDocument(
                    document_id=str(document["document_id"]),
                    text=cleaned_text,
                    source_file=str(document["source_file"]),
                    source_type=str(document["source_type"]),
                    category=str(document["category"]),
                    record_index=int(document["record_index"]),
                    metadata=dict(document["metadata"]),
                    cleaning=cleaning_metadata,
                )
            )
            status = "cleaned"

        audit_records.append(
            {
                "document_id": document["document_id"],
                "source_file": document["source_file"],
                "source_type": document["source_type"],
                "status": status,
                "original_character_count": original_count,
                "cleaned_character_count": cleaned_count,
                "characters_removed": original_count - cleaned_count,
                "operations": ";".join(operations),
            }
        )

    return cleaned_documents, rejected_documents, audit_records


def validate_cleaned_documents(documents: Sequence[CleanedDocument]) -> None:
    """Check non-empty text and stable unique IDs after cleaning."""
    if not documents:
        raise RuntimeError("No documents passed cleaning validation.")
    document_ids = [document.document_id for document in documents]
    if len(document_ids) != len(set(document_ids)):
        raise RuntimeError("Duplicate document IDs exist after cleaning.")
    if any(not document.text.strip() for document in documents):
        raise RuntimeError("A cleaned document contains empty text.")


def write_json(path: Path, payload: Any) -> None:
    """Write readable UTF-8 JSON."""
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def write_audit(path: Path, audit_records: Sequence[dict[str, Any]]) -> None:
    """Write one cleaning audit row per input document."""
    fieldnames = [
        "document_id",
        "source_file",
        "source_type",
        "status",
        "original_character_count",
        "cleaned_character_count",
        "characters_removed",
        "operations",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(audit_records)


def generate_plots(
    cleaned_documents: Sequence[CleanedDocument],
    plots_dir: Path,
) -> tuple[Path, Path]:
    """Generate cleaning-operation and character-reduction diagnostics."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as error:
        raise RuntimeError("Install matplotlib to generate cleaning plots.") from error

    plots_dir.mkdir(parents=True, exist_ok=True)
    operations_path = plots_dir / "02_cleaning_operations.png"
    characters_path = plots_dir / "02_characters_removed_by_source_type.png"

    operation_counts = Counter(
        operation
        for document in cleaned_documents
        for operation in document.cleaning["operations"]
    )
    operation_labels = sorted(operation_counts)
    figure, axis = plt.subplots(figsize=(10, 5))
    bars = axis.bar(
        operation_labels,
        [operation_counts[label] for label in operation_labels],
        color="#176B87",
    )
    axis.set_title("Cleaning Operations Applied")
    axis.set_xlabel("Cleaning operation")
    axis.set_ylabel("Documents affected")
    axis.tick_params(axis="x", rotation=25)
    axis.grid(axis="y", alpha=0.25)
    for bar in bars:
        axis.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + 0.08,
            str(int(bar.get_height())),
            ha="center",
        )
    figure.tight_layout()
    figure.savefig(operations_path, dpi=160)
    plt.close(figure)

    characters_by_source = Counter()
    for document in cleaned_documents:
        characters_by_source[document.source_type] += int(
            document.cleaning["characters_removed"]
        )
    source_labels = sorted(characters_by_source)
    figure, axis = plt.subplots(figsize=(9, 5))
    bars = axis.bar(
        source_labels,
        [characters_by_source[label] for label in source_labels],
        color="#4A90A4",
    )
    axis.set_title("Characters Removed by Source Type")
    axis.set_xlabel("Source type")
    axis.set_ylabel("Characters removed")
    axis.grid(axis="y", alpha=0.25)
    for bar in bars:
        axis.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + 1,
            str(int(bar.get_height())),
            ha="center",
        )
    figure.tight_layout()
    figure.savefig(characters_path, dpi=160)
    plt.close(figure)
    return operations_path, characters_path


def run_cleaning(
    input_path: Path,
    output_dir: Path,
    minimum_text_length: int = MINIMUM_TEXT_LENGTH,
) -> CleaningResult:
    """Run loading, cleaning, validation, reporting, and output writing."""
    input_documents = load_ingested_documents(input_path.resolve())
    cleaned, rejected, audit = clean_documents(input_documents, minimum_text_length)
    validate_cleaned_documents(cleaned)

    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    cleaned_path = output_dir / "02_cleaned_documents.json"
    report_path = output_dir / "02_cleaning_report.json"
    audit_path = output_dir / "02_cleaning_audit.csv"
    rejected_path = output_dir / "02_rejected_documents.json"
    plots_dir = output_dir / "plots"

    write_json(cleaned_path, [asdict(document) for document in cleaned])
    write_json(rejected_path, [asdict(document) for document in rejected])
    write_audit(audit_path, audit)
    operations_plot_path, characters_plot_path = generate_plots(cleaned, plots_dir)

    operation_counts = Counter(
        operation
        for document in cleaned
        for operation in document.cleaning["operations"]
    )
    original_characters = sum(len(str(document["text"])) for document in input_documents)
    cleaned_characters = sum(len(document.text) for document in cleaned)
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "input_file": str(input_path.resolve()),
        "input_documents": len(input_documents),
        "cleaned_documents": len(cleaned),
        "rejected_documents": len(rejected),
        "minimum_text_length": minimum_text_length,
        "original_characters": original_characters,
        "cleaned_characters": cleaned_characters,
        "characters_removed": original_characters - cleaned_characters,
        "operation_counts": dict(sorted(operation_counts.items())),
        "documents_by_source_type": dict(
            sorted(Counter(document.source_type for document in cleaned).items())
        ),
        "output_files": [
            cleaned_path.name,
            report_path.name,
            audit_path.name,
            rejected_path.name,
            f"plots/{operations_plot_path.name}",
            f"plots/{characters_plot_path.name}",
        ],
    }
    write_json(report_path, report)

    return CleaningResult(
        cleaned_documents_path=cleaned_path,
        cleaning_report_path=report_path,
        cleaning_audit_path=audit_path,
        rejected_documents_path=rejected_path,
        operations_plot_path=operations_plot_path,
        characters_plot_path=characters_plot_path,
        input_documents=len(input_documents),
        cleaned_documents=len(cleaned),
        rejected_documents=len(rejected),
        characters_removed=report["characters_removed"],
    )


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line argument parser."""
    processed_dir = default_project_root() / "01_data" / "processed"
    parser = argparse.ArgumentParser(
        description="Clean normalized hospital helpdesk documents."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=processed_dir / "01_loaded_documents.json",
        help="Phase 1 normalized JSON input.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=processed_dir,
        help="Directory for cleaning outputs.",
    )
    parser.add_argument(
        "--minimum-text-length",
        type=int,
        default=MINIMUM_TEXT_LENGTH,
        help="Reject text shorter than this value after cleaning.",
    )
    return parser


def print_result(result: CleaningResult) -> None:
    """Print a concise cleaning summary and output paths."""
    print("Document cleaning completed successfully.")
    print(f"Input documents: {result.input_documents}")
    print(f"Cleaned documents: {result.cleaned_documents}")
    print(f"Rejected documents: {result.rejected_documents}")
    print(f"Characters removed: {result.characters_removed:,}")
    print("\nOutput files:")
    for path in (
        result.cleaned_documents_path,
        result.cleaning_report_path,
        result.cleaning_audit_path,
        result.rejected_documents_path,
        result.operations_plot_path,
        result.characters_plot_path,
    ):
        print(f"- {path} ({path.stat().st_size:,} bytes)")


def main() -> None:
    """Run document cleaning from command-line arguments."""
    args = build_parser().parse_args()
    result = run_cleaning(
        input_path=args.input,
        output_dir=args.output_dir,
        minimum_text_length=args.minimum_text_length,
    )
    print_result(result)


if __name__ == "__main__":
    main()
