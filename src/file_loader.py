"""Load and extract text from various file formats.

Supports:
- Text files (.txt, .md, .csv) — direct read
- PDF files (.pdf) — extract text using PyMuPDF
- Excel files (.xlsx, .xls) — openpyxl
- Word files (.docx) — python-docx (text) + zip media extraction (images)
- Image files (.png, .jpg, .jpeg, .webp) — OCR using pytesseract (requires tesseract installed)

For images, OCR requires tesseract to be installed on the system.
"""

from __future__ import annotations

import csv as _csv
from pathlib import Path
from typing import Literal

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

try:
    from PIL import Image
    import pytesseract
except ImportError:
    Image = None
    pytesseract = None

try:
    import openpyxl
except ImportError:
    openpyxl = None

try:
    import docx
except ImportError:
    docx = None


def load_file(file_path: str | Path) -> str:
    """Load text content from a file, auto-detecting format.

    Supported formats: .txt, .md, .csv, .pdf, .xlsx, .xls, .docx,
                       .png, .jpg, .jpeg, .webp

    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If format is not supported or required dependencies are missing
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    suffix = path.suffix.lower()

    if suffix in {".txt", ".md"}:
        return _load_text(path)
    elif suffix == ".csv":
        return _load_csv(path)
    elif suffix == ".pdf":
        return _load_pdf(path)
    elif suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        return _load_image(path)
    elif suffix in {".xlsx", ".xls"}:
        return _load_xlsx(path)
    elif suffix == ".docx":
        return _load_docx(path)
    else:
        raise ValueError(f"Unsupported file format: {suffix}")


def _load_text(path: Path) -> str:
    """Load plain text file."""
    return path.read_text(encoding="utf-8")


def _load_csv(path: Path) -> str:
    """Load CSV file as text — แปลงเป็น text แบบเดียวกับ xlsx."""
    parts: list[str] = []
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = _csv.reader(f)
        for row in reader:
            cells = [str(c) for c in row if c]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _load_pdf(path: Path) -> str:
    """Extract text from PDF using PyMuPDF."""
    if fitz is None:
        raise ValueError(
            "PyMuPDF is required for PDF support. "
            "Install it with: pip install PyMuPDF"
        )

    text_parts: list[str] = []
    with fitz.open(str(path)) as doc:
        for page in doc:
            text = page.get_text()
            if text:
                text_parts.append(text)

    return "\n\n".join(text_parts)


def _load_image(path: Path) -> str:
    """Extract text from image using OCR (pytesseract)."""
    if Image is None or pytesseract is None:
        raise ValueError(
            "Pillow and pytesseract are required for image OCR. "
            "Install them with: pip install Pillow pytesseract\n"
            "Also install tesseract OCR engine on your system."
        )

    try:
        image = Image.open(path)
        text = pytesseract.image_to_string(image, lang="eng+tha")
        return text
    except pytesseract.TesseractNotFoundError:
        raise ValueError(
            "Tesseract OCR engine not found. "
            "Install it on your system (e.g., 'brew install tesseract' on macOS)."
        )
    except Exception as e:
        raise ValueError(f"Failed to extract text from image: {e}")


def _load_xlsx(path: Path) -> str:
    """Extract text from Excel file using openpyxl."""
    if openpyxl is None:
        raise ValueError(
            "openpyxl is required for Excel support. "
            "Install it with: pip install openpyxl"
        )

    wb = openpyxl.load_workbook(path, data_only=True)
    parts: list[str] = []
    for ws in wb.worksheets:
        parts.append(f"--- Sheet: {ws.title} ---")
        for row in ws.iter_rows(values_only=True):
            cells = [str(c) for c in row if c is not None]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _load_docx(path: Path) -> str:
    """Extract text from Word (.docx) file using python-docx.

    รูปที่ฝังใน docx ดึงแยกที่ ingestion.py (_extract_images_from_docx)
    ที่นี่ดึงแค่ text
    """
    if docx is None:
        raise ValueError(
            "python-docx is required for .docx support. "
            "Install it with: pip install python-docx"
        )

    doc = docx.Document(str(path))
    parts: list[str] = []
    for para in doc.paragraphs:
        text = para.text.strip()
        if text:
            parts.append(text)
    # ดึง text ในตารางด้วย
    for table in doc.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def iter_structured_tables(path: str | Path) -> list[dict]:
    """Return positioned tables with provenance — ``[{"page": int|None,
    "rows": [[cell,...], ...]}]``.

    ``page`` is the 1-based PDF page the table was found on (``None`` for
    formats without pages).  Flattened text loses cell positions, which
    makes a hierarchical spec sheet (``section | feature | value`` with a
    sparse first column) look identical to a comparison table (``spec |
    model A | model B``) — and erases the row/identity-column structure a
    multi-product catalog depends on.  Positioned rows keep both
    distinctions available to deterministic extraction and segmentation.
    Returns ``[]`` for non-tabular formats or on any parse failure —
    tables are a best-effort bonus on top of raw text.
    """
    path = Path(path)
    suffix = path.suffix.lower()
    try:
        if suffix in {".xlsx", ".xls"} and openpyxl is not None:
            wb = openpyxl.load_workbook(path, data_only=True)
            return [
                {"page": None, "rows": [
                    ["" if c is None else str(c) for c in row]
                    for row in ws.iter_rows(values_only=True)
                ]}
                for ws in wb.worksheets
            ]
        if suffix == ".csv":
            with open(path, "r", encoding="utf-8", newline="") as f:
                return [{"page": None, "rows": [
                    [str(c) for c in row] for row in _csv.reader(f)
                ]}]
        if suffix == ".docx" and docx is not None:
            doc = docx.Document(str(path))
            return [
                {"page": None, "rows": [
                    [cell.text.strip() for cell in row.cells]
                    for row in t.rows
                ]}
                for t in doc.tables
            ]
        if suffix == ".pdf" and fitz is not None:
            tables: list[dict] = []
            with fitz.open(str(path)) as doc:
                for page_num, page in enumerate(doc):
                    found = page.find_tables()
                    for t in getattr(found, "tables", []) or []:
                        rows = [
                            ["" if c is None else str(c) for c in row]
                            for row in t.extract()
                        ]
                        if rows:
                            tables.append({
                                "page": page_num + 1,
                                "rows": rows,
                                "row_bboxes": [list(row.bbox) for row in t.rows],
                            })
            return tables
    except Exception:
        return []
    return []


def load_table_rows(path: str | Path) -> list[list[list[str]]]:
    """Return positioned table rows for tabular formats — one list of rows
    per table/sheet, cells as strings with ``""`` for empty cells.

    Flattened text loses cell positions, which makes a hierarchical spec
    sheet (``section | feature | value`` with a sparse first column) look
    identical to a comparison table (``spec | model A | model B``).
    Positioned rows keep that distinction available to deterministic fact
    extraction.  Returns ``[]`` for non-tabular formats or on any parse
    failure — tables are a best-effort bonus on top of raw text.
    """
    return [t["rows"] for t in iter_structured_tables(path)]


def get_supported_formats() -> list[str]:
    """Return list of supported file extensions."""
    formats = [".txt", ".md", ".csv", ".pdf", ".xlsx", ".xls"]
    if docx is not None:
        formats.append(".docx")
    if Image is not None and pytesseract is not None:
        formats.extend([".png", ".jpg", ".jpeg"])
        # webp ต้องเช็ค features เพราะ registered_extensions อาจไม่แสดง
        try:
            from PIL import features
            if features.check("webp"):
                formats.append(".webp")
        except Exception:
            pass
    return formats
