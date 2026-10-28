from __future__ import annotations

import re


_CODE_BLOCK_PATTERN = re.compile(r"```[^\n]*\n?(.*?)```", re.DOTALL)
_INLINE_CODE_PATTERN = re.compile(r"`([^`\n]+)`")
_HEADING_PATTERN = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_BULLET_PATTERN = re.compile(r"^(\s*)[-*+]\s+(.+)$")
_BLOCKQUOTE_PATTERN = re.compile(r"^\s*>\s?")
_TABLE_SEPARATOR_PATTERN = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$"
)
_BOLD_PATTERN = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_UNDERSCORE_BOLD_PATTERN = re.compile(r"__(.+?)__", re.DOTALL)
_ITALIC_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])\*(?=[^\d\s])([^*\n]+?)(?<=\S)\*(?![A-Za-z0-9])"
)
_LINK_PATTERN = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_HORIZONTAL_RULE_PATTERN = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
_EMOJI_PATTERN = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\u2600-\u27BF"
    "\u2B00-\u2BFF"
    "\uFE0F"
    "\u200D"
    "]"
)
_KEYCAP_EMOJI_PATTERN = re.compile(r"[0-9#*]\uFE0F?\u20E3")
_KAOMOJI_PATTERN = re.compile(
    r"[（(][^()（）\n]{0,16}[´`ω・∀≧≦°皿益∇︿｡ﾟ♡]"
    r"[^()（）\n]{0,16}[）)]"
)


def format_qq_text(text: str) -> str:
    """Convert common Markdown output into readable plain text for QQ."""
    if not text:
        return ""

    code_blocks: list[str] = []
    inline_codes: list[str] = []

    def keep_code_block(match: re.Match[str]) -> str:
        code_blocks.append(match.group(1).strip("\n"))
        return f"\x00CODEBLOCK{len(code_blocks) - 1}\x00"

    def keep_inline_code(match: re.Match[str]) -> str:
        inline_codes.append(match.group(1))
        return f"\x00INLINECODE{len(inline_codes) - 1}\x00"

    value = _CODE_BLOCK_PATTERN.sub(keep_code_block, text)
    value = _INLINE_CODE_PATTERN.sub(keep_inline_code, value)

    lines: list[str] = []
    for line in value.replace("\r\n", "\n").split("\n"):
        if _TABLE_SEPARATOR_PATTERN.match(line):
            continue

        heading = _HEADING_PATTERN.match(line)
        if heading:
            lines.append(f"【{heading.group(1).strip()}】")
            continue

        if _HORIZONTAL_RULE_PATTERN.match(line):
            lines.append("────────")
            continue

        bullet = _BULLET_PATTERN.match(line)
        if bullet:
            lines.append(f"{bullet.group(1)}• {bullet.group(2)}")
            continue

        if line.lstrip().startswith("|") and line.rstrip().endswith("|"):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            lines.append("  |  ".join(cells))
            continue

        lines.append(_BLOCKQUOTE_PATTERN.sub("", line))

    value = "\n".join(lines)
    value = _LINK_PATTERN.sub(r"\1（\2）", value)
    value = _BOLD_PATTERN.sub(r"\1", value)
    value = _UNDERSCORE_BOLD_PATTERN.sub(r"\1", value)
    value = _ITALIC_PATTERN.sub(r"\1", value)
    value = _KEYCAP_EMOJI_PATTERN.sub("", value)
    value = _EMOJI_PATTERN.sub("", value)
    value = _KAOMOJI_PATTERN.sub("", value)
    value = "\n".join(
        re.sub(r"[ \t]+", " ", line).strip() for line in value.split("\n")
    )
    value = re.sub(r"[ \t]+([，。！？；：])", r"\1", value)

    # Keep at most one blank line and remove formatting-only lines.
    value = re.sub(r"\n{3,}", "\n\n", value).strip()

    for index, code in enumerate(code_blocks):
        value = value.replace(f"\x00CODEBLOCK{index}\x00", code)
    for index, code in enumerate(inline_codes):
        value = value.replace(f"\x00INLINECODE{index}\x00", code)
    return value.strip()
