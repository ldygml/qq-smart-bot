from __future__ import annotations

import sqlite3

import pytest

from qq_ai_bot.quality import QualityStore, evaluate_quality


class QualityChat:
    def __init__(self) -> None:
        self.messages = []

    async def chat(self, messages, *, model=None, extra_payload=None) -> str:
        self.messages.append(messages)
        return (
            '{"score":0.42,"issues":["answer_drift","too_long"],'
            '"notes":"只回答用户问的内容并缩短。"}'
        )


def test_quality_store_marks_follow_up_and_correction(tmp_path) -> None:
    store = QualityStore(tmp_path / "bot.db")
    store.record(
        conversation_id="private:1",
        user_id=1,
        group_id=None,
        user_text="问题",
        assistant_text="答非所问的长回复",
        model="model",
    )
    store.mark_follow_up("private:1", "你答非所问了，我说的是另一件事")
    item = store.list_recent()[0]
    assert item["status"] == "corrected"
    assert "user_corrected" in item["issues"]
    second = store.record(
        conversation_id="private:2",
        user_id=2,
        group_id=None,
        user_text="问题",
        assistant_text="语气太凶的回复",
        model="model",
    )
    store.mark_review(
        second,
        score=0.3,
        issues=["overdone_edge"],
        notes="",
    )
    store.mark_follow_up("private:2", "我还有另一个问题")
    reviewed = store.list_recent()[0]
    assert reviewed["status"] == "follow_up"
    assert "overdone_edge" in reviewed["issues"]


def test_quality_store_turns_reviews_into_scoped_coaching(tmp_path) -> None:
    store = QualityStore(tmp_path / "bot.db")
    first = store.record(
        conversation_id="private:1",
        user_id=1,
        group_id=None,
        user_text="你好",
        assistant_text="少来这套。",
        model="model",
    )
    store.mark_review(
        first,
        score=0.3,
        issues=["overdone_edge"],
        notes="普通问候不要冷嘲。",
    )
    group = store.record(
        conversation_id="group:2",
        user_id=1,
        group_id=2,
        user_text="今晚吃什么",
        assistant_text="先说清楚项目目标。",
        model="model",
    )
    store.mark_review(
        group,
        score=0.4,
        issues=["answer_drift"],
        notes="按群里当前话题回答。",
    )

    assert store.coaching_notes(group_id=None) == [
        "正常聊天不要为了显得有性格而冷嘲或攻击。"
    ]
    assert store.coaching_notes(group_id=2) == [
        "先直接回答当前问题，不要绕去别的话题。"
    ]


def test_legacy_reviews_are_cleared_before_style_recoaching(tmp_path) -> None:
    database = tmp_path / "bot.db"
    store = QualityStore(database)
    quality_id = store.record(
        conversation_id="private:1",
        user_id=1,
        group_id=None,
        user_text="连名带姓谁唱的",
        assistant_text="错误回答",
        model="model",
    )
    store.mark_review(
        quality_id,
        score=0.8,
        issues=["answer_drift"],
        notes="错误的事实评审",
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE response_quality
            SET review_version = 1
            WHERE id = ?
            """,
            (quality_id,),
        )
        connection.execute(
            "DELETE FROM quality_meta WHERE key = 'style_review_v2_initialized'"
        )
    reloaded = QualityStore(database)
    item = reloaded.list_recent()[0]
    assert item["score"] is None
    assert item["review_notes"] == ""
    assert item["issues"] == []
    assert reloaded.coaching_notes(group_id=None) == []


def test_low_quality_reply_is_removed_from_model_history(tmp_path) -> None:
    store = QualityStore(tmp_path / "bot.db")
    quality_id = store.record(
        conversation_id="private:1",
        user_id=1,
        group_id=None,
        user_text="你好",
        assistant_text="一个表情顶啥用。",
        model="model",
    )
    store.mark_review(
        quality_id,
        score=0.2,
        issues=["overdone_edge"],
        notes="普通问候不要冷嘲。",
    )
    filtered = store.filter_history(
        [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "一个表情顶啥用。"},
            {"role": "assistant", "content": "你好，咋了？"},
        ]
    )
    assert filtered == [
        {"role": "assistant", "content": "你好，咋了？"},
    ]


def test_unprompted_care_templates_are_removed_with_their_turn(
    tmp_path,
) -> None:
    store = QualityStore(tmp_path / "bot.db")
    history = [
        {"role": "user", "content": "累了？"},
        {
            "role": "assistant",
            "content": "立冬快乐。蓉城今晚有比赛，你自己看。",
        },
        {"role": "user", "content": "比赛"},
        {
            "role": "assistant",
            "content": "比赛挺累的吧？歇会儿，喝口水。",
        },
    ]

    assert store.filter_history(history) == []


@pytest.mark.asyncio
async def test_quality_evaluator_parses_llm_review() -> None:
    chat = QualityChat()
    result = await evaluate_quality(
        chat,
        model="test",
        user_text="问 A",
        assistant_text="回答 B",
    )
    assert result["score"] == 0.42
    assert result["issues"] == ["answer_drift", "too_long"]
    assert "不得判断" in chat.messages[0][0]["content"]
    assert "事实是否正确" in chat.messages[0][0]["content"]
