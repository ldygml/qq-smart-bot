from __future__ import annotations

import pytest

from qq_ai_bot.quality import QualityStore, evaluate_quality


class QualityChat:
    async def chat(self, messages, *, model=None, extra_payload=None) -> str:
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


@pytest.mark.asyncio
async def test_quality_evaluator_parses_llm_review() -> None:
    result = await evaluate_quality(
        QualityChat(),
        model="test",
        user_text="问 A",
        assistant_text="回答 B",
    )
    assert result["score"] == 0.42
    assert result["issues"] == ["answer_drift", "too_long"]
