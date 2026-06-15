"""Create retrieval metadata for hospital helpdesk text chunks.

This module is the reusable equivalent of
``13_notebooks/04_create_metadata.ipynb``. It enriches Phase 3 chunks with
document identity, department, content category, page references, provenance,
and safety labels while preserving the original text and metadata.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Sequence


UNKNOWN_VALUE: Final = "general"
DEPARTMENT_KEYWORDS: Final[dict[str, tuple[str, ...]]] = {
    "Emergency Department": ("emergency", "severe chest pain", "immediate danger"),
    "Appointments Desk": ("appointment", "booking", "reschedule", "cancellation", "check-in"),
    "Billing and Insurance": ("billing", "insurance", "claim", "coverage", "authorization", "payment", "estimate"),
    "Medical Records": ("medical record", "records request", "record copy", "amendment"),
    "Patient Relations": ("patient relations", "interpreter", "accessibility", "concern", "compliment", "patient rights"),
    "Portal Support": ("patient portal", "portal support", "password", "activation link", "locked account", "locked_account", "missing_result"),
    "Visitor Services": ("visitor", "visiting hours", "quiet hours"),
    "Facilities": ("parking", "wi-fi", "garage", "main lobby"),
    "Data Governance": ("provenance", "source manifest", "reference_only", "synthetic generation"),
    "Cardiology": ("cardiology", "heart-care"),
    "Dermatology": ("dermatology", "skin-care", "rash"),
    "General Medicine": ("general medicine", "adult primary"),
    "Pediatrics": ("pediatrics", "child", "adolescent"),
    "Radiology": ("radiology", "imaging", "mri"),
    "Laboratory": ("laboratory", "specimen", "test result"),
    "Pharmacy": ("pharmacy", "medication", "dosage", "prescription"),
}
CONTENT_CATEGORY_KEYWORDS: Final[dict[str, tuple[str, ...]]] = {
    "emergency": ("emergency", "severe chest pain", "immediate danger", "call local emergency services"),
    "appointments": ("appointment", "booking", "reschedule", "cancellation", "check-in", "referral"),
    "insurance": ("insurance", "coverage", "claim", "prior authorization", "appeal", "deductible"),
    "billing": ("billing", "cost estimate", "payment plan", "charges", "financial assistance"),
    "medical_records": ("medical record", "records request", "record copy", "amendment"),
    "patient_portal": ("patient portal", "portal support", "password", "activation link", "sign-in", "locked_account", "missing_result"),
    "department_information": ("department_name", "department:", "location:", "services:"),
    "doctor_schedule": ("doctor_name", "doctor:", "start_time", "end_time", "outpatient clinic"),
    "visitors": ("visitor", "visiting hours", "quiet hours"),
    "accessibility": ("interpreter", "wheelchair", "accessibility", "sign-language"),
    "facilities": ("parking", "wi-fi", "garage", "main lobby"),
    "clinical_safety": ("cannot diagnose", "dosage advice", "qualified clinician", "pharmacist"),
    "patient_rights": ("patient rights", "without retaliation", "privacy information"),
    "provenance": ("provenance", "source manifest", "reference_only", "content_status"),
    "data_schema": ("create table", "database schema", "primary key"),
}
SAFETY_KEYWORDS: Final[dict[str, tuple[str, ...]]] = {
    "emergency": ("emergency", "severe chest pain", "feel faint", "immediate danger"),
    "medical_advice": ("diagnose", "treatment", "dosage", "medicine to take", "medication"),
    "privacy": ("protected health information", "privacy", "identity verification", "authorization"),
}


@dataclass(frozen=True)
class MetadataRecord:
    """One metadata record associated with a retrieval chunk."""

    chunk_id: str
    document_id: str
    document_name: str
    document_title: str
    department: str
    department_method: str
    department_confidence: float
    content_category: str
    category_method: str
    category_confidence: float
    source_file: str
    source_type: str
    source_group: str
    page_start: int | None
    page_end: int | None
    page_method: str
    chunk_index: int
    chunk_count: int
    character_start: int
    character_end: int
    safety_labels: list[str]
    synthetic_data: bool
    metadata_version: str


@dataclass(frozen=True)
class MetadataResult:
    """Paths and statistics produced by one metadata run."""

    metadata_path: Path
    enriched_chunks_path: Path
    report_path: Path
    audit_path: Path
    unresolved_path: Path
    coverage_plot_path: Path
    department_plot_path: Path
    input_chunks: int
    metadata_records: int
    unresolved_records: int


def default_project_root() -> Path:
    """Return the project root based on this module's location."""
    return Path(__file__).resolve().parents[1]


def load_chunks(path: Path) -> list[dict[str, Any]]:
    """Load and validate the Phase 3 chunk collection."""
    if not path.is_file():
        raise FileNotFoundError(f"Chunk input does not exist: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Chunk input must be a JSON list.")
    required = {
        "chunk_id", "document_id", "text", "source_file", "source_type",
        "category", "record_index", "chunk_index", "chunk_count",
        "character_start", "character_end", "character_count", "metadata",
    }
    for index, chunk in enumerate(payload, start=1):
        if not isinstance(chunk, dict) or not required.issubset(chunk):
            raise ValueError(f"Chunk {index} does not match the Phase 3 schema.")
    return payload


def humanize_name(value: str) -> str:
    """Convert a file stem or identifier into a readable title."""
    return re.sub(r"\s+", " ", value.replace("_", " ").replace("-", " ")).strip().title()


def document_title(chunk: dict[str, Any]) -> str:
    """Choose an explicit source title when possible, then use the file name."""
    metadata = chunk["metadata"]
    for key in ("question", "topic", "name", "department_name", "service"):
        value = metadata.get(key)
        if value:
            return str(value)
    first_line = str(chunk["text"]).splitlines()[0].strip()
    if chunk["source_type"] in {"pdf", "html"} and 3 <= len(first_line) <= 120:
        return first_line
    return humanize_name(Path(str(chunk["source_file"])).stem)


def keyword_match(
    text: str,
    mapping: dict[str, tuple[str, ...]],
) -> tuple[str, float, list[str]]:
    """Return the label with the most transparent keyword matches."""
    normalized = text.casefold()
    scores: list[tuple[int, str, list[str]]] = []
    for label, keywords in mapping.items():
        matched = [keyword for keyword in keywords if keyword.casefold() in normalized]
        scores.append((len(matched), label, matched))
    best_score, best_label, matched_keywords = max(scores, key=lambda item: (item[0], item[1]))
    if best_score == 0:
        return UNKNOWN_VALUE, 0.0, []
    confidence = min(0.95, 0.55 + 0.10 * (best_score - 1))
    return best_label, round(confidence, 2), matched_keywords


def derive_department(chunk: dict[str, Any]) -> tuple[str, str, float]:
    """Prefer explicit department fields, then use auditable keyword inference."""
    metadata = chunk["metadata"]
    if chunk["source_type"] == "sql":
        return "Data Governance", "source_type_rule", 1.0
    explicit = metadata.get("department") or metadata.get("department_name")
    if explicit:
        return str(explicit), "explicit_field", 1.0
    if metadata.get("table_name") == "departments" and metadata.get("name"):
        return str(metadata["name"]), "department_table_name", 1.0
    label, confidence, _ = keyword_match(
        f"{chunk['source_file']} {chunk['text']}", DEPARTMENT_KEYWORDS
    )
    method = "keyword_inference" if confidence else "default_general"
    return label, method, confidence


def derive_content_category(chunk: dict[str, Any]) -> tuple[str, str, float]:
    """Prefer explicit FAQ/log categories, then infer a retrieval category."""
    metadata = chunk["metadata"]
    if chunk["source_type"] == "sql":
        return "data_schema", "source_type_rule", 1.0
    explicit = metadata.get("category")
    if explicit:
        return str(explicit), "explicit_field", 1.0
    table_name = metadata.get("table_name")
    if table_name == "departments":
        return "department_information", "database_table", 1.0
    if table_name == "doctor_schedule":
        return "doctor_schedule", "database_table", 1.0
    label, confidence, _ = keyword_match(
        f"{chunk['source_file']} {chunk['text']}", CONTENT_CATEGORY_KEYWORDS
    )
    method = "keyword_inference" if confidence else "default_general"
    return label, method, confidence


def derive_safety_labels(chunk: dict[str, Any]) -> list[str]:
    """Attach non-clinical routing labels based on explicit source language."""
    normalized = str(chunk["text"]).casefold()
    return sorted(
        label
        for label, keywords in SAFETY_KEYWORDS.items()
        if any(keyword.casefold() in normalized for keyword in keywords)
    )


def normalized_words(text: str) -> set[str]:
    """Return informative words for page-text overlap matching."""
    return {
        word
        for word in re.findall(r"[a-z0-9]+", text.casefold())
        if len(word) > 2
    }


def extract_pdf_pages(path: Path) -> list[str]:
    """Extract one text string per PDF page with pypdf or pdftotext."""
    try:
        from pypdf import PdfReader
    except ImportError:
        PdfReader = None
    if PdfReader is not None:
        return [page.extract_text() or "" for page in PdfReader(path).pages]
    executable = shutil.which("pdftotext")
    if executable is None:
        return []
    with tempfile.TemporaryDirectory() as temp_dir:
        output_path = Path(temp_dir) / "pages.txt"
        subprocess.run(
            [executable, "-layout", str(path), str(output_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        return output_path.read_text(encoding="utf-8", errors="replace").split("\f")


def build_pdf_page_index(
    chunks: Sequence[dict[str, Any]], raw_data_dir: Path
) -> dict[str, list[set[str]]]:
    """Build reusable page-word sets for PDF sources present in the chunks."""
    index: dict[str, list[set[str]]] = {}
    pdf_sources = sorted(
        {str(chunk["source_file"]) for chunk in chunks if chunk["source_type"] == "pdf"}
    )
    for source_file in pdf_sources:
        pages = extract_pdf_pages(raw_data_dir / source_file)
        index[source_file] = [normalized_words(page) for page in pages if page.strip()]
    return index


def derive_page_range(
    chunk: dict[str, Any], page_index: dict[str, list[set[str]]]
) -> tuple[int | None, int | None, str]:
    """Match PDF chunks to source pages by normalized word overlap."""
    if chunk["source_type"] != "pdf":
        return None, None, "not_applicable"
    pages = page_index.get(str(chunk["source_file"]), [])
    chunk_words = normalized_words(str(chunk["text"]))
    if not pages or not chunk_words:
        return None, None, "unavailable"
    scores = [len(chunk_words & page_words) / len(chunk_words) for page_words in pages]
    matched_pages = [index + 1 for index, score in enumerate(scores) if score >= 0.15]
    if not matched_pages:
        best_page = max(range(len(scores)), key=scores.__getitem__)
        if scores[best_page] == 0:
            return None, None, "unavailable"
        matched_pages = [best_page + 1]
    return min(matched_pages), max(matched_pages), "pdf_text_overlap"


def create_metadata_records(
    chunks: Sequence[dict[str, Any]], raw_data_dir: Path
) -> tuple[list[MetadataRecord], list[dict[str, Any]]]:
    """Derive one metadata record and one audit row per chunk."""
    page_index = build_pdf_page_index(chunks, raw_data_dir)
    records: list[MetadataRecord] = []
    audit: list[dict[str, Any]] = []
    for chunk in chunks:
        department, department_method, department_confidence = derive_department(chunk)
        category, category_method, category_confidence = derive_content_category(chunk)
        page_start, page_end, page_method = derive_page_range(chunk, page_index)
        source_file = str(chunk["source_file"])
        record = MetadataRecord(
            chunk_id=str(chunk["chunk_id"]),
            document_id=str(chunk["document_id"]),
            document_name=Path(source_file).name,
            document_title=document_title(chunk),
            department=department,
            department_method=department_method,
            department_confidence=department_confidence,
            content_category=category,
            category_method=category_method,
            category_confidence=category_confidence,
            source_file=source_file,
            source_type=str(chunk["source_type"]),
            source_group=str(chunk["category"]),
            page_start=page_start,
            page_end=page_end,
            page_method=page_method,
            chunk_index=int(chunk["chunk_index"]),
            chunk_count=int(chunk["chunk_count"]),
            character_start=int(chunk["character_start"]),
            character_end=int(chunk["character_end"]),
            safety_labels=derive_safety_labels(chunk),
            synthetic_data=True,
            metadata_version="1.0",
        )
        records.append(record)
        audit.append(
            {
                "chunk_id": record.chunk_id,
                "source_file": record.source_file,
                "department": record.department,
                "department_method": record.department_method,
                "department_confidence": record.department_confidence,
                "content_category": record.content_category,
                "category_method": record.category_method,
                "category_confidence": record.category_confidence,
                "page_reference": (
                    "" if record.page_start is None else
                    str(record.page_start) if record.page_start == record.page_end else
                    f"{record.page_start}-{record.page_end}"
                ),
                "page_method": record.page_method,
                "safety_labels": ";".join(record.safety_labels),
            }
        )
    return records, audit


def validate_metadata(
    chunks: Sequence[dict[str, Any]], records: Sequence[MetadataRecord]
) -> None:
    """Validate one-to-one chunk coverage and required provenance fields."""
    if len(chunks) != len(records):
        raise RuntimeError("Metadata count does not match the chunk count.")
    chunk_ids = [record.chunk_id for record in records]
    if len(chunk_ids) != len(set(chunk_ids)):
        raise RuntimeError("Duplicate metadata chunk IDs were generated.")
    expected_ids = {str(chunk["chunk_id"]) for chunk in chunks}
    if set(chunk_ids) != expected_ids:
        raise RuntimeError("Metadata does not cover every Phase 3 chunk.")
    if any(not record.source_file or not record.document_name for record in records):
        raise RuntimeError("Required source provenance is missing.")


def write_json(path: Path, payload: Any) -> None:
    """Write readable UTF-8 JSON."""
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def write_audit(path: Path, audit: Sequence[dict[str, Any]]) -> None:
    """Write a human-readable metadata audit CSV."""
    fieldnames = list(audit[0]) if audit else []
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(audit)


def generate_plots(
    records: Sequence[MetadataRecord], plots_dir: Path
) -> tuple[Path, Path]:
    """Generate metadata coverage and department distribution plots."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as error:
        raise RuntimeError("Install matplotlib to generate metadata plots.") from error
    plots_dir.mkdir(parents=True, exist_ok=True)
    coverage_path = plots_dir / "04_metadata_field_coverage.png"
    department_path = plots_dir / "04_chunks_by_department.png"

    total = len(records)
    coverage = {
        "Document name": sum(bool(record.document_name) for record in records),
        "Department": sum(record.department != UNKNOWN_VALUE for record in records),
        "Content category": sum(record.content_category != UNKNOWN_VALUE for record in records),
        "Page (PDF only)": sum(record.page_start is not None for record in records if record.source_type == "pdf"),
        "Safety labels": sum(bool(record.safety_labels) for record in records),
    }
    denominators = {
        "Document name": total,
        "Department": total,
        "Content category": total,
        "Page (PDF only)": sum(record.source_type == "pdf" for record in records),
        "Safety labels": total,
    }
    labels = list(coverage)
    percentages = [100 * coverage[label] / max(1, denominators[label]) for label in labels]
    figure, axis = plt.subplots(figsize=(10, 5))
    bars = axis.bar(labels, percentages, color="#176B87")
    axis.set_ylim(0, 110)
    axis.set_ylabel("Coverage (%)")
    axis.set_title("Metadata Field Coverage")
    axis.tick_params(axis="x", rotation=20)
    for bar, value in zip(bars, percentages):
        axis.text(bar.get_x() + bar.get_width() / 2, value + 2, f"{value:.0f}%", ha="center")
    figure.tight_layout()
    figure.savefig(coverage_path, dpi=160)
    plt.close(figure)

    counts = Counter(record.department for record in records)
    top_counts = counts.most_common(10)
    figure, axis = plt.subplots(figsize=(10, 6))
    axis.barh([label for label, _ in reversed(top_counts)], [value for _, value in reversed(top_counts)], color="#4A90A4")
    axis.set_xlabel("Number of chunks")
    axis.set_title("Top Departments by Chunk Count")
    axis.grid(axis="x", alpha=0.25)
    figure.tight_layout()
    figure.savefig(department_path, dpi=160)
    plt.close(figure)
    return coverage_path, department_path


def run_metadata_creation(
    input_path: Path, raw_data_dir: Path, output_dir: Path
) -> MetadataResult:
    """Run metadata derivation, validation, reporting, plotting, and writing."""
    chunks = load_chunks(input_path.resolve())
    records, audit = create_metadata_records(chunks, raw_data_dir.resolve())
    validate_metadata(chunks, records)

    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = output_dir / "04_metadata.json"
    enriched_path = output_dir / "04_enriched_chunks.json"
    report_path = output_dir / "04_metadata_report.json"
    audit_path = output_dir / "04_metadata_audit.csv"
    unresolved_path = output_dir / "04_unresolved_metadata.json"
    plots_dir = output_dir / "plots"

    record_dicts = [asdict(record) for record in records]
    record_map = {record.chunk_id: asdict(record) for record in records}
    enriched = [
        {**chunk, "retrieval_metadata": record_map[str(chunk["chunk_id"])]}
        for chunk in chunks
    ]
    unresolved = [
        asdict(record)
        for record in records
        if record.department == UNKNOWN_VALUE or record.content_category == UNKNOWN_VALUE
    ]
    write_json(metadata_path, record_dicts)
    write_json(enriched_path, enriched)
    write_json(unresolved_path, unresolved)
    write_audit(audit_path, audit)
    coverage_plot, department_plot = generate_plots(records, plots_dir)

    pdf_records = [record for record in records if record.source_type == "pdf"]
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "input_file": str(input_path.resolve()),
        "input_chunks": len(chunks),
        "metadata_records": len(records),
        "unresolved_records": len(unresolved),
        "department_coverage": round(sum(record.department != UNKNOWN_VALUE for record in records) / len(records), 4),
        "category_coverage": round(sum(record.content_category != UNKNOWN_VALUE for record in records) / len(records), 4),
        "pdf_page_coverage": round(sum(record.page_start is not None for record in pdf_records) / max(1, len(pdf_records)), 4),
        "departments": dict(sorted(Counter(record.department for record in records).items())),
        "content_categories": dict(sorted(Counter(record.content_category for record in records).items())),
        "safety_labels": dict(sorted(Counter(label for record in records for label in record.safety_labels).items())),
        "output_files": [
            metadata_path.name,
            enriched_path.name,
            report_path.name,
            audit_path.name,
            unresolved_path.name,
            f"plots/{coverage_plot.name}",
            f"plots/{department_plot.name}",
        ],
    }
    write_json(report_path, report)
    return MetadataResult(
        metadata_path=metadata_path,
        enriched_chunks_path=enriched_path,
        report_path=report_path,
        audit_path=audit_path,
        unresolved_path=unresolved_path,
        coverage_plot_path=coverage_plot,
        department_plot_path=department_plot,
        input_chunks=len(chunks),
        metadata_records=len(records),
        unresolved_records=len(unresolved),
    )


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line argument parser."""
    project_root = default_project_root()
    processed_dir = project_root / "01_data" / "processed"
    parser = argparse.ArgumentParser(description="Create retrieval metadata for hospital chunks.")
    parser.add_argument("--input", type=Path, default=processed_dir / "03_text_chunks.json")
    parser.add_argument("--raw-dir", type=Path, default=project_root / "01_data" / "raw")
    parser.add_argument("--output-dir", type=Path, default=processed_dir)
    return parser


def print_result(result: MetadataResult) -> None:
    """Print a concise metadata summary and output paths."""
    print("Metadata creation completed successfully.")
    print(f"Input chunks: {result.input_chunks}")
    print(f"Metadata records: {result.metadata_records}")
    print(f"Records requiring review: {result.unresolved_records}")
    print("\nOutput files:")
    for path in (
        result.metadata_path, result.enriched_chunks_path, result.report_path,
        result.audit_path, result.unresolved_path, result.coverage_plot_path,
        result.department_plot_path,
    ):
        print(f"- {path} ({path.stat().st_size:,} bytes)")


def main() -> None:
    """Run metadata creation from command-line arguments."""
    args = build_parser().parse_args()
    result = run_metadata_creation(args.input, args.raw_dir, args.output_dir)
    print_result(result)


if __name__ == "__main__":
    main()
