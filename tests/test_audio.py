from __future__ import annotations

import base64

import pytest

from qq_ai_bot.audio import AudioLoader
from qq_ai_bot.onebot import AudioSource


class Resolver:
    async def get_record(self, *, source: AudioSource) -> dict[str, str]:
        return {"base64": base64.b64encode(b"RIFFtest").decode("ascii")}


@pytest.mark.asyncio
async def test_audio_loader_uses_onebot_base64() -> None:
    loader = AudioLoader()
    try:
        loaded = await loader.load(AudioSource(file_id="voice-id"), Resolver())
    finally:
        await loader.close()
    assert loaded.format == "wav"
    assert loaded.data.startswith("data:audio/wav;base64,")
