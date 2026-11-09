from __future__ import annotations

import pytest

from qq_ai_bot.group_policy import (
    GroupMessage,
    LLMGroupReplyDecider,
    obvious_ignore,
)


class DecisionChat:
    def __init__(self, reply: bool) -> None:
        self.reply = reply
        self.prompts: list[str] = []

    async def chat(self, messages, *, model=None, extra_payload=None) -> str:
        self.prompts.append(str(messages[-1]["content"]))
        return (
            '{"reply": true, "confidence": 0.82, "reason": "明确求助"}'
            if self.reply
            else '{"reply": false, "confidence": 0.9, "reason": "群友闲聊"}'
        )


class ChatAdapter:
    def __init__(self, decision_chat: DecisionChat) -> None:
        self.decision_chat = decision_chat

    async def chat(self, messages, *, model=None, extra_payload=None) -> str:
        return await self.decision_chat.chat(
            messages,
            model=model,
            extra_payload=extra_payload,
        )

    async def close(self) -> None:
        return None


class FailingChat:
    async def chat(self, messages, *, model=None, extra_payload=None) -> str:
        raise TimeoutError("model timed out")


@pytest.mark.asyncio
async def test_group_decider_accepts_confident_request() -> None:
    chat = DecisionChat(True)
    decider = LLMGroupReplyDecider(
        ChatAdapter(chat),
        model="test-model",
        min_confidence=0.65,
    )
    result = await decider.should_reply(
        history=[
            GroupMessage("user", "小明", "这个报错怎么解决"),
            GroupMessage("assistant", "给她爱", "把完整日志发一下"),
        ],
        current_sender="小明",
        current_text="日志在下面，帮我看看",
    )
    assert result is True
    assert "足球" in chat.prompts[0]


@pytest.mark.asyncio
async def test_group_decider_keeps_clear_questions_even_if_model_declines() -> None:
    decider = LLMGroupReplyDecider(
        ChatAdapter(DecisionChat(False)),
        model="test-model",
        min_confidence=0.58,
    )
    result = await decider.should_reply(
        history=[],
        current_sender="小明",
        current_text="这个 Python 报错怎么解决，帮我看看",
    )
    assert result is True


@pytest.mark.asyncio
async def test_group_decider_uses_local_question_fallback_on_model_error() -> None:
    decider = LLMGroupReplyDecider(
        ChatAdapter(FailingChat()),
        model="test-model",
        min_confidence=0.58,
    )
    result = await decider.should_reply(
        history=[],
        current_sender="小明",
        current_text="这个配置为什么一直失败？帮我看看",
    )
    assert result is True

    ignored = await decider.should_reply(
        history=[],
        current_sender="小明",
        current_text="哈哈",
    )
    assert ignored is False


def test_obvious_group_acknowledgements_are_ignored() -> None:
    assert obvious_ignore("谢谢") is True
    assert obvious_ignore("哈哈") is True
    assert obvious_ignore("这个报错怎么解决") is False
