from __future__ import annotations

import json

import pytest

from qq_ai_bot import model_router
from qq_ai_bot.model_router import (
    FailoverChatClient,
    RouteTarget,
    parse_model_routes,
)


def test_parse_model_routes_keeps_capability_order() -> None:
    raw = json.dumps(
        {
            "chat": [
                {
                    "base_url": "https://example.com/v1",
                    "api_key": "secret",
                    "model": "chat-primary",
                },
                {
                    "base_url": "https://backup.example/v1",
                    "api_key": "secret2",
                    "model": "chat-backup",
                },
            ],
            "vision": [
                {
                    "base_url": "https://vision.example/v1",
                    "api_key": "secret3",
                    "model": "vision-model",
                }
            ],
        }
    )
    routes = parse_model_routes(raw)
    assert [item.model for item in routes["chat"]] == [
        "chat-primary",
        "chat-backup",
    ]
    assert routes["vision"][0].model == "vision-model"


@pytest.mark.asyncio
async def test_chat_router_falls_back_after_failure(monkeypatch) -> None:
    class FakeClient:
        def __init__(self, *, base_url, model, **kwargs) -> None:
            self.base_url = base_url
            self.model = model

        async def chat(self, messages, *, model=None, extra_payload=None) -> str:
            if self.model == "bad-model":
                raise RuntimeError("upstream failed")
            return f"reply from {self.model}"

        async def close(self) -> None:
            return None

    monkeypatch.setattr(model_router, "CompatibleChatClient", FakeClient)
    client = FailoverChatClient(
        [
            RouteTarget(
                base_url="https://bad.example/v1",
                api_key="one",
                model="bad-model",
            ),
            RouteTarget(
                base_url="https://good.example/v1",
                api_key="two",
                model="good-model",
            ),
        ],
        capability="chat",
    )
    reply = await client.chat([{"role": "user", "content": "hello"}])
    await client.close()
    assert reply == "reply from good-model"
