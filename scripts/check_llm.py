from __future__ import annotations

import asyncio

from qq_ai_bot.config import Settings
from qq_ai_bot.llm import CompatibleChatClient, LLMError


async def main() -> int:
    settings = Settings()
    key = settings.effective_llm_api_key
    if not key and "127.0.0.1" not in settings.llm_base_url:
        print("No API key found in LLM_API_KEY or DASHSCOPE_API_KEY.")
        return 2

    client = CompatibleChatClient(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=key,
        max_tokens=32,
        timeout_seconds=30,
    )
    try:
        reply = await client.chat(
            [
                {
                    "role": "user",
                    "content": "只回复两个字：连接成功",
                }
            ]
        )
        print(f"LLM OK: {reply}")
        return 0
    except LLMError as exc:
        print(f"LLM FAILED: {exc}")
        return 1
    finally:
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

