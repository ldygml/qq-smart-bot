from __future__ import annotations

import json

import httpx
import pytest

from qq_ai_bot.onebot import (
    OneBotClient,
    ReplyTarget,
    StickerSource,
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


def test_store_face_is_not_treated_as_question_image() -> None:
    content = extract_message_content(
        [
            {
                "type": "mface",
                "data": {
                    "summary": "奶龙",
                    "emoji_id": "abc",
                    "emoji_package_id": "123",
                },
            }
        ],
        self_id=10001,
    )
    assert content.image_sources == ()
    assert content.sticker_summaries == ("奶龙",)


def test_image_with_sticker_metadata_is_not_treated_as_question_image() -> None:
    content = extract_message_content(
        [
            {
                "type": "image",
                "data": {
                    "summary": "doge",
                    "emoji_id": "abc",
                    "key": "sticker-key",
                    "url": "https://example.com/sticker.gif",
                },
            }
        ],
        self_id=10001,
    )
    assert content.image_sources == ()
    assert content.sticker_summaries == ("doge",)


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


@pytest.mark.asyncio
async def test_image_only_sticker_is_sent_as_image_segment() -> None:
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
        await client.send_sticker(
            ReplyTarget("private", 123),
            StickerSource(
                summary="动画表情",
                image_source="https://example.com/sticker.png",
            ),
        )
    finally:
        await client.close()
    assert captured["message"][0]["type"] == "image"
    assert captured["message"][0]["data"]["sub_type"] == 1


@pytest.mark.asyncio
async def test_get_group_list_returns_group_names() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/get_group_list")
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "retcode": 0,
                "data": [
                    {"group_id": 100, "group_name": "技术群"},
                    {"group_id": "200", "group_name": "测试群"},
                ],
            },
        )

    client = OneBotClient(
        "http://127.0.0.1:3000",
        "token",
        transport=httpx.MockTransport(handler),
    )
    try:
        groups = await client.get_group_list()
    finally:
        await client.close()
    assert groups == [
        {"group_id": 100, "group_name": "技术群"},
        {"group_id": 200, "group_name": "测试群"},
    ]


@pytest.mark.asyncio
async def test_get_group_member_name_uses_card_or_nickname() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload == {"group_id": 100, "user_id": 200}
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "retcode": 0,
                "data": {"card": "群名片", "nickname": "昵称"},
            },
        )

    client = OneBotClient(
        "http://127.0.0.1:3000",
        "token",
        transport=httpx.MockTransport(handler),
    )
    try:
        name = await client.get_group_member_name(100, 200)
    finally:
        await client.close()
    assert name == "群名片"


@pytest.mark.asyncio
async def test_get_group_root_files_normalizes_napcat_response() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/get_group_root_files")
        assert json.loads(request.content) == {"group_id": 100}
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "retcode": 0,
                "data": {
                    "folders": [
                        {
                            "folder_id": "folder-1",
                            "folder_name": "课程资料",
                            "total_file_count": 2,
                        }
                    ],
                    "files": [
                        {
                            "file_id": "file-1",
                            "file_name": "讲义.pdf",
                            "file_size": 2048,
                            "uploader_name": "小明",
                        }
                    ],
                },
            },
        )

    client = OneBotClient(
        "http://127.0.0.1:3000",
        "token",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.get_group_files(100)
    finally:
        await client.close()
    assert result["folders"][0]["folder_id"] == "folder-1"
    assert result["folders"][0]["name"] == "课程资料"
    assert result["files"][0] == {
        "file_id": "file-1",
        "name": "讲义.pdf",
        "size": 2048,
        "uploader": "小明",
        "busid": None,
        "upload_time": None,
        "modify_time": None,
        "download_times": 0,
    }


def test_at_another_user_is_not_bot_mention() -> None:
    content = extract_message_content(
        [{"type": "at", "data": {"qq": "20002"}}],
        self_id=10001,
    )
    assert content.mentioned is False
    assert content.mentioned_user_ids == (20002,)


def test_extract_mentioned_user_ids_from_cq_code() -> None:
    content = extract_message_content(
        "[CQ:at,qq=20002] 戳一下",
        self_id=10001,
    )
    assert content.text == "戳一下"
    assert content.mentioned_user_ids == (20002,)


def test_strip_trigger_prefix() -> None:
    assert strip_trigger_prefix("/AI：你好", ("/ai", "/问")) == "你好"
    assert strip_trigger_prefix("普通聊天", ("/ai",)) == "普通聊天"


def test_split_reply_uses_paragraph_boundary() -> None:
    text = "第一段" * 30 + "\n\n" + "第二段" * 30
    chunks = split_reply(text, 100)
    assert len(chunks) == 2
    assert chunks[0] == "第一段" * 30
    assert chunks[1] == "第二段" * 30

