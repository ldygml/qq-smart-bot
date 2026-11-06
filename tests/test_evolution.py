from __future__ import annotations

import sqlite3

import pytest

from qq_ai_bot.group_profiles import GroupProfileStore
from qq_ai_bot.presence import PresenceStore
from qq_ai_bot.quality import (
    QualityStore,
    compare_replies,
    reply_scope_key,
)
from qq_ai_bot.shared_memory import SharedMemoryStore
from qq_ai_bot.topics import (
    TopicCandidate,
    TopicStore,
    infer_topics,
)


class JsonChat:
    def __init__(self, reply: str) -> None:
        self.reply = reply

    async def chat(self, messages, *, model=None, extra_payload=None) -> str:
        return self.reply

    async def close(self) -> None:
        return None


def test_contact_style_learning_and_gray_rollout(tmp_path) -> None:
    store = QualityStore(tmp_path / "quality.db")
    assert store.observe_style_request(
        user_id=10,
        group_id=None,
        text="以后说短点，也别这么凶",
    )
    policy = store.contact_policy(user_id=10, group_id=None)
    assert any("短、直接" in item for item in policy["directives"])
    assert any("降低攻击性" in item for item in policy["directives"])

    for _ in range(6):
        store.record_experiment(
            quality_id=None,
            user_id=10,
            group_id=None,
            challenger_name="warm_concise",
            baseline_reply="你应该先反思一下。",
            challenger_reply="先缓缓，别急着逼自己。",
            winner="challenger",
            baseline_score=0.35,
            challenger_score=0.82,
        )
    rollout = store.adaptive_style(user_id=10, group_id=None)
    assert rollout is not None
    assert rollout["challenger_name"] == "warm_concise"
    assert 0.25 <= rollout["weight"] <= 0.75
    assert policy["scope_key"] == reply_scope_key(10, None)

    quality_id = store.record(
        conversation_id="private:10",
        user_id=10,
        group_id=None,
        user_text="你好",
        assistant_text="行吧。",
        model="test",
    )
    store.record_experiment(
        quality_id=quality_id,
        user_id=10,
        group_id=None,
        challenger_name="warm_concise",
        baseline_reply="行吧。",
        challenger_reply="你好，怎么了？",
        winner="challenger",
        baseline_score=0.4,
        challenger_score=0.8,
    )
    store.mark_follow_up("private:10", "你答非所问了")
    experiment = store.adaptive_snapshot()["experiments"][0]
    assert experiment["observed_outcome"] == "corrected"


@pytest.mark.asyncio
async def test_pairwise_reply_comparison() -> None:
    chat = JsonChat(
        '{"winner":"challenger","baseline_score":0.3,'
        '"challenger_score":0.8,"reason":"B 更自然"}'
    )
    result = await compare_replies(
        chat,
        model="test",
        user_text="今天有点累",
        baseline="你要调整时间管理。",
        challenger="那就先歇会儿，别硬撑。",
    )
    assert result["winner"] == "challenger"
    assert result["challenger_score"] == 0.8


def test_presence_busy_quiet_and_backlog(tmp_path) -> None:
    store = PresenceStore(tmp_path / "presence.db")
    assert store.is_quiet("global", "Asia/Shanghai") is False
    store.set_busy("global", busy=True, reason="开会")
    assert store.is_quiet("global", "Asia/Shanghai") is True
    store.set_busy("global", busy=False)
    store.set_quiet_hours(
        "global",
        start="00:00",
        end="23:59",
    )
    assert store.is_quiet("global", "Asia/Shanghai") is True
    store.queue_message(
        scope_key="global",
        user_id=1,
        group_id=None,
        message_type="private",
        content="晚点回我",
    )
    assert store.backlog(scope_key="global")[0]["content"] == "晚点回我"
    assert store.clear_backlog("global") == 1


def test_topic_store_tracks_waiting_and_resolves(tmp_path) -> None:
    store = TopicStore(tmp_path / "topics.db")
    topic_id = store.upsert(
        scope_key="user:1",
        candidate=TopicCandidate(
            title="课程项目",
            summary="正在准备下周展示",
            status="waiting",
            pending_question="展示日期确认了吗？",
            next_action="等待对方确认日期",
        ),
        user_id=1,
        group_id=None,
    )
    context = store.context("user:1")
    assert "课程项目" in context
    assert "展示日期确认了吗" in context
    assert store.resolve(topic_id)
    assert store.active("user:1") == []


def test_group_member_roles_and_relationship_graph(tmp_path) -> None:
    store = GroupProfileStore(tmp_path / "groups.db")
    store.save_member_preference(
        10,
        20,
        nickname="管理员",
        notes="",
        no_jokes=False,
        role="admin",
    )
    store.touch_member(
        10,
        21,
        nickname="小明",
        role="member",
    )
    store.record_relation(10, 21, 20, kind="mention")
    store.record_relation(10, 21, 20, kind="reply")
    store.record_relation(10, 21, 20, kind="poke")
    member = store.member_preference(10, 20)
    assert member is not None and member["role"] == "admin"
    context = store.relationship_context(10, 21)
    assert "管理员" in context
    assert "提及 1 次" in context
    assert store.relationships(10)[0]["poke_count"] == 1


def test_old_memories_decay_and_are_marked_uncertain(tmp_path) -> None:
    database = tmp_path / "memory.db"
    store = SharedMemoryStore(database)
    memory_id = store.add_memory(
        subject_user_id=1,
        subject_name="小明",
        content="很久以前喜欢的课程",
        visibility="shareable",
        source_conversation="private:1",
        category="preference",
        confidence=0.7,
    )
    assert memory_id is not None
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE shared_memories SET updated_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00+00:00", memory_id),
        )
    context = store.context_for(
        current_user_id=1,
        current_user_name="小明",
        query="课程",
        limit=10,
    )
    assert "可靠度" in context
    assert "旧信息" in context


@pytest.mark.asyncio
async def test_topic_inference_parses_json() -> None:
    chat = JsonChat(
        '{"topics":[{"title":"高数复习","summary":"计划周五前复习完",'
        '"status":"active","pending_question":"",'
        '"next_action":"周五前提醒进度","follow_up_days":3}]}'
    )
    result = await infer_topics(
        chat,
        model="test",
        user_message="我准备周五前复习完高数",
        assistant_reply="行，那这几天得安排一下。",
        existing_topics=[],
    )
    assert result[0].title == "高数复习"
    assert result[0].follow_up_days == 3
