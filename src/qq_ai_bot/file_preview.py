from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any, BinaryIO

from docx import Document as WordDocument
from openpyxl import load_workbook
from pptx import Presentation


_TEXT_SUFFIXES = {
    ".txt",
    ".md",
    ".markdown",
    ".svg",
    ".csv",
    ".tsv",
    ".json",
    ".jsonl",
    ".xml",
    ".html",
    ".htm",
    ".css",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".jsx",
    ".py",
    ".java",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".go",
    ".rs",
    ".sql",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".log",
    ".ipynb",
    ".srt",
    ".vtt",
    ".rtf",
}
_TEXT_MEDIA_TYPES = {
    "application/json",
    "application/ld+json",
    "application/xml",
    "application/x-yaml",
    "application/yaml",
}
_ARCHIVE_SUFFIXES = {
    ".zip",
    ".jar",
    ".apk",
    ".epub",
    ".whl",
    ".cbt",
}
_MAX_EXCERPT_CHARS = 24_000
_MAX_TEXT_READ_BYTES = 256_000


def direct_preview_kind(file_name: str, content_type: str) -> str:
    media_type = content_type.split(";", 1)[0].strip().lower()
    suffix = Path(file_name).suffix.lower()
    if media_type == "image/svg+xml" or suffix in {".svg", ".html", ".htm"}:
        return ""
    if media_type.startswith("image/") or suffix in {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".bmp",
        ".avif",
    }:
        return "image"
    if media_type.startswith("audio/") or suffix in {
        ".mp3",
        ".wav",
        ".ogg",
        ".m4a",
        ".aac",
        ".flac",
    }:
        return "audio"
    if media_type.startswith("video/") or suffix in {
        ".mp4",
        ".webm",
        ".mov",
        ".m4v",
        ".ogv",
    }:
        return "video"
    if media_type == "application/pdf" or suffix == ".pdf":
        return "pdf"
    return ""


def extract_file_preview(
    source: BinaryIO,
    file_name: str,
    content_type: str = "",
) -> dict[str, Any]:
    suffix = Path(file_name).suffix.lower()
    direct_kind = direct_preview_kind(file_name, content_type)
    if direct_kind:
        return {"preview_kind": direct_kind}
    if _is_text_file(suffix, content_type):
        text, truncated = _read_text(source)
        return {
            "preview_kind": "text",
            "excerpt": text,
            "truncated": truncated,
        }
    if suffix == ".docx":
        return _preview_docx(source)
    if suffix in {".xlsx", ".xlsm", ".xltx", ".xltm"}:
        return _preview_xlsx(source)
    if suffix == ".pptx":
        return _preview_pptx(source)
    if suffix in _ARCHIVE_SUFFIXES:
        return _preview_archive(source)
    return {
        "preview_kind": "download",
        "message": "这个格式暂不支持网页内解析，请下载后用对应软件打开。",
    }


def _is_text_file(suffix: str, content_type: str) -> bool:
    media_type = content_type.split(";", 1)[0].strip().lower()
    if suffix in _TEXT_SUFFIXES:
        return True
    if media_type.startswith("text/"):
        return True
    if media_type in _TEXT_MEDIA_TYPES:
        return True
    return (
        not suffix
        and media_type in {"", "application/octet-stream"}
    )


def _read_text(source: BinaryIO) -> tuple[str, bool]:
    source.seek(0)
    raw = source.read(_MAX_TEXT_READ_BYTES + 1)
    truncated = len(raw) > _MAX_TEXT_READ_BYTES
    raw = raw[:_MAX_TEXT_READ_BYTES]
    text = ""
    for encoding in ("utf-8-sig", "gb18030", "utf-16", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text[:_MAX_EXCERPT_CHARS], truncated or len(text) > _MAX_EXCERPT_CHARS


def _clip(lines: list[str]) -> tuple[str, bool]:
    text = "\n".join(line for line in lines if line.strip())
    return text[:_MAX_EXCERPT_CHARS], len(text) > _MAX_EXCERPT_CHARS


def _preview_docx(source: BinaryIO) -> dict[str, Any]:
    source.seek(0)
    document = WordDocument(source)
    lines: list[str] = []
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if text:
            lines.append(text)
    for table_index, table in enumerate(document.tables, start=1):
        lines.append(f"\n[表格 {table_index}]")
        for row in table.rows[:40]:
            values = [cell.text.strip().replace("\n", " ") for cell in row.cells]
            if any(values):
                lines.append(" | ".join(values))
    excerpt, truncated = _clip(lines)
    return {
        "preview_kind": "document",
        "excerpt": excerpt,
        "truncated": truncated,
    }


def _preview_xlsx(source: BinaryIO) -> dict[str, Any]:
    source.seek(0)
    workbook = load_workbook(source, read_only=True, data_only=True)
    lines: list[str] = []
    try:
        for worksheet in workbook.worksheets[:8]:
            lines.append(f"[工作表：{worksheet.title}]")
            for row in worksheet.iter_rows(
                min_row=1,
                max_row=40,
                max_col=12,
                values_only=True,
            ):
                values = [
                    str(value).replace("\n", " ").strip()
                    for value in row
                    if value is not None and str(value).strip()
                ]
                if values:
                    lines.append(" | ".join(values))
                if sum(len(line) for line in lines) > _MAX_EXCERPT_CHARS:
                    break
            if sum(len(line) for line in lines) > _MAX_EXCERPT_CHARS:
                break
    finally:
        workbook.close()
    excerpt, truncated = _clip(lines)
    return {
        "preview_kind": "spreadsheet",
        "excerpt": excerpt,
        "truncated": truncated,
    }


def _preview_pptx(source: BinaryIO) -> dict[str, Any]:
    source.seek(0)
    presentation = Presentation(source)
    lines: list[str] = []
    for slide_index, slide in enumerate(presentation.slides[:24], start=1):
        lines.append(f"[幻灯片 {slide_index}]")
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                text = shape.text.strip()
                if text:
                    lines.append(text)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows[:20]:
                    values = [
                        cell.text.strip().replace("\n", " ")
                        for cell in row.cells
                    ]
                    if any(values):
                        lines.append(" | ".join(values))
        if sum(len(line) for line in lines) > _MAX_EXCERPT_CHARS:
            break
    excerpt, truncated = _clip(lines)
    return {
        "preview_kind": "presentation",
        "excerpt": excerpt,
        "truncated": truncated,
    }


def _preview_archive(source: BinaryIO) -> dict[str, Any]:
    source.seek(0)
    with zipfile.ZipFile(source) as archive:
        entries = [
            info.filename
            for info in archive.infolist()
            if not info.is_dir()
        ]
    limited = entries[:240]
    return {
        "preview_kind": "archive",
        "entries": limited,
        "truncated": len(entries) > len(limited),
    }
