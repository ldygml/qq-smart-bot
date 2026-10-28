from __future__ import annotations

import asyncio
import base64
import binascii
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urlparse

import httpx

from .onebot import AudioSource


class AudioLoadError(RuntimeError):
    pass


class AudioResolverProtocol(Protocol):
    async def get_record(self, *, source: AudioSource) -> dict[str, Any]: ...


class AudioLoaderProtocol(Protocol):
    async def load(
        self,
        source: AudioSource,
        resolver: AudioResolverProtocol,
    ) -> "LoadedAudio": ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class LoadedAudio:
    data: str
    format: str = "wav"


class AudioLoader:
    def __init__(self, *, timeout_seconds: float = 120.0) -> None:
        timeout = httpx.Timeout(timeout_seconds)
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            trust_env=True,
        )

    async def load(
        self,
        source: AudioSource,
        resolver: AudioResolverProtocol,
    ) -> LoadedAudio:
        try:
            record = await resolver.get_record(source=source)
        except Exception as exc:
            raise AudioLoadError("QQ 语音下载失败，请重新发送。") from exc

        encoded = record.get("base64")
        if isinstance(encoded, str) and encoded.strip():
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise AudioLoadError("语音数据格式无效。") from exc
            return _to_data_uri(raw)

        file_source = str(record.get("file") or record.get("url") or "")
        if not file_source:
            raise AudioLoadError("没有取得语音文件。")
        if file_source.startswith("file://"):
            path = Path(unquote(urlparse(file_source).path))
            return _to_data_uri(await _read_file(path))
        parsed = urlparse(file_source)
        if parsed.scheme in {"http", "https"}:
            try:
                response = await self._client.get(file_source)
                response.raise_for_status()
            except httpx.TimeoutException as exc:
                raise AudioLoadError("语音下载超时，请重新发送。") from exc
            except httpx.HTTPError as exc:
                raise AudioLoadError("语音下载失败，请重新发送。") from exc
            return _to_data_uri(response.content)
        return _to_data_uri(await _read_file(Path(file_source)))

    async def close(self) -> None:
        await self._client.aclose()


async def _read_file(path: Path) -> bytes:
    try:
        return await asyncio.to_thread(path.read_bytes)
    except OSError as exc:
        raise AudioLoadError("语音文件不存在或无法读取。") from exc


def _to_data_uri(data: bytes) -> LoadedAudio:
    if not data:
        raise AudioLoadError("语音内容为空，请重新发送。")
    encoded = base64.b64encode(data).decode("ascii")
    return LoadedAudio(
        data=f"data:audio/wav;base64,{encoded}",
        format="wav",
    )
