from __future__ import annotations

import io
import json
import math
import re
import secrets
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from docx import Document as WordDocument
from PIL import Image, ImageDraw, ImageFont

from .llm import ChatClient


@dataclass(frozen=True)
class GeneratedArtifact:
    path: Path
    name: str
    kind: str
    summary: str


class ContentGenerationError(RuntimeError):
    pass


async def generate_chart(
    chat_client: ChatClient,
    *,
    model: str,
    prompt: str,
    output_dir: Path,
) -> GeneratedArtifact:
    reply = await chat_client.chat(
        [
            {
                "role": "system",
                "content": (
                    "你是图表数据整理器。严格输出 JSON，不要 Markdown，不要解释。"
                    "只能使用用户提供的信息；缺少数据时返回 error。"
                ),
            },
            {
                "role": "user",
                "content": (
                    '返回 {"title":"标题","kind":"bar|line|pie",'
                    '"unit":"单位","series":[{"label":"项目","value":数字}]}。'
                    "数据点最多 12 个。\n"
                    f"用户要求：{prompt}"
                ),
            },
        ],
        model=model,
        extra_payload={
            "reasoning_effort": "none",
            "response_format": {"type": "json_object"},
        },
    )
    spec = _parse_json(reply)
    if spec.get("error"):
        raise ContentGenerationError(str(spec["error"]))
    title = str(spec.get("title") or "数据图表").strip()[:80]
    unit = str(spec.get("unit") or "").strip()[:20]
    kind = str(spec.get("kind") or "bar").lower()
    series = _valid_series(spec.get("series"))
    if not series:
        raise ContentGenerationError(
            "模型没有从你的要求中得到可绘制的数字数据，请给出项目和数值。"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"chart_{datetime.now():%Y%m%d_%H%M%S_%f}.png"
    image = _render_chart(title, unit, kind, series)
    image.save(path, format="PNG")
    return GeneratedArtifact(
        path=path,
        name=path.name,
        kind="image",
        summary=f"已生成图表：{title}",
    )


async def generate_document(
    chat_client: ChatClient,
    *,
    model: str,
    prompt: str,
    output_dir: Path,
) -> GeneratedArtifact:
    reply = await chat_client.chat(
        [
            {
                "role": "system",
                "content": (
                    "你是文档起草器。严格输出 JSON，不要 Markdown，不要解释。"
                    "保留事实边界，不编造数字、引用或经历。"
                    "不得整段复制歌词、书籍或其他受版权保护作品的正文；"
                    "遇到这类要求时改为作品资料、主题分析、结构分析、来源链接"
                    "和少量必要短引用。"
                ),
            },
            {
                "role": "user",
                "content": (
                    '返回 {"title":"标题","subtitle":"可选副标题",'
                    '"sections":[{"heading":"小节标题","paragraphs":["段落"]}]}。'
                    "内容完整、可直接交付。最多 12 个段落。\n"
                    f"写作要求：{prompt}"
                ),
            },
        ],
        model=model,
        extra_payload={
            "reasoning_effort": "none",
            "response_format": {"type": "json_object"},
        },
    )
    spec = _parse_json(reply)
    title = str(spec.get("title") or "文档").strip()[:120]
    sections = spec.get("sections")
    if not isinstance(sections, list) or not sections:
        raise ContentGenerationError("模型没有生成可用的文档结构。")
    document = WordDocument()
    document.add_heading(title, level=0)
    subtitle = str(spec.get("subtitle") or "").strip()
    if subtitle:
        document.add_paragraph(subtitle)
    for section in sections[:12]:
        if not isinstance(section, dict):
            continue
        heading = str(section.get("heading") or "").strip()
        if heading:
            document.add_heading(heading, level=1)
        paragraphs = section.get("paragraphs")
        if isinstance(paragraphs, list):
            for paragraph in paragraphs[:20]:
                text = str(paragraph).strip()
                if text:
                    document.add_paragraph(text)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"document_{datetime.now():%Y%m%d_%H%M%S_%f}.docx"
    document.save(path)
    return GeneratedArtifact(
        path=path,
        name=f"{_safe_name(title)}.docx",
        kind="file",
        summary=f"已生成 Word 文档：{title}",
    )


async def generate_file(
    chat_client: ChatClient,
    *,
    model: str,
    prompt: str,
    output_dir: Path,
) -> GeneratedArtifact:
    reply = await chat_client.chat(
        [
            {
                "role": "system",
                "content": (
                    "你是文件生成器。严格输出 JSON，不要 Markdown，不要解释。"
                    "不得生成受版权保护作品的完整正文。"
                ),
            },
            {
                "role": "user",
                "content": (
                    '返回 {"title":"文件名","format":"md|csv|txt",'
                    '"content":"完整文件内容"}。'
                    "CSV 必须使用逗号分隔，字段含逗号时用双引号。\n"
                    f"用户要求：{prompt}"
                ),
            },
        ],
        model=model,
        extra_payload={
            "reasoning_effort": "none",
            "response_format": {"type": "json_object"},
        },
    )
    spec = _parse_json(reply)
    content = str(spec.get("content") or "").strip()
    if not content:
        raise ContentGenerationError("模型没有生成文件内容。")
    extension = str(spec.get("format") or "txt").lower()
    if extension not in {"md", "csv", "txt"}:
        extension = "txt"
    title = _safe_name(str(spec.get("title") or "生成文件"))
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{title}_{datetime.now():%Y%m%d_%H%M%S_%f}.{extension}"
    path.write_text(content, encoding="utf-8")
    return GeneratedArtifact(
        path=path,
        name=f"{title}.{extension}",
        kind="file",
        summary=f"已生成文件：{title}.{extension}",
    )


async def generate_code(
    chat_client: ChatClient,
    *,
    model: str,
    prompt: str,
    output_dir: Path,
) -> GeneratedArtifact:
    reply = await chat_client.chat(
        [
            {
                "role": "system",
                "content": (
                    "你是代码文件生成器。严格输出 JSON，不要 Markdown，不要解释。"
                    "生成可直接保存和审阅的完整源代码文件，不要执行代码，"
                    "不要输出省略号占位符。所有文件路径必须是安全相对路径。"
                ),
            },
            {
                "role": "user",
                "content": (
                    '返回 {"project_name":"项目名","summary":"一句话说明",'
                    '"files":[{"path":"main.py","content":"完整代码"}]}。'
                    "最多 12 个文件，单文件不超过 120000 字符。"
                    "根据需求选择正确扩展名。多文件项目一并提供运行说明、"
                    "依赖文件或 README；单文件需求只返回一个文件。\n"
                    f"代码需求：{prompt}"
                ),
            },
        ],
        model=model,
        extra_payload={
            "reasoning_effort": "none",
            "response_format": {"type": "json_object"},
        },
    )
    spec = _parse_json(reply)
    raw_files = spec.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise ContentGenerationError("模型没有生成代码文件。")
    files = _valid_code_files(raw_files)
    if not files:
        raise ContentGenerationError("模型返回的文件路径或代码内容无效。")
    project_name = _safe_name(
        str(spec.get("project_name") or "generated_code")
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    if len(files) == 1:
        relative_path, content = files[0]
        original_name = Path(relative_path).name
        suffix = Path(original_name).suffix
        stem = _safe_name(Path(original_name).stem)
        path = output_dir / f"{stem}_{stamp}{suffix}"
        path.write_text(content, encoding="utf-8")
        return GeneratedArtifact(
            path=path,
            name=original_name,
            kind="code",
            summary=str(
                spec.get("summary") or "代码文件已生成"
            ).strip()[:300],
        )
    run_dir = output_dir / f"code_{stamp}_{secrets.token_hex(3)}"
    for relative_path, content in files:
        target = (run_dir / relative_path).resolve()
        if target.parent != run_dir.resolve() and not target.is_relative_to(
            run_dir.resolve()
        ):
            raise ContentGenerationError("生成的文件路径不安全。")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    summary = str(spec.get("summary") or "代码文件已生成").strip()[:300]
    archive_path = output_dir / f"{project_name}_{stamp}.zip"
    with zipfile.ZipFile(
        archive_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        for relative_path, _ in files:
            source = (run_dir / relative_path).resolve()
            archive.write(source, f"{project_name}/{relative_path}")
    return GeneratedArtifact(
        path=archive_path,
        name=archive_path.name,
        kind="archive",
        summary=f"{summary}（{len(files)} 个文件，已打包 ZIP）",
    )


def _parse_json(reply: str) -> dict[str, Any]:
    value = re.sub(r"^```(?:json)?\s*", "", reply.strip(), flags=re.I)
    value = re.sub(r"\s*```$", "", value)
    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:
        match = re.search(r"\{.*\}", value, re.DOTALL)
        if not match:
            raise ContentGenerationError("模型返回的生成结构无法解析。") from exc
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as nested:
            raise ContentGenerationError(
                "模型返回的生成结构无法解析。"
            ) from nested
    if not isinstance(data, dict):
        raise ContentGenerationError("模型返回的生成结构格式不正确。")
    return data


_CODE_EXTENSIONS = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".html",
    ".htm",
    ".css",
    ".scss",
    ".java",
    ".c",
    ".h",
    ".cpp",
    ".cc",
    ".hpp",
    ".cs",
    ".go",
    ".rs",
    ".php",
    ".rb",
    ".swift",
    ".kt",
    ".kts",
    ".sql",
    ".sh",
    ".ps1",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".xml",
    ".md",
    ".txt",
    ".vue",
    ".svelte",
    ".gradle",
}


def _valid_code_files(value: list[Any]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    total_size = 0
    for item in value[:12]:
        if not isinstance(item, dict):
            continue
        raw_path = str(item.get("path") or "").strip().replace("\\", "/")
        content = item.get("content")
        if not raw_path or not isinstance(content, str):
            continue
        path = (
            raw_path[2:]
            if raw_path.startswith("./")
            else raw_path
        )
        parts = [part for part in path.split("/") if part]
        if not parts or any(part in {".", ".."} for part in parts):
            continue
        if any(
            re.search(r'[<>:"|?*\x00-\x1f]', part)
            for part in parts
        ):
            continue
        if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
            continue
        if len(content) > 120_000 or not content.strip():
            continue
        normalized = "/".join(parts)
        suffix = Path(normalized).suffix.lower()
        filename = parts[-1]
        if suffix not in _CODE_EXTENSIONS and filename not in {
            "Dockerfile",
            "Makefile",
            ".gitignore",
        }:
            continue
        total_size += len(content)
        if total_size > 300_000 or normalized in seen:
            continue
        seen.add(normalized)
        result.append((normalized, content))
    return result


def _valid_series(value: Any) -> list[tuple[str, float]]:
    if not isinstance(value, list):
        return []
    result: list[tuple[str, float]] = []
    for item in value[:12]:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()[:30]
        try:
            number = float(item.get("value"))
        except (TypeError, ValueError):
            continue
        if label and math.isfinite(number):
            result.append((label, number))
    return result


def _render_chart(
    title: str,
    unit: str,
    kind: str,
    series: list[tuple[str, float]],
) -> Image.Image:
    width, height = 1280, 760
    image = Image.new("RGB", (width, height), "#f7f5ef")
    draw = ImageDraw.Draw(image)
    title_font = _font(42, bold=True)
    label_font = _font(24)
    small_font = _font(20)
    draw.text((70, 45), title, fill="#17202a", font=title_font)
    if unit:
        draw.text((width - 90, 58), unit, fill="#65717c", font=small_font)
    colors = [
        "#d14d41",
        "#1f6f8b",
        "#d69e2e",
        "#3f7d5a",
        "#7559a8",
        "#b45b77",
        "#6b7f3f",
        "#3d6c91",
        "#a65f32",
        "#5a6b75",
        "#8f4b3a",
        "#4e7c75",
    ]
    if kind == "pie":
        total = sum(max(0.0, value) for _, value in series)
        if total <= 0:
            kind = "bar"
        else:
            box = (190, 160, 890, 650)
            start = -90.0
            legend_x = 930
            legend_y = 200
            for index, (label, value) in enumerate(series):
                extent = max(0.0, value) / total * 360
                draw.pieslice(
                    box,
                    start=start,
                    end=start + extent,
                    fill=colors[index % len(colors)],
                    outline="#f7f5ef",
                    width=3,
                )
                percentage = max(0.0, value) / total * 100
                draw.rectangle(
                    (legend_x, legend_y + index * 46, legend_x + 18, legend_y + 34),
                    fill=colors[index % len(colors)],
                )
                draw.text(
                    (legend_x + 30, legend_y + index * 46 - 2),
                    f"{label}  {percentage:.1f}%",
                    fill="#26343f",
                    font=small_font,
                )
                start += extent
    elif kind == "line":
        left, top, right, bottom = 110, 150, 1180, 650
        draw.line((left, bottom, right, bottom), fill="#7c8790", width=2)
        draw.line((left, top, left, bottom), fill="#7c8790", width=2)
        maximum = max(abs(value) for _, value in series) or 1.0
        points: list[tuple[float, float]] = []
        step = (right - left) / max(1, len(series) - 1)
        for index, (_, value) in enumerate(series):
            x = left + step * index
            y = bottom - (value / maximum) * (bottom - top) * 0.82
            points.append((x, y))
        if len(points) > 1:
            draw.line(points, fill="#d14d41", width=5, joint="curve")
        for index, ((label, value), (x, y)) in enumerate(zip(series, points)):
            draw.ellipse((x - 7, y - 7, x + 7, y + 7), fill="#d14d41")
            draw.text(
                (x - 20, y - 42),
                _number_label(value),
                fill="#26343f",
                font=small_font,
            )
            draw.text(
                (x - 35, bottom + 18),
                label[:8],
                fill="#26343f",
                font=small_font,
            )
    else:
        left, top, right, bottom = 110, 150, 1180, 650
        draw.line((left, bottom, right, bottom), fill="#7c8790", width=2)
        draw.line((left, top, left, bottom), fill="#7c8790", width=2)
        maximum = max(abs(value) for _, value in series) or 1.0
        slot = (right - left) / len(series)
        bar_width = min(92.0, slot * 0.58)
        for index, (label, value) in enumerate(series):
            center = left + slot * (index + 0.5)
            bar_height = abs(value) / maximum * (bottom - top) * 0.82
            y0 = bottom - bar_height if value >= 0 else bottom
            y1 = bottom if value >= 0 else bottom + bar_height
            draw.rounded_rectangle(
                (
                    center - bar_width / 2,
                    y0,
                    center + bar_width / 2,
                    y1,
                ),
                radius=8,
                fill=colors[index % len(colors)],
            )
            draw.text(
                (center - 28, y0 - 34),
                _number_label(value),
                fill="#26343f",
                font=small_font,
            )
            draw.text(
                (center - min(55, len(label) * 11), bottom + 18),
                label[:10],
                fill="#26343f",
                font=small_font,
            )
    draw.text(
        (70, height - 44),
        "由“给她爱”根据给定数据生成",
        fill="#87919a",
        font=small_font,
    )
    return image


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = [
        Path("C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for path in candidates:
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


def _number_label(value: float) -> str:
    if abs(value - round(value)) < 0.0001:
        return str(int(round(value)))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _safe_name(value: str) -> str:
    normalized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return (normalized or "生成文件")[:80]
