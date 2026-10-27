from __future__ import annotations

from docx import Document as WordDocument
from openpyxl import Workbook
from pptx import Presentation
import pytest

from qq_ai_bot.files import (
    DocumentLoader,
    ParsedDocument,
    split_document_text,
    summarize_documents,
)
from qq_ai_bot.onebot import FileSource


class NoopResolver:
    async def get_file(self, *, source: FileSource) -> dict[str, object]:
        raise AssertionError("local files should not use the OneBot resolver")


class FakeChat:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, object]]] = []

    async def chat(self, messages, *, model=None) -> str:
        self.calls.append(list(messages))
        if len(self.calls) == 1 and len(str(messages[-1]["content"])) > 15_000:
            return "分段摘要"
        return "最终总结"


@pytest.mark.asyncio
async def test_parse_docx_pptx_xlsx_and_text(tmp_path) -> None:
    docx_path = tmp_path / "report.docx"
    document = WordDocument()
    document.add_heading("周报", level=1)
    document.add_paragraph("本周完成接口联调。")
    document.save(docx_path)

    pptx_path = tmp_path / "slides.pptx"
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[1])
    slide.shapes.title.text = "项目进展"
    slide.placeholders[1].text = "完成第一阶段"
    presentation.save(pptx_path)

    xlsx_path = tmp_path / "data.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["任务", "状态"])
    sheet.append(["接口联调", "已完成"])
    workbook.save(xlsx_path)

    text_path = tmp_path / "notes.md"
    text_path.write_text("# 复习\n行列式交换两行会变号", encoding="utf-8")

    loader = DocumentLoader()
    try:
        docx = await loader.load(
            FileSource(docx_path.name, str(docx_path)),
            NoopResolver(),
        )
        pptx = await loader.load(
            FileSource(pptx_path.name, str(pptx_path)),
            NoopResolver(),
        )
        xlsx = await loader.load(
            FileSource(xlsx_path.name, str(xlsx_path)),
            NoopResolver(),
        )
        text = await loader.load(
            FileSource(text_path.name, str(text_path)),
            NoopResolver(),
        )
    finally:
        await loader.close()

    assert "接口联调" in docx.text
    assert "第一阶段" in pptx.text
    assert "已完成" in xlsx.text
    assert "交换两行会变号" in text.text


@pytest.mark.asyncio
async def test_long_document_is_summarized_in_chunks() -> None:
    chat = FakeChat()
    document = ParsedDocument("book.txt", "内容" * 15_000)
    summary = await summarize_documents(
        chat,
        model="qwen-plus",
        documents=[document],
        instruction="提炼重点",
    )
    assert summary == "最终总结"
    assert len(chat.calls) >= 4


def test_split_document_text_preserves_content() -> None:
    text = "甲" * 25_000
    chunks = split_document_text(text, 12_000)
    assert len(chunks) == 3
    assert "".join(chunks) == text
