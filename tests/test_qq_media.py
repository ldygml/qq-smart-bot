from __future__ import annotations

import pytest

from qq_ai_bot.qq_media import QQMediaError, QQMediaStore


async def _chunks(data: bytes, size: int):
    for index in range(0, len(data), size):
        yield data[index : index + size]


@pytest.mark.asyncio
async def test_upload_reassembles_chunks_and_deduplicates_content(
    tmp_path,
) -> None:
    chunk_bytes = 64 * 1024
    store = QQMediaStore(
        tmp_path / "media.db",
        tmp_path / "media",
        chunk_bytes=chunk_bytes,
    )
    payload = b"0123456789" * (chunk_bytes * 2 // 10 + 1)

    first = store.init_upload(
        name="../../报告.zip",
        size=len(payload),
        mime_type="application/zip",
    )
    assert first["total_chunks"] == 3
    assert first["name"] == "报告.zip"
    for index in range(first["total_chunks"]):
        start = index * first["chunk_size"]
        await store.append_chunk(
            first["upload_id"],
            index,
            _chunks(payload[start : start + first["chunk_size"]], 2),
        )

    item = store.complete_upload(first["upload_id"])
    path = store.path_for(item)
    assert path is not None
    assert path.read_bytes() == payload
    assert item.original_name == "报告.zip"
    assert item.kind == "file"

    second = store.init_upload(
        name="same-content.zip",
        size=len(payload),
        mime_type="application/zip",
    )
    for index in range(second["total_chunks"]):
        start = index * second["chunk_size"]
        await store.append_chunk(
            second["upload_id"],
            index,
            _chunks(payload[start : start + second["chunk_size"]], 4),
        )
    duplicate = store.complete_upload(second["upload_id"])

    assert duplicate.id == item.id
    assert len(store.list_items()) == 1
    assert len(list((tmp_path / "media" / "files").iterdir())) == 1


@pytest.mark.asyncio
async def test_upload_rejects_size_mismatch_and_invalid_chunk(tmp_path) -> None:
    store = QQMediaStore(
        tmp_path / "media.db",
        tmp_path / "media",
        chunk_bytes=4,
    )
    upload = store.init_upload(name="notes.txt", size=5, mime_type="text/plain")
    await store.append_chunk(
        upload["upload_id"],
        0,
        _chunks(b"abc", 3),
    )

    with pytest.raises(QQMediaError, match="大小不一致"):
        store.complete_upload(upload["upload_id"])

    with pytest.raises(QQMediaError, match="分片序号无效"):
        await store.append_chunk(
            upload["upload_id"],
            9,
            _chunks(b"x", 1),
        )


def test_upload_limit_and_safe_display_name(tmp_path) -> None:
    store = QQMediaStore(
        tmp_path / "media.db",
        tmp_path / "media",
        max_upload_bytes=3,
    )

    with pytest.raises(QQMediaError, match="上传大小"):
        store.init_upload(name="large.bin", size=4)

    upload = store.init_upload(name="..\\..\\CON?.png", size=1)
    assert upload["name"] == "CON_.png"
