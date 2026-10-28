from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, Mapping, Protocol

import httpx

logger = logging.getLogger(__name__)


class ChatClient(Protocol):
    async def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        model: str | None = None,
        extra_payload: Mapping[str, Any] | None = None,
    ) -> str: ...

    async def close(self) -> None: ...


class LLMError(RuntimeError):
    pass


class CompatibleChatClient:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        timeout_seconds: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._endpoint = _chat_endpoint(base_url)
        self._client = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
        )

    async def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        model: str | None = None,
        extra_payload: Mapping[str, Any] | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": list(messages),
            "temperature": self.temperature,
            "stream": False,
        }
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        if extra_payload:
            payload.update(extra_payload)
        try:
            response = await self._client.post(self._endpoint, json=payload)
        except httpx.TimeoutException as exc:
            raise LLMError("模型接口响应超时了，请稍后再试。") from exc
        except httpx.HTTPError as exc:
            logger.warning("LLM network request failed: %s", type(exc).__name__)
            raise LLMError("模型接口暂时连接不上，请检查网络和 LLM_BASE_URL。") from exc

        if response.is_error:
            message = _public_error_for_status(response.status_code)
            logger.warning(
                "LLM request failed with HTTP %s; response body omitted",
                response.status_code,
            )
            raise LLMError(message)

        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            logger.warning("LLM returned an unexpected response shape")
            raise LLMError("模型返回了无法识别的数据格式。") from exc

        text = _content_to_text(content).strip()
        if not text:
            raise LLMError("模型这次没有生成可用回复，请换个问法试试。")
        return text

    async def close(self) -> None:
        await self._client.aclose()


def _chat_endpoint(base_url: str) -> str:
    base = base_url.strip().rstrip("/")
    if not base:
        raise ValueError("LLM_BASE_URL cannot be empty")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return ""


def _public_error_for_status(status_code: int) -> str:
    if status_code in {401, 403}:
        return "模型服务鉴权失败，请检查 LLM_API_KEY 和 LLM_BASE_URL。"
    if status_code == 404:
        return "没有找到模型接口或模型名称，请检查 LLM_BASE_URL 和 LLM_MODEL。"
    if status_code == 429:
        return "模型服务限流或余额不足，请稍后再试。"
    if status_code >= 500:
        return "模型服务暂时异常，请稍后再试。"
    return f"模型接口返回 HTTP {status_code}，请检查相关配置。"

