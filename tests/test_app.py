from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pydantic import SecretStr

from qq_ai_bot.app import create_app
from qq_ai_bot.audio import LoadedAudio
from qq_ai_bot.config import Settings
from qq_ai_bot.files import DocumentSection, ParsedDocument
from qq_ai_bot.memory import ChatMemory
from qq_ai_bot.onebot import ReplyTarget, StickerSource
from qq_ai_bot.productivity import ProductivityStore
from qq_ai_bot.shared_memory import SharedMemoryStore
from qq_ai_bot.stickers import StickerStore
from qq_ai_bot.vector_memory import VectorMemoryStore
from qq_ai_bot.web_reader import WebArticle


class FakeChatClient:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []
        self.models: list[str | None] = []
        self.extra_payloads: list[object] = []
        self.reply = "测试回复"

    async def chat(
        self,
        messages: Sequence[dict[str, str]],
        *,
        model: str | None = None,
        extra_payload=None,
    ) -> str:
        self.calls.append(list(messages))
        self.models.append(model)
        self.extra_payloads.append(extra_payload)
        return self.reply

    async def close(self) -> None:
        return None


class FakeOneBotClient:
    def __init__(self) -> None:
        self.sent: list[tuple[ReplyTarget, str]] = []
        self.poked: list[ReplyTarget] = []
        self.quoted_sender_id: int | None = 999
        self.images: list[tuple[ReplyTarget, str]] = []
        self.records: list[tuple[ReplyTarget, str]] = []
        self.stickers: list[tuple[ReplyTarget, StickerSource]] = []
        self.files: list[tuple[ReplyTarget, str, str]] = []
        self.groups: list[dict[str, object]] = []

    async def send_text(self, target: ReplyTarget, text: str) -> None:
        self.sent.append((target, text))

    async def poke(self, target: ReplyTarget) -> None:
        self.poked.append(target)

    async def get_msg_sender_id(self, message_id: int | str) -> int | None:
        return self.quoted_sender_id

    async def get_group_list(self) -> list[dict[str, object]]:
        return list(self.groups)

    async def send_image(self, target: ReplyTarget, image: str) -> None:
        self.images.append((target, image))

    async def send_record(self, target: ReplyTarget, record: str) -> None:
        self.records.append((target, record))

    async def send_sticker(
        self,
        target: ReplyTarget,
        sticker: StickerSource,
    ) -> None:
        self.stickers.append((target, sticker))

    async def upload_file(
        self,
        target: ReplyTarget,
        *,
        path: str,
        name: str,
    ) -> None:
        self.files.append((target, path, name))

    async def close(self) -> None:
        return None


class FakeImageLoader:
    def __init__(self) -> None:
        self.sources: list[str] = []

    async def load(self, source: str) -> str:
        self.sources.append(source)
        return "data:image/png;base64,AAAA"

    async def close(self) -> None:
        return None


class FakeDocumentLoader:
    def __init__(self, document: ParsedDocument | None = None) -> None:
        self.document = document or ParsedDocument("notes.txt", "测试文档内容")
        self.sources: list[str] = []

    async def load(self, source, resolver) -> ParsedDocument:
        self.sources.append(source.name)
        return self.document

    async def close(self) -> None:
        return None


class FakeAudioLoader:
    def __init__(self) -> None:
        self.sources = []

    async def load(self, source, resolver) -> LoadedAudio:
        self.sources.append(source)
        return LoadedAudio("data:audio/wav;base64,AAAA", "wav")

    async def close(self) -> None:
        return None


class FakeTTS:
    async def synthesize(self, text: str) -> str:
        return "https://example.com/tts.wav"

    async def close(self) -> None:
        return None


class FakeWebReader:
    async def read(self, url: str) -> WebArticle:
        return WebArticle(
            url=url,
            title="测试文章",
            text="这是网页正文。核心观点是测试网页阅读功能。",
        )

    async def close(self) -> None:
        return None


class FakeEmbeddingClient:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0] for _ in texts]

    async def close(self) -> None:
        return None


class FakeGroupDecider:
    def __init__(self, result: bool) -> None:
        self.result = result
        self.calls = 0

    async def should_reply(
        self,
        *,
        history,
        current_sender: str,
        current_text: str,
    ) -> bool:
        self.calls += 1
        return self.result


class ReflectionOnlyChat:
    async def chat(self, messages, *, model=None, extra_payload=None) -> str:
        return (
            '{"reflections":[{"content":"多人都在准备杭州项目",'
            '"visibility":"shareable","confidence":0.75}],'
            '"growth":[{"aspect":"shared_topics",'
            '"content":"近期经常讨论杭州项目","visibility":"shareable"}]}'
        )

    async def close(self) -> None:
        return None


class FakeStickerDecider:
    def __init__(self, sticker_id: int | None) -> None:
        self.sticker_id = sticker_id
        self.calls = 0

    async def choose(self, **kwargs) -> int | None:
        self.calls += 1
        return self.sticker_id


class VerboseThenConciseChatClient(FakeChatClient):
    async def chat(
        self,
        messages: Sequence[dict[str, str]],
        *,
        model: str | None = None,
        extra_payload=None,
    ) -> str:
        self.calls.append(list(messages))
        self.models.append(model)
        if len(self.calls) == 1:
            return (
                "**首先，我想说你辛苦了。**\n"
                "- 你可以先休息一下\n"
                "- 然后再完成作业\n"
                "需要我继续帮你列计划吗？"
            )
        return "确实挺烦的，先挑一项写，别硬扛。"


def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        "onebot_token": SecretStr("test-secret"),
        "llm_model": "fake-model",
        "database_path": tmp_path / "bot.db",
        "private_allowlist": "",
        "enable_group": False,
        "vector_memory_enabled": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.mark.asyncio
async def test_private_message_gets_ai_reply(tmp_path) -> None:
    llm = FakeChatClient()
    sender = FakeOneBotClient()
    app = create_app(
        settings=make_settings(tmp_path),
        chat_client=llm,
        onebot_client=sender,
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.post(
            "/onebot",
            headers={"Authorization": "Bearer test-secret"},
            json={
                "post_type": "message",
                "message_type": "private",
                "self_id": 999,
                "user_id": 123,
                "message_id": 888,
                "message": [{"type": "text", "data": {"text": "你好"}}],
            },
        )
        assert response.status_code == 204
        await _wait_for_condition(lambda: bool(sender.sent))

    assert sender.sent[0][1] == "测试回复"
    assert sender.sent[0][0].user_id == 123
    assert llm.calls[0][-1] == {"role": "user", "content": "你好"}
    assert "足球迷" in llm.calls[0][0]["content"]


@pytest.mark.asyncio
async def test_webhook_rejects_bad_token(tmp_path) -> None:
    app = create_app(
        settings=make_settings(tmp_path),
        chat_client=FakeChatClient(),
        onebot_client=FakeOneBotClient(),
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.post(
            "/onebot",
            headers={"Authorization": "Bearer wrong"},
            json={"post_type": "message"},
        )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_duplicate_message_id_is_ignored(tmp_path) -> None:
    llm = FakeChatClient()
    sender = FakeOneBotClient()
    app = create_app(
        settings=make_settings(tmp_path),
        chat_client=llm,
        onebot_client=sender,
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
    )
    transport = httpx.ASGITransport(app=app)
    event = {
        "post_type": "message",
        "message_type": "private",
        "self_id": 999,
        "user_id": 123,
        "message_id": 777,
        "message": "你好",
    }
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        for _ in range(2):
            response = await client.post(
                "/onebot",
                headers={"Authorization": "Bearer test-secret"},
                json=event,
            )
            assert response.status_code == 204
        await _wait_for_condition(lambda: bool(sender.sent))
        await asyncio.sleep(0.02)

    assert len(llm.calls) == 1
    assert len(sender.sent) == 1


@pytest.mark.asyncio
async def test_group_requires_mention_or_prefix(tmp_path) -> None:
    llm = FakeChatClient()
    sender = FakeOneBotClient()
    app = create_app(
        settings=make_settings(tmp_path, enable_group=True),
        chat_client=llm,
        onebot_client=sender,
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
    )
    transport = httpx.ASGITransport(app=app)
    base_event = {
        "post_type": "message",
        "message_type": "group",
        "self_id": 999,
        "user_id": 123,
        "group_id": 456,
    }
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        await client.post(
            "/onebot",
            headers={"Authorization": "Bearer test-secret"},
            json={
                **base_event,
                "message_id": 1,
                "message": [{"type": "text", "data": {"text": "普通消息"}}],
            },
        )
        await asyncio.sleep(0.02)
        assert not sender.sent

        await client.post(
            "/onebot",
            headers={"Authorization": "Bearer test-secret"},
            json={
                **base_event,
                "message_id": 2,
                "message": [{"type": "text", "data": {"text": "/ai 你好"}}],
            },
        )
        await _wait_for_condition(lambda: bool(sender.sent))

    assert sender.sent[0][0].group_id == 456
    assert sender.sent[0][0].message_id is None
    assert llm.calls[0][-1] == {"role": "user", "content": "你好"}


@pytest.mark.asyncio
async def test_quoting_bot_message_triggers_group_reply_without_mention(tmp_path) -> None:
    llm = FakeChatClient()
    sender = FakeOneBotClient()
    app = create_app(
        settings=make_settings(tmp_path, enable_group=True),
        chat_client=llm,
        onebot_client=sender,
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
        productivity_store=ProductivityStore(tmp_path / "tools.db"),
        group_reply_decider=FakeGroupDecider(True),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        await client.post(
            "/onebot",
            headers={"Authorization": "Bearer test-secret"},
            json={
                "post_type": "message",
                "message_type": "group",
                "self_id": 999,
                "user_id": 123,
                "group_id": 456,
                "message_id": 2000,
                "message": [
                    {"type": "reply", "data": {"id": "42"}},
                    {"type": "text", "data": {"text": "继续讲"}},
                ],
            },
        )
        await _wait_for_condition(lambda: bool(sender.sent))

    assert sender.sent[0][0].message_id == 2000
    assert sender.sent[0][1] == "测试回复"


@pytest.mark.asyncio
async def test_group_can_reply_without_mention(tmp_path) -> None:
    llm = FakeChatClient()
    sender = FakeOneBotClient()
    app = create_app(
        settings=make_settings(
            tmp_path,
            enable_group=True,
            group_require_mention=False,
            shared_memory_enabled=True,
        ),
        chat_client=llm,
        onebot_client=sender,
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
        productivity_store=ProductivityStore(tmp_path / "tools.db"),
        group_reply_decider=FakeGroupDecider(True),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        await client.post(
            "/onebot",
            headers={"Authorization": "Bearer test-secret"},
            json={
                "post_type": "message",
                "message_type": "group",
                "self_id": 999,
                "user_id": 123,
                "group_id": 456,
                "message_id": 1800,
                "message": "普通群消息",
            },
        )
        await _wait_for_condition(lambda: bool(sender.sent))

    assert sender.sent[0][0].group_id == 456
    assert sender.sent[0][1] == "测试回复"


@pytest.mark.asyncio
async def test_image_message_uses_vision_model(tmp_path) -> None:
    llm = FakeChatClient()
    sender = FakeOneBotClient()
    image_loader = FakeImageLoader()
    app = create_app(
        settings=make_settings(tmp_path, vision_model="qwen3-vl-plus"),
        chat_client=llm,
        onebot_client=sender,
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=image_loader,
        document_loader=FakeDocumentLoader(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.post(
            "/onebot",
            headers={"Authorization": "Bearer test-secret"},
            json={
                "post_type": "message",
                "message_type": "private",
                "self_id": 999,
                "user_id": 123,
                "message_id": 900,
                "message": [
                    {"type": "text", "data": {"text": "请解这道题"}},
                    {
                        "type": "image",
                        "data": {"url": "https://example.com/question.png"},
                    },
                ],
            },
        )
        assert response.status_code == 204
        await _wait_for_condition(lambda: bool(sender.sent))

    assert image_loader.sources == ["https://example.com/question.png"]
    assert llm.models == ["qwen3-vl-plus"]
    assert sender.sent[0][0].message_id == 900
    current_content = llm.calls[0][-1]["content"]
    assert isinstance(current_content, list)
    assert current_content[0] == {"type": "text", "text": "请解这道题"}
    assert current_content[1]["type"] == "image_url"


@pytest.mark.asyncio
async def test_long_reply_is_not_truncated(tmp_path) -> None:
    llm = FakeChatClient()
    llm.reply = "长" * 5000
    sender = FakeOneBotClient()
    app = create_app(
        settings=make_settings(tmp_path),
        chat_client=llm,
        onebot_client=sender,
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        await client.post(
            "/onebot",
            headers={"Authorization": "Bearer test-secret"},
            json={
                "post_type": "message",
                "message_type": "private",
                "self_id": 999,
                "user_id": 123,
                "message_id": 901,
                "message": "请写长一点",
            },
        )
        await _wait_for_condition(lambda: len(sender.sent) == 4)

    assert len(sender.sent) == 4
    assert "".join(text for _, text in sender.sent) == llm.reply


@pytest.mark.asyncio
async def test_note_and_todo_commands(tmp_path) -> None:
    sender = FakeOneBotClient()
    tools = ProductivityStore(tmp_path / "tools.db")
    app = create_app(
        settings=make_settings(tmp_path),
        chat_client=FakeChatClient(),
        onebot_client=sender,
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        productivity_store=tools,
        document_loader=FakeDocumentLoader(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        for message_id, message in enumerate(
            (
                "/记 线代 | 行列式交换两行会变号",
                "/查 行列式",
                "/待办 完成第三章习题",
                "/待办",
            ),
            start=1,
        ):
            await client.post(
                "/onebot",
                headers={"Authorization": "Bearer test-secret"},
                json={
                    "post_type": "message",
                    "message_type": "private",
                    "self_id": 999,
                    "user_id": 123,
                    "message_id": 1100 + message_id,
                    "message": message,
                },
            )
        await _wait_for_condition(lambda: len(sender.sent) == 4)

    assert "已保存笔记 #1" in sender.sent[0][1]
    assert "行列式交换两行会变号" in sender.sent[1][1]
    assert "已添加待办 #1" in sender.sent[2][1]
    assert "完成第三章习题" in sender.sent[3][1]


@pytest.mark.asyncio
async def test_due_reminder_is_delivered(tmp_path) -> None:
    sender = FakeOneBotClient()
    tools = ProductivityStore(tmp_path / "tools.db")
    tools.add_reminder(
        user_id=123,
        message_type="private",
        group_id=None,
        content="该复习了",
        due_at=datetime.now(timezone.utc) - timedelta(seconds=5),
    )
    app = create_app(
        settings=make_settings(tmp_path, reminder_poll_seconds=1),
        chat_client=FakeChatClient(),
        onebot_client=sender,
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        productivity_store=tools,
        document_loader=FakeDocumentLoader(),
    )
    async with app.router.lifespan_context(app):
        await _wait_for_condition(lambda: bool(sender.sent), timeout=3)

    assert sender.sent[0][0] == ReplyTarget("private", 123)
    assert sender.sent[0][1] == "提醒：该复习了"


@pytest.mark.asyncio
async def test_file_message_is_summarized(tmp_path) -> None:
    llm = FakeChatClient()
    sender = FakeOneBotClient()
    document_loader = FakeDocumentLoader(
        ParsedDocument("week-report.docx", "本周完成接口联调，下周准备上线。")
    )
    app = create_app(
        settings=make_settings(tmp_path),
        chat_client=llm,
        onebot_client=sender,
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=document_loader,
        productivity_store=ProductivityStore(tmp_path / "tools.db"),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.post(
            "/onebot",
            headers={"Authorization": "Bearer test-secret"},
            json={
                "post_type": "message",
                "message_type": "private",
                "self_id": 999,
                "user_id": 123,
                "message_id": 1500,
                "message": [
                    {
                        "type": "file",
                        "data": {
                            "file": "week-report.docx",
                            "file_id": "context-file-id",
                        },
                    },
                    {"type": "text", "data": {"text": "/总结 提炼行动项"}},
                ],
            },
        )
        assert response.status_code == 204
        await _wait_for_condition(lambda: len(sender.sent) == 2)

    assert document_loader.sources == ["week-report.docx"]
    assert "正在解析" in sender.sent[0][1]
    assert sender.sent[1][1] == "测试回复"
    assert llm.models == ["fake-model"]


@pytest.mark.asyncio
async def test_verbose_casual_reply_is_rewritten(tmp_path) -> None:
    llm = VerboseThenConciseChatClient()
    sender = FakeOneBotClient()
    app = create_app(
        settings=make_settings(tmp_path),
        chat_client=llm,
        onebot_client=sender,
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
        productivity_store=ProductivityStore(tmp_path / "tools.db"),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        await client.post(
            "/onebot",
            headers={"Authorization": "Bearer test-secret"},
            json={
                "post_type": "message",
                "message_type": "private",
                "self_id": 999,
                "user_id": 123,
                "message_id": 1600,
                "message": "作业好多，烦死了",
            },
        )
        await _wait_for_condition(lambda: bool(sender.sent))

    assert len(llm.calls) == 2
    assert sender.sent[0][1] == "确实挺烦的，先挑一项写，别硬扛。"
    assert "过度完整" in llm.calls[1][0]["content"]


@pytest.mark.asyncio
async def test_poke_command_pokes_sender_without_ai_call(tmp_path) -> None:
    llm = FakeChatClient()
    sender = FakeOneBotClient()
    app = create_app(
        settings=make_settings(tmp_path),
        chat_client=llm,
        onebot_client=sender,
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
        productivity_store=ProductivityStore(tmp_path / "tools.db"),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        await client.post(
            "/onebot",
            headers={"Authorization": "Bearer test-secret"},
            json={
                "post_type": "message",
                "message_type": "private",
                "self_id": 999,
                "user_id": 123,
                "message_id": 1700,
                "message": "/戳",
            },
        )
        await _wait_for_condition(lambda: bool(sender.poked))

    assert len(sender.poked) == 1
    assert sender.poked[0].message_type == "private"
    assert sender.poked[0].user_id == 123
    assert llm.calls == []


@pytest.mark.asyncio
async def test_group_poke_command_can_target_mentioned_member(tmp_path) -> None:
    llm = FakeChatClient()
    sender = FakeOneBotClient()
    app = create_app(
        settings=make_settings(
            tmp_path,
            enable_group=True,
            group_require_mention=True,
            group_smart_participation=False,
        ),
        chat_client=llm,
        onebot_client=sender,
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        await client.post(
            "/onebot",
            headers={"Authorization": "Bearer test-secret"},
            json={
                "post_type": "message",
                "message_type": "group",
                "self_id": 999,
                "group_id": 500,
                "user_id": 123,
                "message_id": 1701,
                "message": [
                    {"type": "at", "data": {"qq": "456"}},
                    {"type": "text", "data": {"text": " 戳一下"}},
                ],
            },
        )
        await _wait_for_condition(lambda: bool(sender.poked))

    assert sender.poked[0].message_type == "group"
    assert sender.poked[0].group_id == 500
    assert sender.poked[0].user_id == 456
    assert llm.calls == []


@pytest.mark.asyncio
async def test_incoming_poke_notice_makes_bot_respond(tmp_path) -> None:
    sender = FakeOneBotClient()
    app = create_app(
        settings=make_settings(
            tmp_path,
            enable_group=True,
            group_require_mention=True,
        ),
        chat_client=FakeChatClient(),
        onebot_client=sender,
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        await client.post(
            "/onebot",
            headers={"Authorization": "Bearer test-secret"},
            json={
                "post_type": "notice",
                "notice_type": "notify",
                "sub_type": "poke",
                "message_type": "group",
                "self_id": 999,
                "group_id": 500,
                "user_id": 123,
                "target_id": 999,
                "sender": {"nickname": "小明"},
            },
        )
        await _wait_for_condition(lambda: bool(sender.sent))

    assert sender.poked[0].user_id == 123
    assert sender.poked[0].group_id == 500
    assert sender.sent[0][0].user_id == 123
    assert sender.sent[0][1] in {
        "在，有话直说。",
        "戳什么，说事。",
        "行，戳回来了。",
    }


@pytest.mark.asyncio
async def test_incoming_poke_between_members_is_recorded_silently(
    tmp_path,
) -> None:
    shared = SharedMemoryStore(tmp_path / "shared.db")
    sender = FakeOneBotClient()
    app = create_app(
        settings=make_settings(
            tmp_path,
            enable_group=True,
            group_require_mention=False,
            shared_memory_enabled=True,
        ),
        chat_client=FakeChatClient(),
        onebot_client=sender,
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
        shared_memory_store=shared,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        await client.post(
            "/onebot",
            headers={"Authorization": "Bearer test-secret"},
            json={
                "post_type": "notice",
                "notice_type": "poke",
                "message_type": "group",
                "self_id": 999,
                "group_id": 500,
                "user_id": 123,
                "target_id": 456,
                "sender": {"nickname": "小明"},
            },
        )
        await _wait_for_condition(
            lambda: bool(shared.observations_since(0))
        )

    assert sender.sent == []
    assert sender.poked == []
    observation = shared.observations_since(0)[0]
    assert "小明" in observation["content"]
    assert "456" in observation["content"]


@pytest.mark.asyncio
async def test_voice_message_uses_audio_model(tmp_path) -> None:
    llm = FakeChatClient()
    sender = FakeOneBotClient()
    audio_loader = FakeAudioLoader()
    app = create_app(
        settings=make_settings(tmp_path),
        chat_client=llm,
        onebot_client=sender,
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
        productivity_store=ProductivityStore(tmp_path / "tools.db"),
        audio_loader=audio_loader,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        await client.post(
            "/onebot",
            headers={"Authorization": "Bearer test-secret"},
            json={
                "post_type": "message",
                "message_type": "private",
                "self_id": 999,
                "user_id": 123,
                "message_id": 1900,
                "message": [
                    {
                        "type": "record",
                        "data": {
                            "file": "voice.amr",
                            "file_id": "voice-id",
                        },
                    },
                    {"type": "text", "data": {"text": "/转写"}},
                ],
            },
        )
        await _wait_for_condition(lambda: len(sender.sent) == 2)

    assert len(audio_loader.sources) == 1
    assert llm.models == ["qwen3.8-omni-flash"]
    assert llm.extra_payloads[0]["modalities"] == ["text"]
    user_content = llm.calls[0][-1]["content"]
    assert isinstance(user_content, list)
    assert user_content[1]["type"] == "input_audio"
    assert sender.sent[1][1] == "测试回复"


@pytest.mark.asyncio
async def test_other_contact_can_use_shareable_memory(tmp_path) -> None:
    llm = FakeChatClient()
    sender = FakeOneBotClient()
    shared = SharedMemoryStore(tmp_path / "shared.db")
    shared.record_person(222, "小明")
    shared.add_memory(
        subject_user_id=222,
        subject_name="小明",
        content="下周三要去杭州出差",
        visibility="shareable",
        source_conversation="private:222",
    )
    shared.add_memory(
        subject_user_id=222,
        subject_name="小明",
        content="最近睡眠不好",
        visibility="private",
        source_conversation="private:222",
    )
    app = create_app(
        settings=make_settings(tmp_path, shared_memory_enabled=True),
        chat_client=llm,
        onebot_client=sender,
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
        productivity_store=ProductivityStore(tmp_path / "tools.db"),
        shared_memory_store=shared,
        group_reply_decider=FakeGroupDecider(True),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        await client.post(
            "/onebot",
            headers={"Authorization": "Bearer test-secret"},
            json={
                "post_type": "message",
                "message_type": "private",
                "self_id": 999,
                "user_id": 333,
                "message_id": 2100,
                "message": "小明最近要去哪",
                "sender": {"nickname": "小李"},
            },
        )
        await _wait_for_condition(lambda: bool(sender.sent))

    system_prompt = llm.calls[0][0]["content"]
    assert "杭州出差" in system_prompt
    assert "睡眠不好" not in system_prompt


@pytest.mark.asyncio
async def test_privacy_protection_can_be_disabled(tmp_path) -> None:
    llm = FakeChatClient()
    sender = FakeOneBotClient()
    shared = SharedMemoryStore(tmp_path / "shared.db")
    shared.record_person(222, "小明")
    shared.add_memory(
        subject_user_id=222,
        subject_name="小明",
        content="最近睡眠不好",
        visibility="private",
        source_conversation="private:222",
    )
    app = create_app(
        settings=make_settings(
            tmp_path,
            shared_memory_enabled=True,
            memory_privacy_protection=False,
        ),
        chat_client=llm,
        onebot_client=sender,
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
        productivity_store=ProductivityStore(tmp_path / "tools.db"),
        shared_memory_store=shared,
        group_reply_decider=FakeGroupDecider(True),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        await client.post(
            "/onebot",
            headers={"Authorization": "Bearer test-secret"},
            json={
                "post_type": "message",
                "message_type": "private",
                "self_id": 999,
                "user_id": 333,
                "message_id": 2200,
                "message": "小明最近怎么样",
                "sender": {"nickname": "小李"},
            },
        )
        await _wait_for_condition(lambda: bool(sender.sent))

    assert "睡眠不好" in llm.calls[0][0]["content"]


@pytest.mark.asyncio
async def test_sticker_uses_casual_chat_not_vision_model(tmp_path) -> None:
    llm = FakeChatClient()
    sender = FakeOneBotClient()
    sticker_store = StickerStore(tmp_path / "stickers.db")
    app = create_app(
        settings=make_settings(tmp_path),
        chat_client=llm,
        onebot_client=sender,
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
        productivity_store=ProductivityStore(tmp_path / "tools.db"),
        sticker_store=sticker_store,
        sticker_choice_decider=FakeStickerDecider(1),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        await client.post(
            "/onebot",
            headers={"Authorization": "Bearer test-secret"},
            json={
                "post_type": "message",
                "message_type": "private",
                "self_id": 999,
                "user_id": 123,
                "message_id": 2300,
                "message": [
                    {
                        "type": "mface",
                        "data": {
                            "summary": "奶龙",
                            "emoji_id": "abc",
                        },
                    }
                ],
            },
        )
        await _wait_for_condition(lambda: bool(sender.sent))

    assert llm.models
    assert all(model == "qwen3.8-flash" for model in llm.models)
    assert all(model != "qwen3-vl-plus" for model in llm.models)
    assert "表情包" in llm.calls[0][-1]["content"]
    assert sender.sent[0][0].message_id is None
    assert sticker_store.count() == 1
    assert sender.stickers[0][1].summary == "奶龙"


@pytest.mark.asyncio
async def test_current_news_request_enables_web_search(tmp_path) -> None:
    llm = FakeChatClient()
    sender = FakeOneBotClient()
    app = create_app(
        settings=make_settings(tmp_path, web_search_enabled=True),
        chat_client=llm,
        onebot_client=sender,
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
        productivity_store=ProductivityStore(tmp_path / "tools.db"),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        await client.post(
            "/onebot",
            headers={"Authorization": "Bearer test-secret"},
            json={
                "post_type": "message",
                "message_type": "private",
                "self_id": 999,
                "user_id": 123,
                "message_id": 2400,
                "message": "最近有什么足球新闻，帮我查一下",
            },
        )
        await _wait_for_condition(lambda: bool(sender.sent))

    assert llm.extra_payloads[0]["enable_search"] is True
    assert llm.extra_payloads[0]["search_options"]["forced_search"] is True
    assert "当前时间" in llm.calls[0][0]["content"]


@pytest.mark.asyncio
async def test_group_can_remember_what_was_said_in_another_group(tmp_path) -> None:
    llm = FakeChatClient()
    sender = FakeOneBotClient()
    shared = SharedMemoryStore(tmp_path / "shared.db")
    app = create_app(
        settings=make_settings(
            tmp_path,
            enable_group=True,
            group_require_mention=False,
            shared_memory_enabled=True,
            memory_privacy_protection=False,
        ),
        chat_client=llm,
        onebot_client=sender,
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
        productivity_store=ProductivityStore(tmp_path / "tools.db"),
        shared_memory_store=shared,
        group_reply_decider=FakeGroupDecider(True),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        for message_id, user_id, group_id, name, message in (
            (2500, 111, 100, "小明", "我周末去看利物浦的比赛"),
            (2501, 222, 200, "小李", "另一个群有人提到了利物浦吗"),
        ):
            await client.post(
                "/onebot",
                headers={"Authorization": "Bearer test-secret"},
                json={
                    "post_type": "message",
                    "message_type": "group",
                    "self_id": 999,
                    "user_id": user_id,
                    "group_id": group_id,
                    "message_id": message_id,
                    "message": message,
                    "sender": {"nickname": name},
                },
            )
            await _wait_for_condition(
                lambda: shared.context_for(
                    current_user_id=user_id,
                    current_user_name=name,
                    query=message,
                    include_private=True,
                        current_conversation_id="group:999:user:999",
                ).find("最近其他会话片段") >= 0
                if group_id == 100
                else len(sender.sent) >= 2
            )

    system_prompts = [call[0]["content"] for call in llm.calls]
    assert any(
        "我周末去看利物浦的比赛" in prompt for prompt in system_prompts
    )


@pytest.mark.asyncio
async def test_web_article_reader_command(tmp_path) -> None:
    llm = FakeChatClient()
    sender = FakeOneBotClient()
    app = create_app(
        settings=make_settings(tmp_path, web_reader_enabled=True),
        chat_client=llm,
        onebot_client=sender,
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
        productivity_store=ProductivityStore(tmp_path / "tools.db"),
        web_reader=FakeWebReader(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        await client.post(
            "/onebot",
            headers={"Authorization": "Bearer test-secret"},
            json={
                "post_type": "message",
                "message_type": "private",
                "self_id": 999,
                "user_id": 123,
                "message_id": 2600,
                "message": "/网页 https://example.com/article",
            },
        )
        await _wait_for_condition(lambda: len(sender.sent) >= 2)

    assert any(
        "网页正文" in call[-1]["content"]
        for call in llm.calls
        if isinstance(call[-1]["content"], str)
    )


@pytest.mark.asyncio
async def test_media_commands_send_voice_image_file_and_sticker(tmp_path) -> None:
    sender = FakeOneBotClient()
    sticker_store = StickerStore(tmp_path / "stickers.db")
    sticker_store.record(
        StickerSource(
            summary="开心",
            emoji_id="1",
            emoji_package_id="2",
            key="key",
        )
    )
    file_path = tmp_path / "sample.txt"
    file_path.write_text("sample", encoding="utf-8")
    app = create_app(
        settings=make_settings(
            tmp_path,
            owner_qq=123,
            media_send_enabled=True,
            tts_enabled=True,
        ),
        chat_client=FakeChatClient(),
        onebot_client=sender,
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
        productivity_store=ProductivityStore(tmp_path / "tools.db"),
        tts_client=FakeTTS(),
        sticker_store=sticker_store,
        sticker_choice_decider=FakeStickerDecider(1),
    )
    transport = httpx.ASGITransport(app=app)
    commands = [
        "/发语音 你好",
        "/发图 https://example.com/a.png",
        f"/发文件 {file_path}",
        "/发表情",
    ]
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        for index, command in enumerate(commands, start=1):
            await client.post(
                "/onebot",
                headers={"Authorization": "Bearer test-secret"},
                json={
                    "post_type": "message",
                    "message_type": "private",
                    "self_id": 999,
                    "user_id": 123,
                    "message_id": 2700 + index,
                    "message": command,
                },
            )
        await _wait_for_condition(
            lambda: bool(sender.records and sender.images and sender.files and sender.stickers)
        )

    assert sender.records[0][1] == "https://example.com/tts.wav"
    assert sender.images[0][1].endswith("a.png")
    assert sender.files[0][2] == "sample.txt"
    assert sender.stickers[0][1].summary == "开心"


@pytest.mark.asyncio
async def test_image_only_animation_sticker_is_collected_and_organized(
    tmp_path,
) -> None:
    store = StickerStore(tmp_path / "stickers.db")
    app = create_app(
        settings=make_settings(tmp_path),
        chat_client=FakeChatClient(),
        onebot_client=FakeOneBotClient(),
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
        sticker_store=store,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        await client.post(
            "/onebot",
            headers={"Authorization": "Bearer test-secret"},
            json={
                "post_type": "message",
                "message_type": "private",
                "self_id": 999,
                "user_id": 123,
                "message_id": 2750,
                "message": [
                    {
                        "type": "image",
                        "data": {
                            "sub_type": "1",
                            "summary": "无语",
                            "url": "https://example.com/wuyu.png",
                        },
                    }
                ],
            },
        )
        await _wait_for_condition(lambda: store.count() == 1)
        await _wait_for_condition(
            lambda: (
                store.get_metadata(int(store.list_recent()[0]["id"]))
                or {}
            ).get("category")
            == "无语"
        )
    metadata = store.get_metadata(int(store.list_recent()[0]["id"]))
    assert metadata is not None
    assert metadata["category"] == "无语"
    assert metadata["usage_hint"]


@pytest.mark.asyncio
async def test_document_chunks_can_be_questioned_with_citation(tmp_path) -> None:
    llm = FakeChatClient()
    sender = FakeOneBotClient()
    vector_store = VectorMemoryStore(tmp_path / "vectors.db")
    app = create_app(
        settings=make_settings(
            tmp_path,
            vector_memory_enabled=True,
            shared_memory_enabled=False,
        ),
        chat_client=llm,
        onebot_client=sender,
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(
            ParsedDocument(
                "week-report.docx",
                "本周完成接口联调。下周准备上线。",
                sections=(
                    DocumentSection(
                        "第 1 页",
                        "本周完成接口联调。下周准备上线。",
                    ),
                ),
            )
        ),
        productivity_store=ProductivityStore(tmp_path / "tools.db"),
        embedding_client=FakeEmbeddingClient(),
        vector_store=vector_store,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        await client.post(
            "/onebot",
            headers={"Authorization": "Bearer test-secret"},
            json={
                "post_type": "message",
                "message_type": "private",
                "self_id": 999,
                "user_id": 123,
                "message_id": 2800,
                "message": [
                    {
                        "type": "file",
                        "data": {
                            "file": "week-report.docx",
                            "file_id": "doc-id",
                        },
                    }
                ],
            },
        )
        await _wait_for_condition(lambda: len(sender.sent) >= 2)
        await client.post(
            "/onebot",
            headers={"Authorization": "Bearer test-secret"},
            json={
                "post_type": "message",
                "message_type": "private",
                "self_id": 999,
                "user_id": 123,
                "message_id": 2801,
                "message": "/问文件 行动项是什么",
            },
        )
        await _wait_for_condition(lambda: len(sender.sent) >= 3)

    prompts = [call[0]["content"] for call in llm.calls]
    assert any(
        "知识库检索结果" in prompt and "week-report.docx" in prompt
        for prompt in prompts
    )


@pytest.mark.asyncio
async def test_admin_page_and_api(tmp_path) -> None:
    shared = SharedMemoryStore(tmp_path / "shared.db")
    shared.record_person(123, "test-user")
    shared.update_person_profile(
        123,
        style_profile="直接、简短",
        relationship_note="测试朋友",
    )
    memory_id = shared.add_memory(
        subject_user_id=123,
        subject_name="test-user",
        content="这条记忆应该可以删除",
        visibility="shareable",
        source_conversation="private:123",
    )
    app = create_app(
        settings=make_settings(tmp_path, admin_web_enabled=True),
        chat_client=FakeChatClient(),
        onebot_client=FakeOneBotClient(),
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
        productivity_store=ProductivityStore(tmp_path / "tools.db"),
        shared_memory_store=shared,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        page = await client.get("/admin")
        people = await client.get("/api/admin/people")
        detail = await client.get("/api/admin/people/123")
        deleted = await client.delete(
            f"/api/admin/people/123/memories/{memory_id}"
        )
    assert page.status_code == 200
    assert "给她爱控制台" in page.text
    assert people.json()["people"][0]["display_name"] == "test-user"
    assert detail.json()["person"]["style_profile"] == "直接、简短"
    assert deleted.status_code == 200
    assert shared.list_memories(123) == []


@pytest.mark.asyncio
async def test_admin_memory_bulk_purge_endpoints(tmp_path) -> None:
    shared = SharedMemoryStore(tmp_path / "shared.db")
    chat_memory = ChatMemory(tmp_path / "memory.db")
    for user_id, name in ((101, "甲"), (202, "乙")):
        shared.record_person(user_id, name)
        shared.add_memory(
            subject_user_id=user_id,
            subject_name=name,
            content=f"{name}的长期记忆",
            visibility="shareable",
            source_conversation=f"private:{user_id}",
        )
        shared.add_episode(
            conversation_id=f"private:{user_id}",
            message_type="private",
            group_id=None,
            speaker_user_id=user_id,
            speaker_name=name,
            user_content=f"{name}说过的话",
            assistant_content="回复",
        )
        chat_memory.append_exchange(
            f"private:{user_id}",
            "用户消息",
            "机器人回复",
            max_rows=10,
        )
    app = create_app(
        settings=make_settings(tmp_path),
        chat_client=FakeChatClient(),
        onebot_client=FakeOneBotClient(),
        memory=chat_memory,
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
        shared_memory_store=shared,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        one = await client.delete("/api/admin/people/101/all-memory")
        assert one.status_code == 200
        assert shared.get_person(101) is None
        assert shared.get_person(202) is not None
        assert chat_memory.count("private:101") == 0
        assert chat_memory.count("private:202") == 2

        everything = await client.delete("/api/admin/memory/all")
        assert everything.status_code == 200
    assert shared.stats()["people"] == 0
    assert shared.stats()["memories"] == 0
    assert shared.stats()["episodes"] == 0
    assert chat_memory.stats()["stored_messages"] == 0


@pytest.mark.asyncio
async def test_group_profiles_use_original_names_and_keep_custom_names(
    tmp_path,
) -> None:
    sender = FakeOneBotClient()
    sender.groups = [
        {"group_id": 93001, "group_name": "原始测试群"},
    ]
    app = create_app(
        settings=make_settings(tmp_path, enable_group=True),
        chat_client=FakeChatClient(),
        onebot_client=sender,
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        initial = await client.get("/api/admin/group-profiles")
        profile = next(
            item
            for item in initial.json()["groups"]
            if item["group_id"] == 93001
        )
        assert profile["name"] == "原始测试群"

        renamed = await client.patch(
            "/api/admin/group-profiles/93001",
            json={"name": "我的自定义群名"},
        )
        assert renamed.status_code == 200
        refreshed = await client.get("/api/admin/group-profiles")
        profile = next(
            item
            for item in refreshed.json()["groups"]
            if item["group_id"] == 93001
        )
        assert profile["name"] == "我的自定义群名"


@pytest.mark.asyncio
async def test_smart_group_mode_can_stay_silent(tmp_path) -> None:
    sender = FakeOneBotClient()
    decider = FakeGroupDecider(False)
    app = create_app(
        settings=make_settings(
            tmp_path,
            enable_group=True,
            group_require_mention=False,
            group_smart_participation=True,
        ),
        chat_client=FakeChatClient(),
        onebot_client=sender,
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
        productivity_store=ProductivityStore(tmp_path / "tools.db"),
        group_reply_decider=decider,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        for message_id, message in (
            (2900, "你们觉得这个方案怎么样"),
            (2901, "谢谢"),
        ):
            await client.post(
                "/onebot",
                headers={"Authorization": "Bearer test-secret"},
                json={
                    "post_type": "message",
                    "message_type": "group",
                    "self_id": 999,
                    "user_id": 123,
                    "group_id": 456,
                    "message_id": message_id,
                    "message": message,
                    "sender": {"nickname": "测试用户"},
                },
            )
        await asyncio.sleep(0.1)

    assert sender.sent == []
    assert decider.calls == 1


@pytest.mark.asyncio
async def test_manual_reflection_persists_insights(tmp_path) -> None:
    shared = SharedMemoryStore(tmp_path / "shared.db")
    for user_id, name, content in (
        (201, "小明", "下周去杭州出差"),
        (202, "小李", "正在准备杭州项目"),
        (203, "小王", "询问杭州项目进度"),
    ):
        shared.add_memory(
            subject_user_id=user_id,
            subject_name=name,
            content=content,
            visibility="shareable",
            source_conversation=f"private:{user_id}",
        )
    app = create_app(
        settings=make_settings(
            tmp_path,
            shared_memory_enabled=True,
            reflection_enabled=True,
            reflection_min_new_memories=20,
        ),
        chat_client=ReflectionOnlyChat(),
        onebot_client=FakeOneBotClient(),
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
        productivity_store=ProductivityStore(tmp_path / "tools.db"),
        shared_memory_store=shared,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.post("/api/admin/reflect")
    assert response.status_code == 200
    assert response.json()["memories"] == 3
    assert response.json()["reflections"] == 1
    assert response.json()["growth"] == 1
    assert shared.reflection_summary(limit=10)[0]["content"].startswith("多人")
    assert shared.growth_summary(limit=10)[0]["content"] == "近期经常讨论杭州项目"


async def _wait_for_condition(predicate, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not met before timeout")
        await asyncio.sleep(0.01)

