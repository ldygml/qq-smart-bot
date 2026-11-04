from __future__ import annotations

import pytest

from qq_ai_bot.shared_memory import (
    SharedMemoryStore,
    _parse_memory_candidates,
    detect_common_terms,
    extract_person_profile,
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


class ProfileChat:
    async def chat(self, messages, *, model=None, extra_payload=None) -> str:
        return (
            '{"style_profile":"喜欢短句和直接回答，偶尔接梗；'
            '不喜欢长篇解释。","relationship_note":"熟悉的普通朋友。"}'
        )


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


def test_memory_conflict_supersedes_old_fact_and_preserves_timeline(tmp_path) -> None:
    store = SharedMemoryStore(tmp_path / "memory.db")
    store.record_person(222, "小明")
    episode_id = store.add_episode(
        conversation_id="private:222",
        message_type="private",
        group_id=None,
        speaker_user_id=222,
        speaker_name="小明",
        user_content="我搬去上海了",
        assistant_content="什么时候的事？",
    )
    old_id = store.add_memory(
        subject_user_id=222,
        subject_name="小明",
        content="住在杭州",
        visibility="shareable",
        source_conversation="private:222",
        category="profile",
        fact_key="居住城市",
    )
    new_id = store.add_memory(
        subject_user_id=222,
        subject_name="小明",
        content="住在上海",
        visibility="shareable",
        source_conversation="private:222",
        category="profile",
        fact_key="居住城市",
        source_episode_id=episode_id,
    )
    assert old_id and new_id
    active = store.list_memories(222)
    assert [item["id"] for item in active] == [new_id]
    timeline = store.memory_timeline(222)
    old = next(item for item in timeline if item["id"] == old_id)
    new = next(item for item in timeline if item["id"] == new_id)
    assert old["status"] == "superseded"
    assert old["superseded_by"] == new_id
    assert new["source_text"] == "我搬去上海了"
    assert store.list_conflicts(user_id=222)[0]["old_content"] == "住在杭州"
    context = store.context_for(
        current_user_id=222,
        current_user_name="小明",
        query="我现在住哪",
    )
    assert "住在上海" in context
    assert "住在杭州" not in context


def test_memory_edit_merge_and_growth_edit_delete(tmp_path) -> None:
    store = SharedMemoryStore(tmp_path / "memory.db")
    store.record_person(222, "小明")
    first = store.add_memory(
        subject_user_id=222,
        subject_name="小明",
        content="喜欢周末跑步",
        visibility="shareable",
        source_conversation="private:222",
    )
    second = store.add_memory(
        subject_user_id=222,
        subject_name="小明",
        content="正在准备半程马拉松",
        visibility="shareable",
        source_conversation="private:222",
    )
    assert first and second
    assert store.update_memory(
        first,
        user_id=222,
        content="喜欢周末慢跑",
        category="preference",
        fact_key="运动习惯",
    )
    merged = store.merge_memories(
        user_id=222,
        primary_memory_id=first,
        secondary_memory_id=second,
    )
    assert merged == first
    assert len(store.list_memories(222)) == 1
    growth_id = store.add_growth(
        aspect="interests",
        content="运动是这个圈子近期的话题",
        visibility="shareable",
    )
    reflection_id = store.add_reflection(
        content="小明可能在准备跑步比赛",
        visibility="shareable",
        confidence=0.6,
    )
    assert growth_id and reflection_id
    assert store.update_growth(
        growth_id,
        aspect="hobbies",
        content="运动是这个圈子近期的主要话题",
        visibility="shareable",
    )
    assert store.update_reflection(
        reflection_id,
        content="小明可能正在系统准备跑步比赛",
        visibility="shareable",
        confidence=0.8,
    )
    assert store.delete_growth(growth_id)
    assert store.delete_reflection(reflection_id)


def test_cross_conversation_episode_is_visible_to_other_contact(tmp_path) -> None:
    store = SharedMemoryStore(tmp_path / "memory.db")
    store.record_person(222, "小明")
    store.add_episode(
        conversation_id="group:100:user:222",
        message_type="group",
        group_id=100,
        speaker_user_id=222,
        speaker_name="小明",
        user_content="我周末去看利物浦的比赛",
        assistant_content="行，别又看完骂中场。",
    )
    context = store.context_for(
        current_user_id=333,
        current_user_name="小李",
        query="另一个群有人说了什么",
        include_private=True,
        current_conversation_id="group:200:user:333",
    )
    assert "我周末去看利物浦的比赛" in context
    assert "别又看完骂中场" in context


def test_unreplied_observation_enters_cross_chat_context(tmp_path) -> None:
    store = SharedMemoryStore(tmp_path / "memory.db")
    store.record_person(222, "小明")
    store.observe_message(
        conversation_id="group:100:user:222",
        message_type="group",
        group_id=100,
        speaker_user_id=222,
        speaker_name="小明",
        content="我下个月要去苏州参加比赛",
    )
    context = store.context_for(
        current_user_id=333,
        current_user_name="小李",
        query="小明下个月有什么安排",
        include_private=True,
        current_conversation_id="private:333",
    )
    assert "苏州参加比赛" in context
    assert "未回复的其他群聊消息" in context


@pytest.mark.asyncio
async def test_person_profile_is_stored_and_used_in_context(tmp_path) -> None:
    store = SharedMemoryStore(tmp_path / "memory.db")
    store.record_person(222, "小明")
    profile = await extract_person_profile(
        ProfileChat(),
        model="test-model",
        display_name="小明",
        current_profile="",
        current_relationship_note="",
        samples=[
            {
                "user_content": "行，别废话，直接说",
                "assistant_content": "好，结论是这个。",
            }
        ],
    )
    assert store.update_person_profile(
        222,
        style_profile=profile.style_profile,
        relationship_note=profile.relationship_note,
    )
    person = store.get_person(222)
    assert person["style_profile"].startswith("喜欢短句")
    context = store.context_for(
        current_user_id=222,
        current_user_name="小明",
        query="说重点",
    )
    assert "当前联系人互动画像" in context
    assert "熟悉的普通朋友" in context


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
