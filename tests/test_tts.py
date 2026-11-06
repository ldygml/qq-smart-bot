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


@pytest.mark.asyncio
async def test_tts_styled_voice_and_rate_payload() -> None:
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"output": {"audio": {"url": "https://example.com/b.wav"}}},
        )

    client = TTSClient(
        compatible_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="test",
        model="tts",
        voice="default",
        transport=httpx.MockTransport(handler),
    )
    try:
        await client.synthesize_styled(
            "你好...",
            voice="calm",
            rate=0.9,
        )
    finally:
        await client.close()
    assert captured["input"]["voice"] == "calm"
    assert captured["input"]["text"] == "你好……"
    assert captured["parameters"]["rate"] == 0.9
