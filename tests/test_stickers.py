from __future__ import annotations

import base64

import httpx
import pytest

from qq_ai_bot.onebot import StickerSource
from qq_ai_bot.stickers import StickerAssetStore, StickerStore


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


def test_sticker_catalog_merges_same_saved_image(tmp_path) -> None:
    asset_dir = tmp_path / "assets"
    asset_dir.mkdir()
    asset_path = asset_dir / "same.gif"
    asset_path.write_bytes(b"GIF89a" + b"\x00" * 24)
    store = StickerStore(tmp_path / "stickers.db", asset_dir=asset_dir)
    first_id = store.record(
        StickerSource(
            summary="动画表情",
            image_source="https://cdn.example/one.gif",
        )
    )
    second_id = store.record(
        StickerSource(
            summary="动画表情",
            image_source="https://cdn.example/two.gif",
        )
    )
    assert first_id is not None
    assert second_id is not None
    assert first_id != second_id
    store.update_classification(
        second_id,
        category="无语",
        usage_hint="熟人吐槽时使用",
        tags=("无语",),
        source="manual",
    )

    first_canonical = store.attach_asset(
        first_id,
        local_path=asset_path,
        sha256="a" * 64,
        mime_type="image/gif",
    )
    second_canonical = store.attach_asset(
        second_id,
        local_path=asset_path,
        sha256="a" * 64,
        mime_type="image/gif",
    )

    assert first_canonical == second_canonical
    assert store.count() == 1
    metadata = store.get_metadata(second_canonical or 0) or {}
    assert metadata["classification_source"] == "manual"
    assert metadata["category"] == "无语"
