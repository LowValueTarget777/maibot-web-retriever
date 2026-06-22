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


# 疑似「提示词注入」行模式（中英）：命中即整行删除，让恶意指令到不了 AI 面前。
# 设计原则：宁可收紧也别误删正常内容——「system prompt」「你现在是」这类词必须搭配
# 攻击动作/角色词才命中，避免误删正常科技文章或中文新闻句。
_INJECTION_PATTERNS = [
    # ignore/disregard/forget/override + (可选填充词) + previous/above/all + instructions
    re.compile(r"\b(?:ignore|disregard|forget|override)\b(?:\s+\w+){0,4}\s+(?:previous|prior|above|earlier|preceding|all|these|those|the)\s+(?:instruction|prompt|message|rule|direction|command|context)", re.IGNORECASE),
    # reveal/show/leak/print/repeat/output + system prompt / your instructions（需攻击动作）
    re.compile(r"\b(?:reveal|show|leak|print|repeat|output|tell\s+me|give\s+me)\b.{0,25}(?:system\s+prompt|your\s+(?:system\s+)?(?:instruction|prompt|rule)s?)", re.IGNORECASE),
    # 标签/角色头式：行首的 system/assistant/user 冒号，或 <system> 类标签
    re.compile(r"(?:^|\n)\s*(?:system|assistant|user)\s*(?:prompt)?\s*[:：]", re.IGNORECASE),
    re.compile(r"</?\s*(?:system|assistant|user|im_start|im_end)\s*[>|]", re.IGNORECASE),
    # you are now (a) <角色词>——必须接角色/越狱词，避免误删 "you are now reading…"
    re.compile(r"\byou\s+are\s+now\s+(?:a |an |my |the )?(?:assistant|ai|model|bot|dan|developer|admin|root|jailbroken|unrestricted|free|in\s+developer\s+mode)\b", re.IGNORECASE),
    re.compile(r"\b(?:act|behave|respond|pretend)\s+(?:as|to\s+be)\s+(?:if\s+you\s+are\s+)?(?:a\s+)?(?:dan|developer\s+mode|jailbroken|unrestricted)", re.IGNORECASE),
    # 中文：忽略/无视 … 指令/设定/规则（用强注入词，避开「提示音」之类误伤）
    re.compile(r"(?:忽略|无视|忽视|不要理会|别理会|不要遵守|无需遵守|不用管)[^\n]{0,12}(?:指令|设定|规则|限制|约束|系统提示|提示词)"),
    # 中文：你(现在)是/扮演 + <角色词>——必须接角色词
    re.compile(r"你(?:现在)?(?:是|就是|要扮演|将扮演|需要扮演|得扮演)[^\n]{0,10}(?:助手|AI|机器人|黑客|管理员|开发者|不受限|越狱|DAN|超级AI|大模型)", re.IGNORECASE),
    re.compile(r"假装你是|忘记你(?:之前|原来|以前)的(?:身份|设定|指令|角色|规则)"),
    # 中文：重复/输出 系统提示词；新指令/新系统提示 标签头
    re.compile(r"(?:重复|输出|打印|说出|告诉我)(?:你的)?(?:系统)?(?:提示词|系统提示|系统指令)"),
    re.compile(r"(?:^|\n)\s*(?:新指令|新系统提示|新角色|系统指令)\s*[:：]"),
]


def redact_injection(text: str, extra_keywords=None) -> tuple[str, int]:
    """删除疑似提示词注入的整行，返回 (清洗后文本, 删除行数)。命中才动手。
    extra_keywords：用户自定义触发词，行内含任一即视为注入。"""
    if not text:
        return text, 0
    extra = [k.strip() for k in (extra_keywords or []) if k and str(k).strip()]
    out: list[str] = []
    removed = 0
    for line in text.split("\n"):
        if any(p.search(line) for p in _INJECTION_PATTERNS) or any(k in line for k in extra):
            removed += 1
            continue
        out.append(line)
    return "\n".join(out), removed
