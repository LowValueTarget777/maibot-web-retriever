"""Content cleaning helpers for fetched markdown."""

import re

_NOISE_PATTERNS = [
    re.compile(r"^Copyright\s+\d{4}", re.IGNORECASE),
    re.compile(r"^All Rights Reserved", re.IGNORECASE),
    re.compile(r"^TOP$", re.IGNORECASE),
    re.compile(r"^#top$", re.IGNORECASE),
    re.compile(r"^Cookie", re.IGNORECASE),
    re.compile(r"^\s*\w\s*$"),
    re.compile(r"^[|\-_*#~]{3,}$"),
    re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$"),
    re.compile(r"^\[\s*\]$"),
]

_NOISE_LINE_PATTERNS = [
    re.compile(r"^\s*(?:Share|Tweet|Pin)\s*$", re.IGNORECASE),
    re.compile(r"^(?:Read more|View original|Click to view)", re.IGNORECASE),
    re.compile(r"^(?:AD|Sponsored)\s*$", re.IGNORECASE),
]

_CODE_FENCE_MARKERS = {
    "```",
    "```python",
    "```json",
    "```html",
    "```bash",
    "---",
}


def clean_markdown(md: str, min_line_length: int = 5, max_content_chars: int = 6000) -> str:
    """Strip common noise while preserving enough body text for summarization."""
    if not md or not md.strip():
        return ""

    lines = md.split("\n")
    cleaned: list[str] = []
    prev_blank = False
    total_chars = 0

    for line in lines:
        stripped = line.strip()

        if len(stripped) < min_line_length:
            is_structural_line = (
                stripped.startswith("#")
                or stripped.startswith("- ")
                or stripped.startswith("* ")
                or stripped.startswith("|")
                or stripped in _CODE_FENCE_MARKERS
            )
            if not is_structural_line:
                continue

        if _is_noise(stripped):
            continue

        if not stripped:
            if not prev_blank:
                cleaned.append("")
                prev_blank = True
            continue
        prev_blank = False

        cleaned.append(line)
        total_chars += len(line)
        if total_chars > max_content_chars:
            cleaned.append("\n\n[Content truncated]")
            break

    result = "\n".join(cleaned).strip()
    if len(result) < 50 and md.strip():
        return md.strip()[:max_content_chars]

    return result


def _is_noise(line: str) -> bool:
    for pattern in _NOISE_PATTERNS:
        if pattern.match(line):
            return True
    for pattern in _NOISE_LINE_PATTERNS:
        if pattern.match(line):
            return True
    return False
