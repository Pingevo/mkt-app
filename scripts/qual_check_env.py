"""Evaluation-only: report presence of env keys without exposing values.

Prints only "SET" or "(empty)" per key. Never prints secret values.
Loads .env into os.environ if present, but does not echo any value.
"""
import os
from pathlib import Path

def _load_env_silent(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

_load_env_silent()

KEYS = [
    "OPENROUTER_API_KEY",
    "OPENROUTER_API_KEY_FREE",
    "GOOGLE_API_KEY",
    "GOOGLE_GENAI_API_KEY",
    "TENSORLANCE_API_KEY",
    "MEDIA_API_KEY",
    "BFL_API_KEY",
    "RUNWAYML_API_KEY",
    "MINIMAX_API_KEY",
    "VEO_API_KEY",
    "IMAGEN_API_KEY",
    "HUB_API_KEY",
    "AI_USAGE_HUB_URL",
    "AI_USAGE_HUB_TOKEN",
    "MONGO_PASSWORD",
    "GITHUB_TOKEN",
]

for k in KEYS:
    present = bool(os.environ.get(k))
    print(f"{k}: {'SET' if present else '(empty)'}")
