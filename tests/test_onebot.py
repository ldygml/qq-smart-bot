from __future__ import annotations

import json

import httpx
import pytest

from qq_ai_bot.onebot import (
    OneBotClient,
    ReplyTarget,
    extract_message_content,
    extract_message_text,
    split_reply,
    strip_trigger_prefix,
)


def test_extract_array_message_and_bot_mention() -> None:
    text, mentioned = extract_message_text(
        [
            {"type": "at", "data": {"qq": "10001"}},
            {"type": "text", "data": {"text": " 你好"}},
            {"type": "image", "data": {"file": "x.jpg"}},
        ],
        self_id=10001,
    )
    assert mentioned is True
    assert text == "你好[图片]"


def test_extract_image_source_from_array_message() -> None:
    content = extract_message_content(
        [
            {"type": "text", "data": {"text": "请解题"}},
            {
                "type": "image",
                "data": {"file": "question.jpg", "url": "https://example.com/q.jpg"},
            },
        ],
        self_id=10001,
    )
    assert content.text == "请解题[图片]"
    assert content.image_sources == ("https://example.com/q.jpg",)


def test_extract_cq_code_message() -> None:
    text, mentioned = extract_message_text(
        "[CQ:at,qq=10001] 讲个笑话",
        self_id=10001,
    )
    assert mentioned is True
    assert text == "讲个笑话"


def test_extract_image_from_cq_code() -> None:
    content = extract_message_content(
        "[CQ:image,file=a.jpg,url=https://example.com/a.jpg] 看看这题",
        self_id=10001,
    )
    assert content.image_sources == ("https://example.com/a.jpg",)
    assert content.text == "[图片] 看看这题"


def test_extract_file_source_from_array_message() -> None:
    content = extract_message_content(
        [
            {
                "type": "file",
                "data": {
                    "file": "week-report.docx",
                    "file_id": "file-context-id",
                    "file_size": "2048",
                },
            },
            {"type": "text", "data": {"text": "帮我总结"}},
        ],
        self_id=10001,
    )
    assert content.text == "[文件: week-report.docx]帮我总结"
    assert content.file_sources[0].name == "week-report.docx"
    assert content.file_sources[0].file_id == "file-context-id"
    assert content.file_sources[0].size == 2048


def test_extract_voice_source_from_array_message() -> None:
    content = extract_message_content(
        [
            {
                "type": "record",
                "data": {
                    "file": "voice.amr",
                    "file_id": "voice-context-id",
                    "path": "C:\\cache\\voice.amr",
                    "file_size": "4096",
                },
            }
        ],
        self_id=10001,
    )
    assert content.text == "[语音]"
    assert content.audio_sources[0].file_id == "voice-context-id"
    assert content.audio_sources[0].source == "C:\\cache\\voice.amr"


def test_extract_quoted_message_id() -> None:
    content = extract_message_content(
        [
            {"type": "reply", "data": {"id": "12345"}},
            {"type": "text", "data": {"text": "继续"}},
        ],
        self_id=10001,
    )
    assert content.reply_id == "12345"


@pytest.mark.asyncio
async def test_group_reply_includes_quote_segment() -> None:
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"status": "ok", "retcode": 0, "data": {"message_id": 1}},
        )

    client = OneBotClient(
        "http://127.0.0.1:3000",
        "token",
        transport=httpx.MockTransport(handler),
    )
    try:
        await client.send_text(
            ReplyTarget("group", 123, 456, message_id=789),
            "收到",
        )
    finally:
        await client.close()

    assert captured["message"][0] == {
        "type": "reply",
        "data": {"id": "789"},
    }
    assert captured["message"][1]["data"]["text"] == "收到"


def test_at_another_user_is_not_bot_mention() -> None:
    _, mentioned = extract_message_text(
        [{"type": "at", "data": {"qq": "20002"}}],
        self_id=10001,
    )
    assert mentioned is False


def test_strip_trigger_prefix() -> None:
    assert strip_trigger_prefix("/AI：你好", ("/ai", "/问")) == "你好"
    assert strip_trigger_prefix("普通聊天", ("/ai",)) == "普通聊天"


def test_split_reply_uses_paragraph_boundary() -> None:
    text = "第一段" * 30 + "\n\n" + "第二段" * 30
    chunks = split_reply(text, 100)
    assert len(chunks) == 2
    assert chunks[0] == "第一段" * 30
    assert chunks[1] == "第二段" * 30

