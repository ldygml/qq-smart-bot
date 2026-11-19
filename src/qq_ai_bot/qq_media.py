from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import secrets
import sqlite3
import threading
import unicodedata
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class QQMediaError(ValueError):
    pass


@dataclass(frozen=True)
class QQMediaItem:
    id: int
    sha256: str
    original_name: str
    stored_name: str
    mime_type: str
    size: int
    kind: str
    created_at: str
    last_used_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "library_id": f"upload:{self.id}",
            "source": "upload",
            "id": self.id,
            "name": self.original_name,
            "mime_type": self.mime_type,
            "size": self.size,
            "type": self.kind,
            "sha256": self.sha256,
            "created_at": self.created_at,
        }


def _safe_display_name(value: str, fallback: str = "文件") -> str:
    normalized = unicodedata.normalize("NFC", str(value or ""))
    normalized = normalized.replace("\x00", "").replace(chr(92), "/")
    name = Path(normalized).name.strip()
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", name)
    name = name.strip(" .")
    if not name or name in {".", ".."}:
        name = fallback
    if len(name) <= 180:
        return name
    suffix = Path(name).suffix[:20]
    stem = Path(name).stem
    return f"{stem[: max(1, 180 - len(suffix))]}{suffix}"


def media_kind_for(name: str, mime_type: str) -> str:
    guessed = str(mime_type or "").split(";", 1)[0].strip().lower()
    if guessed.startswith("image/"):
        return "image"
    if guessed.startswith("video/"):
        return "video"
    if guessed.startswith("audio/"):
        return "audio"
    fallback = mimetypes.guess_type(name)[0] or ""
    if fallback.startswith("image/"):
        return "image"
    if fallback.startswith("video/"):
        return "video"
    if fallback.startswith("audio/"):
        return "audio"
    return "file"


def _safe_mime_type(value: str, name: str) -> str:
    mime_type = str(value or "").split(";", 1)[0].strip().lower()
    if not re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", mime_type):
        mime_type = mimetypes.guess_type(name)[0] or ""
    return mime_type[:120] or "application/octet-stream"


def _stored_suffix(name: str) -> str:
    suffix = Path(name).suffix.lower()
    if re.fullmatch(r"\.[a-z0-9]{1,12}", suffix):
        return suffix
    return ".bin"


class QQMediaStore:
    """Durable, deduplicated storage for files uploaded from the web console."""

    def __init__(
        self,
        database_path: Path | str,
        root_dir: Path | str,
        *,
        chunk_bytes: int = 4 * 1024 * 1024,
        max_upload_bytes: int = 0,
        session_hours: int = 24,
    ) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.root_dir = Path(root_dir).expanduser().resolve()
        self.files_dir = self.root_dir / "files"
        self.uploads_dir = self.root_dir / "uploads"
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        self.chunk_bytes = max(64 * 1024, int(chunk_bytes))
        self.max_upload_bytes = max(0, int(max_upload_bytes))
        self.session_hours = max(1, int(session_hours))
        self._lock = threading.RLock()
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS qq_media_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sha256 TEXT NOT NULL UNIQUE,
                    original_name TEXT NOT NULL,
                    stored_name TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_used_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS qq_media_uploads (
                    id TEXT PRIMARY KEY,
                    original_name TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    expected_size INTEGER NOT NULL,
                    total_chunks INTEGER NOT NULL,
                    received_chunks INTEGER NOT NULL DEFAULT 0,
                    received_bytes INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_qq_media_created
                ON qq_media_items(created_at DESC)
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=20)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def init_upload(
        self,
        *,
        name: str,
        size: int,
        mime_type: str = "",
    ) -> dict[str, Any]:
        expected_size = int(size)
        if expected_size < 0:
            raise QQMediaError("文件大小无效")
        if self.max_upload_bytes and expected_size > self.max_upload_bytes:
            raise QQMediaError("文件超过当前实例允许的上传大小")
        display_name = _safe_display_name(name)
        safe_type = _safe_mime_type(mime_type, display_name)
        total_chunks = max(1, (expected_size + self.chunk_bytes - 1) // self.chunk_bytes)
        upload_id = secrets.token_urlsafe(24)
        now = self._now()
        with self._lock, self._connect() as connection:
            self._cleanup_expired(connection)
            connection.execute(
                """
                INSERT INTO qq_media_uploads (
                    id, original_name, mime_type, expected_size,
                    total_chunks, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    upload_id,
                    display_name,
                    safe_type,
                    expected_size,
                    total_chunks,
                    now,
                    now,
                ),
            )
        (self.uploads_dir / upload_id).mkdir(parents=True, exist_ok=True)
        return {
            "upload_id": upload_id,
            "name": display_name,
            "size": expected_size,
            "mime_type": safe_type,
            "chunk_size": self.chunk_bytes,
            "total_chunks": total_chunks,
        }

    def _upload_row(self, upload_id: str) -> sqlite3.Row:
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", upload_id):
            raise QQMediaError("上传任务不存在或已过期")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM qq_media_uploads WHERE id = ?",
                (upload_id,),
            ).fetchone()
        if row is None:
            raise QQMediaError("上传任务不存在或已过期")
        return row

    async def append_chunk(
        self,
        upload_id: str,
        index: int,
        chunks: AsyncIterator[bytes],
    ) -> dict[str, Any]:
        row = self._upload_row(upload_id)
        total_chunks = int(row["total_chunks"])
        if index < 0 or index >= total_chunks:
            raise QQMediaError("分片序号无效")
        upload_dir = self.uploads_dir / upload_id
        upload_dir.mkdir(parents=True, exist_ok=True)
        temporary = upload_dir / f".{index}.{uuid.uuid4().hex}.part"
        written = 0
        try:
            with temporary.open("wb") as target:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > self.chunk_bytes:
                        raise QQMediaError("单个分片超过上传限制")
                    target.write(chunk)
            self._commit_chunk(upload_id, index, temporary, written)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return {"ok": True, "index": index, "size": written}

    def _commit_chunk(
        self,
        upload_id: str,
        index: int,
        temporary: Path,
        written: int,
    ) -> None:
        target = self.uploads_dir / upload_id / f"{index:08d}.chunk"
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM qq_media_uploads WHERE id = ?",
                (upload_id,),
            ).fetchone()
            if row is None:
                raise QQMediaError("上传任务不存在或已过期")
            old_size = target.stat().st_size if target.is_file() else 0
            received_bytes = (
                int(row["received_bytes"] or 0) - old_size + written
            )
            if received_bytes > int(row["expected_size"]):
                raise QQMediaError("上传内容超过声明的文件大小")
            os.replace(temporary, target)
            received_chunks = len(
                [
                    path
                    for path in target.parent.glob("*.chunk")
                    if path.is_file()
                ]
            )
            connection.execute(
                """
                UPDATE qq_media_uploads
                SET received_chunks = ?, received_bytes = ?, updated_at = ?
                WHERE id = ?
                """,
                (received_chunks, received_bytes, self._now(), upload_id),
            )

    def upload_status(self, upload_id: str) -> dict[str, Any]:
        row = self._upload_row(upload_id)
        return {
            "upload_id": upload_id,
            "name": str(row["original_name"]),
            "size": int(row["expected_size"]),
            "received_chunks": int(row["received_chunks"]),
            "received_bytes": int(row["received_bytes"]),
            "total_chunks": int(row["total_chunks"]),
        }

    def complete_upload(self, upload_id: str) -> QQMediaItem:
        with self._lock:
            row = self._upload_row(upload_id)
            upload_dir = self.uploads_dir / upload_id
            total_chunks = int(row["total_chunks"])
            chunk_paths = [
                upload_dir / f"{index:08d}.chunk"
                for index in range(total_chunks)
            ]
            missing = [index for index, path in enumerate(chunk_paths) if not path.is_file()]
            if missing:
                raise QQMediaError(
                    f"仍有 {len(missing)} 个分片未上传，请继续上传"
                )
            expected_size = int(row["expected_size"])
            actual_size = sum(path.stat().st_size for path in chunk_paths)
            if actual_size != expected_size:
                raise QQMediaError("上传文件大小不一致，请重新上传")
            display_name = _safe_display_name(str(row["original_name"]))
            mime_type = _safe_mime_type(str(row["mime_type"]), display_name)
            digest = hashlib.sha256()
            temporary = self.files_dir / f".{uuid.uuid4().hex}.assembling"
            try:
                with temporary.open("wb") as target:
                    for path in chunk_paths:
                        with path.open("rb") as source:
                            while True:
                                block = source.read(1024 * 1024)
                                if not block:
                                    break
                                digest.update(block)
                                target.write(block)
                sha256 = digest.hexdigest()
                existing = self._find_by_sha256(sha256)
                if existing is not None:
                    item = existing
                    temporary.unlink(missing_ok=True)
                else:
                    stored_name = f"{sha256}{_stored_suffix(display_name)}"
                    final_path = self.files_dir / stored_name
                    os.replace(temporary, final_path)
                    item = self._insert_item(
                        sha256=sha256,
                        original_name=display_name,
                        stored_name=stored_name,
                        mime_type=mime_type,
                        size=actual_size,
                    )
            finally:
                temporary.unlink(missing_ok=True)
            connection = self._connect()
            try:
                connection.execute(
                    "DELETE FROM qq_media_uploads WHERE id = ?",
                    (upload_id,),
                )
                connection.commit()
            finally:
                connection.close()
            self._remove_upload_dir(upload_dir)
            return item

    def _insert_item(
        self,
        *,
        sha256: str,
        original_name: str,
        stored_name: str,
        mime_type: str,
        size: int,
    ) -> QQMediaItem:
        now = self._now()
        kind = media_kind_for(original_name, mime_type)
        with self._connect() as connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO qq_media_items (
                        sha256, original_name, stored_name, mime_type,
                        size, kind, created_at, last_used_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sha256,
                        original_name,
                        stored_name,
                        mime_type,
                        size,
                        kind,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                existing = self._find_by_sha256(sha256)
                if existing is None:
                    raise
                return existing
            record_id = int(cursor.lastrowid)
        item = self.get(record_id)
        if item is None:
            raise QQMediaError("上传文件索引写入失败")
        return item

    def _find_by_sha256(self, sha256: str) -> QQMediaItem | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM qq_media_items WHERE sha256 = ?",
                (sha256,),
            ).fetchone()
        return self._item(row) if row is not None else None

    def get(self, item_id: int) -> QQMediaItem | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM qq_media_items WHERE id = ?",
                (int(item_id),),
            ).fetchone()
        return self._item(row) if row is not None else None

    def path_for(self, item: QQMediaItem) -> Path | None:
        candidate = (self.files_dir / item.stored_name).resolve()
        try:
            candidate.relative_to(self.files_dir)
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    def list_items(self, limit: int = 300) -> list[QQMediaItem]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM qq_media_items
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (max(1, min(1000, int(limit))),),
            ).fetchall()
        return [self._item(row) for row in rows]

    def mark_used(self, item_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE qq_media_items SET last_used_at = ? WHERE id = ?",
                (self._now(), int(item_id)),
            )

    def delete(self, item_id: int) -> bool:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT stored_name FROM qq_media_items WHERE id = ?",
                (int(item_id),),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                "DELETE FROM qq_media_items WHERE id = ?",
                (int(item_id),),
            )
        path = (self.files_dir / str(row["stored_name"])).resolve()
        try:
            path.relative_to(self.files_dir)
        except ValueError:
            return True
        path.unlink(missing_ok=True)
        return True

    def cancel_upload(self, upload_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM qq_media_uploads WHERE id = ?",
                (upload_id,),
            )
        self._remove_upload_dir(self.uploads_dir / upload_id)
        return cursor.rowcount > 0

    def _cleanup_expired(self, connection: sqlite3.Connection) -> None:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=self.session_hours)
        ).isoformat(timespec="seconds")
        rows = connection.execute(
            "SELECT id FROM qq_media_uploads WHERE updated_at < ?",
            (cutoff,),
        ).fetchall()
        for row in rows:
            connection.execute(
                "DELETE FROM qq_media_uploads WHERE id = ?",
                (str(row["id"]),),
            )
            self._remove_upload_dir(self.uploads_dir / str(row["id"]))

    @staticmethod
    def _remove_upload_dir(path: Path) -> None:
        if not path.is_dir():
            return
        for child in path.iterdir():
            if child.is_file():
                child.unlink(missing_ok=True)
        try:
            path.rmdir()
        except OSError:
            pass

    @staticmethod
    def _item(row: sqlite3.Row) -> QQMediaItem:
        return QQMediaItem(
            id=int(row["id"]),
            sha256=str(row["sha256"] or ""),
            original_name=str(row["original_name"] or "文件"),
            stored_name=str(row["stored_name"] or ""),
            mime_type=str(row["mime_type"] or "application/octet-stream"),
            size=int(row["size"] or 0),
            kind=str(row["kind"] or "file"),
            created_at=str(row["created_at"] or ""),
            last_used_at=str(row["last_used_at"] or ""),
        )
