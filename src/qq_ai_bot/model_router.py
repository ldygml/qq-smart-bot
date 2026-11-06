from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .llm import ChatClient, CompatibleChatClient, LLMError
from .tts import TTSClient, TTSError
from .usage import UsageStore
from .vector_memory import EmbeddingClient


CAPABILITIES = (
    "chat",
    "code",
    "vision",
    "ocr",
    "audio",
    "tts",
    "embedding",
    "reflection",
)


@dataclass(frozen=True)
class RouteTarget:
    base_url: str
    api_key: str = field(repr=False)
    model: str
    timeout_seconds: float = 90.0
    trust_env: bool = False
    voice: str = ""
    requests_per_minute: int = 60

    @property
    def key(self) -> tuple[str, str, float, bool]:
        return (
            self.base_url.rstrip("/"),
            self.model,
            self.timeout_seconds,
            self.trust_env,
        )


def parse_model_routes(raw: str) -> dict[str, list[RouteTarget]]:
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    result: dict[str, list[RouteTarget]] = {}
    for capability in CAPABILITIES:
        items = data.get(capability)
        if not isinstance(items, list):
            continue
        targets: list[RouteTarget] = []
        for item in items[:8]:
            if not isinstance(item, dict):
                continue
            base_url = str(item.get("base_url") or "").strip()
            model = str(item.get("model") or "").strip()
            if not base_url or not model:
                continue
            targets.append(
                RouteTarget(
                    base_url=base_url,
                    api_key=str(item.get("api_key") or ""),
                    model=model,
                    timeout_seconds=float(
                        item.get("timeout_seconds") or 90
                    ),
                    trust_env=bool(item.get("trust_env", False)),
                    voice=str(item.get("voice") or ""),
                    requests_per_minute=max(
                        1,
                        int(item.get("requests_per_minute") or 60),
                    ),
                )
            )
        if targets:
            result[capability] = targets
    return result


class FailoverChatClient:
    def __init__(
        self,
        targets: list[RouteTarget],
        *,
        capability: str,
        usage: UsageStore | None = None,
    ) -> None:
        self.targets = targets
        self.capability = capability
        self.usage = usage
        self._clients: dict[tuple[str, str, float, bool], ChatClient] = {}
        self._failures: dict[tuple[str, str, float, bool], int] = {}
        self._open_until: dict[tuple[str, str, float, bool], float] = {}
        self._request_windows: dict[
            tuple[str, str, float, bool],
            deque[float],
        ] = {}

    async def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        model: str | None = None,
        extra_payload: Mapping[str, Any] | None = None,
    ) -> str:
        errors: list[str] = []
        now = time.monotonic()
        payload = dict(extra_payload or {})
        allow_model_override = bool(
            payload.pop("_allow_model_override", False)
        )
        for target in self.targets:
            if self._open_until.get(target.key, 0.0) > now:
                continue
            window = self._request_windows.setdefault(
                target.key,
                deque(maxlen=target.requests_per_minute),
            )
            while window and now - window[0] >= 60:
                window.popleft()
            if len(window) >= target.requests_per_minute:
                continue
            window.append(now)
            client = self._clients.get(target.key)
            if client is None:
                client = CompatibleChatClient(
                    base_url=target.base_url,
                    model=target.model,
                    api_key=target.api_key,
                    timeout_seconds=target.timeout_seconds,
                    trust_env=target.trust_env,
                )
                self._clients[target.key] = client
            started = time.perf_counter()
            try:
                reply = await client.chat(
                    messages,
                    model=(
                        model
                        if allow_model_override and model
                        else target.model
                    ),
                    extra_payload=payload or None,
                )
            except Exception as exc:
                errors.append(f"{target.model}: {exc}")
                count = self._failures.get(target.key, 0) + 1
                self._failures[target.key] = count
                if count >= 2:
                    self._open_until[target.key] = now + 30.0
                self._record(
                    target,
                    started,
                    messages,
                    "",
                    False,
                    str(exc),
                )
                continue
            self._failures[target.key] = 0
            self._open_until.pop(target.key, None)
            self._record(
                target,
                started,
                messages,
                reply,
                True,
                "",
            )
            return reply
        summary = "；".join(errors[-3:])
        raise LLMError(
            "所有模型路由均不可用"
            + (f"：{summary}" if summary else "，请稍后重试。")
        )

    def _record(
        self,
        target: RouteTarget,
        started: float,
        messages: Sequence[dict[str, Any]],
        reply: str,
        success: bool,
        error: str,
    ) -> None:
        if self.usage is None:
            return
        input_chars = len(
            json.dumps(list(messages), ensure_ascii=False, default=str)
        )
        self.usage.record(
            operation=self.capability,
            model=target.model,
            duration_ms=(time.perf_counter() - started) * 1000,
            input_chars=input_chars,
            output_chars=len(reply),
            success=success,
            error=error,
        )

    async def close(self) -> None:
        for client in self._clients.values():
            await client.close()
        self._clients.clear()


class FailoverEmbeddingClient:
    def __init__(self, targets: list[RouteTarget]) -> None:
        self.targets = targets
        self._clients: dict[tuple[str, str, float, bool], EmbeddingClient] = {}
        self._failures: dict[tuple[str, str, float, bool], int] = {}
        self._open_until: dict[tuple[str, str, float, bool], float] = {}

    async def embed(self, texts: list[str]) -> list[list[float]]:
        now = time.monotonic()
        errors: list[str] = []
        for target in self.targets:
            if self._open_until.get(target.key, 0.0) > now:
                continue
            client = self._clients.get(target.key)
            if client is None:
                client = EmbeddingClient(
                    base_url=target.base_url,
                    api_key=target.api_key,
                    model=target.model,
                    timeout_seconds=target.timeout_seconds,
                    trust_env=target.trust_env,
                )
                self._clients[target.key] = client
            try:
                result = await client.embed(texts)
            except Exception as exc:
                errors.append(f"{target.model}: {exc}")
                count = self._failures.get(target.key, 0) + 1
                self._failures[target.key] = count
                if count >= 2:
                    self._open_until[target.key] = now + 30.0
                continue
            self._failures[target.key] = 0
            self._open_until.pop(target.key, None)
            return result
        raise LLMError(
            "所有向量模型均不可用"
            + (f"：{errors[-1]}" if errors else "。")
        )

    async def close(self) -> None:
        for client in self._clients.values():
            await client.close()
        self._clients.clear()


class FailoverTTSClient:
    def __init__(
        self,
        targets: list[RouteTarget],
        *,
        default_voice: str,
    ) -> None:
        self.targets = targets
        self.default_voice = default_voice
        self._clients: dict[tuple[str, str, float, bool], TTSClient] = {}
        self._failures: dict[tuple[str, str, float, bool], int] = {}
        self._open_until: dict[tuple[str, str, float, bool], float] = {}

    async def synthesize(self, text: str) -> str:
        return await self.synthesize_styled(text)

    async def synthesize_styled(
        self,
        text: str,
        *,
        voice: str = "",
        rate: float | None = None,
    ) -> str:
        now = time.monotonic()
        errors: list[str] = []
        for target in self.targets:
            if self._open_until.get(target.key, 0.0) > now:
                continue
            client = self._clients.get(target.key)
            if client is None:
                client = TTSClient(
                    compatible_base_url=target.base_url,
                    api_key=target.api_key,
                    model=target.model,
                    voice=voice or target.voice or self.default_voice,
                    timeout_seconds=target.timeout_seconds,
                    trust_env=target.trust_env,
                )
                self._clients[target.key] = client
            try:
                return await client.synthesize_styled(
                    text,
                    voice=voice or target.voice or self.default_voice,
                    rate=rate,
                )
            except Exception as exc:
                errors.append(f"{target.model}: {exc}")
                count = self._failures.get(target.key, 0) + 1
                self._failures[target.key] = count
                if count >= 2:
                    self._open_until[target.key] = now + 30.0
        raise TTSError(
            "所有语音合成模型均不可用"
            + (f"：{errors[-1]}" if errors else "。")
        )

    async def close(self) -> None:
        for client in self._clients.values():
            await client.close()
        self._clients.clear()
