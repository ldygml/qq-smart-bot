from __future__ import annotations

import pytest

from qq_ai_bot.sticker_policy import LLMStickerChoiceDecider, LLMStickerOrganizer


class ChoiceChat:
    async def chat(self, messages, *, model=None, extra_payload=None) -> str:
        return '{"sticker_id": 2, "confidence": 0.86}'

    async def close(self) -> None:
        return None


class LowConfidenceChoiceChat:
    async def chat(self, messages, *, model=None, extra_payload=None) -> str:
        return '{"sticker_id": 1, "confidence": 0.2}'

    async def close(self) -> None:
        return None


class OrganizerChat:
    async def chat(self, messages, *, model=None, extra_payload=None) -> str:
        prompt = messages[-1]["content"][0]["text"]
        assert "三部分" in prompt
        return (
            '{"category":"无语","usage_hint":"朋友吐槽离谱事情时用；'
            '适合熟人轻松聊天，严肃求助时不用",'
            '"tags":["汗","无语","尴尬"]}'
        )

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


@pytest.mark.asyncio
async def test_explicit_sticker_request_accepts_valid_choice_below_threshold() -> None:
    decider = LLMStickerChoiceDecider(
        LowConfidenceChoiceChat(),
        model="test-model",
        min_confidence=0.7,
    )
    candidates = [{"id": 1, "summary": "无语", "use_count": 0}]
    explicit = await decider.choose(
        history=[],
        user_text="来个表情包",
        assistant_text="",
        candidates=candidates,
        explicit_request=True,
    )
    automatic = await decider.choose(
        history=[],
        user_text="今天有点累",
        assistant_text="先歇会儿",
        candidates=candidates,
        explicit_request=False,
    )
    assert explicit == 1
    assert automatic is None


@pytest.mark.asyncio
async def test_sticker_organizer_uses_vision_when_image_is_available() -> None:
    organizer = LLMStickerOrganizer(
        OrganizerChat(),
        model="text-model",
        vision_model="vision-model",
    )
    classification = await organizer.classify(
        summary="汗",
        recent_context="朋友：这也太离谱了",
        image_url="data:image/jpeg;base64,AAAA",
    )
    assert classification.category == "无语"
    assert "严肃求助时不用" in classification.usage_hint
    assert classification.source == "vision"
