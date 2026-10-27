from __future__ import annotations

import json

import httpx
import pytest

from qq_ai_bot.llm import CompatibleChatClient


@pytest.mark.asyncio
async def test_chat_omits_max_tokens_when_unlimited() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "ok",
                        }
                    }
                ]
            },
        )

    client = CompatibleChatClient(
        base_url="https://example.com/v1",
        model="test-model",
        api_key="test-key",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert await client.chat([{"role": "user", "content": "hello"}]) == "ok"
    finally:
        await client.close()

    assert "max_tokens" not in captured
