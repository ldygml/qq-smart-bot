from __future__ import annotations

import pytest

from qq_ai_bot.shared_memory import (
    SharedMemoryStore,
    extract_memory_candidates,
)


class MemoryChat:
    async def chat(self, messages, *, model=None, extra_payload=None) -> str:
        return (
            '{"memories": ['
            '{"content": "下周三要去杭州出差", "visibility": "shareable"},'
            '{"content": "最近睡眠不好", "visibility": "private"}'
            "]}"
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
