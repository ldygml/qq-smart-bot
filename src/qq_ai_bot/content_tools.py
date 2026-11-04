from __future__ import annotations

import ast
import io
import json
import math
import re
import secrets
import tomllib
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


_RESEARCH_MARKERS = (
    "歌词",
    "歌曲",
    "专辑",
    "资料",
    "背景",
    "来源",
    "论文",
    "研究",
    "历史",
    "新闻",
    "政策",
    "人物",
    "数据",
    "事实",
    "比赛",
    "最新",
    "联网",
    "搜索",
    "查证",
    "核实",
)


async def _research_context(
    chat_client: ChatClient,
    *,
    model: str,
    prompt: str,
    required: bool = False,
) -> str:
    if not required and not any(
        marker in prompt for marker in _RESEARCH_MARKERS
    ):
        return ""
    try:
        reply = await chat_client.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "你是资料核查助手。只整理可核实的事实和来源，"
                        "明确区分已确认、存在争议和无法确认的信息。"
                        "不得输出完整歌词、书籍正文或其他受版权保护作品的大段原文。"
                        "不要编造链接、日期、数字、奖项或引语。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        '只返回 JSON：{"facts":[{"claim":"可核实的事实",'
                        '"source_url":"https://...","quote":"来源中的必要短句"}],'
                        '"uncertain":["无法确认的信息"]}。'
                        "每条 fact 必须有真实来源链接；不得编造链接、日期、数字、"
                        "奖项、销量或采访。歌词任务只提取作品信息、发行资料、"
                        "主题和结构，不得输出歌词正文。\n"
                        f"任务：{prompt}"
                    ),
                },
            ],
            model=model,
            extra_payload={
                "enable_search": True,
                "search_options": {
                    "forced_search": True,
                    "enable_source": True,
                },
            },
        )
    except Exception as exc:
        if required:
            raise ContentGenerationError(
                "联网检索失败，已停止生成，避免发送没有来源核对的文档。"
            ) from exc
        return ""
    try:
        data = _parse_json(reply)
    except ContentGenerationError as exc:
        if required and "https://" not in reply and "http://" not in reply:
            raise ContentGenerationError(
                "联网检索没有返回可核实的来源，已停止生成。"
            ) from exc
        return reply.strip()[:16_000]
    facts = data.get("facts")
    lines: list[str] = []
    if isinstance(facts, list):
        for item in facts[:24]:
            if not isinstance(item, dict):
                continue
            claim = str(item.get("claim") or "").strip()[:500]
            url = str(item.get("source_url") or "").strip()
            quote = str(item.get("quote") or "").strip()[:160]
            if not claim or not url.startswith(("https://", "http://")):
                continue
            lines.append(f"- 事实：{claim}")
            if quote:
                lines.append(f"  来源短句：{quote}")
            lines.append(f"  来源：{url}")
    uncertain = data.get("uncertain")
    if isinstance(uncertain, list):
        for item in uncertain[:12]:
            value = str(item).strip()
            if value:
                lines.append(f"- 未确认：{value}")
    context = "\n".join(lines)[:16_000]
    if required and not context:
        raise ContentGenerationError(
            "联网检索没有返回可核实的事实，已停止生成。"
        )
    return context


async def _audit_document(
    chat_client: ChatClient,
    *,
    model: str,
    prompt: str,
    research: str,
    draft: dict[str, Any],
    lyrics_requested: bool,
) -> dict[str, Any]:
    reply = await chat_client.chat(
        [
            {
                "role": "system",
                "content": (
                    "你是严格的事实审校器。只输出修正后的 JSON，不要解释。"
                    "不得新增草稿和研究材料之外的任何事实。凡是没有被研究材料"
                    "直接支持的具体日期、数字、奖项、销量、采访、引语、因果关系"
                    "或来源，必须删除；无法确认但必须保留的内容标为“未核实”。"
                    "不得输出完整歌词或其他受版权保护作品的大段正文。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "任务："
                    f"{prompt}\n\n"
                    "研究材料：\n"
                    f"{research}\n\n"
                    "待审校 JSON：\n"
                    + json.dumps(draft, ensure_ascii=False)
                    + "\n\n保持 title、subtitle、sections、sources 结构。"
                    "sources 只保留研究材料中真实出现的链接。"
                    + (
                        "\n这是歌词任务：删除所有歌词正文和可连续演唱的分段，"
                        "只保留作品资料、主题、结构和来源。"
                        if lyrics_requested
                        else ""
                    )
                ),
            },
        ],
        model=model,
        extra_payload={
            "reasoning_effort": "none",
            "response_format": {"type": "json_object"},
        },
    )
    return _parse_json(reply)


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
        name=_canonical_file_name(title, "png", "数据图表"),
        kind="image",
        summary=f"已生成图表：{title}",
    )


async def generate_document(
    chat_client: ChatClient,
    *,
    model: str,
    prompt: str,
    output_dir: Path,
    force_research: bool = False,
) -> GeneratedArtifact:
    research = await _research_context(
        chat_client,
        model=model,
        prompt=prompt,
        required=force_research,
    )
    lyrics_requested = "歌词" in prompt and any(
        marker in prompt for marker in ("完整", "全文", "收集", "整理", "生成")
    )
    reply = await chat_client.chat(
        [
            {
                "role": "system",
                "content": (
                    "你是文档起草器。严格输出 JSON，不要 Markdown，不要解释。"
                    "保留事实边界，不编造数字、引用或经历。"
                    "不得整段复制歌词、书籍或其他受版权保护作品的正文；"
                    "遇到这类要求时改为作品资料、主题分析、结构分析、来源链接"
                    "和少量必要短引用。所有具体日期、数字、奖项、销量、引语和"
                    "来源都必须有材料支持；没有依据就写“未核实”或省略。"
                ),
            },
            {
                "role": "user",
                "content": (
                    '返回 {"title":"标题","subtitle":"可选副标题",'
                    '"sections":[{"heading":"小节标题","paragraphs":["段落"]}],'
                    '"sources":["https://..."]}。'
                    "内容准确、完整、可直接交付。最多 16 个段落。"
                    "每个章节必须有明确主题，不能把 JSON 格式说明、生成过程、"
                    "免责声明或“以下是文档”写进正文。"
                    "不得声称已经打开来源、交叉验证或拥有官方资料，除非材料明确支持。"
                    + (
                        "这是歌词相关任务：绝对不要输出歌词正文、分段歌词或"
                        "可连续演唱的歌词内容；只写歌曲基本信息、创作与发行资料、"
                        "主题、结构、语言特点和可核验来源。"
                        if lyrics_requested
                        else ""
                    )
                    + "\n"
                    f"写作要求：{prompt}"
                    + (
                        "\n\n检索核对材料（只作为事实依据，不能照抄无关内容）：\n"
                        + research
                        if research
                        else "\n\n没有取得联网核对材料。"
                        "所有无法从用户要求直接确定的事实都必须省略或标为未核实。"
                    )
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
    if research:
        spec = await _audit_document(
            chat_client,
            model=model,
            prompt=prompt,
            research=research,
            draft=spec,
            lyrics_requested=lyrics_requested,
        )
    title = str(spec.get("title") or "文档").strip()[:120]
    sections = spec.get("sections")
    if not isinstance(sections, list) or not sections:
        raise ContentGenerationError("模型没有生成可用的文档结构。")
    sections = _sanitize_document_sections(
        sections,
        lyrics_requested=lyrics_requested,
    )
    if not sections:
        raise ContentGenerationError(
            "生成内容没有通过质量检查，请把要求写得更具体后重试。"
        )
    document = WordDocument()
    document.add_heading(_document_title(title), level=0)
    subtitle = str(spec.get("subtitle") or "").strip()
    if subtitle:
        document.add_paragraph(subtitle)
    for section in sections[:16]:
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
    sources = _verified_sources(spec.get("sources"), research)
    if sources:
        document.add_heading("参考资料", level=1)
        for source in sources[:8]:
            document.add_paragraph(source)
    output_dir.mkdir(parents=True, exist_ok=True)
    clean_name = _canonical_file_name(title, "docx", "文档")
    stem = Path(clean_name).stem
    path = output_dir / (
        f"{stem}_{datetime.now():%Y%m%d_%H%M%S_%f}.docx"
    )
    document.save(path)
    return GeneratedArtifact(
        path=path,
        name=clean_name,
        kind="file",
        summary=f"已生成 Word 文档：{Path(clean_name).stem}",
    )


async def generate_file(
    chat_client: ChatClient,
    *,
    model: str,
    prompt: str,
    output_dir: Path,
    force_research: bool = False,
) -> GeneratedArtifact:
    research = await _research_context(
        chat_client,
        model=model,
        prompt=prompt,
        required=force_research,
    )
    reply = await chat_client.chat(
        [
            {
                "role": "system",
                "content": (
                    "你是文件生成器。严格输出 JSON，不要 Markdown，不要解释。"
                    "不得生成受版权保护作品的完整正文。事实性内容必须有依据，"
                    "不能编造日期、数字、来源或引语。"
                ),
            },
            {
                "role": "user",
                "content": (
                    '返回 {"title":"文件名","format":"md|csv|txt",'
                    '"content":"完整文件内容","sources":["https://..."]}。'
                    "CSV 必须使用逗号分隔，字段含逗号时用双引号。\n"
                    f"用户要求：{prompt}"
                    + (
                        "\n\n检索核对材料：\n" + research
                        if research
                        else ""
                    )
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
    title = str(spec.get("title") or "生成文件")
    extension = _infer_file_extension(
        title,
        str(spec.get("format") or ""),
        content,
    )
    sources = _verified_sources(spec.get("sources"), research)
    if extension == "md" and sources:
        content = content.rstrip() + "\n\n## 参考资料\n\n" + "\n".join(
            f"- {source}" for source in sources[:8]
        )
    clean_name = _canonical_file_name(title, extension, "生成文件")
    stem = Path(clean_name).stem
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / (
        f"{_safe_name(stem)}_{datetime.now():%Y%m%d_%H%M%S_%f}.{extension}"
    )
    path.write_text(content, encoding="utf-8")
    return GeneratedArtifact(
        path=path,
        name=clean_name,
        kind="file",
        summary=f"已生成文件：{clean_name}",
    )


async def _code_chat_with_fallback(
    chat_client: ChatClient,
    *,
    messages: list[dict[str, Any]],
    model: str,
    fallback_model: str | None,
    extra_payload: dict[str, Any],
) -> str:
    try:
        return await chat_client.chat(
            messages,
            model=model,
            extra_payload=extra_payload,
        )
    except Exception as first_error:
        fallback = (fallback_model or "").strip()
        if not fallback or fallback == model:
            raise ContentGenerationError(
                "代码模型请求失败或超时。已停止发送不完整文件，请稍后重试。"
            ) from first_error
        fallback_messages = [
            *messages,
            {
                "role": "user",
                "content": (
                    "上一个模型请求超时。请使用备用模型快速生成完整文件，"
                    "优先保证代码可运行和 JSON 结构完整。"
                ),
            },
        ]
        try:
            return await chat_client.chat(
                fallback_messages,
                model=fallback,
                extra_payload=extra_payload,
            )
        except Exception as second_error:
            raise ContentGenerationError(
                "代码主模型和备用模型都未能完成请求。"
                "请稍后重试或降低多文件规模。"
            ) from second_error


async def generate_code(
    chat_client: ChatClient,
    *,
    model: str,
    fallback_model: str | None = None,
    prompt: str,
    output_dir: Path,
) -> GeneratedArtifact:
    request_messages = [
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
        ]
    reply = await _code_chat_with_fallback(
        chat_client,
        messages=request_messages,
        model=model,
        fallback_model=fallback_model,
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
    syntax_errors = _code_syntax_errors(files)
    if syntax_errors:
        repair_messages = [
                {
                    "role": "system",
                    "content": (
                        "你是代码修复器。严格输出 JSON，不要 Markdown，不要解释。"
                        "修复给定文件中的语法错误，保持原始需求，不得省略功能。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "原始需求：\n"
                        f"{prompt}\n\n"
                        "检测到的错误：\n"
                        + "\n".join(syntax_errors[:20])
                        + "\n\n原始文件：\n"
                        + json.dumps(
                            [
                                {"path": path, "content": content}
                                for path, content in files
                            ],
                            ensure_ascii=False,
                        )
                        + "\n\n返回与之前相同的 files JSON 结构，并修复全部错误。"
                    ),
                },
            ]
        repair_reply = await _code_chat_with_fallback(
            chat_client,
            messages=repair_messages,
            model=model,
            fallback_model=fallback_model,
            extra_payload={
                "reasoning_effort": "none",
                "response_format": {"type": "json_object"},
            },
        )
        repaired = _parse_json(repair_reply)
        repaired_files = _valid_code_files(
            repaired.get("files")
            if isinstance(repaired.get("files"), list)
            else []
        )
        if repaired_files:
            remaining_errors = _code_syntax_errors(repaired_files)
            if not remaining_errors:
                files = repaired_files
            else:
                raise ContentGenerationError(
                    "代码语法检查未通过：" + "；".join(remaining_errors[:3])
                )
        else:
            raise ContentGenerationError(
                "代码语法检查未通过，未能生成安全可发送的文件。"
            )
    project_stem = _safe_name(
        _strip_known_suffix(
            str(spec.get("project_name") or "generated_code")
        )
        or "generated_code"
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
    archive_path = output_dir / f"{project_stem}_{stamp}.zip"
    with zipfile.ZipFile(
        archive_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        for relative_path, _ in files:
            source = (run_dir / relative_path).resolve()
            archive.write(source, f"{project_stem}/{relative_path}")
    return GeneratedArtifact(
        path=archive_path,
        name=f"{project_stem}.zip",
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
        content = _strip_code_fence(content)
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


def _strip_code_fence(content: str) -> str:
    value = content.strip()
    match = re.match(
        r"^```[A-Za-z0-9_+.-]*\s*\n(.*)\n```$",
        value,
        re.DOTALL,
    )
    return match.group(1).strip() + "\n" if match else content


def _code_syntax_errors(files: list[tuple[str, str]]) -> list[str]:
    errors: list[str] = []
    for path, content in files:
        suffix = Path(path).suffix.lower()
        try:
            if suffix == ".py":
                ast.parse(content, filename=path)
            elif suffix == ".json":
                json.loads(content)
            elif suffix == ".toml":
                tomllib.loads(content)
        except (SyntaxError, ValueError, TypeError) as exc:
            errors.append(f"{path}: {exc}")
    return errors


_OUTPUT_EXTENSIONS = {
    "md",
    "markdown",
    "txt",
    "csv",
    "docx",
    "py",
    "js",
    "ts",
    "html",
    "css",
    "java",
    "c",
    "h",
    "cpp",
    "cs",
    "go",
    "rs",
    "php",
    "rb",
    "swift",
    "kt",
    "sql",
    "sh",
    "ps1",
    "json",
    "yaml",
    "yml",
    "toml",
}


def _strip_known_suffix(value: str) -> str:
    stem = value.strip()
    changed = True
    while changed:
        changed = False
        suffix = Path(stem).suffix.lower().lstrip(".")
        if suffix in _OUTPUT_EXTENSIONS:
            stem = stem[: -(len(suffix) + 1)].rstrip(" ._-")
            changed = True
    return stem


def _canonical_file_name(
    title: str,
    extension: str,
    fallback: str,
) -> str:
    clean_extension = extension.lower().lstrip(".")
    stem = _safe_name(_strip_known_suffix(title) or fallback)
    return f"{stem}.{clean_extension}"


def _infer_file_extension(
    title: str,
    requested: str,
    content: str,
) -> str:
    title_suffix = Path(title.strip()).suffix.lower().lstrip(".")
    normalized = requested.lower().strip().lstrip(".")
    if normalized in _OUTPUT_EXTENSIONS:
        if (
            normalized == "txt"
            and title_suffix in _OUTPUT_EXTENSIONS
            and title_suffix not in {"txt", "md", "markdown", "csv"}
        ):
            return title_suffix
        return normalized
    if title_suffix in _OUTPUT_EXTENSIONS:
        return title_suffix
    stripped = content.lstrip()
    if stripped.startswith("#include"):
        return "cpp"
    if stripped.startswith("<?php"):
        return "php"
    if stripped.startswith("<!DOCTYPE html") or stripped.startswith("<!doctype html"):
        return "html"
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            json.loads(stripped)
            return "json"
        except json.JSONDecodeError:
            pass
    if stripped.startswith("#!") and "python" in stripped.splitlines()[0]:
        return "py"
    return "txt"


def _document_title(value: str) -> str:
    return _safe_name(_strip_known_suffix(value) or "文档")


def _sanitize_document_sections(
    sections: list[Any],
    *,
    lyrics_requested: bool,
) -> list[dict[str, Any]]:
    prohibited = (
        "json",
        "格式说明",
        "交付备注",
        "生成过程",
        "免责声明",
        "以下是文档",
    )
    lyric_headings = (
        "歌词正文",
        "完整歌词",
        "歌词全文",
        "主歌",
        "副歌",
        "桥段",
        "尾声",
    )
    cleaned: list[dict[str, Any]] = []
    seen_paragraphs: set[str] = set()
    for section in sections[:16]:
        if not isinstance(section, dict):
            continue
        heading = str(section.get("heading") or "").strip()[:120]
        paragraphs = section.get("paragraphs")
        if not isinstance(paragraphs, list):
            continue
        text_lines = [
            str(paragraph).strip()
            for paragraph in paragraphs[:30]
            if str(paragraph).strip()
        ]
        if not text_lines:
            continue
        lowered_heading = heading.lower()
        if any(marker in lowered_heading for marker in prohibited):
            continue
        if lyrics_requested and any(
            marker in heading for marker in lyric_headings
        ):
            continue
        if lyrics_requested and _looks_like_lyrics(text_lines):
            continue
        if any(
            line.startswith(("返回 {", "只返回 JSON", "JSON 结构"))
            or '"sections"' in line
            for line in text_lines
        ):
            continue
        unique_lines: list[str] = []
        for line in text_lines:
            normalized = re.sub(r"\s+", "", line).strip("。！？；;")
            if len(normalized) >= 20 and normalized in seen_paragraphs:
                continue
            if len(normalized) >= 20:
                seen_paragraphs.add(normalized)
            unique_lines.append(line)
        if not unique_lines or all(
            line in {"未核实", "暂无", "待确认"}
            for line in unique_lines
        ):
            continue
        cleaned.append(
            {
                "heading": heading,
                "paragraphs": unique_lines,
            }
        )
    return cleaned


def _looks_like_lyrics(lines: list[str]) -> bool:
    short_lines = sum(
        1
        for line in lines
        if 2 <= len(line) <= 60
        and line[-1:] not in "。！？!?：:；;"
    )
    return len(lines) >= 8 and short_lines >= max(6, len(lines) * 0.7)


def _verified_sources(value: Any, research: str) -> list[str]:
    if not research:
        return []
    available = list(
        dict.fromkeys(
            match.rstrip(".,;:!?，。；：！？)")
            for match in re.findall(r"https?://[^\s<>]+", research)
        )
    )
    result: list[str] = []
    raw_sources = value if isinstance(value, list) else []
    for item in raw_sources[:8]:
        if not isinstance(item, str):
            continue
        source = item.strip()
        if not source.startswith(("https://", "http://")):
            continue
        if source not in available or source in result:
            continue
        result.append(source)
    return result or available[:4]


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
