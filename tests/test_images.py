from __future__ import annotations

import pytest

from qq_ai_bot.images import ImageLoader, ImageLoadError


PIXEL_PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
    "/x8AAusB9Wl2n1cAAAAASUVORK5CYII="
)


@pytest.mark.asyncio
async def test_image_loader_accepts_valid_data_uri() -> None:
    loader = ImageLoader()
    try:
        assert await loader.load(PIXEL_PNG) == PIXEL_PNG
    finally:
        await loader.close()


@pytest.mark.asyncio
async def test_image_loader_rejects_non_image() -> None:
    loader = ImageLoader()
    try:
        with pytest.raises(ImageLoadError):
            await loader.load("data:text/plain;base64,SGVsbG8=")
    finally:
        await loader.close()


@pytest.mark.asyncio
async def test_image_loader_can_disable_size_limit() -> None:
    loader = ImageLoader(max_bytes=0)
    try:
        assert loader.max_bytes is None
        assert await loader.load(PIXEL_PNG) == PIXEL_PNG
    finally:
        await loader.close()
