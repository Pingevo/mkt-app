"""Load brand reference files from a directory.

All ``.md`` and ``.txt`` files in the brand directory are read and combined
into a single context string that gets injected into every agent's system prompt.
"""

from __future__ import annotations

from pathlib import Path


def load_brand_context(brand_dir: str | Path | None = None) -> str:
    """Read all text files from ``brand_dir`` and return combined context.

    If the directory doesn't exist or is empty, returns an empty string
    (agents will still work, just without brand context).

    Files are sorted alphabetically by filename for consistent ordering.
    """
    if brand_dir is None:
        project_root = Path(__file__).resolve().parent.parent
        brand_dir = project_root / "brand"

    brand_dir = Path(brand_dir)
    if not brand_dir.exists() or not brand_dir.is_dir():
        return ""

    extensions = {".md", ".txt"}
    files = sorted(
        f for f in brand_dir.iterdir() if f.is_file() and f.suffix.lower() in extensions
    )

    if not files:
        return ""

    parts: list[str] = []
    for f in files:
        content = f.read_text(encoding="utf-8").strip()
        if content:
            parts.append(f"### {f.stem.replace('_', ' ').title()}\n\n{content}")

    if not parts:
        return ""

    return "--- ข้อมูลแบรนด์อ้างอิง ---\n\n" + "\n\n---\n\n".join(parts) + "\n\n--- สิ้นสุดข้อมูลแบรนด์ ---"
