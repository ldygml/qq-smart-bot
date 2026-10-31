from __future__ import annotations

import pytest

from qq_ai_bot.shared_memory import (
    SharedMemoryStore,
    _parse_memory_candidates,
    detect_common_terms,
    extract_memory_candidates,
    reflect_on_memories,
)


class MemoryChat:
    async def chat(self, messages, *, model=None, extra_payload=None) -> str:
        return (
            '{"memories": ['
            '{"content": "下周三要去杭州出差", "visibility": "shareable"},'
            '{"content": "最近睡眠不好", "visibility": "private"}'
            "]}"
        )


class ReflectionChat:
    async def chat(self, messages, *, model=None, extra_payload=None) -> str:
        return (
            '{"reflections": ['
            '{"content": "小明和小李可能都在准备杭州项目",'
            '"visibility": "shareable", "confidence": 0.7},'
            '{"content": "小明最近状态不好",'
            '"visibility": "private", "confidence": 0.6}'
            '], "growth": ['
            '{"aspect": "interests", "content": "这个圈子经常讨论杭州项目",'
            '"visibility": "shareable"}'
            "]}"
        )


class EmptyReflectionChat:
    async def chat(self, messages, *, model=None, extra_payload=None) -> str:
        return '{"reflections":[],"growth":[]}'


def test_shared_context_respects_visibility(tmp_path) -> None:
    store = SharedMemoryStore(tmp_path / "memory.db")
    store.record_person(222, "小明")
    store.add_memory(
        subject_user_id=222,
        subject_name="小明",
        content="下周三要去杭州出差",
        visibility="shareable",
        source_conversation="private:222",
    )
    store.add_memory(
        subject_user_id=222,
        subject_name="小明",
        content="最近睡眠不好",
        visibility="private",
        source_conversation="private:222",
    )

    other_context = store.context_for(
        current_user_id=333,
        current_user_name="小李",
        query="小明最近有什么安排",
    )
    assert "杭州出差" in other_context
    assert "睡眠不好" not in other_context

    own_context = store.context_for(
        current_user_id=222,
        current_user_name="小明",
        query="最近怎么样",
    )
    assert "睡眠不好" in own_context

    unrestricted_context = store.context_for(
        current_user_id=333,
        current_user_name="小李",
        query="小明最近有什么安排",
        include_private=True,
    )
    assert "睡眠不好" in unrestricted_context


def test_memory_parser_accepts_top_level_array() -> None:
    memories = _parse_memory_candidates(
        '[{"fact":"长期在北京工作","visibility":"shareable"},'
        '{"memory":"不喝咖啡"}]'
    )
    assert [item.content for item in memories] == ["长期在北京工作", "不喝咖啡"]


@pytest.mark.asyncio
async def test_memory_extraction_parses_json() -> None:
    memories = await extract_memory_candidates(
        MemoryChat(),
        model="test-model",
        speaker_name="小明",
        speaker_id=222,
        user_message="我下周三要去杭州出差，最近睡眠不好",
    )
    assert len(memories) == 2
    assert memories[0].content == "下周三要去杭州出差"
    assert memories[0].visibility == "shareable"
    assert memories[1].visibility == "private"


def test_forget_user_removes_all_personal_memories(tmp_path) -> None:
    store = SharedMemoryStore(tmp_path / "memory.db")
    store.record_person(222, "小明")
    store.add_memory(
        subject_user_id=222,
        subject_name="小明",
        content="喜欢打羽毛球",
        visibility="shareable",
        source_conversation="private:222",
    )
    assert store.forget_user(222) == 1
    assert store.list_memories(222) == []


@pytest.mark.asyncio
async def test_reflection_discovers_shareable_associations() -> None:
    memories = [
        {
            "subject_user_id": 222,
            "subject_name": "小明",
            "content": "下周三去杭州做项目",
            "visibility": "shareable",
        },
        {
            "subject_user_id": 333,
            "subject_name": "小李",
            "content": "最近在准备杭州的新项目",
            "visibility": "shareable",
        },
    ]
    result = await reflect_on_memories(
        ReflectionChat(),
        model="test-model",
        memories=memories,
        existing_growth=[],
    )
    assert len(result.reflections) == 2
    assert result.reflections[0].confidence == 0.7
    assert result.reflections[1].visibility == "private"
    assert result.growth[0].aspect == "interests"


def test_private_reflections_are_not_cross_visible(tmp_path) -> None:
    store = SharedMemoryStore(tmp_path / "memory.db")
    store.record_person(222, "小明")
    store.add_memory(
        subject_user_id=222,
        subject_name="小明",
        content="下周三去杭州",
        visibility="shareable",
        source_conversation="private:222",
    )
    store.add_reflection(
        content="小明可能和小李在合作",
        visibility="shareable",
        confidence=0.7,
    )
    store.add_reflection(
        content="小明最近状态不好",
        visibility="private",
        confidence=0.6,
    )
    store.add_growth(
        aspect="interests",
        content="大家经常讨论杭州项目",
        visibility="shareable",
    )
    context = store.context_for(
        current_user_id=333,
        current_user_name="小李",
        query="小明最近在忙什么",
    )
    assert "可能和小李在合作" in context
    assert "最近状态不好" not in context
    assert "经常讨论杭州项目" in context
    assert "成长笔记" in context


@pytest.mark.asyncio
async def test_reflection_uses_grounded_fallback_for_common_terms() -> None:
    memories = [
        {
            "subject_user_id": 201,
            "subject_name": "小明",
            "content": "下周三去杭州出差",
            "visibility": "shareable",
        },
        {
            "subject_user_id": 202,
            "subject_name": "小李",
            "content": "正在准备杭州项目",
            "visibility": "shareable",
        },
        {
            "subject_user_id": 203,
            "subject_name": "小王",
            "content": "问过杭州项目开始时间",
            "visibility": "shareable",
        },
    ]
    assert "杭州" in detect_common_terms(memories)
    result = await reflect_on_memories(
        EmptyReflectionChat(),
        model="test-model",
        memories=memories,
        existing_growth=[],
    )
    assert result.reflections
    assert "杭州" in result.reflections[0].content
    assert result.growth
