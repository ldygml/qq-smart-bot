from __future__ import annotations

import asyncio
from io import BytesIO
import json
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from openpyxl import Workbook
from PIL import Image
from pydantic import SecretStr

from qq_ai_bot.app import (
    _admin_access_signature,
    _clean_model_reply,
    _detect_natural_sticker_request,
    _enrich_sticker_history,
    _history_for_current_turn,
    _is_simple_fact_lookup,
    _is_unsolicited_presence_ping,
    _looks_like_task_request,
    _metric_inference_is_unsupported,
    _needs_casual_rewrite,
    _needs_claim_review,
    _needs_web_search,
    _reply_variety_hint,
    _rank_sticker_candidates,
    _social_only_reply,
    _social_tone_hint,
    create_app,
)
from qq_ai_bot.audio import LoadedAudio
from qq_ai_bot.config import Settings
from qq_ai_bot.files import DocumentSection, ParsedDocument
from qq_ai_bot.grounded_qa import GroundedAnswer, GroundedSource
from qq_ai_bot.memory import ChatMemory
from qq_ai_bot.onebot import OneBotError, ReplyTarget, StickerSource
from qq_ai_bot.productivity import ProductivityStore
from qq_ai_bot.quality import QualityStore
from qq_ai_bot.shared_memory import SharedMemoryStore
from qq_ai_bot.stickers import StickerAssetStore, StickerStore
from qq_ai_bot.sticker_policy import StickerClassification
from qq_ai_bot.topics import TopicCandidate, TopicStore
from qq_ai_bot.tracing import MessageTraceStore
from qq_ai_bot.vector_memory import VectorChunk, VectorMemoryStore
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


class ClaimReviewChat(FakeChatClient):
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
        if "事实与一致性审查员" in str(messages[0].get("content") or ""):
            return json.dumps(
                {
                    "changed": True,
                    "reply": (
                        "单看这些指标，只能说明产出和引用情况，"
                        "不能直接判断学术水平。"
                    ),
                    "issues": ["unsupported_causal_claim"],
                },
                ensure_ascii=False,
            )
        return "h指数38配100篇说明引用没被灌水，水平肯定靠谱。"


class FakeOneBotClient:
    def __init__(self) -> None:
        self.sent: list[tuple[ReplyTarget, str]] = []
        self.mentions: list[tuple[ReplyTarget, tuple[int, ...]]] = []
        self.forwarded_single: list[tuple[ReplyTarget, str]] = []
        self.forwarded_nodes: list[tuple[ReplyTarget, list[dict[str, object]]]] = []
        self.poked: list[ReplyTarget] = []
        self.quoted_sender_id: int | None = 999
        self.images: list[tuple[ReplyTarget, str]] = []
        self.videos: list[tuple[ReplyTarget, str]] = []
        self.records: list[tuple[ReplyTarget, str]] = []
        self.stickers: list[tuple[ReplyTarget, StickerSource]] = []
        self.files: list[tuple[ReplyTarget, str, str]] = []
        self.groups: list[dict[str, object]] = []
        self.friends: list[dict[str, object]] = []
        self.login_info: dict[str, object] = {
            "user_id": 999,
            "nickname": "测试机器人",
        }
        self.group_members: list[dict[str, object]] = []
        self.private_history: list[dict[str, object]] = []
        self.group_history: list[dict[str, object]] = []
        self.group_files: dict[str, list[dict[str, object]]] = {
            "folders": [],
            "files": [],
        }
        self.group_file_url = ""
        self.group_file_name = ""

    async def send_text(
        self,
        target: ReplyTarget,
        text: str,
        *,
        mentions: tuple[int, ...] | list[int] = (),
        reply_to: int | str | None = None,
    ) -> str | None:
        _ = reply_to
        self.sent.append((target, text))
        self.mentions.append((target, tuple(int(value) for value in mentions)))
        return "fake-message-id"

    async def forward_single_message(
        self,
        target: ReplyTarget,
        message_id: int | str,
    ) -> str | None:
        self.forwarded_single.append((target, str(message_id)))
        return "fake-forward-id"

    async def send_forward(
        self,
        target: ReplyTarget,
        nodes: list[dict[str, object]],
    ) -> str | None:
        self.forwarded_nodes.append((target, nodes))
        return "fake-merged-forward-id"

    async def poke(self, target: ReplyTarget) -> None:
        self.poked.append(target)

    async def get_msg_sender_id(
        self,
        message_id: int | str,
        *,
        group_id: int | None = None,
    ) -> int | None:
        return self.quoted_sender_id

    async def get_group_list(self) -> list[dict[str, object]]:
        return list(self.groups)

    async def get_friend_list(self) -> list[dict[str, object]]:
        return list(self.friends)

    async def get_login_info(self) -> dict[str, object]:
        return dict(self.login_info)

    async def get_group_member_list(
        self,
        group_id: int,
    ) -> list[dict[str, object]]:
        return list(self.group_members)

    async def get_friend_msg_history(
        self,
        user_id: int,
        *,
        message_seq: int | str = 0,
        count: int = 50,
        reverse_order: bool = True,
    ) -> list[dict[str, object]]:
        return list(self.private_history)

    async def get_group_msg_history(
        self,
        group_id: int,
        *,
        message_seq: int | str = 0,
        count: int = 50,
        reverse_order: bool = True,
    ) -> list[dict[str, object]]:
        return list(self.group_history)

    async def get_group_files(
        self,
        group_id: int,
        *,
        folder_id: str = "",
    ) -> dict[str, list[dict[str, object]]]:
        return self.group_files

    async def get_group_file_url(
        self,
        group_id: int,
        file_id: str,
        *,
        busid: str | int | None = None,
    ) -> dict[str, str]:
        if not self.group_file_url:
            raise OneBotError("test file URL is not configured")
        return {
            "url": self.group_file_url,
            "file_name": self.group_file_name or file_id,
        }

    async def send_image(self, target: ReplyTarget, image: str) -> None:
        self.images.append((target, image))

    async def send_video(self, target: ReplyTarget, video: str) -> None:
        self.videos.append((target, video))

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


class DataTTS(FakeTTS):
    async def synthesize_styled(
        self,
        text: str,
        *,
        voice: str = "",
        rate: float | None = None,
    ) -> str:
        assert rate is not None
        return "data:audio/wav;base64,AAAA"


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
        self.last_kwargs: dict[str, object] = {}

    async def choose(self, **kwargs) -> int | None:
        self.calls += 1
        self.last_kwargs = kwargs
        return self.sticker_id


class RecordingStickerOrganizer:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def classify(
        self,
        *,
        summary: str,
        recent_context: str = "",
        image_url: str = "",
    ) -> StickerClassification:
        self.calls.append(
            {
                "summary": summary,
                "recent_context": recent_context,
                "image_url": image_url,
            }
        )
        return StickerClassification(
            "无语",
            "朋友遇到离谱或尴尬的事情时用；适合熟人轻松吐槽，求助和严肃争议时不用",
            ("汗", "无语", "尴尬"),
            source="vision",
        )


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
        "qq_media_dir": tmp_path / "qq-media",
        "private_allowlist": "",
        "enable_group": False,
        "vector_memory_enabled": False,
          "sticker_download_enabled": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_social_tone_hint_softens_requested_edge() -> None:
    hint = _social_tone_hint("你能不能温柔一点，别这么凶")
    assert "降低攻击性" in hint
    assert "不要辩解" in hint
    invitation = _social_tone_hint("吃烧烤不")
    assert "轻松邀约" in invitation
    assert "旧任务" in invitation
    fake = _social_tone_hint("回答得好假")
    assert "关心模板" in fake
    assert "不要长篇道歉" in fake


def test_unsolicited_presence_ping_is_rejected() -> None:
    assert _is_unsolicited_presence_ping("发完了", "在吗")
    assert _is_unsolicited_presence_ping("嗯", "在吗？")
    assert not _is_unsolicited_presence_ping("你在吗", "在")


def test_reply_variety_hint_detects_repeated_openings() -> None:
    history = [
        {"role": "assistant", "content": "行，我收着点。"},
        {"role": "user", "content": "嗯"},
        {"role": "assistant", "content": "行，那就算了。"},
        {"role": "assistant", "content": "行，你说得对。"},
    ]
    hint = _reply_variety_hint(history)
    assert "最近重复较多的开头" in hint
    assert "行，" in hint


def test_social_short_turns_and_task_detection() -> None:
    assert _social_only_reply("👌") in {"嗯。", "行。", "收到。", "好。"}
    assert _social_only_reply("🤔") in {"？", "咋了。", "怎么了？", "嗯？"}
    assert _social_only_reply("你在干嘛") is None
    assert _looks_like_task_request("做一下第五题")
    assert _looks_like_task_request("连名带姓这首歌是谁唱的")
    assert _is_simple_fact_lookup("连名带姓这首歌是谁唱的")
    assert _needs_web_search("连名带姓这首歌是谁唱的")
    assert _needs_web_search("帮我上网查一下最近的人工智能新闻")
    assert _needs_web_search("现查一下这个政策")


def test_natural_sticker_request_recognizes_conversational_variants() -> None:
    for text in (
        "来个表情包",
        "给我发个无语的表情包",
        "整点搞笑的",
        "能不能发张表情看看",
        "用表情包回我",
        "有没有表情包",
        "表情包呢",
        "这氛围不得来一张？",
        "别打字了，整点节目效果",
        "给我整个活",
        "来个能接住这个梗的表情",
        "发张应景的",
    ):
        assert _detect_natural_sticker_request(text), text

    for text in (
        "不要发表情包",
        "你发过什么表情包",
        "表情包怎么收藏",
        "为什么发表情包",
        "你发表情包了吗",
        "这个表情包真好笑",
        "怎么收藏表情包",
    ):
        assert not _detect_natural_sticker_request(text), text


def test_sticker_candidate_ranking_prefers_matching_mood() -> None:
    candidates = [
        {"id": 1, "summary": "奶龙", "category": "开心", "tags": []},
        {"id": 2, "summary": "汗", "category": "无语", "tags": []},
    ]
    ranked = _rank_sticker_candidates(candidates, "给我发个无语的表情包")
    assert ranked[0]["id"] == 2
    for question in (
        "王宝强是谁",
        "上海明天热不热",
        "现在金价多少钱一克",
        "最近有什么好看的电影",
        "网上说苹果出了折叠屏，真的假的",
        "从北京到上海高铁几点",
        "台风现在到哪了",
        "世界杯什么时候开始",
        "王宝强演过什么",
        "他最近有什么新电影",
    ):
        assert _needs_web_search(question), question
    for question in (
        "我好累",
        "你是谁",
        "他是谁",
        "晚上吃什么",
        "帮我编个故事",
        "你觉得我该不该换工作",
        "最近工作有点烦",
    ):
        assert not _needs_web_search(question), question
    assert not _needs_web_search("比赛")
    assert not _looks_like_task_request("换个话题")
    assert not _looks_like_task_request("比赛")
    assert _needs_casual_rewrite(
        "比赛",
        "比赛挺累的吧？歇会儿，喝口水。",
    )
    assert _needs_claim_review(
        "h指数38配100篇说明引用没被灌水。"
    )
    assert _needs_claim_review(
        "他应该就是川大那位，身份对得上。"
    )
    assert _metric_inference_is_unsupported(
        "他有100篇论文，h指数38",
        "100篇能撑到h指数38，不像临时凑的。",
    )
    assert not _needs_claim_review("刚比完？赢没赢。")
    assert _clean_model_reply("自然回应：\n那就先歇会儿。") == "那就先歇会儿。"


def test_old_private_history_is_not_reused_for_a_new_topic() -> None:
    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    history = [
        {"role": "user", "content": "成都蓉城今晚踢球"},
        {"role": "assistant", "content": "立冬快乐，蓉城今晚有比赛"},
        {"role": "user", "content": "你联网查啊"},
        {"role": "assistant", "content": "我这边没有实时赛程"},
    ]

    assert _history_for_current_turn(
        history,
        old,
        "累了？",
        "Asia/Shanghai",
    ) == []
    assert _history_for_current_turn(
        history,
        old,
        "上次说的那个比赛呢",
        "Asia/Shanghai",
    ) == history[-4:]


@pytest.mark.asyncio
async def test_unsupported_claim_is_rewritten_before_sending(tmp_path) -> None:
    llm = ClaimReviewChat()
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
                "message_id": 2951,
                "message": "他有100篇论文，h指数38，说明水平怎么样？",
            },
        )
        await _wait_for_condition(lambda: bool(sender.sent))

    assert sender.sent[0][1] == (
        "这些数字能确认的是产出和引用表现。"
        "至于学术水平、引用质量或是不是凑数，还得看具体论文和同行评价。"
    )
    assert len(llm.calls) == 2


@pytest.mark.asyncio
async def test_admin_routes_require_owner_token_when_enabled(tmp_path) -> None:
    app = create_app(
        settings=make_settings(
            tmp_path,
            admin_require_auth=True,
        ),
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
        denied = await client.get("/api/admin/overview")
        allowed = await client.get(
            "/api/admin/overview",
            headers={"X-Bot-Admin-Token": "test-secret"},
        )
        expires = str(int(datetime.now().timestamp()) + 60)
        nonce = "test-nonce"
        signature = _admin_access_signature(
            "test-secret",
            expires,
            nonce,
        )
        ticket = await client.get(
            "/admin",
            params={
                "expires": expires,
                "nonce": nonce,
                "signature": signature,
            },
            follow_redirects=False,
        )
        page = await client.get("/admin")
    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert ticket.status_code == 307
    assert page.status_code == 200


@pytest.mark.asyncio
async def test_message_trace_records_group_silence_reason(tmp_path) -> None:
    trace_store = MessageTraceStore(tmp_path / "traces.db")
    app = create_app(
        settings=make_settings(
            tmp_path,
            enable_group=True,
            group_require_mention=True,
        ),
        chat_client=FakeChatClient(),
        onebot_client=FakeOneBotClient(),
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
        trace_store=trace_store,
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
                "message_id": 806,
                "message": "今天群里有人踢球吗",
            },
        )
        await _wait_for_condition(
            lambda: bool(trace_store.list_recent(limit=1))
        )

    trace = trace_store.list_recent(limit=1)[0]
    assert trace["status"] == "skipped"
    assert "@" in trace["reason"]


@pytest.mark.asyncio
async def test_group_mention_uses_login_id_when_event_self_id_is_missing(
    tmp_path,
) -> None:
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
        response = await client.post(
            "/onebot",
            headers={"Authorization": "Bearer test-secret"},
            json={
                "post_type": "message",
                "message_type": "group",
                "user_id": 123,
                "group_id": 456,
                "message_id": 807,
                "message": [
                    {"type": "at", "data": {"qq": "999"}},
                    {"type": "text", "data": {"text": " 你怎么看"}},
                ],
            },
        )
        await _wait_for_condition(lambda: bool(sender.sent))

    assert response.status_code == 204
    assert sender.sent
    assert sender.sent[0][0].message_type == "group"


@pytest.mark.asyncio
async def test_conversation_test_endpoint_does_not_send_qq_message(
    tmp_path,
) -> None:
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
            "/api/admin/conversation-test",
            json={
                "message_type": "private",
                "user_id": 123,
                "display_name": "测试用户",
                "message": "今天有点累",
            },
        )
    assert response.status_code == 200
    assert response.json()["reply"] == "测试回复"
    assert sender.sent == []


@pytest.mark.asyncio
async def test_natural_debounce_merges_rapid_private_messages(tmp_path) -> None:
    llm = FakeChatClient()
    sender = FakeOneBotClient()
    app = create_app(
        settings=make_settings(
            tmp_path,
            natural_pacing_enabled=True,
            natural_message_debounce_ms=120,
            natural_reply_delay_min_ms=0,
            natural_reply_delay_max_ms=0,
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
        for message_id, text in ((801, "我今天有点烦"), (802, "先不聊学习了")):
            await client.post(
                "/onebot",
                headers={"Authorization": "Bearer test-secret"},
                json={
                    "post_type": "message",
                    "message_type": "private",
                    "self_id": 999,
                    "user_id": 123,
                    "message_id": message_id,
                    "message": text,
                },
            )
        await _wait_for_condition(lambda: bool(sender.sent))

    assert len(llm.calls) == 1
    assert llm.calls[0][-1]["content"] == (
        "我今天有点烦\n先不聊学习了"
    )


@pytest.mark.asyncio
async def test_recent_quality_feedback_guides_next_reply(
    tmp_path,
) -> None:
    quality = QualityStore(tmp_path / "quality.db")
    quality_id = quality.record(
        conversation_id="private:123",
        user_id=123,
        group_id=None,
        user_text="你好",
        assistant_text="少来这套。",
        model="fake-model",
    )
    quality.mark_review(
        quality_id,
        score=0.3,
        issues=["overdone_edge"],
        notes="普通问候不要冷嘲。",
    )
    assert quality.coaching_notes(group_id=None) == [
        "正常聊天不要为了显得有性格而冷嘲或攻击。"
    ]
    llm = FakeChatClient()
    app = create_app(
        settings=make_settings(tmp_path),
        chat_client=llm,
        onebot_client=FakeOneBotClient(),
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
        quality_store=quality,
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
                "message_id": 803,
                "message": "今天过得还行",
            },
        )
        await _wait_for_condition(lambda: bool(llm.calls))

    assert any(
        "正常聊天不要为了显得有性格而冷嘲或攻击。"
        in call[0]["content"]
        for call in llm.calls
    )


@pytest.mark.asyncio
async def test_adaptive_rollout_can_guide_a_reply(
    tmp_path,
    monkeypatch,
) -> None:
    quality = QualityStore(tmp_path / "quality.db")
    for _ in range(6):
        quality.record_experiment(
            quality_id=None,
            user_id=123,
            group_id=None,
            challenger_name="warm_concise",
            baseline_reply="你应该反思一下。",
            challenger_reply="先缓缓，别急着逼自己。",
            winner="challenger",
            baseline_score=0.3,
            challenger_score=0.8,
        )
    monkeypatch.setattr("qq_ai_bot.app.random.random", lambda: 0.0)
    llm = FakeChatClient()
    app = create_app(
        settings=make_settings(
            tmp_path,
            adaptive_reply_enabled=True,
            adaptive_reply_sample_rate=0,
        ),
        chat_client=llm,
        onebot_client=FakeOneBotClient(),
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
        quality_store=quality,
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
                "message_id": 804,
                "message": "今天有点累",
            },
        )
        await _wait_for_condition(lambda: bool(llm.calls))

    assert "更自然、温和、精简" in llm.calls[0][0]["content"]


@pytest.mark.asyncio
async def test_active_topic_is_injected_into_conversation(tmp_path) -> None:
    topic_store = TopicStore(tmp_path / "topics.db")
    topic_store.upsert(
        scope_key="user:123",
        candidate=TopicCandidate(
            title="课程展示",
            summary="下周要做课程展示",
            status="waiting",
            pending_question="展示日期确认了吗？",
        ),
        user_id=123,
        group_id=None,
    )
    llm = FakeChatClient()
    app = create_app(
        settings=make_settings(
            tmp_path,
            topic_state_enabled=True,
            topic_update_after_messages=10,
        ),
        chat_client=llm,
        onebot_client=FakeOneBotClient(),
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
        topic_store=topic_store,
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
                "message_id": 805,
                "message": "那个展示有点麻烦",
            },
        )
        await _wait_for_condition(lambda: bool(llm.calls))

    system_prompt = llm.calls[0][0]["content"]
    assert "课程展示" in system_prompt
    assert "展示日期确认了吗" in system_prompt


@pytest.mark.asyncio
async def test_pre_startup_queued_message_is_memory_only(tmp_path) -> None:
    llm = FakeChatClient()
    sender = FakeOneBotClient()
    memory = ChatMemory(tmp_path / "memory.db")
    shared = SharedMemoryStore(tmp_path / "memory.db")
    app = create_app(
        settings=make_settings(
            tmp_path,
            shared_memory_enabled=True,
        ),
        chat_client=llm,
        onebot_client=sender,
        memory=memory,
        shared_memory_store=shared,
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
                "message_id": 889,
                "time": int(datetime.now(timezone.utc).timestamp()) - 300,
                "message": "offline queued message",
            },
        )
        await asyncio.sleep(0.05)
        assert sender.sent == []
        assert memory.history("private:123", 10) == []
        archived = memory.archived_messages(
            conversation_id="private:123",
            limit=10,
        )
        assert [item["content"] for item in archived] == [
            "offline queued message"
        ]
        observations = shared.observations_since(0, limit=10)
        assert [item["content"] for item in observations] == [
            "offline queued message"
        ]

        await client.post(
            "/onebot",
            headers={"Authorization": "Bearer test-secret"},
            json={
                "post_type": "message",
                "message_type": "private",
                "self_id": 999,
                "user_id": 123,
                "message_id": 890,
                "message": "live message after startup",
            },
        )
        await _wait_for_condition(lambda: bool(sender.sent))

    assert len(sender.sent) == 1
    assert llm.calls[0][-1] == {
        "role": "user",
        "content": "live message after startup",
    }


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
    assert "没有预设的固定爱好" in llm.calls[0][0]["content"]


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
async def test_quoting_another_member_does_not_trigger_group_reply(
    tmp_path,
) -> None:
    llm = FakeChatClient()
    sender = FakeOneBotClient()
    sender.quoted_sender_id = 777
    app = create_app(
        settings=make_settings(
            tmp_path,
            enable_group=True,
            group_require_mention=False,
            group_quote_counts_as_mention=True,
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
                "message_id": 2001,
                "message": [
                    {"type": "reply", "data": {"id": "43"}},
                    {"type": "text", "data": {"text": "我同意他说的"}},
                ],
            },
        )
        await asyncio.sleep(0.1)

    assert sender.sent == []
    assert llm.calls == []


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
async def test_busy_state_queues_private_messages_until_resumed(tmp_path) -> None:
    llm = FakeChatClient()
    sender = FakeOneBotClient()
    app = create_app(
        settings=make_settings(
            tmp_path,
            owner_qq=123,
            presence_enabled=True,
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
                "message_type": "private",
                "self_id": 999,
                "user_id": 123,
                "message_id": 1802,
                "message": "/忙 写作业",
            },
        )
        await _wait_for_condition(lambda: len(sender.sent) == 1)
        await client.post(
            "/onebot",
            headers={"Authorization": "Bearer test-secret"},
            json={
                "post_type": "message",
                "message_type": "private",
                "self_id": 999,
                "user_id": 123,
                "message_id": 1803,
                "message": "在吗",
            },
        )
        await asyncio.sleep(0.1)
        assert llm.calls == []
        await client.post(
            "/onebot",
            headers={"Authorization": "Bearer test-secret"},
            json={
                "post_type": "message",
                "message_type": "private",
                "self_id": 999,
                "user_id": 123,
                "message_id": 1804,
                "message": "/空闲",
            },
        )
        await _wait_for_condition(lambda: len(sender.sent) == 2)

    assert "1 条" in sender.sent[1][1]
    assert "/忙" in sender.sent[0][1] or "不接话" in sender.sent[0][1]


@pytest.mark.asyncio
async def test_no_mention_group_still_ignores_short_acknowledgements(
    tmp_path,
) -> None:
    llm = FakeChatClient()
    sender = FakeOneBotClient()
    app = create_app(
        settings=make_settings(
            tmp_path,
            enable_group=True,
            group_require_mention=False,
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
                "user_id": 123,
                "group_id": 456,
                "message_id": 1801,
                "message": "谢谢",
            },
        )
        await asyncio.sleep(0.1)

    assert sender.sent == []
    assert llm.calls == []


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
        await _wait_for_condition(lambda: len(sender.sent) == 4, timeout=5.0)

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
    assert sender.sent[0][1] in {
        "提醒你一下，该复习了",
        "到点了，该复习了",
        "之前说的，该复习了",
    }


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
        await _wait_for_condition(lambda: bool(sender.sent), timeout=8.0)

    assert sender.poked[0].user_id == 123
    assert sender.poked[0].group_id == 500
    assert sender.sent[0][0].user_id == 123
    assert sender.sent[0][1] in {
        "在呢。",
        "嗯？",
        "干嘛。",
        "来了。",
        "你戳我，有事？",
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
                    {
                        "type": "record",
                        "data": {
                            "file": "voice-2.amr",
                            "file_id": "voice-id-2",
                        },
                    },
                    {"type": "text", "data": {"text": "/转写"}},
                ],
            },
        )
        await _wait_for_condition(lambda: len(sender.sent) == 2)

    assert len(audio_loader.sources) == 2
    assert llm.models == ["qwen3.8-omni-flash"]
    assert llm.extra_payloads[0]["modalities"] == ["text"]
    user_content = llm.calls[0][-1]["content"]
    assert isinstance(user_content, list)
    assert user_content[1]["type"] == "input_audio"
    assert user_content[2]["type"] == "input_audio"
    assert "2 段语音" in user_content[0]["text"]
    assert sender.sent[1][1] == "测试回复"


@pytest.mark.asyncio
async def test_admin_generates_downloadable_voice_file(tmp_path) -> None:
    sender = FakeOneBotClient()
    app = create_app(
        settings=make_settings(
            tmp_path,
            tts_enabled=True,
            generated_media_dir=tmp_path / "generated",
        ),
        chat_client=FakeChatClient(),
        onebot_client=sender,
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
        tts_client=DataTTS(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.post(
            "/api/admin/generate",
            json={"kind": "voice", "prompt": "今天状态不错"},
        )
        assert response.status_code == 200
        result = response.json()
        assert result["kind"] == "audio"
        assert result["name"].endswith(".wav")
        download = await client.get(result["download_url"])
        assert download.status_code == 200
        assert download.content == b"\x00\x00\x00"

        history = await client.get("/api/admin/generated")
        assert history.status_code == 200
        record = history.json()["artifacts"][0]
        assert record["status"] == "ready"
        assert record["source"] == "web"
        assert record["prompt"] == "今天状态不错"

        preview = await client.get(
            f"/api/admin/generated/records/{result['record_id']}/preview"
        )
        assert preview.status_code == 200
        assert preview.json()["preview_kind"] == "audio"

        sent = await client.post(
            f"/api/admin/generated/records/{result['record_id']}/send",
            json={"target_type": "private", "target_id": 123456},
        )
        assert sent.status_code == 200
        assert sender.files[-1][0].user_id == 123456
        assert sender.files[-1][2] == result["name"]

        deleted = await client.delete(
            f"/api/admin/generated/records/{result['record_id']}"
        )
        assert deleted.status_code == 200
        missing = await client.get(result["download_url"])
        assert missing.status_code == 404


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
async def test_vector_recall_shares_facts_but_not_other_chats(
    tmp_path,
) -> None:
    llm = FakeChatClient()
    sender = FakeOneBotClient()
    shared = SharedMemoryStore(tmp_path / "shared.db")
    shared.record_person(222, "小明")
    public_id = shared.add_memory(
        subject_user_id=222,
        subject_name="小明",
        content="下周三要去杭州出差",
        visibility="shareable",
        source_conversation="private:222",
    )
    private_id = shared.add_memory(
        subject_user_id=222,
        subject_name="小明",
        content="最近睡眠不好",
        visibility="private",
        source_conversation="private:222",
    )
    assert public_id and private_id
    vectors = VectorMemoryStore(tmp_path / "vectors.db")
    vectors.upsert_chunks(
        [
            VectorChunk(
                "memory",
                f"memory:{public_id}",
                "小明",
                "长期记忆",
                "下周三要去杭州出差",
            ),
            VectorChunk(
                "memory",
                f"memory:{private_id}",
                "小明",
                "长期记忆",
                "最近睡眠不好",
            ),
            VectorChunk(
                "episode",
                "episode:9",
                "小明 的对话",
                "private:222",
                "小明说周五一个人哭到很晚",
            ),
        ],
        [[1.0, 0.0, 0.0] for _ in range(3)],
        metadata={"user_id": 222, "conversation_id": "private:222"},
    )
    app = create_app(
        settings=make_settings(
            tmp_path,
            shared_memory_enabled=True,
            vector_memory_enabled=True,
            memory_privacy_protection=True,
        ),
        chat_client=llm,
        onebot_client=sender,
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
        productivity_store=ProductivityStore(tmp_path / "tools.db"),
        shared_memory_store=shared,
        embedding_client=FakeEmbeddingClient(),
        vector_store=vectors,
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
                "message_id": 2150,
                "message": "小明最近有什么安排",
                "sender": {"nickname": "小李"},
            },
        )
        await _wait_for_condition(lambda: bool(sender.sent))

    prompt_text = "\n".join(
        str(message.get("content") or "")
        for call in llm.calls
        for message in call
    )
    assert "杭州出差" in prompt_text
    assert "睡眠不好" not in prompt_text
    assert "一个人哭" not in prompt_text


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
    chat_memory = ChatMemory(tmp_path / "memory.db")
    sticker_store = StickerStore(tmp_path / "stickers.db")
    sticker_decider = FakeStickerDecider(1)
    app = create_app(
        settings=make_settings(tmp_path),
        chat_client=llm,
        onebot_client=sender,
        memory=chat_memory,
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
        productivity_store=ProductivityStore(tmp_path / "tools.db"),
        sticker_store=sticker_store,
        sticker_choice_decider=sticker_decider,
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
        await _wait_for_condition(
            lambda: chat_memory.count("private:123") >= 2
        )

    assert all(model == "qwen3.8-flash" for model in llm.models)
    assert all(model != "qwen3-vl-plus" for model in llm.models)
    assert sender.sent == []
    assert sticker_store.count() == 1
    assert sender.stickers[0][1].summary == "奶龙"
    assert "QQ：123" in str(sticker_decider.last_kwargs["contact_context"])
    history = chat_memory.history("private:123", 10)
    assert history[0]["content"] == "[对方发来表情包：奶龙]"
    assert history[1]["content"] == "[我刚刚发了一个表情包：奶龙]"


def test_old_sent_sticker_history_gets_current_visual_context(
    tmp_path,
) -> None:
    sticker_store = StickerStore(tmp_path / "stickers.db")
    sticker_id = sticker_store.record(
        StickerSource(summary="无语", emoji_id="old-sticker")
    )
    assert sticker_id is not None
    sticker_store.update_classification(
        sticker_id,
        category="无语",
        usage_hint="对方说了离谱的话时使用",
        tags=("白眼", "离谱"),
        source="manual",
    )

    history = _enrich_sticker_history(
        [
            {
                "role": "assistant",
                "content": "[我刚刚发了一个表情包：无语]",
            }
        ],
        sticker_store,
    )

    assert "画面/情绪：无语" in history[0]["content"]
    assert "标签：白眼、离谱" in history[0]["content"]
    assert "适合：对方说了离谱的话时使用" in history[0]["content"]


@pytest.mark.asyncio
async def test_emoji_only_private_message_does_not_call_language_model(
    tmp_path,
) -> None:
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
        await client.post(
            "/onebot",
            headers={"Authorization": "Bearer test-secret"},
            json={
                "post_type": "message",
                "message_type": "private",
                "self_id": 999,
                "user_id": 123,
                "message_id": 2303,
                "message": "🤔",
            },
        )
        await _wait_for_condition(lambda: bool(sender.sent))

    assert sender.sent[0][1] in {"？", "咋了。", "怎么了？", "嗯？"}
    assert llm.calls == []


@pytest.mark.asyncio
async def test_question_number_uses_task_mode(tmp_path) -> None:
    llm = FakeChatClient()
    sender = FakeOneBotClient()
    app = create_app(
        settings=make_settings(
            tmp_path,
            llm_model="task-model",
            casual_model="casual-model",
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
                "message_type": "private",
                "self_id": 999,
                "user_id": 123,
                "message_id": 2304,
                "message": "做一下第五题",
            },
        )
        await _wait_for_condition(lambda: bool(llm.calls))

    assert llm.models == ["task-model"]
    assert "任务模式" in llm.calls[0][0]["content"]


@pytest.mark.asyncio
async def test_auto_sticker_respects_per_conversation_cooldown(tmp_path) -> None:
    llm = FakeChatClient()
    sender = FakeOneBotClient()
    sticker_store = StickerStore(tmp_path / "stickers.db")
    sticker_store.record(
        StickerSource(
            summary="笑死",
            emoji_id="sticker-1",
            emoji_package_id="pack-1",
            key="sticker-key",
        )
    )
    decider = FakeStickerDecider(1)
    app = create_app(
        settings=make_settings(
            tmp_path,
            auto_sticker_enabled=True,
            auto_sticker_cooldown_seconds=300,
        ),
        chat_client=llm,
        onebot_client=sender,
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
        sticker_store=sticker_store,
        sticker_choice_decider=decider,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        for message_id, text in ((2301, "笑死"), (2302, "这也太离谱了")):
            before = len(sender.sent)
            await client.post(
                "/onebot",
                headers={"Authorization": "Bearer test-secret"},
                json={
                    "post_type": "message",
                    "message_type": "private",
                    "self_id": 999,
                    "user_id": 123,
                    "message_id": message_id,
                    "message": text,
                },
            )
            await _wait_for_condition(
                lambda: len(sender.sent) > before,
                timeout=5.0,
            )
            await asyncio.sleep(0.05)

    assert decider.calls == 1
    assert len(sender.stickers) == 1


@pytest.mark.asyncio
async def test_casual_reply_can_trigger_contextual_sticker_without_keywords(
    tmp_path,
) -> None:
    sender = FakeOneBotClient()
    chat_memory = ChatMemory(tmp_path / "memory.db")
    sticker_store = StickerStore(tmp_path / "stickers.db")
    sticker_id = sticker_store.record(
        StickerSource(
            summary="安慰",
            emoji_id="sticker-1",
            emoji_package_id="pack-1",
            key="sticker-key",
        )
    )
    assert sticker_id is not None
    sticker_store.update_classification(
        sticker_id,
        category="安慰",
        usage_hint="朋友说累或情绪低落时使用",
        tags=("抱抱", "安慰", "温柔"),
        source="manual",
    )
    decider = FakeStickerDecider(1)
    app = create_app(
        settings=make_settings(
            tmp_path,
            auto_sticker_enabled=True,
            auto_sticker_cooldown_seconds=0,
        ),
        chat_client=FakeChatClient(),
        onebot_client=sender,
        memory=chat_memory,
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
        sticker_store=sticker_store,
        sticker_choice_decider=decider,
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
                "message_id": 2305,
                "message": "最近工作有点累",
            },
        )
        await _wait_for_condition(
            lambda: bool(
                sender.sent
                and sender.stickers
                and any(
                    "我刚刚发了一个表情包：安慰" in item["content"]
                    for item in chat_memory.history("private:123", 10)
                )
            ),
            timeout=5.0,
        )

    assert decider.calls == 1
    assert decider.last_kwargs["explicit_request"] is False
    assert sender.stickers[0][1].summary == "安慰"
    sent_sticker_context = chat_memory.history("private:123", 10)[-1]["content"]
    assert sent_sticker_context.startswith("[我刚刚发了一个表情包：安慰；")
    assert "画面/情绪：安慰" in sent_sticker_context
    assert "标签：抱抱、安慰、温柔" in sent_sticker_context
    assert "适合：朋友说累或情绪低落时使用" in sent_sticker_context


@pytest.mark.asyncio
async def test_current_news_request_enables_web_search(
    tmp_path,
    monkeypatch,
) -> None:
    async def fake_grounded_lookup(*args, **kwargs):
        assert kwargs["model"] == "fake-model"
        return GroundedAnswer(
            answer="最近有一场足球比赛。",
            sources=(
                GroundedSource(
                    "新华网",
                    "https://www.xinhuanet.com/sports/test",
                    "足球比赛",
                ),
                GroundedSource(
                    "人民网",
                    "https://sports.people.com.cn/test",
                    "足球比赛",
                ),
            ),
            verified=True,
        )

    monkeypatch.setattr(
        "qq_ai_bot.app.grounded_lookup",
        fake_grounded_lookup,
    )
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

    assert "足球比赛" in sender.sent[0][1]
    assert "xinhuanet.com" in sender.sent[0][1]
    assert llm.calls == []


@pytest.mark.asyncio
async def test_natural_fact_question_enables_web_search(
    tmp_path,
    monkeypatch,
) -> None:
    calls: list[str] = []

    async def fake_grounded_lookup(*args, **kwargs):
        calls.append(str(kwargs.get("question") or ""))
        return GroundedAnswer(
            answer="王宝强是中国演员、导演。",
            sources=(
                GroundedSource(
                    "百度百科",
                    "https://baike.baidu.com/item/test",
                    "王宝强是中国演员",
                ),
            ),
            verified=True,
        )

    monkeypatch.setattr(
        "qq_ai_bot.app.grounded_lookup",
        fake_grounded_lookup,
    )
    sender = FakeOneBotClient()
    app = create_app(
        settings=make_settings(tmp_path, web_search_enabled=True),
        chat_client=FakeChatClient(),
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
                "message_id": 2402,
                "message": "王宝强是谁",
            },
        )
        await _wait_for_condition(lambda: bool(sender.sent))

    assert calls == ["王宝强是谁"]
    assert "baike.baidu.com" in sender.sent[0][1]


@pytest.mark.asyncio
async def test_song_fact_question_uses_task_and_search(
    tmp_path,
    monkeypatch,
) -> None:
    async def fake_grounded_lookup(*args, **kwargs):
        return GroundedAnswer(
            answer="《连名带姓》由张惠妹演唱。",
            sources=(
                GroundedSource(
                    "百度百科",
                    "https://baike.baidu.com/item/test",
                    "张惠妹演唱",
                ),
                GroundedSource(
                    "维基百科",
                    "https://zh.wikipedia.org/wiki/test",
                    "张惠妹",
                ),
            ),
            verified=True,
        )

    monkeypatch.setattr(
        "qq_ai_bot.app.grounded_lookup",
        fake_grounded_lookup,
    )
    sender = FakeOneBotClient()
    app = create_app(
        settings=make_settings(
            tmp_path,
            llm_model="task-model",
            casual_model="casual-model",
            web_search_enabled=True,
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
                "post_type": "message",
                "message_type": "private",
                "self_id": 999,
                "user_id": 123,
                "message_id": 2401,
                "message": "连名带姓这首歌是谁唱的",
            },
        )
        await _wait_for_condition(lambda: bool(sender.sent))

    assert "张惠妹" in sender.sent[0][1]
    assert "baike.baidu.com" in sender.sent[0][1]
    assert "zh.wikipedia.org" in sender.sent[0][1]


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
async def test_natural_sticker_request_falls_back_when_model_abstains(
    tmp_path,
) -> None:
    sender = FakeOneBotClient()
    chat_memory = ChatMemory(tmp_path / "memory.db")
    sticker_store = StickerStore(tmp_path / "stickers.db")
    sticker_store.record(
        StickerSource(
            summary="无语",
            emoji_id="sticker-1",
            emoji_package_id="pack-1",
            key="sticker-key",
        )
    )
    decider = FakeStickerDecider(None)
    app = create_app(
        settings=make_settings(tmp_path),
        chat_client=FakeChatClient(),
        onebot_client=sender,
        memory=chat_memory,
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
        sticker_store=sticker_store,
        sticker_choice_decider=decider,
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
                "message": "给我发个无语的表情包",
            },
        )
        await _wait_for_condition(
            lambda: bool(
                sender.stickers
                and chat_memory.history("private:123", 5)
            ),
            timeout=5.0,
        )

    assert decider.last_kwargs["explicit_request"] is True
    assert sender.stickers[0][1].summary == "无语"
    assert chat_memory.history("private:123", 5)[-1]["content"] == (
        "[我刚刚发了一个表情包：无语]"
    )


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
async def test_new_sticker_uses_saved_image_and_context_for_vision_classification(
    tmp_path,
) -> None:
    image_buffer = BytesIO()
    Image.new("RGB", (32, 32), (210, 40, 40)).save(image_buffer, format="PNG")
    png_bytes = image_buffer.getvalue()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=png_bytes,
            headers={"content-type": "image/png"},
        )

    asset_store = StickerAssetStore(
        tmp_path / "sticker-assets",
        transport=httpx.MockTransport(handler),
    )
    sticker_store = StickerStore(
        tmp_path / "stickers.db",
        asset_dir=asset_store.asset_dir,
    )
    organizer = RecordingStickerOrganizer()
    app = create_app(
        settings=make_settings(
            tmp_path,
            sticker_download_enabled=True,
        ),
        chat_client=FakeChatClient(),
        onebot_client=FakeOneBotClient(),
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
        sticker_store=sticker_store,
        sticker_asset_store=asset_store,
        sticker_organizer=organizer,
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
                "message_id": 2755,
                "message": [
                    {
                        "type": "image",
                        "data": {
                            "sub_type": "1",
                            "summary": "汗",
                            "url": "https://example.com/sweat.png",
                        },
                    }
                ],
            },
        )
        sticker_id = int(sticker_store.list_recent()[0]["id"])
        await _wait_for_condition(
            lambda: (
                sticker_store.get_metadata(sticker_id) or {}
            ).get("classification_source")
            == "vision",
            timeout=5.0,
        )

    metadata = sticker_store.get_metadata(sticker_id) or {}
    assert metadata["has_local_asset"] is True
    assert metadata["usage_hint"].startswith("朋友遇到离谱")
    assert organizer.calls[0]["summary"] == "汗"
    assert organizer.calls[0]["image_url"].startswith("data:image/jpeg;base64,")
    assert "只发送了一个表情包" in organizer.calls[0]["recent_context"]


@pytest.mark.asyncio
async def test_incoming_sticker_is_saved_locally_and_previewed(
    tmp_path,
) -> None:
    gif_bytes = b"GIF89a" + b"\x00" * 32

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/sticker.gif"
        return httpx.Response(
            200,
            content=gif_bytes,
            headers={"content-type": "image/gif"},
        )

    asset_store = StickerAssetStore(
        tmp_path / "sticker-assets",
        transport=httpx.MockTransport(handler),
    )
    sticker_store = StickerStore(
        tmp_path / "stickers.db",
        asset_dir=asset_store.asset_dir,
    )
    app = create_app(
        settings=make_settings(
            tmp_path,
            sticker_download_enabled=True,
        ),
        chat_client=FakeChatClient(),
        onebot_client=FakeOneBotClient(),
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
        sticker_store=sticker_store,
        sticker_asset_store=asset_store,
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
                "message_id": 2760,
                "message": [
                    {
                        "type": "image",
                        "data": {
                            "sub_type": "1",
                            "summary": "开心",
                            "url": "https://example.com/sticker.gif",
                        },
                    }
                ],
            },
        )
        await _wait_for_condition(
            lambda: bool(
                sticker_store.list_recent()[0].get("has_local_asset")
            )
        )
        sticker_id = int(sticker_store.list_recent()[0]["id"])
        preview = await client.get(
            f"/api/admin/stickers/{sticker_id}/asset"
        )

    assert preview.status_code == 200
    assert preview.content == gif_bytes
    sticker = sticker_store.get_by_id(sticker_id)
    assert sticker is not None
    assert Path(sticker.local_path).is_file()
    assert Path(sticker.local_path).read_bytes() == gif_bytes
    assert sticker.mime_type == "image/gif"


@pytest.mark.asyncio
async def test_existing_sticker_assets_can_be_repaired_from_admin(
    tmp_path,
) -> None:
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=png_bytes,
            headers={"content-type": "image/png"},
        )

    asset_store = StickerAssetStore(
        tmp_path / "assets",
        transport=httpx.MockTransport(handler),
    )
    sticker_store = StickerStore(
        tmp_path / "stickers.db",
        asset_dir=asset_store.asset_dir,
    )
    sticker_store.record(
        StickerSource(
            summary="旧表情",
            image_source="https://example.com/old.png",
        )
    )
    app = create_app(
        settings=make_settings(
            tmp_path,
            sticker_download_enabled=True,
        ),
        chat_client=FakeChatClient(),
        onebot_client=FakeOneBotClient(),
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
        sticker_store=sticker_store,
        sticker_asset_store=asset_store,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.post(
            "/api/admin/stickers/repair-assets"
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["candidates"] == 1
    assert payload["repaired"] == 1
    assert payload["stickers"][0]["has_local_asset"] is True
    assert Path(payload["stickers"][0]["local_path"]).is_file()


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
async def test_qq_web_console_distinguishes_friends_and_group_members(
    tmp_path,
) -> None:
    sender = FakeOneBotClient()
    sender.friends = [
        {
            "user_id": 123,
            "nickname": "Aloha",
            "remark": "阿洛哈",
            "display_name": "阿洛哈",
        }
    ]
    sender.groups = [{"group_id": 456, "group_name": "测试群"}]
    sender.group_members = [
        {
            "user_id": 123,
            "nickname": "Aloha",
            "card": "阿洛哈",
            "display_name": "阿洛哈",
            "role": "member",
        },
        {
            "user_id": 789,
            "nickname": "路人",
            "card": "群友",
            "display_name": "群友",
            "role": "member",
        },
    ]
    sender.private_history = [
        {
            "post_type": "message",
            "message_id": "p2",
            "time": 1_700_000_020,
            "sender": {"user_id": 999, "nickname": "测试机器人"},
            "message": [{"type": "text", "data": {"text": "收到。"}}],
        },
        {
            "post_type": "message",
            "message_id": "p1",
            "time": 1_700_000_010,
            "sender": {"user_id": 123, "nickname": "Aloha"},
            "message": [{"type": "text", "data": {"text": "在吗"}}],
        },
    ]
    sender.group_history = [
        {
            "post_type": "message",
            "message_id": "g1",
            "time": 1_700_000_030,
            "sender": {"user_id": 789, "card": "群友"},
            "message": [{"type": "text", "data": {"text": "群消息"}}],
        }
    ]
    chat_memory = ChatMemory(tmp_path / "memory.db")
    app = create_app(
        settings=make_settings(tmp_path, enable_group=True),
        chat_client=FakeChatClient(),
        onebot_client=sender,
        memory=chat_memory,
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
        productivity_store=ProductivityStore(tmp_path / "tools.db"),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        contacts = await client.get("/api/admin/qq-web/contacts")
        members = await client.get(
            "/api/admin/qq-web/group-members",
            params={"group_id": 456},
        )
        private_history = await client.get(
            "/api/admin/qq-web/history",
            params={"target_type": "private", "target_id": 123},
        )
        group_history = await client.get(
            "/api/admin/qq-web/history",
            params={"target_type": "group", "target_id": 456},
        )
        sent_private = await client.post(
            "/api/admin/qq-web/send",
            json={
                "target_type": "private",
                "target_id": 123,
                "message": "网页私聊消息",
            },
        )
        rejected_member = await client.post(
            "/api/admin/qq-web/send",
            json={
                "target_type": "private",
                "target_id": 789,
                "message": "不应该发出去",
            },
        )
        sent_group = await client.post(
            "/api/admin/qq-web/send",
            json={
                "target_type": "group",
                "target_id": 456,
                "message": "网页群消息",
            },
        )

    assert contacts.status_code == 200
    assert contacts.json()["friends"][0]["can_private_chat"] is True
    assert contacts.json()["groups"][0]["display_name"] == "测试群"
    member_by_id = {
        int(item["user_id"]): item for item in members.json()["members"]
    }
    assert member_by_id[123]["can_private_chat"] is True
    assert member_by_id[789]["can_private_chat"] is False
    assert member_by_id[789]["relationship"] == "group_member"
    assert [item["role"] for item in private_history.json()["messages"]] == [
        "user",
        "assistant",
    ]
    assert group_history.json()["messages"][0]["sender_name"] == "群友"
    assert sent_private.status_code == 200
    assert rejected_member.status_code == 403
    assert sent_group.status_code == 200
    assert sender.sent == [
        (ReplyTarget("private", 123), "网页私聊消息"),
        (ReplyTarget("group", 456, 456), "网页群消息"),
    ]
    assert chat_memory.history("private:123", 3)[-1] == {
        "role": "assistant",
        "content": "网页私聊消息",
    }


@pytest.mark.asyncio
async def test_qq_web_group_mentions_and_forward(tmp_path) -> None:
    sender = FakeOneBotClient()
    sender.friends = [
        {
            "user_id": 123,
            "nickname": "Aloha",
            "display_name": "Aloha",
        }
    ]
    sender.groups = [{"group_id": 456, "group_name": "测试群"}]
    app = create_app(
        settings=make_settings(tmp_path, enable_group=True),
        chat_client=FakeChatClient(),
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
        mentioned = await client.post(
            "/api/admin/qq-web/send",
            json={
                "target_type": "group",
                "target_id": 456,
                "message": "看这里",
                "mentions": [123, 123, 789],
            },
        )
        forwarded = await client.post(
            "/api/admin/qq-web/forward",
            json={
                "targets": [{"target_type": "group", "target_id": 456}],
                "messages": [
                    {
                        "message_id": "source-1",
                        "sender_id": 789,
                        "sender_name": "群友",
                        "content": "原始消息",
                        "attachments": [],
                    }
                ],
            },
        )

        async def reject_direct_forward(
            target: ReplyTarget,
            message_id: int | str,
        ) -> str | None:
            _ = target, message_id
            raise OneBotError("single forward unsupported")

        sender.forward_single_message = reject_direct_forward  # type: ignore[method-assign]
        merged = await client.post(
            "/api/admin/qq-web/forward",
            json={
                "targets": [{"target_type": "group", "target_id": 456}],
                "messages": [
                    {
                        "message_id": "source-2",
                        "sender_id": 789,
                        "sender_name": "群友",
                        "content": "回退消息",
                        "attachments": [],
                    }
                ],
            },
        )

    assert mentioned.status_code == 200
    assert sender.mentions[0] == (
        ReplyTarget("group", 456, 456),
        (123, 123, 789),
    )
    assert forwarded.status_code == 200
    assert forwarded.json()["success_count"] == 1, forwarded.json()["results"]
    assert forwarded.json()["results"][0]["method"] == "single_message"
    assert sender.forwarded_single == [
        (ReplyTarget("group", 456, 456), "source-1")
    ]
    assert merged.status_code == 200
    assert merged.json()["results"][0]["method"] == "merged_nodes"
    assert sender.forwarded_nodes[0][1][0]["data"]["content"] == [
        {"type": "text", "data": {"text": "回退消息"}}
    ]


@pytest.mark.asyncio
async def test_qq_web_history_returns_cached_images_and_stickers(
    tmp_path,
) -> None:
    sender = FakeOneBotClient()
    sender.friends = [
        {
            "user_id": 123,
            "nickname": "Aloha",
            "display_name": "Aloha",
        }
    ]
    image_path = tmp_path / "received.png"
    sticker_path = tmp_path / "sticker.png"
    Image.new("RGB", (4, 4), (40, 120, 220)).save(image_path)
    Image.new("RGB", (8, 8), (220, 80, 60)).save(sticker_path)

    asset_store = StickerAssetStore(tmp_path / "assets")
    sticker_store = StickerStore(
        tmp_path / "stickers.db",
        asset_dir=asset_store.asset_dir,
    )
    sticker_id = sticker_store.record(
        StickerSource(
            summary="无语",
            emoji_id="emoji-1",
            emoji_package_id="pack-1",
            key="sticker-key",
        )
    )
    assert sticker_id is not None
    stored = await asset_store.persist(str(sticker_path))
    sticker_store.attach_asset(
        sticker_id,
        local_path=stored.path,
        sha256=stored.sha256,
        mime_type=stored.mime_type,
    )
    sender.private_history = [
        {
            "post_type": "message",
            "message_id": "image-1",
            "time": 1_700_000_010,
            "sender": {"user_id": 123, "nickname": "Aloha"},
            "message": [
                {
                    "type": "image",
                    "data": {"file": str(image_path)},
                }
            ],
        },
        {
            "post_type": "message_sent",
            "message_id": "sticker-1",
            "time": 1_700_000_020,
            "sender": {"user_id": 999, "nickname": "测试机器人"},
            "message": [
                {
                    "type": "mface",
                    "data": {
                        "summary": "无语",
                        "emoji_id": "emoji-1",
                        "emoji_package_id": "pack-1",
                        "key": "sticker-key",
                    },
                }
            ],
        },
        {
            "post_type": "message",
            "message_id": "sticker-2",
            "time": 1_700_000_030,
            "sender": {"user_id": 123, "nickname": "Aloha"},
            "message": [
                {
                    "type": "image",
                    "data": {
                        "summary": "[动画表情]",
                        "sub_type": 1,
                        "file": str(image_path),
                    },
                }
            ],
        },
        {
            "post_type": "message_sent",
            "message_id": "sticker-3",
            "time": 1_700_000_040,
            "sender": {"user_id": 999, "nickname": "测试机器人"},
            "message": [
                {
                    "type": "image",
                    "data": {
                        "summary": "[动画表情]",
                        "sub_type": 1,
                        "file": str(sticker_path),
                    },
                }
            ],
        },
    ]
    app = create_app(
        settings=make_settings(tmp_path),
        chat_client=FakeChatClient(),
        onebot_client=sender,
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
        sticker_store=sticker_store,
        sticker_asset_store=asset_store,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        history = await client.get(
            "/api/admin/qq-web/history",
            params={"target_type": "private", "target_id": 123},
        )
        assert history.status_code == 200
        payload = history.json()
        image_url = payload["messages"][0]["attachments"][0]["url"]
        sticker_url = payload["messages"][1]["attachments"][0]["url"]
        source_sticker_urls = [
            payload["messages"][2]["attachments"][0]["url"],
            payload["messages"][3]["attachments"][0]["url"],
        ]
        image_preview = await client.get(image_url)
        source_sticker_previews = [
            await client.get(url) for url in source_sticker_urls
        ]

    assert payload["messages"][0]["attachments"] == [
        {
            "type": "image",
            "name": "图片",
            "url": image_url,
            "mime_type": "image/png",
            "size": 0,
        }
    ]
    assert payload["messages"][1]["attachments"][0]["type"] == "sticker"
    assert payload["messages"][1]["attachments"][0]["name"] == "无语"
    assert sticker_url == f"/api/admin/stickers/{sticker_id}/asset"
    assert source_sticker_urls[0] != source_sticker_urls[1]
    assert all(
        url.startswith("/api/admin/qq-web/media/")
        for url in source_sticker_urls
    )
    assert [preview.content for preview in source_sticker_previews] == [
        image_path.read_bytes(),
        sticker_path.read_bytes(),
    ]
    assert str(tmp_path) not in history.text
    assert image_preview.status_code == 200
    assert image_preview.content == image_path.read_bytes()


@pytest.mark.asyncio
async def test_file_without_caption_is_summarized_instead_of_social_ping(
    tmp_path,
) -> None:
    sender = FakeOneBotClient()
    loader = FakeDocumentLoader(
        ParsedDocument("课程笔记.txt", "第一页讲文件解析，第二页讲总结。")
    )
    app = create_app(
        settings=make_settings(tmp_path),
        chat_client=FakeChatClient(),
        onebot_client=sender,
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=loader,
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
                "message_id": 3200,
                "message": [
                    {
                        "type": "file",
                        "data": {
                            "name": "课程笔记.txt",
                            "file": "course-notes.txt",
                            "file_size": 42,
                        },
                    }
                ],
            },
        )
        await _wait_for_condition(lambda: len(sender.sent) >= 2)

    replies = [text for _, text in sender.sent]
    assert replies[0] != "在呢。"
    assert all(text not in {"在呢。", "在，怎么了。"} for text in replies)
    assert loader.sources == ["课程笔记.txt"]
    assert replies[-1] == "测试回复"


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
async def test_group_messages_learn_roles_and_member_relations(tmp_path) -> None:
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
                "post_type": "message",
                "message_type": "group",
                "self_id": 999,
                "group_id": 93010,
                "user_id": 123,
                "message_id": 2960,
                "message": [
                    {"type": "at", "data": {"qq": "456"}},
                    {"type": "text", "data": {"text": " 你觉得呢"}},
                ],
                "sender": {"nickname": "小明", "role": "admin"},
            },
        )
        await asyncio.sleep(0.1)
        details = await client.get(
            "/api/admin/group-profiles/93010/details"
        )

    members = details.json()["members"]
    member = next(item for item in members if item["user_id"] == 123)
    assert member["role"] == "admin"
    assert details.json()["relationships"][0]["mention_count"] == 1


@pytest.mark.asyncio
async def test_group_files_are_available_in_admin_and_chat(tmp_path) -> None:
    sender = FakeOneBotClient()
    sender.group_files = {
        "folders": [
            {
                "folder_id": "folder-1",
                "name": "课程资料",
                "file_count": 1,
            }
        ],
        "files": [
            {
                "file_id": "file-1",
                "name": "讲义.pdf",
                "size": 2048,
                "uploader": "小明",
            }
        ],
    }
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
        listing = await client.get(
            "/api/admin/group-profiles/456/files?folder_id=folder-1"
        )
        assert listing.status_code == 200
        assert listing.json()["files"][0]["name"] == "讲义.pdf"

        await client.post(
            "/onebot",
            headers={"Authorization": "Bearer test-secret"},
            json={
                "post_type": "message",
                "message_type": "group",
                "self_id": 999,
                "user_id": 123,
                "group_id": 456,
                "message_id": 2950,
                "message": "/群文件",
                "sender": {"nickname": "测试用户"},
            },
        )
        await _wait_for_condition(lambda: bool(sender.sent))

    reply = sender.sent[-1][1]
    assert "课程资料" in reply
    assert "讲义.pdf" in reply


@pytest.mark.asyncio
async def test_group_file_can_be_previewed_and_downloaded_safely(
    tmp_path,
) -> None:
    sender = FakeOneBotClient()
    sender.group_file_url = "https://cdn.example.com/temporary/file-1"
    sender.group_file_name = "讲义.pdf"

    async def file_handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == sender.group_file_url
        assert request.headers["range"] == "bytes=0-9"
        return httpx.Response(
            206,
            content=b"%PDF-1.7\n",
            headers={
                "content-type": "application/pdf",
                "content-range": "bytes 0-9/100",
                "accept-ranges": "bytes",
            },
        )

    app = create_app(
        settings=make_settings(
            tmp_path,
            enable_group=True,
            generated_media_dir=tmp_path / "generated",
        ),
        chat_client=FakeChatClient(),
        onebot_client=sender,
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
        group_file_transport=httpx.MockTransport(file_handler),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        preview = await client.get(
            "/api/admin/group-profiles/456/file-content",
            params={
                "file_id": "file-1",
                "busid": "102",
                "name": "讲义.pdf",
            },
            headers={"Range": "bytes=0-9"},
        )
        download = await client.get(
            "/api/admin/group-profiles/456/file-content",
            params={
                "file_id": "file-1",
                "busid": "102",
                "name": "../../危险.pdf",
                "download": "true",
            },
            headers={"Range": "bytes=0-9"},
        )

    assert preview.status_code == 206
    assert preview.content == b"%PDF-1.7\n"
    assert preview.headers["content-type"].startswith("application/pdf")
    assert preview.headers["content-disposition"].startswith("inline;")
    assert "temporary%2Ffile-1" not in preview.text
    assert download.status_code == 206
    assert download.headers["content-disposition"].startswith("attachment;")
    assert "%E5%8D%B1%E9%99%A9.pdf" in download.headers[
        "content-disposition"
    ]


@pytest.mark.asyncio
async def test_group_spreadsheet_preview_extracts_rows_without_persisting(
    tmp_path,
) -> None:
    sender = FakeOneBotClient()
    sender.group_file_url = "https://cdn.example.com/temporary/sheet"
    sender.group_file_name = "成绩表.xlsx"
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "成绩"
    worksheet.append(["姓名", "成绩"])
    worksheet.append(["张三", 90])
    payload = BytesIO()
    workbook.save(payload)
    file_bytes = payload.getvalue()

    async def file_handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == sender.group_file_url
        assert request.headers.get("range") is None
        return httpx.Response(
            200,
            content=file_bytes,
            headers={
                "content-type": (
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet"
                )
            },
        )

    generated_dir = tmp_path / "generated"
    app = create_app(
        settings=make_settings(
            tmp_path,
            enable_group=True,
            generated_media_dir=generated_dir,
        ),
        chat_client=FakeChatClient(),
        onebot_client=sender,
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
        group_file_transport=httpx.MockTransport(file_handler),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        preview = await client.get(
            "/api/admin/group-profiles/456/file-preview",
            params={
                "file_id": "sheet-1",
                "busid": "102",
                "name": "成绩表.xlsx",
            },
        )

    assert preview.status_code == 200
    result = preview.json()
    assert result["preview_kind"] == "spreadsheet"
    assert "姓名 | 成绩" in result["excerpt"]
    assert "张三 | 90" in result["excerpt"]
    assert result["temporary_cache"] is False
    assert not generated_dir.exists() or list(generated_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_generation_workspace_lists_targets_and_local_storage(
    tmp_path,
) -> None:
    shared = SharedMemoryStore(tmp_path / "shared.db")
    shared.record_person(1172134688, "Aloha")
    sender = FakeOneBotClient()
    sender.groups = [{"group_id": 456, "group_name": "测试群"}]
    generated_dir = tmp_path / "generated"
    generated_dir.mkdir()
    (generated_dir / "sample.txt").write_text("sample", encoding="utf-8")
    app = create_app(
        settings=make_settings(
            tmp_path,
            enable_group=True,
            generated_media_dir=generated_dir,
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
        targets = await client.get("/api/admin/generated/targets")
        history = await client.get("/api/admin/generated")

    assert targets.status_code == 200
    assert targets.json()["people"][0]["display_name"] == "Aloha"
    assert any(
        item["group_id"] == 456 and item["name"] == "测试群"
        for item in targets.json()["groups"]
    )
    storage = history.json()["storage"]
    assert storage["path"] == str(generated_dir)
    assert storage["file_count"] == 1
    assert storage["total_bytes"] == 6
    assert storage["group_files_cached"] is False


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
async def test_explicit_web_search_bypasses_group_participation_skip(
    tmp_path,
    monkeypatch,
) -> None:
    async def fake_grounded_lookup(*args, **kwargs):
        return GroundedAnswer(
            answer="第一条新闻。第二条新闻。",
            sources=(
                GroundedSource(
                    "新华网",
                    "https://www.xinhuanet.com/test",
                    "人工智能新闻",
                ),
                GroundedSource(
                    "人民网",
                    "https://www.people.com.cn/test",
                    "人工智能新闻",
                ),
            ),
            verified=True,
        )

    monkeypatch.setattr(
        "qq_ai_bot.app.grounded_lookup",
        fake_grounded_lookup,
    )
    sender = FakeOneBotClient()
    decider = FakeGroupDecider(False)
    app = create_app(
        settings=make_settings(
            tmp_path,
            enable_group=True,
            group_require_mention=False,
            group_smart_participation=True,
            web_search_enabled=True,
        ),
        chat_client=FakeChatClient(),
        onebot_client=sender,
        memory=ChatMemory(tmp_path / "memory.db"),
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
        group_reply_decider=decider,
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
                "message_id": 2920,
                "message": "联网搜索最近的人工智能新闻，整理两条",
                "sender": {"nickname": "测试用户"},
            },
        )
        await _wait_for_condition(lambda: bool(sender.sent))

    assert decider.calls == 0
    assert "第二条新闻" in sender.sent[0][1]


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


@pytest.mark.asyncio
async def test_qq_web_uploads_and_sends_stored_media_by_type(tmp_path) -> None:
    sender = FakeOneBotClient()
    sender.friends = [
        {
            "user_id": 123,
            "nickname": "Aloha",
            "display_name": "Aloha",
        }
    ]
    sticker_asset_store = StickerAssetStore(tmp_path / "sticker-assets")
    sticker_store = StickerStore(
        tmp_path / "stickers.db",
        asset_dir=sticker_asset_store.asset_dir,
    )
    sticker_file = tmp_path / "sticker.png"
    Image.new("RGB", (8, 8), (240, 80, 60)).save(sticker_file)
    sticker_id = sticker_store.record(
        StickerSource(summary="无语", image_source=str(sticker_file))
    )
    assert sticker_id is not None
    stored_sticker = await sticker_asset_store.persist(str(sticker_file))
    sticker_store.attach_asset(
        sticker_id,
        local_path=stored_sticker.path,
        sha256=stored_sticker.sha256,
        mime_type=stored_sticker.mime_type,
    )
    memory = ChatMemory(tmp_path / "memory.db")
    app = create_app(
        settings=make_settings(tmp_path),
        chat_client=FakeChatClient(),
        onebot_client=sender,
        memory=memory,
        image_loader=FakeImageLoader(),
        document_loader=FakeDocumentLoader(),
        productivity_store=ProductivityStore(tmp_path / "tools.db"),
        sticker_store=sticker_store,
        sticker_asset_store=sticker_asset_store,
    )
    transport = httpx.ASGITransport(app=app)

    async def upload(client, name: str, content: bytes, mime_type: str):
        initialized = await client.post(
            "/api/admin/qq-web/uploads/init",
            json={"name": name, "size": len(content), "mime_type": mime_type},
        )
        assert initialized.status_code == 200
        details = initialized.json()
        for index in range(details["total_chunks"]):
            start = index * details["chunk_size"]
            chunk = content[start : start + details["chunk_size"]]
            response = await client.post(
                (
                    f"/api/admin/qq-web/uploads/{details['upload_id']}"
                    f"/chunks/{index}"
                ),
                content=chunk,
                headers={"Content-Type": "application/octet-stream"},
            )
            assert response.status_code == 200
        completed = await client.post(
            f"/api/admin/qq-web/uploads/{details['upload_id']}/complete"
        )
        assert completed.status_code == 200
        return completed.json()["item"]

    image_buffer = BytesIO()
    Image.new("RGB", (12, 12), (30, 120, 220)).save(
        image_buffer,
        format="PNG",
    )
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        image_item = await upload(
            client,
            "截图.png",
            image_buffer.getvalue(),
            "image/png",
        )
        video_item = await upload(
            client,
            "演示.mp4",
            b"fake-mp4-video",
            "video/mp4",
        )
        archive_item = await upload(
            client,
            "资料.zip",
            b"PK\x03\x04fake-zip",
            "application/zip",
        )
        library = await client.get("/api/admin/qq-web/library")
        sent = await client.post(
            "/api/admin/qq-web/send-attachments",
            json={
                "target_type": "private",
                "target_id": 123,
                "message": "这些是附件",
                "attachments": [
                    image_item["library_id"],
                    video_item["library_id"],
                    archive_item["library_id"],
                    f"sticker:{sticker_id}",
                ],
            },
        )
        image_response = await client.get(image_item["url"])

    assert library.status_code == 200
    library_payload = library.json()
    assert {item["source"] for item in library_payload["items"]} == {
        "upload",
        "sticker",
    }
    assert library_payload["counts"]["upload"] == 3
    assert sent.status_code == 200
    assert [item["type"] for item in sent.json()["message"]["attachments"]] == [
        "image",
        "video",
        "file",
        "sticker",
    ]
    assert sender.sent == [(ReplyTarget("private", 123), "这些是附件")]
    assert len(sender.images) == 1
    assert Path(sender.images[0][1]).read_bytes() == image_buffer.getvalue()
    assert len(sender.videos) == 1
    assert Path(sender.videos[0][1]).read_bytes() == b"fake-mp4-video"
    assert len(sender.files) == 1
    assert sender.files[0][2] == "资料.zip"
    assert len(sender.stickers) == 1
    assert image_response.status_code == 200
    assert image_response.content == image_buffer.getvalue()
    archived = memory.archived_messages(conversation_id="private:123")
    assert "截图.png" in archived[-1]["content"]
    assert "资料.zip" in archived[-1]["content"]

