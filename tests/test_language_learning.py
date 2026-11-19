from __future__ import annotations

import pytest

from qq_ai_bot.language_learning import (
    ExpressionCandidate,
    LanguageLearningStore,
    LanguageProfile,
    extract_trending_expressions,
    learn_language_batch,
)


class JsonChat:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.messages = []

    async def chat(self, messages, *, model=None, extra_payload=None) -> str:
        self.messages.append(messages)
        return self.reply

    async def close(self) -> None:
        return None


def test_language_store_learns_scoped_style_and_deduplicates_phrases(tmp_path) -> None:
    store = LanguageLearningStore(tmp_path / "language.db")
    observation_id = store.observe(
        scope_type="group",
        scope_id=123,
        scope_name="测试群",
        speaker_user_id=9,
        speaker_name="小明",
        content="这也太离谱了，我直接绷不住",
    )
    assert observation_id
    store.upsert_profile(
        scope_type="group",
        scope_id=123,
        scope_name="测试群",
        profile=LanguageProfile(
            summary="大家说话短，接梗快",
            rhythm="通常先回一句再补充",
            vocabulary="会让判断更口语化",
            avoid="不要连续追问",
        ),
        evidence_count=12,
    )
    candidate = ExpressionCandidate(
        phrase="绷不住",
        meaning="忍不住笑或无语",
        usage="轻松吐槽时",
        tone="口语、轻微夸张",
        confidence=0.8,
    )
    first = store.upsert_expression(
        scope_type="group",
        scope_id=123,
        scope_name="测试群",
        kind="slang",
        candidate=candidate,
        ttl_days=30,
    )
    second = store.upsert_expression(
        scope_type="group",
        scope_id=123,
        scope_name="测试群",
        kind="slang",
        candidate=candidate,
        ttl_days=30,
    )
    assert first == second
    expression = store.expressions(scope_type="group", scope_id=123)[0]
    assert expression["seen_count"] == 2

    context = store.context_for(
        scope_type="group",
        scope_id=123,
        query="这个真绷不住",
    )
    assert "先回一句再补充" in context
    assert "绷不住" in context
    assert "最多顺手用一个" in context

    store.update_expression(first, status="blocked")
    assert "绷不住" not in store.context_for(
        scope_type="group",
        scope_id=123,
        query="这个真绷不住",
    )


def test_language_store_can_forget_one_scene(tmp_path) -> None:
    store = LanguageLearningStore(tmp_path / "language.db")
    store.observe(
        scope_type="contact",
        scope_id=88,
        scope_name="阿洛",
        speaker_user_id=88,
        speaker_name="阿洛",
        content="今天先把手头这个搞完",
    )
    store.upsert_profile(
        scope_type="contact",
        scope_id=88,
        scope_name="阿洛",
        profile=LanguageProfile(summary="句子短"),
        evidence_count=1,
    )
    deleted = store.delete_scope("contact", 88)
    assert deleted == {"observations": 1, "expressions": 0, "profiles": 1}
    assert store.profile("contact", 88) is None


@pytest.mark.asyncio
async def test_chat_learning_parses_style_and_rejects_unsafe_phrase() -> None:
    chat = JsonChat(
        '{"profile":{"summary":"消息短，接话快","rhythm":"一两句",'
        '"vocabulary":"口语化","avoid":"客服腔"},'
        '"expressions":['
        '{"phrase":"有点东西","meaning":"有点厉害","usage":"认可别人时",'
        '"tone":"轻松","confidence":0.8},'
        '{"phrase":"傻逼玩意","meaning":"骂人","usage":"任何场景",'
        '"tone":"攻击","confidence":0.9}]}'
    )
    result = await learn_language_batch(
        chat,
        model="test",
        scope_label="群聊 测试群",
        observations=[
            {"speaker_name": "甲", "content": "这个有点东西"},
            {"speaker_name": "乙", "content": "确实，接得挺快"},
        ],
    )
    assert result.profile.rhythm == "一两句"
    assert [item.phrase for item in result.expressions] == ["有点东西"]


@pytest.mark.asyncio
async def test_web_terms_must_appear_in_research_and_keep_source() -> None:
    chat = JsonChat(
        '{"expressions":['
        '{"phrase":"已老实","meaning":"认输或服气","usage":"开玩笑认输时",'
        '"tone":"自嘲","confidence":0.7,"source_index":1},'
        '{"phrase":"资料里没有的词","meaning":"不应入库","usage":"无",'
        '"tone":"", "confidence":0.9,"source_index":1}]}'
    )
    result = await extract_trending_expressions(
        chat,
        model="test",
        research="近期有人用“已老实”表达认输、服气或不再争论。",
        sources=[
            {
                "title": "网络热梗整理",
                "url": "https://example.com/slang",
            }
        ],
        current_date="2026-11-19",
    )
    assert len(result) == 1
    assert result[0].phrase == "已老实"
    assert result[0].source_title == "网络热梗整理"
    assert result[0].source_kind == "web"
