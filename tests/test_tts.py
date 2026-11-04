from __future__ import annotations

import json

import httpx
import pytest

from qq_ai_bot.tts import TTSClient


@pytest.mark.asyncio
async def test_tts_returns_audio_url() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "qwen-audio-3.1-tts-flash"
        assert body["input"]["text"] == "你好"
        return httpx.Response(
            200,
            json={"output": {"audio": {"url": "https://example.com/a.wav"}}},
        )

    client = TTSClient(
        compatible_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="test",
        model="qwen-audio-3.1-tts-flash",
        voice="longanhuan_v3.1",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert await client.synthesize("你好") == "https://example.com/a.wav"
    finally:
        await client.close()
