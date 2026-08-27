"""Offline regression tests for PDF text extraction.

Tests the public seam `file_loader.load_file()` against CACGO catalog PDFs.
Does not call OpenRouter or any other paid API.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.file_loader import load_file


def _cacgo_pdf_paths() -> list[Path]:
    data = Path(__file__).parent.parent / "data"
    return sorted(data.glob("CACGO */CACGO Smart Watch Price List- Grace.pdf"))


@pytest.mark.parametrize("pdf_path", _cacgo_pdf_paths()[:5])
def test_cacgo_price_list_preserves_model_number_boundaries(pdf_path: Path) -> None:
    """Table cells (model number vs. spec number) must not be concatenated.

    Regression guard against PyPDF2-style flattening such as:
      '5 K77 | 1. CPU' -> '5 K771. CPU'
    """
    text = load_file(str(pdf_path))

    assert len(text) > 1000, "PDF should produce substantial text"

    # Bad concatenations must not appear
    assert "K721." not in text, "K72 should not be concatenated with '1.'"
    assert "K771." not in text, "K77 should not be concatenated with '1.'"
    assert "K751." not in text, "K75 should not be concatenated with '1.'"
    assert "K731." not in text, "K73 should not be concatenated with '1.'"

    # Original model numbers must still be present
    assert re.search(r"\bK72\b", text), "K72 should appear in output"
    assert re.search(r"\bK77\b", text), "K77 should appear in output"
    assert re.search(r"\bK75\b", text), "K75 should appear in output"
    assert re.search(r"\bK73\b", text), "K73 should appear in output"

    # Section numbering and prices must survive
    assert "1. CPU" in text, "numbered spec lines should be preserved"
    assert "US$15.00" in text, "K77 price should appear in extracted text"
    assert "US$22.50" in text, "K72 price should appear in extracted text"
