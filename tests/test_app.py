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
from qq_ai_bot.files import ParsedDocument
from qq_ai_bot.memory import ChatMemory
from qq_ai_bot.onebot import ReplyTarget
from qq_ai_bot.productivity import ProductivityStore
from qq_ai_bot.shared_memory import SharedMemoryStore


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

    async def send_text(self, target: ReplyTarget, text: str) -> None:
        self.sent.append((target, text))

    async def poke(self, target: ReplyTarget) -> None:
        self.poked.append(target)

    async def get_msg_sender_id(self, message_id: int | str) -> int | None:
        return self.quoted_sender_id

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
        ),
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


async def _wait_for_condition(predicate, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not met before timeout")
        await asyncio.sleep(0.01)

