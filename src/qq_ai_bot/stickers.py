from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, unquote_to_bytes, urlparse

import httpx

from .onebot import StickerSource

logger = logging.getLogger(__name__)


class StickerAssetError(RuntimeError):
    pass


@dataclass(frozen=True)
class StickerAsset:
    path: Path
    sha256: str
    mime_type: str
    source: str


class StickerAssetStore:
    def __init__(
        self,
        asset_dir: Path | str,
        *,
        timeout_seconds: float = 45.0,
        trust_env: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.asset_dir = Path(asset_dir).expanduser().resolve()
        self.asset_dir.mkdir(parents=True, exist_ok=True)
        timeout = httpx.Timeout(timeout_seconds)
        self._local_client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            trust_env=False,
        )
        self._external_client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            trust_env=trust_env,
            transport=transport,
        )

    async def persist(self, source: str) -> StickerAsset:
        value = source.strip()
        if not value:
            raise StickerAssetError("Sticker source is empty")

        declared_type = ""
        if value.startswith("data:"):
            declared_type, data = self._decode_data_uri(value)
        elif value.startswith("base64://"):
            try:
                data = base64.b64decode(value[9:], validate=True)
            except (ValueError, binascii.Error) as exc:
                raise StickerAssetError("Invalid base64 sticker data") from exc
        elif value.startswith("file://"):
            data = await self._read_local_file(
                self._path_from_file_uri(value)
            )
        elif urlparse(value).scheme in {"http", "https"}:
            data, declared_type = await self._download(value)
        else:
            path = Path(value).expanduser()
            if not path.is_absolute():
                raise StickerAssetError(
                    "Sticker source is neither a URL nor an absolute file path"
                )
            data = await self._read_local_file(path)

        mime_type, suffix = _detect_sticker_type(data, declared_type)
        digest = hashlib.sha256(data).hexdigest()
        target = self.asset_dir / f"{digest}{suffix}"
        if target.is_file() and target.stat().st_size == len(data):
            return StickerAsset(target, digest, mime_type, source)

        temporary = self.asset_dir / f".{digest}.{uuid.uuid4().hex}.tmp"
        try:
            await asyncio.to_thread(temporary.write_bytes, data)
            await asyncio.to_thread(os.replace, temporary, target)
        except OSError as exc:
            raise StickerAssetError(
                f"Unable to save sticker asset: {exc}"
            ) from exc
        finally:
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass
        return StickerAsset(target, digest, mime_type, source)

    def contains(self, path: Path | str) -> bool:
        candidate = Path(path).expanduser().resolve()
        try:
            candidate.relative_to(self.asset_dir)
        except ValueError:
            return False
        return candidate.is_file()

    async def close(self) -> None:
        await self._local_client.aclose()
        await self._external_client.aclose()

    async def _download(self, url: str) -> tuple[bytes, str]:
        hostname = (urlparse(url).hostname or "").lower()
        client = (
            self._local_client
            if hostname in {"127.0.0.1", "::1", "localhost"}
            else self._external_client
        )
        try:
            response = await client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise StickerAssetError(
                f"Unable to download sticker image: {exc}"
            ) from exc
        return response.content, response.headers.get("content-type", "")

    async def _read_local_file(self, path: Path) -> bytes:
        try:
            if not path.is_file():
                raise StickerAssetError(f"Sticker file does not exist: {path}")
            return await asyncio.to_thread(path.read_bytes)
        except StickerAssetError:
            raise
        except OSError as exc:
            raise StickerAssetError(
                f"Unable to read sticker file: {exc}"
            ) from exc

    def _decode_data_uri(self, value: str) -> tuple[str, bytes]:
        header, separator, payload = value.partition(",")
        if not separator or not header.lower().startswith("data:image/"):
            raise StickerAssetError("Invalid sticker data URI")
        parts = header[5:].split(";")
        declared_type = parts[0].strip().lower()
        try:
            if "base64" in {part.strip().lower() for part in parts[1:]}:
                return declared_type, base64.b64decode(payload, validate=True)
            return declared_type, unquote_to_bytes(payload)
        except (ValueError, binascii.Error) as exc:
            raise StickerAssetError("Invalid sticker data URI") from exc

    def _path_from_file_uri(self, value: str) -> Path:
        path = unquote(urlparse(value).path)
        if os.name == "nt" and re.match(r"^/[A-Za-z]:/", path):
            path = path[1:]
        return Path(path)


def _detect_sticker_type(data: bytes, declared_type: str = "") -> tuple[str, str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if data.startswith(b"BM"):
        return "image/bmp", ".bmp"

    normalized = declared_type.split(";", 1)[0].strip().lower()
    known = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
    }
    if not data or normalized not in known:
        raise StickerAssetError(
            "Downloaded sticker is not a supported bitmap image"
        )
    return normalized, known[normalized]


class StickerStore:
    def __init__(
        self,
        database_path: Path | str,
        *,
        asset_dir: Path | str | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.asset_dir = Path(
            asset_dir or self.database_path.parent / "sticker_assets"
        ).expanduser()
        self._lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sticker_catalog (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    summary TEXT NOT NULL,
                    emoji_id TEXT NOT NULL,
                    emoji_package_id TEXT NOT NULL,
                    sticker_key TEXT NOT NULL,
                    image_source TEXT NOT NULL DEFAULT '',
                    local_path TEXT NOT NULL DEFAULT '',
                    asset_sha256 TEXT NOT NULL DEFAULT '',
                    mime_type TEXT NOT NULL DEFAULT '',
                    asset_verified_at TEXT NOT NULL DEFAULT '',
                    asset_error TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '未分类',
                    usage_hint TEXT NOT NULL DEFAULT '',
                    tags TEXT NOT NULL DEFAULT '[]',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    seen_at TEXT NOT NULL,
                    use_count INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(summary, emoji_id, emoji_package_id, sticker_key)
                )
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(sticker_catalog)"
                ).fetchall()
            }
            for column, definition in (
                ("image_source", "TEXT NOT NULL DEFAULT ''"),
                ("category", "TEXT NOT NULL DEFAULT '未分类'"),
                ("usage_hint", "TEXT NOT NULL DEFAULT ''"),
                ("tags", "TEXT NOT NULL DEFAULT '[]'"),
                ("enabled", "INTEGER NOT NULL DEFAULT 1"),
                ("local_path", "TEXT NOT NULL DEFAULT ''"),
                ("asset_sha256", "TEXT NOT NULL DEFAULT ''"),
                ("mime_type", "TEXT NOT NULL DEFAULT ''"),
                ("asset_verified_at", "TEXT NOT NULL DEFAULT ''"),
                ("asset_error", "TEXT NOT NULL DEFAULT ''"),
            ):
                if column not in columns:
                    connection.execute(
                        f"ALTER TABLE sticker_catalog "
                        f"ADD COLUMN {column} {definition}"
                    )
            asset_root = self.asset_dir.resolve()
            for row in connection.execute(
                """
                SELECT id, local_path
                FROM sticker_catalog
                WHERE local_path != ''
                """
            ).fetchall():
                current = Path(str(row["local_path"]))
                if not current.is_absolute():
                    continue
                try:
                    relative = current.resolve().relative_to(asset_root)
                except ValueError:
                    continue
                connection.execute(
                    "UPDATE sticker_catalog SET local_path = ? WHERE id = ?",
                    (str(relative), int(row["id"])),
                )

    def record(self, sticker: StickerSource) -> int | None:
        summary = sticker.summary.strip() or "表情包"
        fingerprint = sticker.key
        if not fingerprint and sticker.image_source:
            fingerprint = "url-" + hashlib.sha256(
                sticker.image_source.encode("utf-8")
            ).hexdigest()[:24]
        if not fingerprint and (
            sticker.emoji_id or sticker.emoji_package_id
        ):
            fingerprint = "emoji-" + hashlib.sha256(
                (
                    sticker.emoji_id
                    + "|"
                    + sticker.emoji_package_id
                ).encode("utf-8")
            ).hexdigest()[:24]
        if not fingerprint:
            if not sticker.image_source:
                return None
            fingerprint = "image"
        local_path = str(sticker.local_path).strip()
        asset_sha256 = sticker.asset_sha256.strip()
        mime_type = sticker.mime_type.strip()
        asset_verified_at = sticker.asset_verified_at.strip()
        if local_path and not asset_verified_at:
            asset_verified_at = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sticker_catalog
                    (
                        summary, emoji_id, emoji_package_id,
                        sticker_key, image_source, local_path,
                        asset_sha256, mime_type, asset_verified_at, seen_at
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(summary, emoji_id, emoji_package_id, sticker_key)
                DO UPDATE SET seen_at = excluded.seen_at,
                    image_source = CASE
                        WHEN excluded.image_source != ''
                        THEN excluded.image_source
                        ELSE sticker_catalog.image_source
                    END,
                    local_path = CASE
                        WHEN excluded.local_path != ''
                        THEN excluded.local_path
                        ELSE sticker_catalog.local_path
                    END,
                    asset_sha256 = CASE
                        WHEN excluded.asset_sha256 != ''
                        THEN excluded.asset_sha256
                        ELSE sticker_catalog.asset_sha256
                    END,
                    mime_type = CASE
                        WHEN excluded.mime_type != ''
                        THEN excluded.mime_type
                        ELSE sticker_catalog.mime_type
                    END,
                    asset_verified_at = CASE
                        WHEN excluded.asset_verified_at != ''
                        THEN excluded.asset_verified_at
                        ELSE sticker_catalog.asset_verified_at
                    END,
                    asset_error = CASE
                        WHEN excluded.local_path != '' THEN ''
                        ELSE sticker_catalog.asset_error
                    END
                """,
                (
                    summary,
                    sticker.emoji_id,
                    sticker.emoji_package_id,
                    fingerprint,
                    sticker.image_source,
                    local_path,
                    asset_sha256,
                    mime_type,
                    asset_verified_at,
                    _utc_now(),
                ),
            )
            row = connection.execute(
                """
                SELECT id FROM sticker_catalog
                WHERE summary = ? AND emoji_id = ?
                  AND emoji_package_id = ? AND sticker_key = ?
                """,
                (
                    summary,
                    sticker.emoji_id,
                    sticker.emoji_package_id,
                    fingerprint,
                ),
            ).fetchone()
            return int(row["id"]) if row else None

    def random_sticker(self, keyword: str = "") -> StickerSource | None:
        with self._lock, self._connect() as connection:
            if keyword:
                rows = connection.execute(
                    """
                    SELECT summary, emoji_id, emoji_package_id,
                           sticker_key, image_source, local_path,
                           asset_sha256, mime_type, asset_verified_at
                    FROM sticker_catalog
                    WHERE enabled = 1
                      AND (summary LIKE ? OR usage_hint LIKE ?
                           OR category LIKE ?)
                    ORDER BY RANDOM()
                    LIMIT 1
                    """,
                    (
                        f"%{keyword}%",
                        f"%{keyword}%",
                        f"%{keyword}%",
                    ),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT summary, emoji_id, emoji_package_id,
                           sticker_key, image_source, local_path,
                           asset_sha256, mime_type, asset_verified_at
                    FROM sticker_catalog
                    WHERE enabled = 1
                    ORDER BY RANDOM()
                    LIMIT 1
                    """
                ).fetchall()
        if not rows:
            return None
        row = rows[0]
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE sticker_catalog
                SET use_count = use_count + 1
                WHERE summary = ? AND emoji_id = ?
                  AND emoji_package_id = ? AND sticker_key = ?
                """,
                (
                    row["summary"],
                    row["emoji_id"],
                    row["emoji_package_id"],
                    row["sticker_key"],
                ),
            )
        return _source_from_row(row, self.asset_dir)

    def count(self) -> int:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM sticker_catalog WHERE enabled = 1"
            ).fetchone()
        return int(row["total"]) if row else 0

    def list_recent(
        self,
        limit: int = 50,
        *,
        include_disabled: bool = False,
    ) -> list[dict[str, object]]:
        where = "" if include_disabled else "WHERE enabled = 1"
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, summary, emoji_id, emoji_package_id,
                       sticker_key, image_source, local_path,
                       asset_sha256, mime_type, asset_verified_at,
                       asset_error, category, usage_hint, tags, enabled,
                       seen_at, use_count
                FROM sticker_catalog
                {where}
                ORDER BY seen_at DESC
                LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()
        result = [dict(row) for row in rows]
        for item in result:
            try:
                item["tags"] = json.loads(str(item.get("tags") or "[]"))
            except json.JSONDecodeError:
                item["tags"] = []
            asset_path = self._resolve_asset_path(
                str(item.get("local_path") or "")
            )
            item["local_path"] = (
                str(asset_path) if asset_path is not None else ""
            )
            has_local_asset = bool(
                asset_path is not None and asset_path.is_file()
            )
            item["has_local_asset"] = has_local_asset
            item["asset_url"] = (
                f"/api/admin/stickers/{item['id']}/asset"
                if has_local_asset
                else ""
            )
        return result

    def get_by_id(self, sticker_id: int) -> StickerSource | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT summary, emoji_id, emoji_package_id,
                       sticker_key, image_source, local_path,
                       asset_sha256, mime_type, asset_verified_at
                FROM sticker_catalog
                WHERE id = ? AND enabled = 1
                """,
                (sticker_id,),
            ).fetchone()
            if not row:
                return None
            connection.execute(
                "UPDATE sticker_catalog SET use_count = use_count + 1 WHERE id = ?",
                (sticker_id,),
            )
        return _source_from_row(row, self.asset_dir)

    def get_metadata(self, sticker_id: int) -> dict[str, object] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, summary, emoji_id, emoji_package_id,
                       sticker_key, image_source, local_path,
                       asset_sha256, mime_type, asset_verified_at,
                       asset_error, category, usage_hint, tags, enabled,
                       seen_at, use_count
                FROM sticker_catalog
                WHERE id = ?
                """,
                (sticker_id,),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        try:
            item["tags"] = json.loads(str(item.get("tags") or "[]"))
        except json.JSONDecodeError:
            item["tags"] = []
        asset_path = self._resolve_asset_path(
            str(item.get("local_path") or "")
        )
        item["local_path"] = str(asset_path) if asset_path is not None else ""
        has_local_asset = bool(
            asset_path is not None and asset_path.is_file()
        )
        item["has_local_asset"] = has_local_asset
        item["asset_url"] = (
            f"/api/admin/stickers/{sticker_id}/asset"
            if has_local_asset
            else ""
        )
        return item

    def attach_asset(
        self,
        sticker_id: int,
        *,
        local_path: Path | str,
        sha256: str,
        mime_type: str,
    ) -> bool:
        asset = Path(local_path).expanduser().resolve()
        try:
            stored_path = str(asset.relative_to(self.asset_dir.resolve()))
        except ValueError:
            stored_path = str(asset)
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE sticker_catalog
                SET local_path = ?, asset_sha256 = ?, mime_type = ?,
                    asset_verified_at = ?, asset_error = ''
                WHERE id = ?
                """,
                (
                    stored_path,
                    sha256,
                    mime_type,
                    _utc_now(),
                    sticker_id,
                ),
            )
            return cursor.rowcount > 0

    def mark_asset_failed(self, sticker_id: int, error: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE sticker_catalog
                SET asset_error = ?
                WHERE id = ?
                """,
                (error.strip()[:500], sticker_id),
            )
            return cursor.rowcount > 0

    def list_repair_candidates(self, limit: int = 100) -> list[dict[str, object]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, summary, emoji_id, emoji_package_id,
                       sticker_key, image_source, local_path,
                       asset_sha256, mime_type, asset_verified_at
                FROM sticker_catalog
                WHERE image_source != ''
                ORDER BY seen_at DESC
                LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()
        candidates: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            asset_path = self._resolve_asset_path(
                str(item.get("local_path") or "")
            )
            item["local_path"] = (
                str(asset_path) if asset_path is not None else ""
            )
            if (
                asset_path is None
                or not asset_path.is_file()
            ):
                candidates.append(item)
        return candidates

    def local_asset_path(self, sticker_id: int) -> Path | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT local_path FROM sticker_catalog WHERE id = ?",
                (sticker_id,),
            ).fetchone()
        if not row:
            return None
        path = self._resolve_asset_path(str(row["local_path"] or ""))
        return path if path is not None and path.is_file() else None

    def _resolve_asset_path(self, value: str) -> Path | None:
        if not value:
            return None
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.asset_dir / path
        return path.resolve()

    def update(
        self,
        sticker_id: int,
        *,
        category: str,
        usage_hint: str,
        enabled: bool,
    ) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE sticker_catalog
                SET category = ?, usage_hint = ?, enabled = ?
                WHERE id = ?
                """,
                (
                    category.strip()[:80] or "未分类",
                    usage_hint.strip()[:300],
                    1 if enabled else 0,
                    sticker_id,
                ),
            )
            return cursor.rowcount > 0

    def delete(self, sticker_id: int) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM sticker_catalog WHERE id = ?",
                (sticker_id,),
            )
            return cursor.rowcount > 0

    def categories(self) -> list[str]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT category
                FROM sticker_catalog
                WHERE enabled = 1
                ORDER BY category ASC
                """
            ).fetchall()
        return [str(row["category"]) for row in rows]

    def list_unclassified(self, limit: int = 100) -> list[dict[str, object]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, summary, emoji_id, emoji_package_id,
                       sticker_key, image_source, category, usage_hint
                FROM sticker_catalog
                WHERE category = '未分类' AND usage_hint = ''
                ORDER BY seen_at DESC
                LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_classification(
        self,
        sticker_id: int,
        *,
        category: str,
        usage_hint: str,
        tags: tuple[str, ...] = (),
    ) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE sticker_catalog
                SET category = ?, usage_hint = ?, tags = ?
                WHERE id = ?
                """,
                (
                    category.strip()[:80] or "未分类",
                    usage_hint.strip()[:300],
                    json.dumps(list(tags[:12]), ensure_ascii=False),
                    sticker_id,
                ),
            )
            return cursor.rowcount > 0


def _source_from_row(
    row: sqlite3.Row,
    asset_dir: Path,
) -> StickerSource:
    local_path = str(row["local_path"])
    if local_path and not Path(local_path).is_absolute():
        local_path = str(asset_dir / local_path)
    return StickerSource(
        summary=str(row["summary"]),
        emoji_id=str(row["emoji_id"]),
        emoji_package_id=str(row["emoji_package_id"]),
        key=str(row["sticker_key"]),
        image_source=str(row["image_source"]),
        local_path=local_path,
        asset_sha256=str(row["asset_sha256"]),
        mime_type=str(row["mime_type"]),
        asset_verified_at=str(row["asset_verified_at"]),
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
