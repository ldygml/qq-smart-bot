from __future__ import annotations

import asyncio
import base64
import binascii
import csv
import io
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urlparse

import httpx
from docx import Document as WordDocument
from openpyxl import load_workbook
from pypdf import PdfReader
import pypdfium2 as pdfium
from pptx import Presentation

from .onebot import FileSource


class FileParserProtocol(Protocol):
    async def load(
        self,
        source: FileSource,
        resolver: FileResolverProtocol,
    ) -> ParsedDocument: ...

    async def close(self) -> None: ...


class FileResolverProtocol(Protocol):
    async def get_file(self, *, source: FileSource) -> dict[str, Any]: ...


class FileParseError(RuntimeError):
    pass


@dataclass(frozen=True)
class DocumentSection:
    location: str
    content: str


@dataclass(frozen=True)
class ParsedDocument:
    name: str
    text: str
    detail: str = ""
    sections: tuple[DocumentSection, ...] = ()


TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".json",
    ".log",
    ".py",
    ".js",
    ".ts",
    ".java",
    ".c",
    ".cpp",
    ".h",
    ".html",
    ".htm",
    ".xml",
    ".yaml",
    ".yml",
    ".toml",
}


class DocumentLoader:
    def __init__(
        self,
        *,
        timeout_seconds: float = 120.0,
        ocr_client: Any = None,
        vision_model: str = "qwen3-vl-plus",
        ocr_enabled: bool = True,
    ) -> None:
        timeout = httpx.Timeout(timeout_seconds)
        self.ocr_client = ocr_client
        self.vision_model = vision_model
        self.ocr_enabled = ocr_enabled
        self._local_client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            trust_env=False,
        )
        self._external_client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            trust_env=True,
        )

    async def load(
        self,
        source: FileSource,
        resolver: FileResolverProtocol,
    ) -> ParsedDocument:
        file_name = source.name
        file_source = source.source
        file_data: dict[str, Any] | None = None

        if not file_source or not _looks_resolvable(file_source):
            try:
                file_data = await resolver.get_file(source=source)
            except Exception as exc:
                raise FileParseError("QQ 文件下载失败，请重新发送附件。") from exc
            file_name = str(file_data.get("file_name") or file_name)
            file_source = str(
                file_data.get("file")
                or file_data.get("url")
                or ""
            )
            if not file_source and file_data.get("base64"):
                file_source = f"base64://{file_data['base64']}"

        suffix = Path(file_name).suffix.lower() or Path(file_source).suffix.lower()
        if not suffix:
            raise FileParseError(
                "无法判断文件类型，请确保文件名带有 .pdf、.docx、.pptx、.xlsx 等后缀。"
            )
        if (
            suffix not in TEXT_EXTENSIONS
            and suffix not in {".pdf", ".docx", ".pptx", ".xlsx", ".csv"}
        ):
            raise FileParseError(
                f"暂不支持 {suffix} 文件。支持 PDF、DOCX、PPTX、XLSX、CSV、TXT 和 Markdown。"
            )

        data = await self._read_source(file_source)
        try:
            return await asyncio.to_thread(
                _parse_document,
                file_name,
                suffix,
                data,
            )
        except FileParseError as exc:
            if (
                suffix == ".pdf"
                and self.ocr_enabled
                and self.ocr_client is not None
                and "没有可提取的文字" in str(exc)
            ):
                return await self._ocr_pdf(file_name, data)
            raise

    async def _ocr_pdf(self, file_name: str, data: bytes) -> ParsedDocument:
        try:
            document = pdfium.PdfDocument(data)
        except Exception as exc:
            raise FileParseError("扫描版 PDF 无法打开。") from exc
        sections: list[DocumentSection] = []
        try:
            for index in range(len(document)):
                page = document[index]
                bitmap = page.render(scale=2.0)
                image = bitmap.to_pil().convert("RGB")
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=90)
                encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
                reply = await self.ocr_client.chat(
                    [
                        {
                            "role": "system",
                            "content": (
                                "你是 OCR 转写器。只转写页面上的可见文字、"
                                "公式和表格内容，保留原始顺序。看不清就标注。"
                                "不要解题，不要补充页面中没有的内容。"
                            ),
                        },
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        f"请转写第 {index + 1} 页的全部内容，"
                                        "包括手写文字和数学公式。"
                                    ),
                                },
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": (
                                            "data:image/jpeg;base64,"
                                            + encoded
                                        )
                                    },
                                },
                            ],
                        },
                    ],
                    model=self.vision_model,
                    extra_payload={"reasoning_effort": "none"},
                )
                text = reply.strip()
                if text:
                    sections.append(
                        DocumentSection(f"第 {index + 1} 页（OCR）", text)
                    )
        finally:
            document.close()
        if not sections:
            raise FileParseError("扫描版 PDF 的 OCR 没有识别出文字。")
        text = "\n\n".join(
            f"--- {section.location} ---\n{section.content}"
            for section in sections
        )
        return ParsedDocument(
            file_name,
            text,
            f"（{len(sections)} 页，OCR）",
            tuple(sections),
        )

    async def _read_source(self, source: str) -> bytes:
        if not source:
            raise FileParseError("没有取得文件下载地址。")
        if source.startswith("base64://"):
            try:
                return base64.b64decode(source[9:], validate=True)
            except (ValueError, binascii.Error) as exc:
                raise FileParseError("文件 Base64 数据无效。") from exc
        if source.startswith("data:"):
            _, separator, payload = source.partition(",")
            if not separator:
                raise FileParseError("文件数据格式无效。")
            try:
                return base64.b64decode(payload, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise FileParseError("文件 Base64 数据无效。") from exc
        if source.startswith("file://"):
            path = Path(unquote(urlparse(source).path))
            return await _read_local_file(path)

        parsed = urlparse(source)
        if parsed.scheme in {"http", "https"}:
            client = (
                self._local_client
                if parsed.hostname in {"127.0.0.1", "::1", "localhost"}
                else self._external_client
            )
            try:
                response = await client.get(source)
                response.raise_for_status()
                return response.content
            except httpx.TimeoutException as exc:
                raise FileParseError("文件下载超时，请稍后重试。") from exc
            except httpx.HTTPError as exc:
                raise FileParseError("文件下载失败，请重新发送附件。") from exc

        return await _read_local_file(Path(source))

    async def close(self) -> None:
        await self._local_client.aclose()
        await self._external_client.aclose()


async def summarize_documents(
    chat_client: Any,
    *,
    model: str,
    documents: Sequence[ParsedDocument],
    instruction: str,
) -> str:
    if not documents:
        raise FileParseError("没有可总结的文件。")

    combined = "\n\n".join(
        f"===== 文件 {index}：{document.name} =====\n"
        f"{document.text}"
        for index, document in enumerate(documents, start=1)
    )
    chunks = split_document_text(combined, 12_000)
    if len(chunks) == 1:
        content = combined
    else:
        partials: list[str] = []
        for index, chunk in enumerate(chunks, start=1):
            partial = await chat_client.chat(
                [
                    {
                        "role": "system",
                        "content": "你是文档分段摘要器。忠实保留事实、数据、结论和行动项，不要编造。",
                    },
                    {
                        "role": "user",
                        "content": (
                            f"这是文档第 {index}/{len(chunks)} 段。请提炼本段全部关键信息，"
                            "保留数字、术语和待办事项：\n\n"
                            f"{chunk}"
                        ),
                    },
                ],
                model=model,
            )
            partials.append(f"第 {index} 段摘要：\n{partial}")
        content = await _reduce_partials(chat_client, model, partials)

    names = "、".join(document.name for document in documents)
    details = "；".join(
        f"{document.name}{document.detail}" for document in documents if document.detail
    )
    request = instruction.strip() or "请总结这些文件。"
    final_prompt = (
        f"文件：{names}\n"
        f"{details}\n\n"
        f"用户要求：{request}\n\n"
        "请输出一份可直接使用的文档总结，包含：\n"
        "1. 一句话结论\n"
        "2. 关键要点\n"
        "3. 重要数据、结论或决策\n"
        "4. 行动项（如原文有）\n"
        "5. 风险、疑问或信息缺口（如原文有）\n"
        "严格依据材料，不要补造内容。\n\n"
        "使用 QQ 可读的纯文本，不要使用星号加粗、井号标题、"
        "代码围栏或 Markdown 表格。\n\n"
        f"{content}"
    )
    return await chat_client.chat(
        [
            {
                "role": "system",
                "content": (
                    "你是严谨的学习与工作文档分析助手。"
                    "总结要结构清楚、保留关键数字和限定条件。"
                    "输出纯文本，不使用 Markdown 标记。"
                ),
            },
            {"role": "user", "content": final_prompt},
        ],
        model=model,
    )


def split_document_text(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        split_at = max(
            window.rfind("\n\n"),
            window.rfind("\n"),
            window.rfind("。"),
            window.rfind("；"),
        )
        if split_at < max_chars // 2:
            split_at = max_chars
        else:
            split_at += 1
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining:
        chunks.append(remaining)
    return [chunk for chunk in chunks if chunk]


async def _reduce_partials(
    chat_client: Any,
    model: str,
    partials: list[str],
) -> str:
    current = partials
    while sum(len(item) for item in current) > 45_000:
        joined = "\n\n".join(current)
        groups = split_document_text(joined, 15_000)
        reduced: list[str] = []
        for group in groups:
            summary = await chat_client.chat(
                [
                    {
                        "role": "system",
                        "content": "请压缩以下分段摘要，只保留事实、结论和行动项。",
                    },
                    {"role": "user", "content": group},
                ],
                model=model,
            )
            reduced.append(summary)
        current = reduced
    return "\n\n".join(current)


async def _read_local_file(path: Path) -> bytes:
    try:
        return await asyncio.to_thread(path.read_bytes)
    except OSError as exc:
        raise FileParseError("文件不存在或无法读取，请重新发送附件。") from exc


def _parse_document(file_name: str, suffix: str, data: bytes) -> ParsedDocument:
    if suffix == ".pdf":
        return _parse_pdf(file_name, data)
    if suffix == ".docx":
        return _parse_docx(file_name, data)
    if suffix == ".pptx":
        return _parse_pptx(file_name, data)
    if suffix == ".xlsx":
        return _parse_xlsx(file_name, data)
    if suffix == ".csv":
        text = _decode_text(data)
        rows = list(csv.reader(io.StringIO(text)))
        content = "\n".join("\t".join(row) for row in rows)
        return ParsedDocument(
            file_name,
            content,
            f"（{len(rows)} 行）",
            (DocumentSection("全文", content),),
        )
    text = _decode_text(data)
    return ParsedDocument(
        file_name,
        text,
        sections=(DocumentSection("全文", text),),
    )


def _parse_pdf(file_name: str, data: bytes) -> ParsedDocument:
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = []
        sections: list[DocumentSection] = []
        for index, page in enumerate(reader.pages, start=1):
            text = (page.extract_text() or "").strip()
            if text:
                pages.append(f"--- 第 {index} 页 ---\n{text}")
                sections.append(DocumentSection(f"第 {index} 页", text))
    except Exception as exc:
        raise FileParseError("PDF 解析失败，文件可能已损坏或加密。") from exc
    if not pages:
        raise FileParseError(
            "这个 PDF 没有可提取的文字，可能是扫描版。可以发送页面截图让我按图片解题。"
        )
    return ParsedDocument(
        file_name,
        "\n\n".join(pages),
        f"（{len(reader.pages)} 页）",
        tuple(sections),
    )


def _parse_docx(file_name: str, data: bytes) -> ParsedDocument:
    try:
        document = WordDocument(io.BytesIO(data))
        parts = [paragraph.text.strip() for paragraph in document.paragraphs]
        for table in document.tables:
            for row in table.rows:
                parts.append("\t".join(cell.text.strip() for cell in row.cells))
    except Exception as exc:
        raise FileParseError("Word 文档解析失败，请确认文件是 .docx 格式。") from exc
    text = "\n".join(part for part in parts if part)
    if not text:
        raise FileParseError("Word 文档中没有可提取的文字。")
    return ParsedDocument(
        file_name,
        text,
        sections=(DocumentSection("正文", text),),
    )


def _parse_pptx(file_name: str, data: bytes) -> ParsedDocument:
    try:
        presentation = Presentation(io.BytesIO(data))
        slides: list[str] = []
        sections: list[DocumentSection] = []
        for index, slide in enumerate(presentation.slides, start=1):
            texts = [
                shape.text.strip()
                for shape in slide.shapes
                if hasattr(shape, "text") and shape.text.strip()
            ]
            if texts:
                slide_text = "\n".join(texts)
                slides.append(f"--- 第 {index} 页幻灯片 ---\n{slide_text}")
                sections.append(
                    DocumentSection(f"第 {index} 页幻灯片", slide_text)
                )
    except Exception as exc:
        raise FileParseError("PPT 解析失败，请确认文件是 .pptx 格式。") from exc
    if not slides:
        raise FileParseError("PPT 中没有可提取的文字。")
    return ParsedDocument(
        file_name,
        "\n\n".join(slides),
        f"（{len(presentation.slides)} 页）",
        tuple(sections),
    )


def _parse_xlsx(file_name: str, data: bytes) -> ParsedDocument:
    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        sheets: list[str] = []
        sections: list[DocumentSection] = []
        for worksheet in workbook.worksheets:
            rows = [
                "\t".join("" if value is None else str(value) for value in row)
                for row in worksheet.iter_rows(values_only=True)
            ]
            rows = [row for row in rows if row.strip("\t")]
            if rows:
                sheet_text = "\n".join(rows)
                sheets.append(
                    f"--- 工作表：{worksheet.title} ---\n{sheet_text}"
                )
                sections.append(
                    DocumentSection(f"工作表：{worksheet.title}", sheet_text)
                )
    except Exception as exc:
        raise FileParseError("Excel 解析失败，请确认文件是 .xlsx 格式。") from exc
    if not sheets:
        raise FileParseError("Excel 文件中没有可提取的数据。")
    return ParsedDocument(
        file_name,
        "\n\n".join(sheets),
        f"（{len(workbook.worksheets)} 个工作表）",
        tuple(sections),
    )


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise FileParseError("文本文件编码无法识别，请转换为 UTF-8 后重试。")


def _looks_resolvable(source: str) -> bool:
    if source.startswith(("http://", "https://", "file://", "base64://", "data:")):
        return True
    path = Path(source)
    return path.is_absolute() and path.exists()
