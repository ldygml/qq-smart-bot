from __future__ import annotations

import asyncio
import base64
import binascii
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

import httpx


class ImageLoadError(RuntimeError):
    pass


class ImageLoaderProtocol(Protocol):
    async def load(self, source: str) -> str: ...

    async def close(self) -> None: ...


class ImageLoader:
    def __init__(
        self,
        *,
        max_bytes: int | None = 0,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.max_bytes = max_bytes if max_bytes and max_bytes > 0 else None
        timeout = httpx.Timeout(timeout_seconds)
        self._local_client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            trust_env=False,
        )
        self._external_client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            trust_env=True,
        )

    async def load(self, source: str) -> str:
        value = source.strip()
        if not value:
            raise ImageLoadError("图片地址为空。")
        if value.startswith("data:"):
            return self._validate_data_uri(value)
        if value.startswith("base64://"):
            return self._to_data_uri(_decode_base64(value[9:]))
        if value.startswith("file://"):
            return await self._load_file(urlparse(value).path)

        parsed = urlparse(value)
        if parsed.scheme in {"http", "https"}:
            return await self._download(value, parsed.hostname or "")

        path = Path(value)
        if path.is_absolute():
            return await self._load_file(path)
        raise ImageLoadError("QQ 图片没有提供可读取的链接，请重新发送原图。")

    async def _download(self, url: str, hostname: str) -> str:
        client = (
            self._local_client
            if hostname in {"127.0.0.1", "::1", "localhost"}
            else self._external_client
        )
        try:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                declared_length = response.headers.get("content-length")
                if (
                    self.max_bytes is not None
                    and declared_length
                    and int(declared_length) > self.max_bytes
                ):
                    raise self._too_large_error()

                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if self.max_bytes is not None and len(content) > self.max_bytes:
                        raise self._too_large_error()
                content_type = response.headers.get("content-type", "")
        except ImageLoadError:
            raise
        except httpx.TimeoutException as exc:
            raise ImageLoadError("图片下载超时，请重新发送。") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ImageLoadError("图片下载失败，请重新发送原图。") from exc

        return self._to_data_uri(bytes(content), content_type)

    async def _load_file(self, path: str | Path) -> str:
        file_path = Path(path)
        try:
            if not file_path.is_file():
                raise ImageLoadError("图片文件不存在，请重新发送原图。")
            if (
                self.max_bytes is not None
                and file_path.stat().st_size > self.max_bytes
            ):
                raise self._too_large_error()
            data = await asyncio.to_thread(file_path.read_bytes)
        except ImageLoadError:
            raise
        except OSError as exc:
            raise ImageLoadError("图片文件读取失败，请重新发送原图。") from exc
        return self._to_data_uri(data)

    def _validate_data_uri(self, value: str) -> str:
        header, separator, payload = value.partition(",")
        if not separator or not header.lower().startswith("data:image/"):
            raise ImageLoadError("图片数据格式不受支持。")
        estimated_size = (len(payload) * 3) // 4
        if self.max_bytes is not None and estimated_size > self.max_bytes:
            raise self._too_large_error()
        return value

    def _to_data_uri(self, data: bytes, declared_type: str = "") -> str:
        if not data:
            raise ImageLoadError("图片内容为空，请重新发送原图。")
        if self.max_bytes is not None and len(data) > self.max_bytes:
            raise self._too_large_error()
        content_type = _detect_image_type(data, declared_type)
        encoded = base64.b64encode(data).decode("ascii")
        return f"data:{content_type};base64,{encoded}"

    def _too_large_error(self) -> ImageLoadError:
        assert self.max_bytes is not None
        if self.max_bytes >= 1024 * 1024:
            limit = f"{self.max_bytes / (1024 * 1024):g} MB"
        else:
            limit = f"{self.max_bytes} 字节"
        return ImageLoadError(f"图片超过限制（{limit}），请压缩后再发送。")

    async def close(self) -> None:
        await self._local_client.aclose()
        await self._external_client.aclose()


def _decode_base64(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ImageLoadError("图片 Base64 数据无效。") from exc


def _detect_image_type(data: bytes, declared_type: str) -> str:
    normalized = declared_type.split(";", 1)[0].strip().lower()
    if normalized in {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/bmp",
    }:
        return normalized
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    raise ImageLoadError("收到的文件不是可识别的图片。")
