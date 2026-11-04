from __future__ import annotations

import pytest

from qq_ai_bot.sticker_policy import LLMStickerChoiceDecider


class ChoiceChat:
    async def chat(self, messages, *, model=None, extra_payload=None) -> str:
        return '{"sticker_id": 2, "confidence": 0.86}'

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_sticker_decider_chooses_matching_catalog_item() -> None:
    decider = LLMStickerChoiceDecider(
        ChoiceChat(),
        model="test-model",
        min_confidence=0.7,
    )
    choice = await decider.choose(
        history=[{"role": "user", "content": "这也太离谱了"}],
        user_text="发个表情包",
        assistant_text="确实离谱。",
        candidates=[
            {"id": 1, "summary": "开心", "use_count": 0},
            {"id": 2, "summary": "无语", "use_count": 2},
        ],
        explicit_request=True,
    )
    assert choice == 2
