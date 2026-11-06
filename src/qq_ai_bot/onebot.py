from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)


class OneBotEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    post_type: str = ""
    message_type: str | None = None
    self_id: int | None = None
    user_id: int | None = None
    group_id: int | None = None
    group_name: str = ""
    message_id: int | str | None = None
    message: Any = None
    raw_message: str = ""
    sender: dict[str, Any] | None = None
    notice_type: str = ""
    sub_type: str = ""
    target_id: int | None = None


@dataclass(frozen=True)
class ReplyTarget:
    message_type: str
    user_id: int
    group_id: int | None = None
    message_id: int | str | None = None

    @property
    def conversation_id(self) -> str:
        if self.message_type == "group" and self.group_id is not None:
            return f"group:{self.group_id}:user:{self.user_id}"
        return f"private:{self.user_id}"


@dataclass(frozen=True)
class FileSource:
    name: str
    source: str = ""
    file_id: str = ""
    size: int | None = None


@dataclass(frozen=True)
class AudioSource:
    source: str = ""
    file_id: str = ""
    size: int | None = None


@dataclass(frozen=True)
class StickerSource:
    summary: str
    emoji_id: str = ""
    emoji_package_id: str = ""
    key: str = ""
    image_source: str = ""


@dataclass(frozen=True)
class MessageContent:
    text: str
    mentioned: bool
    mentioned_user_ids: tuple[int, ...] = ()
    image_sources: tuple[str, ...] = ()
    file_sources: tuple[FileSource, ...] = ()
    audio_sources: tuple[AudioSource, ...] = ()
    reply_id: str = ""
    sticker_summaries: tuple[str, ...] = ()
    sticker_sources: tuple[StickerSource, ...] = ()


class OneBotError(RuntimeError):
    pass


class OneBotClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers=headers,
            timeout=20.0,
            transport=transport,
            trust_env=False,
        )

    async def send_text(self, target: ReplyTarget, text: str) -> None:
        if target.message_type == "group":
            if target.group_id is None:
                raise OneBotError("Group reply target has no group_id")
            action = "send_group_msg"
            payload = {"group_id": target.group_id}
        else:
            action = "send_private_msg"
            payload = {"user_id": target.user_id}

        message: list[dict[str, Any]] = []
        if target.message_id is not None:
            message.append(
                {"type": "reply", "data": {"id": str(target.message_id)}}
            )
        message.append({"type": "text", "data": {"text": text}})
        payload["message"] = message
        response = await self._client.post(action, json=payload)
        try:
            data = response.json()
        except ValueError as exc:
            raise OneBotError(
                f"OneBot returned a non-JSON response (HTTP {response.status_code})"
            ) from exc

        if response.is_error or data.get("status") != "ok":
            retcode = data.get("retcode")
            raise OneBotError(
                f"OneBot {action} failed (HTTP {response.status_code}, retcode={retcode})"
            )

    async def poke(self, target: ReplyTarget) -> None:
        if target.message_type == "group":
            if target.group_id is None:
                raise OneBotError("Group poke target has no group_id")
            action = "group_poke"
            payload = {
                "group_id": target.group_id,
                "user_id": target.user_id,
            }
        else:
            action = "friend_poke"
            payload = {"user_id": target.user_id}

        response = await self._client.post(action, json=payload)
        try:
            data = response.json()
        except ValueError as exc:
            raise OneBotError(
                f"OneBot returned a non-JSON response (HTTP {response.status_code})"
            ) from exc
        if response.is_error or data.get("status") != "ok":
            retcode = data.get("retcode")
            raise OneBotError(
                f"OneBot {action} failed (HTTP {response.status_code}, retcode={retcode})"
            )

    async def send_image(self, target: ReplyTarget, image: str) -> None:
        await self._send_segment(target, "image", {"file": image})

    async def send_record(self, target: ReplyTarget, record: str) -> None:
        await self._send_segment(target, "record", {"file": record})

    async def send_sticker(
        self,
        target: ReplyTarget,
        sticker: StickerSource,
    ) -> None:
        if (
            sticker.image_source
            and not sticker.emoji_id
            and not sticker.emoji_package_id
            and not sticker.key
        ):
            await self._send_segment(
                target,
                "image",
                {
                    "file": sticker.image_source,
                    "sub_type": 1,
                    "summary": sticker.summary,
                },
            )
            return
        data: dict[str, Any] = {"summary": sticker.summary}
        if sticker.emoji_id:
            data["emoji_id"] = sticker.emoji_id
        if sticker.emoji_package_id:
            data["emoji_package_id"] = sticker.emoji_package_id
        if sticker.key:
            data["key"] = sticker.key
        await self._send_segment(target, "mface", data)

    async def upload_file(
        self,
        target: ReplyTarget,
        *,
        path: str,
        name: str,
    ) -> None:
        if target.message_type == "group":
            if target.group_id is None:
                raise OneBotError("Group file target has no group_id")
            action = "upload_group_file"
            payload = {
                "group_id": target.group_id,
                "file": path,
                "name": name,
                "upload_file": True,
            }
        else:
            action = "upload_private_file"
            payload = {
                "user_id": target.user_id,
                "file": path,
                "name": name,
                "upload_file": True,
            }
        response = await self._client.post(action, json=payload, timeout=120.0)
        try:
            data = response.json()
        except ValueError as exc:
            raise OneBotError(
                f"OneBot returned a non-JSON response (HTTP {response.status_code})"
            ) from exc
        if response.is_error or data.get("status") != "ok":
            retcode = data.get("retcode")
            raise OneBotError(
                f"OneBot {action} failed (HTTP {response.status_code}, retcode={retcode})"
            )

    async def _send_segment(
        self,
        target: ReplyTarget,
        segment_type: str,
        data: dict[str, Any],
    ) -> None:
        if target.message_type == "group":
            if target.group_id is None:
                raise OneBotError("Group target has no group_id")
            action = "send_group_msg"
            payload = {"group_id": target.group_id}
        else:
            action = "send_private_msg"
            payload = {"user_id": target.user_id}
        message: list[dict[str, Any]] = []
        if target.message_id is not None:
            message.append(
                {"type": "reply", "data": {"id": str(target.message_id)}}
            )
        message.append({"type": segment_type, "data": data})
        payload["message"] = message
        response = await self._client.post(action, json=payload)
        try:
            result = response.json()
        except ValueError as exc:
            raise OneBotError(
                f"OneBot returned a non-JSON response (HTTP {response.status_code})"
            ) from exc
        if response.is_error or result.get("status") != "ok":
            retcode = result.get("retcode")
            raise OneBotError(
                f"OneBot {action} failed (HTTP {response.status_code}, retcode={retcode})"
            )

    async def get_file(self, *, source: FileSource) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if source.file_id:
            payload["file_id"] = source.file_id
        elif source.source:
            payload["file"] = source.source
        else:
            raise OneBotError("File message has neither a source nor a file_id")

        response = await self._client.post("get_file", json=payload, timeout=120.0)
        try:
            data = response.json()
        except ValueError as exc:
            raise OneBotError(
                f"OneBot returned a non-JSON response (HTTP {response.status_code})"
            ) from exc
        if response.is_error or data.get("status") != "ok":
            retcode = data.get("retcode")
            raise OneBotError(
                f"OneBot get_file failed (HTTP {response.status_code}, retcode={retcode})"
            )
        file_data = data.get("data")
        if not isinstance(file_data, dict):
            raise OneBotError("OneBot get_file returned no file data")
        return file_data

    async def get_record(self, *, source: AudioSource) -> dict[str, Any]:
        payload: dict[str, Any] = {"out_format": "wav"}
        if source.file_id:
            payload["file_id"] = source.file_id
        elif source.source:
            payload["file"] = source.source
        else:
            raise OneBotError("Voice message has neither a source nor a file_id")

        response = await self._client.post("get_record", json=payload, timeout=120.0)
        try:
            data = response.json()
        except ValueError as exc:
            raise OneBotError(
                f"OneBot returned a non-JSON response (HTTP {response.status_code})"
            ) from exc
        if response.is_error or data.get("status") != "ok":
            retcode = data.get("retcode")
            raise OneBotError(
                f"OneBot get_record failed (HTTP {response.status_code}, retcode={retcode})"
            )
        record_data = data.get("data")
        if not isinstance(record_data, dict):
            raise OneBotError("OneBot get_record returned no voice data")
        return record_data

    async def get_msg_sender_id(self, message_id: int | str) -> int | None:
        response = await self._client.post(
            "get_msg",
            json={"message_id": str(message_id)},
            timeout=10.0,
        )
        try:
            data = response.json()
        except ValueError:
            return None
        if response.is_error or data.get("status") != "ok":
            return None
        message_data = data.get("data")
        if not isinstance(message_data, dict):
            return None
        sender = message_data.get("sender")
        if not isinstance(sender, dict):
            return None
        user_id = sender.get("user_id")
        try:
            return int(user_id)
        except (TypeError, ValueError):
            return None

    async def get_group_list(self) -> list[dict[str, Any]]:
        response = await self._client.post(
            "get_group_list",
            json={},
            timeout=15.0,
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise OneBotError(
                "OneBot get_group_list returned non-JSON data"
            ) from exc
        if response.is_error or data.get("status") != "ok":
            raise OneBotError("OneBot get_group_list failed")
        groups = data.get("data")
        if not isinstance(groups, list):
            raise OneBotError("OneBot get_group_list returned no list")
        result: list[dict[str, Any]] = []
        for item in groups:
            if not isinstance(item, dict):
                continue
            group_id = item.get("group_id") or item.get("groupId")
            group_name = item.get("group_name") or item.get("groupName")
            try:
                normalized_id = int(group_id)
            except (TypeError, ValueError):
                continue
            if normalized_id > 0:
                result.append(
                    {
                        "group_id": normalized_id,
                        "group_name": str(group_name or "").strip(),
                    }
                )
        return result

    async def get_group_files(
        self,
        group_id: int,
        *,
        folder_id: str = "",
    ) -> dict[str, list[dict[str, Any]]]:
        if folder_id:
            action = "get_group_files_by_folder"
            payload = {
                "group_id": group_id,
                "folder_id": folder_id,
            }
        else:
            action = "get_group_root_files"
            payload = {"group_id": group_id}
        response = await self._client.post(action, json=payload, timeout=30.0)
        try:
            data = response.json()
        except ValueError as exc:
            raise OneBotError(
                "OneBot get_group_files returned non-JSON data"
            ) from exc
        if response.is_error or data.get("status") != "ok":
            retcode = data.get("retcode")
            raise OneBotError(
                f"OneBot {action} failed "
                f"(HTTP {response.status_code}, retcode={retcode})"
            )
        result = data.get("data")
        if not isinstance(result, dict):
            raise OneBotError("OneBot get_group_files returned no data")
        raw_files = result.get("files") or []
        raw_folders = result.get("folders") or []
        if not isinstance(raw_files, list) or not isinstance(raw_folders, list):
            raise OneBotError("OneBot get_group_files returned invalid data")
        files = [
            _normalize_group_file(item)
            for item in raw_files
            if isinstance(item, dict)
        ]
        folders = [
            _normalize_group_folder(item)
            for item in raw_folders
            if isinstance(item, dict)
        ]
        return {"folders": folders, "files": files}

    async def get_group_member_name(
        self,
        group_id: int,
        user_id: int,
    ) -> str:
        response = await self._client.post(
            "get_group_member_info",
            json={"group_id": group_id, "user_id": user_id},
            timeout=10.0,
        )
        try:
            data = response.json()
        except ValueError:
            return ""
        if response.is_error or data.get("status") != "ok":
            return ""
        member = data.get("data")
        if not isinstance(member, dict):
            return ""
        return str(
            member.get("card")
            or member.get("nickname")
            or ""
        ).strip()[:80]

    async def close(self) -> None:
        await self._client.aclose()


_CQ_AT_PATTERN = re.compile(r"\[CQ:at,qq=([^,\]]+)(?:,[^\]]*)?\]")
_CQ_IMAGE_PATTERN = re.compile(r"\[CQ:image,([^\]]+)\]")
_CQ_FILE_PATTERN = re.compile(r"\[CQ:file,([^\]]+)\]")
_CQ_RECORD_PATTERN = re.compile(r"\[CQ:record,([^\]]+)\]")
_CQ_REPLY_PATTERN = re.compile(r"\[CQ:reply,id=([^,\]]+)(?:,[^\]]*)?\]")
_CQ_MFACE_PATTERN = re.compile(r"\[CQ:mface,([^\]]+)\]")
_CQ_OTHER_PATTERN = re.compile(r"\[CQ:[^\]]+\]")
_UNSUPPORTED_SEGMENTS = {
    "image": "[图片]",
    "record": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
    "face": "[表情]",
    "mface": "[表情]",
    "reply": "",
    "at": "",
}


def _normalize_group_file(item: dict[str, Any]) -> dict[str, Any]:
    size_value = item.get("file_size", item.get("size"))
    try:
        size = int(size_value) if size_value is not None else 0
    except (TypeError, ValueError):
        size = 0
    uploader = item.get("uploader_name") or item.get("uploader") or ""
    return {
        "file_id": str(item.get("file_id") or item.get("id") or ""),
        "name": str(
            item.get("file_name")
            or item.get("name")
            or item.get("file")
            or "文件"
        )[:240],
        "size": max(0, size),
        "uploader": str(uploader)[:120],
        "busid": item.get("busid"),
        "upload_time": item.get("upload_time"),
        "modify_time": item.get("modify_time"),
        "download_times": item.get("download_times", 0),
    }


def _normalize_group_folder(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "folder_id": str(
            item.get("folder_id")
            or item.get("folderId")
            or item.get("id")
            or ""
        ),
        "name": str(
            item.get("folder_name")
            or item.get("folderName")
            or item.get("name")
            or "文件夹"
        )[:240],
        "file_count": item.get("file_count", item.get("total_file_count", 0)),
        "create_time": item.get("create_time"),
        "modify_time": item.get("modify_time"),
    }


def extract_message_text(
    message: Any,
    *,
    self_id: int | None,
    raw_message: str = "",
) -> tuple[str, bool]:
    """Return plain text and whether the bot was mentioned."""
    content = extract_message_content(
        message,
        self_id=self_id,
        raw_message=raw_message,
    )
    return content.text, content.mentioned


def extract_message_content(
    message: Any,
    *,
    self_id: int | None,
    raw_message: str = "",
) -> MessageContent:
    if isinstance(message, list):
        parts: list[str] = []
        image_sources: list[str] = []
        file_sources: list[FileSource] = []
        audio_sources: list[AudioSource] = []
        reply_id = ""
        sticker_summaries: list[str] = []
        sticker_sources: list[StickerSource] = []
        mentioned_user_ids: list[int] = []
        mentioned = False
        for segment in message:
            if not isinstance(segment, dict):
                continue
            segment_type = str(segment.get("type", ""))
            data = segment.get("data")
            data = data if isinstance(data, dict) else {}
            if segment_type == "text":
                value = data.get("text")
                if isinstance(value, str):
                    parts.append(value)
            elif segment_type == "at":
                qq = str(data.get("qq", ""))
                if self_id is not None and qq == str(self_id):
                    mentioned = True
                else:
                    try:
                        mentioned_id = int(qq)
                    except ValueError:
                        mentioned_id = 0
                    if mentioned_id > 0:
                        mentioned_user_ids.append(mentioned_id)
            elif segment_type == "image":
                is_sticker = any(
                    data.get(key)
                    for key in ("emoji_id", "emoji_package_id", "key")
                ) or str(data.get("sub_type", "")) == "1"
                if is_sticker:
                    summary = str(data.get("summary") or "表情包")
                    sticker_summaries.append(summary)
                    sticker_sources.append(
                        StickerSource(
                            summary=summary,
                            emoji_id=str(data.get("emoji_id") or ""),
                            emoji_package_id=str(
                                data.get("emoji_package_id") or ""
                            ),
                            key=str(data.get("key") or ""),
                            image_source=str(
                                data.get("url")
                                or data.get("path")
                                or data.get("file")
                                or ""
                            ),
                        )
                    )
                    parts.append("[表情包]")
                else:
                    parts.append("[图片]")
                    source = data.get("url") or data.get("file")
                    if isinstance(source, str) and source.strip():
                        image_sources.append(source.strip())
            elif segment_type == "mface":
                summary = str(data.get("summary") or "表情包")
                sticker_summaries.append(summary)
                sticker_sources.append(
                    StickerSource(
                        summary=summary,
                        emoji_id=str(data.get("emoji_id") or ""),
                        emoji_package_id=str(
                            data.get("emoji_package_id") or ""
                        ),
                        key=str(data.get("key") or ""),
                        image_source=str(
                            data.get("url")
                            or data.get("path")
                            or data.get("file")
                            or ""
                        ),
                    )
                )
                parts.append("[表情包]")
            elif segment_type == "file":
                name = str(data.get("name") or data.get("file") or "文件")
                file_value = str(data.get("file") or "")
                file_source = str(data.get("url") or data.get("path") or "")
                if not file_source and (
                    file_value.startswith(
                        ("http://", "https://", "file://", "base64://")
                    )
                    or "/" in file_value
                    or "\\" in file_value
                ):
                    file_source = file_value
                size_value = data.get("file_size")
                try:
                    size = int(size_value) if size_value is not None else None
                except (TypeError, ValueError):
                    size = None
                file_sources.append(
                    FileSource(
                        name=name,
                        source=file_source,
                        file_id=str(data.get("file_id") or ""),
                        size=size,
                    )
                )
                parts.append(f"[文件: {name}]")
            elif segment_type == "record":
                parts.append("[语音]")
                size_value = data.get("file_size")
                try:
                    size = int(size_value) if size_value is not None else None
                except (TypeError, ValueError):
                    size = None
                audio_sources.append(
                    AudioSource(
                        source=str(data.get("path") or data.get("file") or ""),
                        file_id=str(data.get("file_id") or ""),
                        size=size,
                    )
                )
            elif segment_type == "reply":
                reply_id = str(data.get("id") or "")
            elif segment_type in _UNSUPPORTED_SEGMENTS:
                parts.append(_UNSUPPORTED_SEGMENTS[segment_type])
        return MessageContent(
            text=_normalize_text("".join(parts)),
            mentioned=mentioned,
            mentioned_user_ids=tuple(dict.fromkeys(mentioned_user_ids)),
            image_sources=tuple(image_sources),
            file_sources=tuple(file_sources),
            audio_sources=tuple(audio_sources),
            reply_id=reply_id,
            sticker_summaries=tuple(sticker_summaries),
            sticker_sources=tuple(sticker_sources),
        )

    if isinstance(message, str):
        source = message
    else:
        source = raw_message or ""

    mentioned = False
    image_sources: list[str] = []
    file_sources: list[FileSource] = []
    audio_sources: list[AudioSource] = []
    sticker_summaries: list[str] = []
    sticker_sources: list[StickerSource] = []
    mentioned_user_ids: list[int] = []

    def replace_image(match: re.Match[str]) -> str:
        parameters = _cq_parameters(match.group(1))
        is_sticker = any(
            parameters.get(key)
            for key in ("emoji_id", "emoji_package_id", "key")
        ) or parameters.get("sub_type") == "1"
        if is_sticker:
            summary = parameters.get("summary") or "表情包"
            sticker_summaries.append(summary)
            sticker_sources.append(
                StickerSource(
                    summary=summary,
                    emoji_id=parameters.get("emoji_id", ""),
                    emoji_package_id=parameters.get(
                        "emoji_package_id",
                        "",
                    ),
                    key=parameters.get("key", ""),
                    image_source=(
                        parameters.get("url")
                        or parameters.get("path")
                        or parameters.get("file", "")
                    ),
                )
            )
            return "[表情包]"
        source = parameters.get("url") or parameters.get("file")
        if source:
            image_sources.append(source)
        return "[图片]"

    source = _CQ_IMAGE_PATTERN.sub(replace_image, source)

    def replace_mface(match: re.Match[str]) -> str:
        parameters = _cq_parameters(match.group(1))
        summary = parameters.get("summary") or "表情包"
        sticker_summaries.append(summary)
        sticker_sources.append(
            StickerSource(
                summary=summary,
                emoji_id=parameters.get("emoji_id", ""),
                emoji_package_id=parameters.get("emoji_package_id", ""),
                key=parameters.get("key", ""),
                image_source=(
                    parameters.get("url")
                    or parameters.get("path")
                    or parameters.get("file", "")
                ),
            )
        )
        return "[表情包]"

    source = _CQ_MFACE_PATTERN.sub(replace_mface, source)

    def replace_file(match: re.Match[str]) -> str:
        parameters = _cq_parameters(match.group(1))
        file_name = parameters.get("name") or parameters.get("file") or "文件"
        file_value = parameters.get("file", "")
        file_sources.append(
            FileSource(
                name=file_name,
                source=parameters.get("url") or file_value,
                file_id=parameters.get("file_id", ""),
            )
        )
        return f"[文件: {file_name}]"

    source = _CQ_FILE_PATTERN.sub(replace_file, source)

    def replace_record(match: re.Match[str]) -> str:
        parameters = _cq_parameters(match.group(1))
        audio_sources.append(
            AudioSource(
                source=parameters.get("path") or parameters.get("file", ""),
                file_id=parameters.get("file_id", ""),
            )
        )
        return "[语音]"

    source = _CQ_RECORD_PATTERN.sub(replace_record, source)

    reply_match = _CQ_REPLY_PATTERN.search(source)
    reply_id = reply_match.group(1) if reply_match else ""
    source = _CQ_REPLY_PATTERN.sub("", source)
    def replace_at(match: re.Match[str]) -> str:
        nonlocal mentioned
        qq = match.group(1)
        if self_id is not None and qq == str(self_id):
            mentioned = True
        else:
            try:
                mentioned_id = int(qq)
            except ValueError:
                mentioned_id = 0
            if mentioned_id > 0:
                mentioned_user_ids.append(mentioned_id)
        return ""

    source = _CQ_AT_PATTERN.sub(replace_at, source)
    source = _CQ_OTHER_PATTERN.sub("", source)
    return MessageContent(
        text=_normalize_text(source),
        mentioned=mentioned,
        mentioned_user_ids=tuple(dict.fromkeys(mentioned_user_ids)),
        image_sources=tuple(image_sources),
        file_sources=tuple(file_sources),
        audio_sources=tuple(audio_sources),
        reply_id=reply_id,
        sticker_summaries=tuple(sticker_summaries),
        sticker_sources=tuple(sticker_sources),
    )


def strip_trigger_prefix(text: str, prefixes: tuple[str, ...]) -> str:
    for prefix in sorted(prefixes, key=len, reverse=True):
        if text.lower().startswith(prefix.lower()):
            return text[len(prefix) :].lstrip(" \t\r\n:：,，")
    return text


def split_reply(text: str, max_chars: int) -> list[str]:
    value = text.strip()
    if not value:
        return []
    max_chars = max(100, max_chars)
    chunks: list[str] = []
    while len(value) > max_chars:
        window = value[:max_chars]
        split_at = max(
            window.rfind("\n\n"),
            window.rfind("\n"),
            window.rfind("。"),
            window.rfind("！"),
            window.rfind("？"),
        )
        if split_at < max_chars // 2:
            split_at = max_chars
        else:
            split_at += 1
        chunk = value[:split_at].strip()
        if chunk:
            chunks.append(chunk)
        value = value[split_at:].strip()
    if value:
        chunks.append(value)
    return chunks


def _normalize_text(value: str) -> str:
    lines = [line.rstrip() for line in value.replace("\r\n", "\n").split("\n")]
    return "\n".join(lines).strip()


def _cq_parameters(raw_parameters: str) -> dict[str, str]:
    parameters: dict[str, str] = {}
    for item in raw_parameters.split(","):
        key, separator, value = item.partition("=")
        if separator:
            parameters[key.strip()] = value.strip().replace("&amp;", "&")
    return parameters

