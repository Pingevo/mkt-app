"""Static contract guard — ONE canonical OpenRouter access gate.

Root cause this prevents:
    scripts/diag_wan27_qual.py and scripts/diag_refvideo_qual.py previously
    called ``https://openrouter.ai/api/v1/videos`` directly via httpx, bypassing
    ``media_gen.generate_video`` and therefore AI Usage accounting.  The Wan 2.7
    qualification spent $0.50 that never reached the AI Usage Hub.

    scripts/m6_judge_runner.py previously called ``/chat/completions`` directly
    via httpx, bypassing ``LLMClient`` and therefore AI Usage accounting.

    src/asset_library.py and src/content_history.py previously called
    ``/embeddings`` directly, performing manual accounting that could diverge
    from the canonical seam.

Contract (single-gate architecture, phase AI-USAGE-SINGLE-GATE-01):
    There is exactly ONE module that owns OpenRouter credential access and
    authenticated transport for paid provider traffic:
        src/openrouter_gateway.py

    No other active application/module/script may:
      - read OPENROUTER_API_KEY for a paid OpenRouter request;
      - construct its own authenticated OpenRouter provider client;
      - send a paid OpenRouter request directly.

    The public API modules (src/llm_client.py, src/media_gen.py) retain their
    public interfaces and endpoint URLs, but delegate credential access and
    authenticated transport to the gate.  Scripts and every other active
    module must not touch the credential, construct authenticated clients, or
    contain paid endpoint URLs.

Scope:
    - Scans active Python source under ``src/`` and ``scripts/``.
    - Excludes historical reports, artifacts, test fixtures, and this test file.
    - Free metadata endpoints (e.g. /api/v1/key, /images/models, /videos/models)
      are not paid-capable and are not checked as paid endpoints.

This is a static source scan — no real provider call, no paid call, no Hub POST.
"""
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Active code directories that can make real paid OpenRouter requests.
ACTIVE_CODE_DIRS = [REPO_ROOT / "src", REPO_ROOT / "scripts"]

# The single gate module — the ONLY active module that may read the credential
# and construct authenticated OpenRouter transport.
GATE_MODULE = Path("src/openrouter_gateway.py")

# Public API modules that own endpoint URLs but delegate transport to the gate.
# They use the gate's client, so their endpoint URLs are expected.
ENDPOINT_URL_OWNERS = {
    Path("src/llm_client.py"),
    Path("src/media_gen.py"),
}

# Full-URL patterns that indicate direct OpenRouter paid-endpoint access in
# actual HTTP call code.  Free metadata endpoints (/images/models,
# /videos/models) are excluded.
ENDPOINT_PATTERNS: dict[str, list[re.Pattern]] = {
    "chat": [
        re.compile(r'https://openrouter\.ai/api/v1/chat/completions'),
    ],
    "embeddings": [
        re.compile(r'https://openrouter\.ai/api/v1/embeddings'),
    ],
    "images": [
        re.compile(r'https://openrouter\.ai/api/v1/images(?!/models)'),
    ],
    "videos": [
        re.compile(r'https://openrouter\.ai/api/v1/videos(?!/models)'),
    ],
}

# Patterns that indicate reading the OpenRouter credential from the environment.
KEY_READ_PATTERNS = [
    re.compile(r'OPENROUTER_API_KEY'),
]

# Patterns that indicate constructing an authenticated OpenRouter client/request.
AUTH_PATTERNS = [
    re.compile(r'Authorization'),
]

# Files/dirs to exclude: historical reports, artifacts, test fixtures, this test.
EXCLUDE_GLOBS = [
    "data/qualification_reports/**",
    "output/**",
    "cache/**",
    "logs/**",
    "tests/**",
]


def _is_excluded(path: Path) -> bool:
    """True if the file is a historical artifact, test fixture, or this guard."""
    rel = path.relative_to(REPO_ROOT).as_posix()
    if path.suffix != ".py":
        return True
    for glob in EXCLUDE_GLOBS:
        if Path(rel).match(glob):
            return True
    return False


def _strip_comments_and_docstrings(text: str) -> str:
    """Remove docstrings and comments to avoid false positives on documentation."""
    cleaned = re.sub(r'""".*?"""', '', text, flags=re.DOTALL)
    cleaned = re.sub(r"'''.*?'''", '', cleaned, flags=re.DOTALL)
    cleaned_lines = []
    for line in cleaned.splitlines():
        if '#' in line:
            line = line[:line.index('#')]
        cleaned_lines.append(line)
    return '\n'.join(cleaned_lines)


def _active_python_files() -> list[Path]:
    """Yield active Python files under src/ and scripts/."""
    files: list[Path] = []
    for d in ACTIVE_CODE_DIRS:
        if not d.exists():
            continue
        for p in d.rglob("*.py"):
            if _is_excluded(p):
                continue
            files.append(p)
    return files


def test_single_gate_module_exists():
    """The canonical gate module must exist."""
    assert (REPO_ROOT / GATE_MODULE).exists(), (
        f"Single OpenRouter gate module {GATE_MODULE} not found. "
        "All paid OpenRouter credential access and authenticated transport "
        "must live in this one module."
    )


def test_only_gate_reads_openrouter_api_key():
    """No active code outside src/openrouter_gateway.py may read
    OPENROUTER_API_KEY from the environment.

    The gate is the single credential owner.  Every other active module
    (including scripts) must obtain the key through the gate, not directly
    from os.environ / get_env / os.getenv.
    """
    violations: list[str] = []
    for path in _active_python_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if Path(rel) == GATE_MODULE:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        cleaned = _strip_comments_and_docstrings(text)
        for i, line in enumerate(cleaned.splitlines(), start=1):
            # Detect actual env reads of the key, not string literals in error
            # messages.  An env read pairs the key name with an env-access
            # function on the same line.
            if any(p.search(line) for p in KEY_READ_PATTERNS):
                env_access = re.search(
                    r'(os\.environ\.get|os\.getenv|get_env|os\.environ\[)'
                    r'\s*\(\s*["\']OPENROUTER_API_KEY',
                    line,
                )
                if env_access:
                    violations.append(f"{rel}:{i}: {line.strip()}")
    assert not violations, (
        "OPENROUTER_API_KEY read outside the single gate. Only "
        "src/openrouter_gateway.py may read the credential for paid provider "
        "traffic. All other modules must obtain it through the gate. "
        "Violations:\n" + "\n".join(violations)
    )


def test_only_gate_constructs_authenticated_client():
    """No active code outside src/openrouter_gateway.py may construct an
    authenticated OpenRouter client (Authorization header).

    The gate is the single authenticated-transport owner.  No other module
    may build an httpx client/request with an Authorization header.
    """
    violations: list[str] = []
    for path in _active_python_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if Path(rel) == GATE_MODULE:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        cleaned = _strip_comments_and_docstrings(text)
        for i, line in enumerate(cleaned.splitlines(), start=1):
            for pat in AUTH_PATTERNS:
                if pat.search(line):
                    violations.append(f"{rel}:{i}: {line.strip()}")
    assert not violations, (
        "Authenticated OpenRouter client construction (Authorization header) "
        "found outside the single gate. Only src/openrouter_gateway.py may "
        "construct authenticated transport. Violations:\n" + "\n".join(violations)
    )


def test_paid_endpoint_urls_only_in_public_api_owners():
    """Paid OpenRouter endpoint URLs may appear only in the public API modules
    (src/llm_client.py, src/media_gen.py) which delegate transport to the gate.

    Scripts and every other active module must not contain paid endpoint URLs.
    """
    violations: list[str] = []
    for path in _active_python_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if Path(rel) in ENDPOINT_URL_OWNERS or Path(rel) == GATE_MODULE:
            continue
        hits = _scan_file_for_endpoints(path)
        for endpoint_type, endpoint_hits in hits.items():
            for lineno, line in endpoint_hits:
                violations.append(
                    f"{rel}:{lineno}: {endpoint_type} endpoint: {line}"
                )
    assert not violations, (
        "Direct OpenRouter paid-endpoint URL found outside the public API "
        "modules. Paid chat/embeddings/image/video requests must go through "
        "src/llm_client.py or src/media_gen.py (which delegate transport to "
        "the gate). Violations:\n" + "\n".join(violations)
    )


def _scan_file_for_endpoints(path: Path) -> dict[str, list[tuple[int, str]]]:
    """Return {endpoint_type: [(line_number, line_text), ...]} for paid
    endpoint URLs found in actual code (not comments or docstrings)."""
    hits: dict[str, list[tuple[int, str]]] = {k: [] for k in ENDPOINT_PATTERNS}
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return hits
    cleaned = _strip_comments_and_docstrings(text)
    for i, line in enumerate(cleaned.splitlines(), start=1):
        for endpoint_type, patterns in ENDPOINT_PATTERNS.items():
            for pat in patterns:
                if pat.search(line):
                    hits[endpoint_type].append((i, line.strip()))
                    break
    return hits


def test_no_accounting_bypass_override_flag():
    """No active code may define an env flag, CLI flag, or operator override
    that permits a real paid OpenRouter request without AI Usage accounting."""
    bypass_patterns = [
        re.compile(r'ALLOW_DIAG_BYPASS'),
        re.compile(r'BYPASS_ACCOUNTING'),
        re.compile(r'SKIP_AI_USAGE'),
        re.compile(r'SKIP_USAGE_LOG'),
        re.compile(r'DISABLE_MEDIA_ACCOUNTING'),
    ]
    violations: list[str] = []
    for path in _active_python_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for pat in bypass_patterns:
            for m in pat.finditer(text):
                lineno = text[:m.start()].count('\n') + 1
                line = text.splitlines()[lineno - 1].strip()
                violations.append(f"{rel}:{lineno}: {line}")

    assert not violations, (
        "Accounting bypass override flag found. No env flag, CLI flag, or "
        "operator override may permit a real paid OpenRouter request without "
        "AI Usage accounting. Violations:\n" + "\n".join(violations)
    )


# Private paid transport helpers that must NOT be imported/called outside the
# gate.  Python "_" naming is only a convention — this test enforces it as a
# repository architecture contract.
PRIVATE_PAID_HELPERS = [
    "_make_client",
    "_video_submit",
    "_video_poll",
    "_video_download",
]


def test_no_active_code_imports_private_gate_helpers():
    """No active code outside src/openrouter_gateway.py may import or reference
    the gate's private paid transport helpers.

    These helpers can initiate authenticated paid OpenRouter requests while
    bypassing gateway-owned accounting.  Future src/ or scripts/ code must
    not be able to add ``from src.openrouter_gateway import _make_client`` or
    ``openrouter_gateway._video_submit(...)`` and still pass the ownership
    contract.

    Scans active Python source under src/ and scripts/, excluding the gate
    module itself, tests, and historical artifacts.
    """
    violations: list[str] = []
    for path in _active_python_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if Path(rel) == GATE_MODULE:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        cleaned = _strip_comments_and_docstrings(text)
        for helper in PRIVATE_PAID_HELPERS:
            # Match: from <...>openrouter_gateway import <...helper...>
            # Match: openrouter_gateway.helper(...)
            # Match: gate.helper(...)  (common alias)
            import_pat = re.compile(
                rf'from\s+\S*openrouter_gateway\s+import\s+[^\n]*\b{re.escape(helper)}\b'
            )
            attr_pat = re.compile(
                rf'\bopenrouter_gateway\.{re.escape(helper)}\b'
            )
            gate_alias_pat = re.compile(
                rf'\bgate\.{re.escape(helper)}\b'
            )
            for i, line in enumerate(cleaned.splitlines(), start=1):
                if import_pat.search(line) or attr_pat.search(line) or gate_alias_pat.search(line):
                    violations.append(f"{rel}:{i}: {line.strip()} ({helper})")
    assert not violations, (
        "Private paid transport helper imported/referenced outside the gate. "
        "Only src/openrouter_gateway.py may use _make_client, _video_submit, "
        "_video_poll, _video_download. Active code must use the public "
        "operations (chat_post, embeddings_post, image_post, video_generate, "
        "etc.) which own the accounting lifecycle. Violations:\n"
        + "\n".join(violations)
    )
