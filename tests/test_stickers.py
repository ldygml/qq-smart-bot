from __future__ import annotations

import base64

import httpx
import pytest

from qq_ai_bot.stickers import StickerAssetStore


@pytest.mark.asyncio
async def test_sticker_assets_are_persisted_and_deduplicated(tmp_path) -> None:
    data = b"GIF89a" + b"\x00" * 24
    source = "data:image/gif;base64," + base64.b64encode(data).decode("ascii")
    store = StickerAssetStore(tmp_path / "stickers")
    try:
        first = await store.persist(source)
        second = await store.persist(source)
    finally:
        await store.close()

    assert first.path == second.path
    assert first.sha256 == second.sha256
    assert first.path.read_bytes() == data
    assert first.path.suffix == ".gif"
    assert first.mime_type == "image/gif"
    assert len(list((tmp_path / "stickers").iterdir())) == 1


@pytest.mark.asyncio
async def test_sticker_asset_store_downloads_with_mocked_transport(
    tmp_path,
) -> None:
    data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=data,
            headers={"content-type": "image/png"},
        )

    store = StickerAssetStore(
        tmp_path / "stickers",
        transport=httpx.MockTransport(handler),
    )
    try:
        asset = await store.persist("https://cdn.example/sticker.png")
    finally:
        await store.close()

    assert asset.path.read_bytes() == data
    assert asset.mime_type == "image/png"
    assert store.contains(asset.path)
    assert not store.contains(tmp_path / "outside.png")
