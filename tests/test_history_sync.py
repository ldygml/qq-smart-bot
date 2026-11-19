from __future__ import annotations

import time

import pytest

from qq_ai_bot.history_sync import (
    history_cursor_key,
    sync_recent_history,
)
from qq_ai_bot.memory import ChatMemory
from qq_ai_bot.shared_memory import SharedMemoryStore


def _message(
    message_id: int,
    text: str,
    *,
    user_id: int,
    post_type: str = "message",
    name: str = "Alice",
) -> dict:
    return {
        "message_id": message_id,
        "real_seq": message_id,
        "time": int(time.time()),
        "post_type": post_type,
        "sender": {"user_id": user_id, "nickname": name},
        "message": [{"type": "text", "data": {"text": text}}],
        "raw_message": text,
    }


class FakeHistoryClient:
    def __init__(self, contacts: list[dict], pages: dict[tuple, list[dict]]) -> None:
        self.contacts = contacts
        self.pages = pages
        self.history_calls: list[tuple] = []

    async def get_recent_contact(self, *, count: int = 50) -> list[dict]:
        return self.contacts[:count]

    async def get_friend_msg_history(
        self,
        user_id: int,
        *,
        message_seq=0,
        count: int = 50,
        reverse_order: bool = True,
    ) -> list[dict]:
        self.history_calls.append(("private", user_id, message_seq, reverse_order))
        return self.pages.get(("private", user_id, message_seq), [])

    async def get_group_msg_history(
        self,
        group_id: int,
        *,
        message_seq=0,
        count: int = 50,
        reverse_order: bool = True,
    ) -> list[dict]:
        self.history_calls.append(("group", group_id, message_seq, reverse_order))
        return self.pages.get(("group", group_id, message_seq), [])


@pytest.mark.asyncio
async def test_private_history_is_imported_once_and_new_messages_are_incremental(
    tmp_path,
) -> None:
    memory = ChatMemory(tmp_path / "bot.db")
    shared = SharedMemoryStore(tmp_path / "bot.db")
    contacts = [
        {
            "peer_uin": 42,
            "chat_type": "private",
            "name": "Alice",
            "msg_time": int(time.time()),
        }
    ]
    initial = [
        _message(1, "older question", user_id=42),
        _message(2, "older answer", user_id=999, post_type="message_sent"),
        _message(3, "offline question", user_id=42),
    ]
    client = FakeHistoryClient(contacts, {("private", 42, 0): initial})

    first = await sync_recent_history(
        client=client,
        memory=memory,
        shared_memory=shared,
        max_days=30,
        max_pages=2,
        page_size=20,
    )
    assert first["status"] == "ok"
    assert first["imported"] == 3
    assert memory.history("private:42", 10) == []
    assert {
        item["content"]
        for item in memory.archived_messages(limit=10)
    } == {
        "offline question",
        "older answer",
        "older question",
    }
    assert [
        item["content"]
        for item in shared.observations_since(0, limit=10)
    ] == [
        "older question",
        "offline question",
    ]
    assert all(call[3] is True for call in client.history_calls)

    updated = initial + [
        _message(4, "new question", user_id=42),
        _message(5, "new answer", user_id=999, post_type="message_sent"),
    ]
    client.pages[("private", 42, 0)] = updated
    second = await sync_recent_history(
        client=client,
        memory=memory,
        shared_memory=shared,
        max_days=30,
        max_pages=2,
        page_size=20,
    )
    assert second["imported"] == 2
    assert memory.history("private:42", 10) == []
    assert len(memory.archived_messages(limit=10)) == 5
    assert shared.get_state(history_cursor_key("private", 42)) == "5"


@pytest.mark.asyncio
async def test_group_history_restores_threads_and_group_context(tmp_path) -> None:
    memory = ChatMemory(tmp_path / "bot.db")
    shared = SharedMemoryStore(tmp_path / "bot.db")
    contacts = [
        {
            "peer_uin": 100,
            "chat_type": "group",
            "name": "Test group",
            "msg_time": int(time.time()),
        }
    ]
    messages = [
        _message(11, "Alice message", user_id=11, name="Alice"),
        _message(12, "Bot reply", user_id=999, post_type="message_sent"),
        _message(13, "Bob message", user_id=22, name="Bob"),
    ]
    context: list[tuple[int, str, str, str]] = []
    client = FakeHistoryClient(contacts, {("group", 100, 0): messages})

    result = await sync_recent_history(
        client=client,
        memory=memory,
        shared_memory=shared,
        max_days=30,
        max_pages=2,
        page_size=20,
        on_group_message=lambda *args: context.append(args),
    )

    assert result["imported"] == 3
    assert memory.history("group:100:user:11", 10) == []
    assert memory.history("group:100:user:22", 10) == []
    assert len(memory.archived_messages(limit=10)) == 3
    assert [item[1] for item in context] == ["user", "assistant", "user"]


@pytest.mark.asyncio
async def test_history_sync_failure_does_not_raise(tmp_path) -> None:
    class UnsupportedClient:
        async def get_recent_contact(self, *, count: int = 50):
            raise RuntimeError("unsupported")

    result = await sync_recent_history(
        client=UnsupportedClient(),
        memory=ChatMemory(tmp_path / "bot.db"),
        shared_memory=SharedMemoryStore(tmp_path / "bot.db"),
    )
    assert result["status"] == "failed"
    assert "unsupported" in result["errors"][0]
