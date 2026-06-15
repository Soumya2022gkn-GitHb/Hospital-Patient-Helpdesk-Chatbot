"""Load hospital helpdesk sources into a normalized document collection.

This module implements the production-oriented equivalent of
``13_notebooks/01_load_documents.ipynb``. It supports PDF, CSV, JSON, JSONL,
HTML, SQLite, SQL, and text sources while preserving source provenance.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sqlite3
import subprocess
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Final, Sequence


SUPPORTED_EXTENSIONS: Final = (
    ".csv",
    ".db",
    ".html",
    ".json",
    ".jsonl",
    ".pdf",
    ".sql",
    ".txt",
)
REQUIRED_SOURCE_CATEGORIES: Final = frozenset(
    {"database", "faqs", "manuals", "pdfs", "support_logs", "tabular", "web_pages"}
)


@dataclass(frozen=True)
class LoadedDocument:
    """One normalized source record for downstream RAG processing."""

    document_id: str
    text: str
    source_file: str
    source_type: str
    category: str
    record_index: int
    metadata: dict[str, Any]


@dataclass(frozen=True)
class LoadFailure:
    """A non-fatal source loading error."""

    source_file: str
    error_type: str
    message: str


@dataclass(frozen=True)
class IngestionResult:
    """Paths and statistics produced by one ingestion run."""

    documents_path: Path
    manifest_path: Path
    inventory_path: Path
    failures_path: Path
    source_type_plot_path: Path
    source_category_plot_path: Path
    documents_created: int
    files_discovered: int
    files_loaded: int
    files_skipped: int
    files_failed: int


class VisibleTextParser(HTMLParser):
    """Collect visible text from simple HTML pages."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del attrs
        if tag in {"script", "style"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth and data.strip():
            self.parts.append(data.strip())


def default_project_root() -> Path:
    """Return the project root based on this module's location."""
    return Path(__file__).resolve().parents[1]


def row_to_text(row: dict[str, Any]) -> str:
    """Convert a structured record into readable, deterministic text."""
    return " | ".join(
        f"{key}: {value}"
        for key, value in row.items()
        if value not in (None, "")
    )


def source_key(relative_path: str) -> str:
    """Create a stable identifier prefix from a source-relative path."""
    return (
        relative_path.lower()
        .replace("/", "-")
        .replace(".", "-")
        .replace("_", "-")
        .replace(" ", "-")
    )


def make_document(
    raw_data_dir: Path,
    path: Path,
    text: str,
    source_type: str,
    record_index: int = 1,
    metadata: dict[str, Any] | None = None,
) -> LoadedDocument:
    """Create a normalized document while preserving source provenance."""
    relative_path = path.relative_to(raw_data_dir).as_posix()
    return LoadedDocument(
        document_id=f"{source_key(relative_path)}-{record_index:04d}",
        text=text.strip(),
        source_file=relative_path,
        source_type=source_type,
        category=path.parent.name,
        record_index=record_index,
        metadata=metadata or {},
    )


def load_csv(raw_data_dir: Path, path: Path) -> list[LoadedDocument]:
    """Load one normalized document per CSV row."""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return [
        make_document(raw_data_dir, path, row_to_text(row), "csv", index, row)
        for index, row in enumerate(rows, start=1)
    ]


def load_json(raw_data_dir: Path, path: Path) -> list[LoadedDocument]:
    """Load a JSON object or list of objects."""
    data = json.loads(path.read_text(encoding="utf-8"))
    records = data if isinstance(data, list) else [data]
    documents: list[LoadedDocument] = []
    for index, record in enumerate(records, start=1):
        metadata = record if isinstance(record, dict) else {}
        text = row_to_text(record) if isinstance(record, dict) else str(record)
        documents.append(
            make_document(raw_data_dir, path, text, "json", index, metadata)
        )
    return documents


def load_jsonl(raw_data_dir: Path, path: Path) -> list[LoadedDocument]:
    """Load one JSON object per non-empty line."""
    records = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [
        make_document(
            raw_data_dir,
            path,
            row_to_text(record),
            "jsonl",
            index,
            record,
        )
        for index, record in enumerate(records, start=1)
    ]


def load_html(raw_data_dir: Path, path: Path) -> list[LoadedDocument]:
    """Extract visible text from a local HTML page."""
    parser = VisibleTextParser()
    parser.feed(path.read_text(encoding="utf-8"))
    return [make_document(raw_data_dir, path, " ".join(parser.parts), "html")]


def load_text(raw_data_dir: Path, path: Path) -> list[LoadedDocument]:
    """Load a UTF-8 text or SQL file."""
    source_type = path.suffix.lower().lstrip(".")
    return [
        make_document(
            raw_data_dir,
            path,
            path.read_text(encoding="utf-8"),
            source_type,
        )
    ]


def extract_pdf_text(path: Path) -> str:
    """Extract PDF text using pypdf or the local pdftotext executable."""
    try:
        from pypdf import PdfReader
    except ImportError:
        PdfReader = None

    if PdfReader is not None:
        reader = PdfReader(path)
        return "\n".join(page.extract_text() or "" for page in reader.pages)

    executable = shutil.which("pdftotext")
    if executable is None:
        raise RuntimeError("Install pypdf or pdftotext to ingest PDF documents.")

    with tempfile.TemporaryDirectory() as temp_dir:
        output_path = Path(temp_dir) / "document.txt"
        subprocess.run(
            [executable, "-layout", str(path), str(output_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        return output_path.read_text(encoding="utf-8", errors="replace")


def load_pdf(raw_data_dir: Path, path: Path) -> list[LoadedDocument]:
    """Load extracted text from a PDF document."""
    return [make_document(raw_data_dir, path, extract_pdf_text(path), "pdf")]


def load_sqlite(raw_data_dir: Path, path: Path) -> list[LoadedDocument]:
    """Load one document for every row in every user-defined SQLite table."""
    documents: list[LoadedDocument] = []
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        table_names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        record_index = 1
        for table_name in table_names:
            quoted_name = table_name.replace('"', '""')
            for row in connection.execute(f'SELECT * FROM "{quoted_name}"'):
                record = dict(row)
                metadata = {"table_name": table_name, **record}
                documents.append(
                    make_document(
                        raw_data_dir,
                        path,
                        row_to_text(record),
                        "sqlite",
                        record_index,
                        metadata,
                    )
                )
                record_index += 1
    return documents


Loader = Callable[[Path, Path], list[LoadedDocument]]
LOADERS: Final[dict[str, Loader]] = {
    ".csv": load_csv,
    ".db": load_sqlite,
    ".html": load_html,
    ".json": load_json,
    ".jsonl": load_jsonl,
    ".pdf": load_pdf,
    ".sql": load_text,
    ".txt": load_text,
}


def discover_source_files(raw_data_dir: Path) -> list[Path]:
    """Return all raw files in deterministic order."""
    return sorted(
        (path for path in raw_data_dir.rglob("*") if path.is_file()),
        key=lambda path: path.as_posix().lower(),
    )


def load_documents(
    raw_data_dir: Path,
    paths: Sequence[Path],
) -> tuple[list[LoadedDocument], list[LoadFailure], list[dict[str, Any]]]:
    """Load supported files without aborting the run on one bad source."""
    documents: list[LoadedDocument] = []
    failures: list[LoadFailure] = []
    inventory: list[dict[str, Any]] = []

    for path in paths:
        relative_path = path.relative_to(raw_data_dir).as_posix()
        loader = LOADERS.get(path.suffix.lower())
        if loader is None:
            inventory.append(
                inventory_record(path, relative_path, "skipped", records_loaded=0)
            )
            continue

        try:
            loaded = [document for document in loader(raw_data_dir, path) if document.text]
            documents.extend(loaded)
            inventory.append(
                inventory_record(
                    path,
                    relative_path,
                    "loaded",
                    records_loaded=len(loaded),
                )
            )
        except Exception as error:  # Continue so all source failures are reported.
            failures.append(
                LoadFailure(relative_path, type(error).__name__, str(error))
            )
            inventory.append(
                inventory_record(path, relative_path, "failed", records_loaded=0)
            )

    return documents, failures, inventory


def inventory_record(
    path: Path,
    relative_path: str,
    status: str,
    records_loaded: int,
) -> dict[str, Any]:
    """Build one source inventory row."""
    return {
        "source_file": relative_path,
        "extension": path.suffix.lower(),
        "status": status,
        "records_loaded": records_loaded,
        "size_bytes": path.stat().st_size,
    }


def write_json(path: Path, payload: Any) -> None:
    """Write readable UTF-8 JSON."""
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def write_inventory(path: Path, inventory: Sequence[dict[str, Any]]) -> None:
    """Write the human-readable source inventory."""
    fieldnames = [
        "source_file",
        "extension",
        "status",
        "records_loaded",
        "size_bytes",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(inventory)


def validate_documents(documents: Sequence[LoadedDocument]) -> None:
    """Validate required categories, text, and identifier uniqueness."""
    if not documents:
        raise RuntimeError("No documents were created.")

    missing_categories = REQUIRED_SOURCE_CATEGORIES - {
        document.category for document in documents
    }
    if missing_categories:
        raise RuntimeError(
            f"Required source categories were not loaded: {sorted(missing_categories)}"
        )

    document_ids = [document.document_id for document in documents]
    if len(document_ids) != len(set(document_ids)):
        raise RuntimeError("Duplicate normalized document IDs were generated.")


def generate_plots(
    documents: Sequence[LoadedDocument],
    inventory: Sequence[dict[str, Any]],
    plots_dir: Path,
) -> tuple[Path, Path]:
    """Generate source-type and source-category ingestion diagnostics."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as error:
        raise RuntimeError("Install matplotlib to generate ingestion plots.") from error

    plots_dir.mkdir(parents=True, exist_ok=True)
    source_type_path = plots_dir / "01_documents_by_source_type.png"
    source_category_path = plots_dir / "01_files_by_source_category.png"

    type_counts = Counter(document.source_type for document in documents)
    type_labels = sorted(type_counts)
    figure, axis = plt.subplots(figsize=(9, 5))
    bars = axis.bar(type_labels, [type_counts[label] for label in type_labels], color="#176B87")
    axis.set_title("Normalized Documents by Source Type")
    axis.set_xlabel("Source type")
    axis.set_ylabel("Number of documents")
    axis.grid(axis="y", alpha=0.25)
    for bar in bars:
        axis.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.5, str(int(bar.get_height())), ha="center")
    figure.tight_layout()
    figure.savefig(source_type_path, dpi=160)
    plt.close(figure)

    category_counts = Counter(
        str(item["source_file"]).split("/", maxsplit=1)[0]
        for item in inventory
    )
    category_labels = sorted(category_counts)
    figure, axis = plt.subplots(figsize=(10, 5))
    bars = axis.bar(category_labels, [category_counts[label] for label in category_labels], color="#4A90A4")
    axis.set_title("Discovered Files by Source Category")
    axis.set_xlabel("Raw-data category")
    axis.set_ylabel("Number of files")
    axis.tick_params(axis="x", rotation=25)
    axis.grid(axis="y", alpha=0.25)
    for bar in bars:
        axis.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.05, str(int(bar.get_height())), ha="center")
    figure.tight_layout()
    figure.savefig(source_category_path, dpi=160)
    plt.close(figure)
    return source_type_path, source_category_path


def run_ingestion(raw_data_dir: Path, processed_data_dir: Path) -> IngestionResult:
    """Run document discovery, loading, validation, and output writing."""
    raw_data_dir = raw_data_dir.resolve()
    processed_data_dir = processed_data_dir.resolve()
    if not raw_data_dir.is_dir():
        raise FileNotFoundError(f"Raw data directory does not exist: {raw_data_dir}")
    processed_data_dir.mkdir(parents=True, exist_ok=True)

    source_files = discover_source_files(raw_data_dir)
    documents, failures, inventory = load_documents(raw_data_dir, source_files)
    validate_documents(documents)

    documents_path = processed_data_dir / "01_loaded_documents.json"
    manifest_path = processed_data_dir / "01_ingestion_manifest.json"
    inventory_path = processed_data_dir / "01_source_inventory.csv"
    failures_path = processed_data_dir / "01_failed_documents.json"
    plots_dir = processed_data_dir / "plots"

    write_json(documents_path, [asdict(document) for document in documents])
    write_json(failures_path, [asdict(failure) for failure in failures])
    write_inventory(inventory_path, inventory)
    source_type_plot_path, source_category_plot_path = generate_plots(
        documents,
        inventory,
        plots_dir,
    )

    source_type_counts = Counter(document.source_type for document in documents)
    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "raw_data_directory": str(raw_data_dir),
        "files_discovered": len(source_files),
        "files_loaded": sum(item["status"] == "loaded" for item in inventory),
        "files_skipped": sum(item["status"] == "skipped" for item in inventory),
        "files_failed": len(failures),
        "documents_created": len(documents),
        "documents_by_source_type": dict(sorted(source_type_counts.items())),
        "output_files": [
            documents_path.name,
            manifest_path.name,
            inventory_path.name,
            failures_path.name,
            f"plots/{source_type_plot_path.name}",
            f"plots/{source_category_plot_path.name}",
        ],
    }
    write_json(manifest_path, manifest)

    return IngestionResult(
        documents_path=documents_path,
        manifest_path=manifest_path,
        inventory_path=inventory_path,
        failures_path=failures_path,
        source_type_plot_path=source_type_plot_path,
        source_category_plot_path=source_category_plot_path,
        documents_created=len(documents),
        files_discovered=len(source_files),
        files_loaded=manifest["files_loaded"],
        files_skipped=manifest["files_skipped"],
        files_failed=len(failures),
    )


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line argument parser."""
    project_root = default_project_root()
    parser = argparse.ArgumentParser(
        description="Load hospital helpdesk sources into normalized JSON records."
    )
    parser.add_argument(
        "--raw-dir",
        type=Path,
        default=project_root / "01_data" / "raw",
        help="Directory containing source documents.",
    )
    parser.add_argument(
        "--processed-dir",
        type=Path,
        default=project_root / "01_data" / "processed",
        help="Directory for ingestion outputs.",
    )
    return parser


def print_result(result: IngestionResult) -> None:
    """Print a concise ingestion summary and output paths."""
    print("Ingestion completed successfully.")
    print(f"Files discovered: {result.files_discovered}")
    print(f"Files loaded: {result.files_loaded}")
    print(f"Files skipped: {result.files_skipped}")
    print(f"Files failed: {result.files_failed}")
    print(f"Normalized documents: {result.documents_created}")
    print("\nOutput files:")
    for path in (
        result.documents_path,
        result.manifest_path,
        result.inventory_path,
        result.failures_path,
        result.source_type_plot_path,
        result.source_category_plot_path,
    ):
        print(f"- {path} ({path.stat().st_size:,} bytes)")


def main() -> None:
    """Run ingestion from command-line arguments."""
    args = build_parser().parse_args()
    result = run_ingestion(args.raw_dir, args.processed_dir)
    print_result(result)


if __name__ == "__main__":
    main()
