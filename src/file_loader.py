"""Load and extract text from various file formats.

Supports:
- Text files (.txt, .md) — direct read
- PDF files (.pdf) — extract text using PyPDF2
- Image files (.png, .jpg, .jpeg) — OCR using pytesseract (requires tesseract installed)

For images, OCR requires tesseract to be installed on the system.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

try:
    import PyPDF2
except ImportError:
    PyPDF2 = None

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


def load_file(file_path: str | Path) -> str:
    """Load text content from a file, auto-detecting format.

    Supported formats: .txt, .md, .pdf, .png, .jpg, .jpeg

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
    elif suffix == ".pdf":
        return _load_pdf(path)
    elif suffix in {".png", ".jpg", ".jpeg"}:
        return _load_image(path)
    elif suffix in {".xlsx", ".xls"}:
        return _load_xlsx(path)
    else:
        raise ValueError(f"Unsupported file format: {suffix}")


def _load_text(path: Path) -> str:
    """Load plain text file."""
    return path.read_text(encoding="utf-8")


def _load_pdf(path: Path) -> str:
    """Extract text from PDF using PyPDF2."""
    if PyPDF2 is None:
        raise ValueError(
            "PyPDF2 is required for PDF support. "
            "Install it with: pip install PyPDF2"
        )

    text_parts: list[str] = []
    with open(path, "rb") as f:
        reader = PyPDF2.PdfReader(f)
        for page in reader.pages:
            text = page.extract_text()
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


def get_supported_formats() -> list[str]:
    """Return list of supported file extensions."""
    formats = [".txt", ".md", ".pdf", ".xlsx", ".xls"]
    if Image is not None and pytesseract is not None:
        formats.extend([".png", ".jpg", ".jpeg"])
    return formats
