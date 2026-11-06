from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

import httpx


class TTSError(RuntimeError):
    pass


class TTSClient:
    def __init__(
        self,
        *,
        compatible_base_url: str,
        api_key: str,
        model: str,
        voice: str,
        timeout_seconds: float = 60.0,
        trust_env: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parsed = urlsplit(compatible_base_url)
        self._endpoint = (
            urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")
            + "/api/v1/services/audio/tts/SpeechSynthesizer"
        )
        self._model = model
        self._voice = voice
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(timeout_seconds),
            trust_env=trust_env,
            transport=transport,
        )

    async def synthesize(self, text: str) -> str:
        return await self.synthesize_styled(text)

    async def synthesize_styled(
        self,
        text: str,
        *,
        voice: str = "",
        rate: float | None = None,
    ) -> str:
        value = text.strip()
        if not value:
            raise TTSError("要合成的文字为空。")
        value = (
            value.replace("...", "……")
            .replace("。。。", "……")
            .replace("\n\n", "\n")
        )
        input_payload: dict[str, object] = {
            "text": value,
            "voice": voice or self._voice,
            "format": "wav",
            "sample_rate": 24000,
        }
        request_payload: dict[str, object] = {
            "model": self._model,
            "input": input_payload,
        }
        if rate is not None and abs(float(rate) - 1.0) > 0.001:
            request_payload["parameters"] = {"rate": float(rate)}
        try:
            response = await self._client.post(
                self._endpoint,
                json=request_payload,
            )
        except httpx.TimeoutException as exc:
            raise TTSError("语音合成超时，请稍后再试。") from exc
        except httpx.HTTPError as exc:
            raise TTSError("语音合成服务连接失败。") from exc
        if response.is_error:
            raise TTSError(f"语音合成返回 HTTP {response.status_code}。")
        try:
            audio_url = response.json()["output"]["audio"]["url"]
        except (ValueError, KeyError, TypeError) as exc:
            raise TTSError("语音合成返回格式异常。") from exc
        if not isinstance(audio_url, str) or not audio_url:
            raise TTSError("语音合成没有返回音频地址。")
        return audio_url

    async def close(self) -> None:
        await self._client.aclose()
