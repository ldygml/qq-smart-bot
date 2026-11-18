from __future__ import annotations

import json

import httpx
import pytest

from qq_ai_bot.onebot import OneBotClient


@pytest.mark.asyncio
async def test_recent_contact_and_friend_history_are_normalized() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/get_recent_contact"):
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "retcode": 0,
                    "data": [
                        {
                            "peerUin": "42",
                            "peerName": "Alice",
                            "chatType": 1,
                            "msgTime": "123",
                            "msgId": "88",
                        },
                        {
                            "peerUin": "100",
                            "peerName": "Group",
                            "chatType": 2,
                            "msgTime": "124",
                            "msgId": "89",
                        },
                    ],
                },
            )
        assert json.loads(request.content) == {
            "user_id": 42,
            "message_seq": 7,
            "count": 20,
            "reverse_order": True,
        }
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "retcode": 0,
                "data": {"messages": [{"message_id": 7, "post_type": "message"}]},
            },
        )

    client = OneBotClient(
        "http://127.0.0.1:3000",
        "token",
        transport=httpx.MockTransport(handler),
    )
    try:
        contacts = await client.get_recent_contact(count=20)
        history = await client.get_friend_msg_history(
            42,
            message_seq=7,
            count=20,
        )
    finally:
        await client.close()
    assert contacts[0]["chat_type"] == "private"
    assert contacts[1]["chat_type"] == "group"
    assert contacts[0]["peer_uin"] == 42
    assert history[0]["message_id"] == 7
