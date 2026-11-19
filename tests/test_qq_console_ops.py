from __future__ import annotations

import json
import sqlite3

import httpx
import pytest

from qq_ai_bot.control_plane import (
    build_encrypted_backup,
    restore_encrypted_backup,
)
from qq_ai_bot.memory import ChatMemory
from qq_ai_bot.onebot import OneBotClient, ReplyTarget
from qq_ai_bot.qq_media import QQMediaStore


async def _chunks(data: bytes, size: int):
    for index in range(0, len(data), size):
        yield data[index : index + size]


async def _upload(
    store: QQMediaStore,
    name: str,
    payload: bytes,
) -> object:
    upload = store.init_upload(
        name=name,
        size=len(payload),
        mime_type="application/octet-stream",
    )
    for index in range(upload["total_chunks"]):
        start = index * upload["chunk_size"]
        await store.append_chunk(
            upload["upload_id"],
            index,
            _chunks(payload[start : start + upload["chunk_size"]], 2),
        )
    return store.complete_upload(upload["upload_id"])


def test_archive_search_filters_attachments_and_recalled_messages(tmp_path) -> None:
    memory = ChatMemory(tmp_path / "memory.db")
    memory.archive_message(
        external_key="private-1",
        conversation_id="private:42",
        message_type="private",
        group_id=None,
        sender_user_id=999,
        sender_name="Bot",
        role="assistant",
        content="Quarterly invoice attached",
        event_time=100,
        message_id=900,
        attachments=[{"type": "file", "name": "invoice.pdf"}],
    )
    memory.archive_message(
        external_key="group-1",
        conversation_id="group:7:user:8",
        message_type="group",
        group_id=7,
        sender_user_id=8,
        sender_name="Alice",
        role="user",
        content="ordinary chat",
        event_time=200,
        message_id=901,
    )

    results = memory.search_messages(query="invoice", file_only=True)
    assert len(results) == 1
    assert results[0]["target_type"] == "private"
    assert results[0]["target_id"] == 42
    assert results[0]["attachments"][0]["name"] == "invoice.pdf"

    assert memory.mark_message_deleted(900) == 1
    assert memory.search_messages(query="invoice") == []
    hidden = memory.search_messages(query="invoice", include_deleted=True)
    assert hidden[0]["deleted"] is True


def test_old_archive_database_is_migrated_in_place(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE external_messages (
                external_key TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                message_type TEXT NOT NULL,
                group_id INTEGER,
                sender_user_id INTEGER NOT NULL,
                sender_name TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                event_time INTEGER NOT NULL DEFAULT 0,
                archived_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            INSERT INTO external_messages (
                external_key, conversation_id, message_type, group_id,
                sender_user_id, sender_name, role, content, event_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-1",
                "private:77",
                "private",
                None,
                77,
                "Legacy",
                "user",
                "legacy searchable text",
                50,
            ),
        )

    memory = ChatMemory(path)
    result = memory.search_messages(query="searchable")
    assert len(result) == 1
    assert result[0]["attachments"] == []
    assert result[0]["deleted"] is False
    assert result[0]["target_id"] == 77
    assert memory.mark_message_deleted("legacy-1") == 1


@pytest.mark.asyncio
async def test_media_cleanup_only_removes_requested_items(tmp_path) -> None:
    store = QQMediaStore(
        tmp_path / "media.db",
        tmp_path / "media",
        chunk_bytes=4,
    )
    keep = await _upload(store, "keep.txt", b"keep")
    delete = await _upload(store, "delete.txt", b"delete")
    assert store.update_metadata(keep.id, favorite=True, tags="reference") is not None

    orphan = store.files_dir / "orphan.txt"
    orphan.write_bytes(b"orphan")
    orphan_only = store.cleanup(orphaned=True)
    assert orphan_only["item_count"] == 0
    assert orphan_only["orphan_deleted"] == 1
    assert store.path_for(keep) is not None
    assert store.path_for(delete) is not None
    assert not orphan.exists()

    filtered = store.cleanup(kind="file")
    assert filtered["item_count"] == 1
    assert filtered["deleted_count"] == 1
    assert store.get(keep.id) is not None
    assert store.get(delete.id) is None
    assert store.storage_stats()["favorite_count"] == 1


@pytest.mark.asyncio
async def test_onebot_group_mentions_quote_and_recall() -> None:
    requests: list[tuple[str, dict[str, object]]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, json.loads(request.content)))
        return httpx.Response(
            200,
            json={"status": "ok", "retcode": 0, "data": {"message_id": 1234}},
        )

    client = OneBotClient(
        "http://127.0.0.1:3000",
        "token",
        transport=httpx.MockTransport(handler),
    )
    try:
        message_id = await client.send_text(
            ReplyTarget("group", 456, 789, message_id=321),
            "hello",
            mentions=[11, 11, 12],
        )
        await client.delete_msg(message_id or "1234")
    finally:
        await client.close()

    assert message_id == "1234"
    assert requests[0][0].endswith("/send_group_msg")
    assert requests[0][1]["message"] == [
        {"type": "reply", "data": {"id": "321"}},
        {"type": "at", "data": {"qq": "11"}},
        {"type": "at", "data": {"qq": "12"}},
        {"type": "text", "data": {"text": "hello"}},
    ]
    assert requests[1][0].endswith("/delete_msg")
    assert requests[1][1] == {"message_id": "1234"}


def test_encrypted_backup_round_trip_includes_assets(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    control = project / "control.db"
    instance = project / "data" / "bot.db"
    instance.parent.mkdir(parents=True)
    for path in (control, instance):
        connection = sqlite3.connect(path)
        try:
            connection.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY)")
            connection.commit()
        finally:
            connection.close()

    asset = project / "data" / "qq_media" / "files" / "asset.bin"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"encrypted-backup-asset")
    encrypted = build_encrypted_backup(
        instances=[{"database_path": "data/bot.db"}],
        username="owner",
        passphrase="backup-passphrase",
        project_root=project,
        control_path=control,
    )
    assert b"encrypted-backup-asset" not in encrypted
    asset.unlink()

    result = restore_encrypted_backup(
        encrypted=encrypted,
        passphrase="backup-passphrase",
        project_root=project,
        control_path=control,
    )
    assert asset.read_bytes() == b"encrypted-backup-asset"
    assert result["assets_restored"] == 1
    assert result["backup_includes_assets"] is True
    assert result["safety_directory"]
