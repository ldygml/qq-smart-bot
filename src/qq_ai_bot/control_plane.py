from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import psutil
import uvicorn
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from dotenv import dotenv_values
from fastapi import Body, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEB_ROOT = PROJECT_ROOT / "web"
RUNTIME_ROOT = PROJECT_ROOT / ".runtime"
CONTROL_DB = PROJECT_ROOT / "data" / "control.db"
SESSION_COOKIE = "qq_control_session"
SESSION_HOURS = 24
BACKUP_MAGIC = b"QQBOTBK1"
MAX_BACKUP_BYTES = 300 * 1024 * 1024


def _control_key() -> bytes:
    env_key = os.getenv("CONTROL_MASTER_KEY", "").strip()
    if not env_key:
        env_key = str(
            dotenv_values(PROJECT_ROOT / ".env").get(
                "CONTROL_MASTER_KEY",
                "",
            )
            or ""
        ).strip()
    if env_key:
        return env_key.encode("ascii")
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    path = RUNTIME_ROOT / "control-master.key"
    if path.exists():
        return path.read_bytes().strip()
    key = Fernet.generate_key()
    path.write_bytes(key)
    return key


class SecretBox:
    def __init__(self) -> None:
        self._fernet = Fernet(_control_key())

    def encrypt(self, value: str) -> str:
        if not value:
            return ""
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        if not value:
            return ""
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise RuntimeError("Encrypted secret cannot be decrypted") from exc


def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        600_000,
    )
    return (
        base64.b64encode(salt).decode("ascii")
        + "$"
        + base64.b64encode(digest).decode("ascii")
    )


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt_text, digest_text = stored.split("$", 1)
        expected = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            base64.b64decode(salt_text),
            600_000,
        )
        return secrets.compare_digest(
            expected,
            base64.b64decode(digest_text),
        )
    except (ValueError, TypeError):
        return False


def encrypt_backup(payload: bytes, passphrase: str) -> bytes:
    if len(passphrase) < 8:
        raise ValueError("备份口令至少需要 8 个字符")
    salt = secrets.token_bytes(16)
    key = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=390_000,
    ).derive(passphrase.encode("utf-8"))
    token = Fernet(base64.urlsafe_b64encode(key)).encrypt(payload)
    return BACKUP_MAGIC + salt + token


def decrypt_backup(payload: bytes, passphrase: str) -> bytes:
    if not payload.startswith(BACKUP_MAGIC) or len(payload) < 60:
        raise ValueError("不是有效的机器人备份文件")
    salt = payload[len(BACKUP_MAGIC) : len(BACKUP_MAGIC) + 16]
    token = payload[len(BACKUP_MAGIC) + 16 :]
    key = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=390_000,
    ).derive(passphrase.encode("utf-8"))
    try:
        return Fernet(base64.urlsafe_b64encode(key)).decrypt(token)
    except InvalidToken as exc:
        raise ValueError("备份口令错误或文件已损坏") from exc


def _sqlite_snapshot(path: Path) -> bytes:
    if not path.is_file():
        raise ValueError(f"数据库不存在：{path}")
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "snapshot.db"
        source_connection = sqlite3.connect(path, timeout=20)
        target_connection = sqlite3.connect(target)
        try:
            source_connection.backup(target_connection)
            target_connection.commit()
        finally:
            target_connection.close()
            source_connection.close()
        return target.read_bytes()


def _project_path(path_text: str, project_root: Path) -> Path:
    path = Path(path_text)
    if not path.is_absolute():
        path = project_root / path
    resolved = path.resolve()
    if not resolved.is_relative_to(project_root.resolve()):
        raise ValueError(f"备份路径超出项目目录：{path_text}")
    return resolved


def build_encrypted_backup(
    *,
    instances: list[dict[str, Any]],
    username: str,
    passphrase: str,
    project_root: Path = PROJECT_ROOT,
    control_path: Path = CONTROL_DB,
) -> bytes:
    database_paths = [control_path]
    for item in instances:
        database_paths.append(
            _project_path(str(item["database_path"]), project_root)
        )
    unique_paths = list(dict.fromkeys(path.resolve() for path in database_paths))
    manifest: dict[str, Any] = {
        "version": 1,
        "created_at": _utc_now(),
        "username": username,
        "databases": [],
        "files": [],
    }
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(
        archive_buffer,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as archive:
        for index, path in enumerate(unique_paths):
            archive_name = f"databases/{index}.db"
            archive.writestr(archive_name, _sqlite_snapshot(path))
            manifest_path = (
                "__CONTROL_DB__"
                if path == control_path.resolve()
                else path.relative_to(project_root.resolve()).as_posix()
            )
            manifest["databases"].append(
                {
                    "archive": archive_name,
                    "path": manifest_path,
                }
            )
        optional_files = {
            ".env": project_root / ".env",
            ".runtime/control-master.key": (
                project_root / ".runtime" / "control-master.key"
            ),
        }
        for relative_name, path in optional_files.items():
            if not path.is_file():
                continue
            archive_name = f"config/{relative_name.replace('/', '_')}"
            archive.write(path, archive_name)
            manifest["files"].append(
                {
                    "archive": archive_name,
                    "path": relative_name,
                }
            )
        archive.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )
    return encrypt_backup(archive_buffer.getvalue(), passphrase)


def restore_encrypted_backup(
    *,
    encrypted: bytes,
    passphrase: str,
    project_root: Path = PROJECT_ROOT,
    control_path: Path = CONTROL_DB,
) -> dict[str, Any]:
    raw = decrypt_backup(encrypted, passphrase)
    if len(raw) > MAX_BACKUP_BYTES:
        raise ValueError("备份解压后体积过大")
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ValueError("备份压缩包已损坏") from exc
    with archive:
        if sum(item.file_size for item in archive.infolist()) > MAX_BACKUP_BYTES:
            raise ValueError("备份解压后体积过大")
        try:
            manifest = json.loads(archive.read("manifest.json"))
        except (KeyError, json.JSONDecodeError) as exc:
            raise ValueError("备份清单缺失或损坏") from exc
        if not isinstance(manifest, dict) or manifest.get("version") != 1:
            raise ValueError("不支持的备份版本")
        safety_dir = (
            project_root
            / ".runtime"
            / f"pre-restore-{datetime.now():%Y%m%d-%H%M%S}"
        )
        safety_dir.mkdir(parents=True, exist_ok=True)
        restored: list[str] = []
        for index, item in enumerate(manifest.get("databases") or []):
            if not isinstance(item, dict):
                continue
            archive_name = str(item.get("archive") or "")
            path_text = str(item.get("path") or "")
            if not archive_name or not path_text:
                continue
            if path_text == "__CONTROL_DB__":
                target = control_path.resolve()
            else:
                target = _project_path(path_text, project_root)
            if target.is_file():
                (safety_dir / f"database-{index}.db").write_bytes(
                    _sqlite_snapshot(target)
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            temp_path = target.with_name(
                f".{target.name}.restore-{secrets.token_hex(4)}.tmp"
            )
            temp_path.write_bytes(archive.read(archive_name))
            os.replace(temp_path, target)
            try:
                restored.append(
                    str(target.relative_to(project_root.resolve()))
                )
            except ValueError:
                restored.append("control.db")
        for index, item in enumerate(manifest.get("files") or []):
            if not isinstance(item, dict):
                continue
            archive_name = str(item.get("archive") or "")
            relative_name = str(item.get("path") or "")
            if not archive_name or not relative_name:
                continue
            target = _project_path(relative_name, project_root)
            if target.is_file():
                (safety_dir / f"config-{index}-{target.name}").write_bytes(
                    target.read_bytes()
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            temp_path = target.with_name(
                f".{target.name}.restore-{secrets.token_hex(4)}.tmp"
            )
            temp_path.write_bytes(archive.read(archive_name))
            os.replace(temp_path, target)
            restored.append(relative_name)
    return {
        "restored": restored,
        "safety_directory": str(safety_dir),
        "created_at": manifest.get("created_at"),
    }


class ControlStore:
    def __init__(self, path: Path = CONTROL_DB) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS control_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'user',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS control_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    csrf_token TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS provider_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    provider_type TEXT NOT NULL,
                    base_url TEXT NOT NULL,
                    model TEXT NOT NULL,
                    api_key_encrypted TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bot_instances (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    port INTEGER NOT NULL,
                    onebot_api_base TEXT NOT NULL,
                    onebot_token_encrypted TEXT NOT NULL,
                    provider_id INTEGER NOT NULL,
                    database_path TEXT NOT NULL,
                    pid INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS instance_model_routes (
                    instance_id INTEGER NOT NULL,
                    capability TEXT NOT NULL,
                    primary_provider_id INTEGER NOT NULL,
                    primary_model TEXT NOT NULL,
                    fallbacks_json TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (instance_id, capability)
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def user_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM control_users"
            ).fetchone()
        return int(row["total"]) if row else 0

    def create_user(self, username: str, password: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO control_users
                    (username, password_hash, role, created_at)
                VALUES (?, ?, 'admin', ?)
                """,
                (username, _hash_password(password), _utc_now()),
            )
            return int(cursor.lastrowid)

    def authenticate(self, username: str, password: str) -> sqlite3.Row | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM control_users WHERE username = ?",
                (username,),
            ).fetchone()
        if row and _verify_password(password, str(row["password_hash"])):
            return row
        return None

    def create_session(self, user_id: int) -> tuple[str, str]:
        token = secrets.token_urlsafe(36)
        csrf = secrets.token_urlsafe(24)
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        expires = (
            datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)
        ).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO control_sessions
                    (token_hash, user_id, csrf_token, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (digest, user_id, csrf, expires),
            )
        return token, csrf

    def get_session(self, token: str) -> sqlite3.Row | None:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT s.csrf_token, s.expires_at,
                       u.id, u.username, u.role
                FROM control_sessions s
                JOIN control_users u ON u.id = s.user_id
                WHERE s.token_hash = ?
                """,
                (digest,),
            ).fetchone()
            if not row:
                return None
            if datetime.fromisoformat(str(row["expires_at"])) <= datetime.now(
                timezone.utc
            ):
                connection.execute(
                    "DELETE FROM control_sessions WHERE token_hash = ?",
                    (digest,),
                )
                return None
        return row

    def delete_session(self, token: str) -> None:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM control_sessions WHERE token_hash = ?",
                (digest,),
            )

    def providers(self, user_id: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, name, provider_type, base_url, model,
                       api_key_encrypted, created_at, updated_at
                FROM provider_profiles
                WHERE user_id = ?
                ORDER BY id DESC
                """,
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def provider(self, provider_id: int, user_id: int) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                """
                SELECT * FROM provider_profiles
                WHERE id = ? AND user_id = ?
                """,
                (provider_id, user_id),
            ).fetchone()

    def provider_by_name(self, user_id: int, name: str) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                """
                SELECT * FROM provider_profiles
                WHERE user_id = ? AND name = ?
                LIMIT 1
                """,
                (user_id, name),
            ).fetchone()

    def save_provider(
        self,
        *,
        user_id: int,
        provider_id: int | None,
        name: str,
        provider_type: str,
        base_url: str,
        model: str,
        api_key_encrypted: str,
    ) -> int:
        now = _utc_now()
        with self._connect() as connection:
            if provider_id:
                existing = connection.execute(
                    """
                    SELECT api_key_encrypted FROM provider_profiles
                    WHERE id = ? AND user_id = ?
                    """,
                    (provider_id, user_id),
                ).fetchone()
                if not existing:
                    raise ValueError("Provider not found")
                encrypted = (
                    api_key_encrypted
                    or str(existing["api_key_encrypted"])
                )
                connection.execute(
                    """
                    UPDATE provider_profiles
                    SET name = ?, provider_type = ?, base_url = ?,
                        model = ?, api_key_encrypted = ?, updated_at = ?
                    WHERE id = ? AND user_id = ?
                    """,
                    (
                        name,
                        provider_type,
                        base_url,
                        model,
                        encrypted,
                        now,
                        provider_id,
                        user_id,
                    ),
                )
                return provider_id
            cursor = connection.execute(
                """
                INSERT INTO provider_profiles
                    (
                        user_id, name, provider_type, base_url,
                        model, api_key_encrypted, created_at, updated_at
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    name,
                    provider_type,
                    base_url,
                    model,
                    api_key_encrypted,
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def delete_provider(self, provider_id: int, user_id: int) -> bool:
        with self._connect() as connection:
            in_use = connection.execute(
                "SELECT 1 FROM bot_instances WHERE provider_id = ? LIMIT 1",
                (provider_id,),
            ).fetchone()
            if in_use:
                raise ValueError("Provider is used by an instance")
            cursor = connection.execute(
                "DELETE FROM provider_profiles WHERE id = ? AND user_id = ?",
                (provider_id, user_id),
            )
            return cursor.rowcount > 0

    def instances(self, user_id: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT i.*, p.name AS provider_name
                FROM bot_instances i
                LEFT JOIN provider_profiles p ON p.id = i.provider_id
                WHERE i.user_id = ?
                ORDER BY i.id ASC
                """,
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def instance(self, instance_id: int, user_id: int) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                """
                SELECT i.*, p.name AS provider_name,
                       p.base_url, p.model, p.api_key_encrypted,
                       p.provider_type
                FROM bot_instances i
                JOIN provider_profiles p ON p.id = i.provider_id
                WHERE i.id = ? AND i.user_id = ?
                """,
                (instance_id, user_id),
            ).fetchone()

    def instance_by_port(self, port: int, user_id: int) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                """
                SELECT * FROM bot_instances
                WHERE port = ? AND user_id = ?
                LIMIT 1
                """,
                (port, user_id),
            ).fetchone()

    def save_instance(
        self,
        *,
        user_id: int,
        instance_id: int | None,
        name: str,
        port: int,
        onebot_api_base: str,
        onebot_token_encrypted: str,
        provider_id: int,
        database_path: str,
    ) -> int:
        now = _utc_now()
        with self._connect() as connection:
            if instance_id:
                existing = connection.execute(
                    """
                    SELECT onebot_token_encrypted FROM bot_instances
                    WHERE id = ? AND user_id = ?
                    """,
                    (instance_id, user_id),
                ).fetchone()
                if not existing:
                    raise ValueError("Instance not found")
                token = (
                    onebot_token_encrypted
                    or str(existing["onebot_token_encrypted"])
                )
                connection.execute(
                    """
                    UPDATE bot_instances
                    SET name = ?, port = ?, onebot_api_base = ?,
                        onebot_token_encrypted = ?, provider_id = ?,
                        database_path = ?, updated_at = ?
                    WHERE id = ? AND user_id = ?
                    """,
                    (
                        name,
                        port,
                        onebot_api_base,
                        token,
                        provider_id,
                        database_path,
                        now,
                        instance_id,
                        user_id,
                    ),
                )
                return instance_id
            cursor = connection.execute(
                """
                INSERT INTO bot_instances
                    (
                        user_id, name, port, onebot_api_base,
                        onebot_token_encrypted, provider_id, database_path,
                        created_at, updated_at
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    name,
                    port,
                    onebot_api_base,
                    onebot_token_encrypted,
                    provider_id,
                    database_path,
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def set_instance_pid(self, instance_id: int, pid: int | None) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE bot_instances SET pid = ?, updated_at = ? WHERE id = ?",
                (pid, _utc_now(), instance_id),
            )

    def delete_instance(self, instance_id: int, user_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM bot_instances WHERE id = ? AND user_id = ?",
                (instance_id, user_id),
            )
            return cursor.rowcount > 0

    def model_routes(
        self,
        instance_id: int,
        user_id: int,
    ) -> list[dict[str, Any]]:
        if self.instance(instance_id, user_id) is None:
            raise ValueError("Instance not found")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT capability, primary_provider_id,
                       primary_model, fallbacks_json, updated_at
                FROM instance_model_routes
                WHERE instance_id = ?
                ORDER BY capability ASC
                """,
                (instance_id,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["fallbacks"] = json.loads(
                    str(item.pop("fallbacks_json"))
                )
            except json.JSONDecodeError:
                item["fallbacks"] = []
            result.append(item)
        return result

    def save_model_routes(
        self,
        instance_id: int,
        user_id: int,
        routes: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if self.instance(instance_id, user_id) is None:
            raise ValueError("Instance not found")
        capabilities = {
            "chat",
            "code",
            "vision",
            "ocr",
            "audio",
            "tts",
            "embedding",
            "reflection",
        }
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM instance_model_routes WHERE instance_id = ?",
                (instance_id,),
            )
            for item in routes:
                capability = str(item.get("capability") or "")
                if capability not in capabilities:
                    raise ValueError(f"Unsupported capability: {capability}")
                provider_id = int(item["primary_provider_id"])
                provider = connection.execute(
                    """
                    SELECT id FROM provider_profiles
                    WHERE id = ? AND user_id = ?
                    """,
                    (provider_id, user_id),
                ).fetchone()
                if provider is None:
                    raise ValueError("Route provider not found")
                fallbacks = item.get("fallbacks")
                if not isinstance(fallbacks, list):
                    fallbacks = []
                connection.execute(
                    """
                    INSERT INTO instance_model_routes
                        (
                            instance_id, capability, primary_provider_id,
                            primary_model, fallbacks_json, updated_at
                        )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        instance_id,
                        capability,
                        provider_id,
                        str(item.get("primary_model") or "").strip(),
                        json.dumps(fallbacks, ensure_ascii=False),
                        now,
                    ),
                )
        return self.model_routes(instance_id, user_id)


class ProviderPayload(BaseModel):
    id: int | None = None
    name: str = Field(min_length=1, max_length=80)
    provider_type: str = Field(default="custom")
    base_url: str = Field(min_length=1, max_length=500)
    model: str = Field(min_length=1, max_length=200)
    api_key: str = Field(default="", max_length=1000)


class InstancePayload(BaseModel):
    id: int | None = None
    name: str = Field(min_length=1, max_length=80)
    port: int = Field(ge=1024, le=65535)
    onebot_api_base: str = Field(default="http://127.0.0.1:3000")
    onebot_token: str = Field(default="", max_length=2000)
    provider_id: int
    database_path: str = "data/bot.db"


class AuthPayload(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=200)


class BackupPayload(BaseModel):
    passphrase: str = Field(min_length=8, max_length=200)


class RestorePayload(BackupPayload):
    content_base64: str = Field(min_length=16)


store = ControlStore()
secrets_box = SecretBox()
processes: dict[int, subprocess.Popen[bytes]] = {}
login_attempts: dict[str, list[float]] = {}


def _request_user(request: Request) -> sqlite3.Row:
    token = request.cookies.get(SESSION_COOKIE, "")
    user = store.get_session(token) if token else None
    if not user:
        raise HTTPException(status_code=401, detail="login required")
    return user


def _require_csrf(request: Request, user: sqlite3.Row) -> None:
    token = request.headers.get("X-CSRF-Token", "")
    if not token or not secrets.compare_digest(
        token,
        str(user["csrf_token"]),
    ):
        raise HTTPException(status_code=403, detail="invalid csrf token")


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="strict",
        max_age=SESSION_HOURS * 3600,
        path="/",
    )


def _public_provider(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "provider_type": row["provider_type"],
        "base_url": row["base_url"],
        "model": row["model"],
        "api_key_configured": bool(row.get("api_key_encrypted")),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _instance_status(port: int) -> str:
    try:
        response = httpx.get(
            f"http://127.0.0.1:{port}/healthz",
            timeout=1.2,
            trust_env=False,
        )
        return "online" if response.status_code == 200 else "error"
    except httpx.HTTPError:
        return "offline"


def _instance_public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    pid = item.get("pid")
    process_online = False
    if pid:
        try:
            process_online = psutil.Process(int(pid)).is_running()
        except (psutil.Error, ValueError):
            process_online = False
    status = _instance_status(int(item["port"]))
    return {
        "id": item["id"],
        "name": item["name"],
        "port": item["port"],
        "onebot_api_base": item["onebot_api_base"],
        "provider_id": item["provider_id"],
        "provider_name": item.get("provider_name"),
        "database_path": item["database_path"],
        "pid": pid,
        "process_running": process_online,
        "status": status,
        "dashboard_url": f"http://127.0.0.1:{item['port']}/admin",
        "health_url": f"http://127.0.0.1:{item['port']}/healthz",
    }


def _import_current_config(
    user_id: int,
    values: dict[str, Any],
) -> dict[str, Any]:
    base_url = str(values.get("LLM_BASE_URL") or "").strip().rstrip("/")
    model = str(values.get("LLM_MODEL") or "").strip()
    api_key = str(values.get("LLM_API_KEY") or "").strip()
    onebot_base = str(
        values.get("ONEBOT_API_BASE") or "http://127.0.0.1:3000"
    ).strip().rstrip("/")
    onebot_token = str(values.get("ONEBOT_TOKEN") or "").strip()
    if not base_url or not model:
        raise ValueError("Current .env is missing LLM_BASE_URL or LLM_MODEL")
    if not onebot_token:
        raise ValueError("Current .env is missing ONEBOT_TOKEN")

    provider_name = "当前机器人配置"
    existing_provider = store.provider_by_name(user_id, provider_name)
    provider_type = (
        "qwen"
        if "dashscope.aliyuncs.com" in base_url
        else "deepseek"
        if "deepseek.com" in base_url
        else "custom"
    )
    provider_id = store.save_provider(
        user_id=user_id,
        provider_id=int(existing_provider["id"]) if existing_provider else None,
        name=provider_name,
        provider_type=provider_type,
        base_url=base_url,
        model=model,
        api_key_encrypted=secrets_box.encrypt(api_key),
    )

    port = int(str(values.get("APP_PORT") or "8080"))
    existing_instance = store.instance_by_port(port, user_id)
    instance_id = store.save_instance(
        user_id=user_id,
        instance_id=int(existing_instance["id"]) if existing_instance else None,
        name="当前机器人",
        port=port,
        onebot_api_base=onebot_base,
        onebot_token_encrypted=secrets_box.encrypt(onebot_token),
        provider_id=provider_id,
        database_path=str(values.get("DATABASE_PATH") or "data/bot.db"),
    )
    row = store.instance(instance_id, user_id)
    if row is None:
        raise ValueError("Imported instance not found")
    return _instance_public(row)


def _route_target(
    user_id: int,
    provider_id: int,
    model: str,
    *,
    voice: str = "",
) -> dict[str, Any]:
    provider = store.provider(provider_id, user_id)
    if provider is None:
        raise ValueError("路由引用的 API 供应商不存在")
    return {
        "base_url": str(provider["base_url"]),
        "api_key": secrets_box.decrypt(
            str(provider["api_key_encrypted"])
        ),
        "model": model or str(provider["model"]),
        "timeout_seconds": 90,
        "trust_env": False,
        "voice": voice,
    }


def _build_model_routes_env(
    instance_id: int,
    user_id: int,
) -> str:
    routes = store.model_routes(instance_id, user_id)
    if not routes:
        return ""
    payload: dict[str, list[dict[str, Any]]] = {}
    for route in routes:
        capability = str(route["capability"])
        targets = [
            _route_target(
                user_id,
                int(route["primary_provider_id"]),
                str(route["primary_model"]),
            )
        ]
        for fallback in route.get("fallbacks") or []:
            if not isinstance(fallback, dict):
                continue
            try:
                provider_id = int(fallback.get("provider_id"))
            except (TypeError, ValueError):
                continue
            targets.append(
                _route_target(
                    user_id,
                    provider_id,
                    str(fallback.get("model") or ""),
                    voice=str(fallback.get("voice") or ""),
                )
            )
        payload[capability] = targets[:8]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _start_instance(instance_id: int, user_id: int) -> dict[str, Any]:
    row = store.instance(instance_id, user_id)
    if not row:
        raise ValueError("Instance not found")
    port = int(row["port"])
    if _instance_status(port) == "online":
        return _instance_public(row)

    env = os.environ.copy()
    env.update(
        {
            "APP_HOST": "127.0.0.1",
            "APP_PORT": str(port),
            "ONEBOT_API_BASE": str(row["onebot_api_base"]),
            "ONEBOT_TOKEN": secrets_box.decrypt(
                str(row["onebot_token_encrypted"])
            ),
            "DATABASE_PATH": str(row["database_path"]),
            "LLM_BASE_URL": str(row["base_url"]),
            "LLM_MODEL": str(row["model"]),
            "LLM_API_KEY": secrets_box.decrypt(
                str(row["api_key_encrypted"])
            ),
            "SHARED_MEMORY_ENABLED": "true",
            "VECTOR_MEMORY_ENABLED": "true",
        }
    )
    routes_env = _build_model_routes_env(instance_id, user_id)
    if routes_env:
        env["MODEL_ROUTES_JSON"] = routes_env
    log_dir = RUNTIME_ROOT / "instances"
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout = (log_dir / f"{instance_id}.stdout.log").open("ab")
    stderr = (log_dir / f"{instance_id}.stderr.log").open("ab")
    creationflags = 0
    if os.name == "nt":
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )
    process = subprocess.Popen(
        [sys.executable, "-m", "qq_ai_bot"],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=stdout,
        stderr=stderr,
        creationflags=creationflags,
    )
    stdout.close()
    stderr.close()
    processes[instance_id] = process
    store.set_instance_pid(instance_id, process.pid)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        if _instance_status(port) == "online":
            break
        time.sleep(0.5)
    refreshed = store.instance(instance_id, user_id)
    if refreshed is None:
        raise ValueError("Instance disappeared")
    return _instance_public(refreshed)


def _stop_instance(instance_id: int, user_id: int) -> bool:
    row = store.instance(instance_id, user_id)
    if not row:
        raise ValueError("Instance not found")
    process = processes.pop(instance_id, None)
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
    elif row["pid"]:
        try:
            proc = psutil.Process(int(row["pid"]))
            for child in proc.children(recursive=True):
                child.terminate()
            proc.terminate()
        except psutil.Error:
            pass
    store.set_instance_pid(instance_id, None)
    return True


app = FastAPI(title="给她爱控制台", docs_url=None, redoc_url=None)


@app.get("/")
async def root(request: Request) -> Response:
    token = request.cookies.get(SESSION_COOKIE, "")
    if token and store.get_session(token):
        return RedirectResponse("/portal")
    if store.user_count() == 0:
        return RedirectResponse("/setup")
    return RedirectResponse("/login")


@app.get("/login")
async def login_page() -> FileResponse:
    return FileResponse(WEB_ROOT / "control_login.html")


@app.get("/setup")
async def setup_page() -> Response:
    if store.user_count() > 0:
        return RedirectResponse("/login")
    return FileResponse(WEB_ROOT / "control_login.html")


@app.get("/portal")
async def portal_page(request: Request) -> Response:
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token or not store.get_session(token):
        return RedirectResponse("/login")
    return FileResponse(WEB_ROOT / "control_portal.html")


@app.get("/portal/control.css")
async def control_css() -> FileResponse:
    return FileResponse(WEB_ROOT / "control.css")


@app.get("/portal/control.js")
async def control_js() -> FileResponse:
    return FileResponse(WEB_ROOT / "control.js")


@app.get("/api/auth/status")
async def auth_status() -> dict[str, Any]:
    return {"setup_required": store.user_count() == 0}


@app.post("/api/auth/setup")
async def setup(payload: AuthPayload, response: Response) -> dict[str, Any]:
    if store.user_count() > 0:
        raise HTTPException(status_code=409, detail="already initialized")
    user_id = store.create_user(payload.username, payload.password)
    token, csrf = store.create_session(user_id)
    _set_session_cookie(response, token)
    return {"ok": True, "username": payload.username, "csrf_token": csrf}


@app.post("/api/auth/login")
async def login(
    payload: AuthPayload,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    client = request.client.host if request.client else "local"
    now = time.monotonic()
    attempts = [
        value
        for value in login_attempts.get(client, [])
        if now - value < 60
    ]
    if len(attempts) >= 8:
        raise HTTPException(status_code=429, detail="too many attempts")
    user = store.authenticate(payload.username, payload.password)
    if not user:
        attempts.append(now)
        login_attempts[client] = attempts
        raise HTTPException(status_code=401, detail="invalid credentials")
    login_attempts.pop(client, None)
    token, csrf = store.create_session(int(user["id"]))
    _set_session_cookie(response, token)
    return {
        "ok": True,
        "username": user["username"],
        "role": user["role"],
        "csrf_token": csrf,
    }


@app.post("/api/auth/logout")
async def logout(request: Request, response: Response) -> dict[str, bool]:
    token = request.cookies.get(SESSION_COOKIE, "")
    store.delete_session(token)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
async def me(request: Request) -> dict[str, Any]:
    user = _request_user(request)
    return {
        "id": user["id"],
        "username": user["username"],
        "role": user["role"],
        "csrf_token": user["csrf_token"],
    }


@app.get("/api/providers")
async def providers(request: Request) -> dict[str, Any]:
    user = _request_user(request)
    return {
        "providers": [
            _public_provider(item)
            for item in store.providers(int(user["id"]))
        ]
    }


@app.post("/api/providers")
async def save_provider(
    payload: ProviderPayload,
    request: Request,
) -> dict[str, Any]:
    user = _request_user(request)
    _require_csrf(request, user)
    encrypted = secrets_box.encrypt(payload.api_key)
    try:
        provider_id = store.save_provider(
            user_id=int(user["id"]),
            provider_id=payload.id,
            name=payload.name.strip(),
            provider_type=payload.provider_type.strip() or "custom",
            base_url=payload.base_url.rstrip("/"),
            model=payload.model.strip(),
            api_key_encrypted=encrypted,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row = store.provider(provider_id, int(user["id"]))
    return {"ok": True, "provider": _public_provider(dict(row))}


@app.delete("/api/providers/{provider_id}")
async def delete_provider(provider_id: int, request: Request) -> dict[str, bool]:
    user = _request_user(request)
    _require_csrf(request, user)
    try:
        removed = store.delete_provider(provider_id, int(user["id"]))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": removed}


@app.get("/api/instances")
async def instances(request: Request) -> dict[str, Any]:
    user = _request_user(request)
    return {
        "instances": [
            _instance_public(item)
            for item in store.instances(int(user["id"]))
        ]
    }


@app.post("/api/import-current")
async def import_current(request: Request) -> dict[str, Any]:
    user = _request_user(request)
    _require_csrf(request, user)
    values = dotenv_values(PROJECT_ROOT / ".env")
    if not values.get("LLM_API_KEY"):
        values["LLM_API_KEY"] = (
            os.getenv("DASHSCOPE_API_KEY")
            or os.getenv("DEEPSEEK_API_KEY")
            or ""
        )
    try:
        instance = _import_current_config(int(user["id"]), values)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "instance": instance}


@app.get("/api/instances/{instance_id}/model-routes")
async def get_model_routes(
    instance_id: int,
    request: Request,
) -> dict[str, Any]:
    user = _request_user(request)
    try:
        routes = store.model_routes(instance_id, int(user["id"]))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"routes": routes}


@app.post("/api/instances/{instance_id}/model-routes")
async def save_model_routes(
    instance_id: int,
    request: Request,
    payload: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    user = _request_user(request)
    _require_csrf(request, user)
    routes = payload.get("routes")
    if not isinstance(routes, list):
        raise HTTPException(status_code=400, detail="routes must be a list")
    try:
        saved = store.save_model_routes(
            instance_id,
            int(user["id"]),
            routes,
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "routes": saved}


@app.post("/api/backup")
async def create_backup(
    payload: BackupPayload,
    request: Request,
) -> Response:
    user = _request_user(request)
    _require_csrf(request, user)
    try:
        data = build_encrypted_backup(
            instances=[
                dict(item)
                for item in store.instances(int(user["id"]))
            ],
            username=str(user["username"]),
            passphrase=payload.passphrase,
            project_root=PROJECT_ROOT,
            control_path=store.path,
        )
    except (OSError, ValueError, sqlite3.Error) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    file_name = f"qq-bot-backup-{datetime.now():%Y%m%d-%H%M%S}.qqbackup"
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{file_name}"',
            "Cache-Control": "no-store",
        },
    )


@app.post("/api/restore")
async def restore_backup(
    payload: RestorePayload,
    request: Request,
) -> dict[str, Any]:
    user = _request_user(request)
    _require_csrf(request, user)
    try:
        encrypted = base64.b64decode(
            payload.content_base64,
            validate=True,
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=400,
            detail="备份文件编码无效",
        ) from exc
    if len(encrypted) > MAX_BACKUP_BYTES:
        raise HTTPException(status_code=413, detail="备份文件过大")
    for item in store.instances(int(user["id"])):
        try:
            _stop_instance(int(item["id"]), int(user["id"]))
        except (ValueError, OSError):
            pass
    try:
        result = restore_encrypted_backup(
            encrypted=encrypted,
            passphrase=payload.passphrase,
            project_root=PROJECT_ROOT,
            control_path=store.path,
        )
    except (OSError, ValueError, sqlite3.Error) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        **result,
        "relogin_required": True,
        "note": (
            "数据已恢复。若恢复包来自其他电脑，请重启控制台后重新登录，"
            "并确认各实例的 NapCat 地址和端口。"
        ),
    }


async def _instance_admin_json(
    instance_id: int,
    user_id: int,
    path: str,
    *,
    method: str = "GET",
    json_body: Any = None,
) -> Any:
    instance = store.instance(instance_id, user_id)
    if not instance:
        raise HTTPException(status_code=404, detail="instance not found")
    url = f"http://127.0.0.1:{instance['port']}{path}"
    try:
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            response = await client.request(method, url, json=json_body)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=503,
            detail="Instance service is not reachable",
        ) from exc
    try:
        data = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail="Instance service returned invalid JSON",
        ) from exc
    if response.is_error:
        raise HTTPException(
            status_code=response.status_code,
            detail=data.get("detail") if isinstance(data, dict) else data,
        )
    return data


@app.get("/api/instances/{instance_id}/people")
async def instance_people(instance_id: int, request: Request) -> Any:
    user = _request_user(request)
    return await _instance_admin_json(
        instance_id,
        int(user["id"]),
        "/api/admin/people",
    )


@app.get("/api/instances/{instance_id}/people/{user_id}")
async def instance_person(
    instance_id: int,
    user_id: int,
    request: Request,
) -> Any:
    user = _request_user(request)
    return await _instance_admin_json(
        instance_id,
        int(user["id"]),
        f"/api/admin/people/{user_id}",
    )


@app.delete("/api/instances/{instance_id}/people/{user_id}/memories/{memory_id}")
async def instance_delete_memory(
    instance_id: int,
    user_id: int,
    memory_id: int,
    request: Request,
) -> Any:
    user = _request_user(request)
    _require_csrf(request, user)
    return await _instance_admin_json(
        instance_id,
        int(user["id"]),
        f"/api/admin/people/{user_id}/memories/{memory_id}",
        method="DELETE",
    )


@app.delete("/api/instances/{instance_id}/people/{user_id}/all-memory")
async def instance_purge_person_memory(
    instance_id: int,
    user_id: int,
    request: Request,
) -> Any:
    user = _request_user(request)
    _require_csrf(request, user)
    return await _instance_admin_json(
        instance_id,
        int(user["id"]),
        f"/api/admin/people/{user_id}/all-memory",
        method="DELETE",
    )


@app.delete("/api/instances/{instance_id}/memory/all")
async def instance_purge_all_memory(
    instance_id: int,
    request: Request,
) -> Any:
    user = _request_user(request)
    _require_csrf(request, user)
    return await _instance_admin_json(
        instance_id,
        int(user["id"]),
        "/api/admin/memory/all",
        method="DELETE",
    )


@app.get("/api/instances/{instance_id}/group-settings")
async def instance_group_settings(
    instance_id: int,
    request: Request,
) -> Any:
    user = _request_user(request)
    return await _instance_admin_json(
        instance_id,
        int(user["id"]),
        "/api/admin/group-settings",
    )


@app.post("/api/instances/{instance_id}/group-settings")
async def instance_update_group_settings(
    instance_id: int,
    request: Request,
    payload: dict[str, Any] = Body(...),
) -> Any:
    user = _request_user(request)
    _require_csrf(request, user)
    return await _instance_admin_json(
        instance_id,
        int(user["id"]),
        "/api/admin/group-settings",
        method="PATCH",
        json_body=payload,
    )


@app.get("/api/instances/{instance_id}/group-profiles")
async def instance_group_profiles(
    instance_id: int,
    request: Request,
) -> Any:
    user = _request_user(request)
    return await _instance_admin_json(
        instance_id,
        int(user["id"]),
        "/api/admin/group-profiles",
    )


@app.post("/api/instances/{instance_id}/group-profiles")
async def instance_add_group_profile(
    instance_id: int,
    request: Request,
    payload: dict[str, Any] = Body(...),
) -> Any:
    user = _request_user(request)
    _require_csrf(request, user)
    return await _instance_admin_json(
        instance_id,
        int(user["id"]),
        "/api/admin/group-profiles",
        method="POST",
        json_body=payload,
    )


@app.post("/api/instances/{instance_id}/group-profiles/{group_id}")
async def instance_update_group_profile(
    instance_id: int,
    group_id: int,
    request: Request,
    payload: dict[str, Any] = Body(...),
) -> Any:
    user = _request_user(request)
    _require_csrf(request, user)
    return await _instance_admin_json(
        instance_id,
        int(user["id"]),
        f"/api/admin/group-profiles/{group_id}",
        method="PATCH",
        json_body=payload,
    )


@app.get(
    "/api/instances/{instance_id}/group-profiles/{group_id}/details"
)
async def instance_group_profile_details(
    instance_id: int,
    group_id: int,
    request: Request,
) -> Any:
    user = _request_user(request)
    return await _instance_admin_json(
        instance_id,
        int(user["id"]),
        f"/api/admin/group-profiles/{group_id}/details",
    )


@app.post(
    "/api/instances/{instance_id}/group-profiles/{group_id}/knowledge"
)
async def instance_add_group_knowledge(
    instance_id: int,
    group_id: int,
    request: Request,
    payload: dict[str, Any] = Body(...),
) -> Any:
    user = _request_user(request)
    _require_csrf(request, user)
    return await _instance_admin_json(
        instance_id,
        int(user["id"]),
        f"/api/admin/group-profiles/{group_id}/knowledge",
        method="POST",
        json_body=payload,
    )


@app.delete(
    "/api/instances/{instance_id}/group-profiles/{group_id}/knowledge/{knowledge_id}"
)
async def instance_delete_group_knowledge(
    instance_id: int,
    group_id: int,
    knowledge_id: int,
    request: Request,
) -> Any:
    user = _request_user(request)
    _require_csrf(request, user)
    return await _instance_admin_json(
        instance_id,
        int(user["id"]),
        f"/api/admin/group-profiles/{group_id}/knowledge/{knowledge_id}",
        method="DELETE",
    )


@app.post(
    "/api/instances/{instance_id}/group-profiles/{group_id}/members"
)
async def instance_save_group_member(
    instance_id: int,
    group_id: int,
    request: Request,
    payload: dict[str, Any] = Body(...),
) -> Any:
    user = _request_user(request)
    _require_csrf(request, user)
    return await _instance_admin_json(
        instance_id,
        int(user["id"]),
        f"/api/admin/group-profiles/{group_id}/members",
        method="POST",
        json_body=payload,
    )


@app.post("/api/instances/{instance_id}/group-profiles/{group_id}/reset")
async def instance_reset_group_profile(
    instance_id: int,
    group_id: int,
    request: Request,
) -> Any:
    user = _request_user(request)
    _require_csrf(request, user)
    return await _instance_admin_json(
        instance_id,
        int(user["id"]),
        f"/api/admin/group-profiles/{group_id}/reset",
        method="POST",
        json_body={"reset_behavior": True},
    )


@app.delete(
    "/api/instances/{instance_id}/group-behavior/{group_id}"
)
async def instance_reset_group_behavior(
    instance_id: int,
    group_id: int,
    request: Request,
) -> Any:
    user = _request_user(request)
    _require_csrf(request, user)
    return await _instance_admin_json(
        instance_id,
        int(user["id"]),
        f"/api/admin/group-behavior/{group_id}",
        method="DELETE",
    )


@app.post("/api/instances")
async def save_instance(
    payload: InstancePayload,
    request: Request,
) -> dict[str, Any]:
    user = _request_user(request)
    _require_csrf(request, user)
    provider = store.provider(payload.provider_id, int(user["id"]))
    if not provider:
        raise HTTPException(status_code=400, detail="provider not found")
    try:
        instance_id = store.save_instance(
            user_id=int(user["id"]),
            instance_id=payload.id,
            name=payload.name.strip(),
            port=payload.port,
            onebot_api_base=payload.onebot_api_base.rstrip("/"),
            onebot_token_encrypted=secrets_box.encrypt(
                payload.onebot_token
            ),
            provider_id=payload.provider_id,
            database_path=payload.database_path.strip() or "data/bot.db",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row = store.instance(instance_id, int(user["id"]))
    return {"ok": True, "instance": _instance_public(row)}


@app.post("/api/instances/{instance_id}/start")
async def start_instance(instance_id: int, request: Request) -> dict[str, Any]:
    user = _request_user(request)
    _require_csrf(request, user)
    try:
        instance = _start_instance(instance_id, int(user["id"]))
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "instance": instance}


@app.post("/api/instances/{instance_id}/stop")
async def stop_instance(instance_id: int, request: Request) -> dict[str, Any]:
    user = _request_user(request)
    _require_csrf(request, user)
    try:
        _stop_instance(instance_id, int(user["id"]))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True}


@app.post("/api/instances/{instance_id}/restart")
async def restart_instance(instance_id: int, request: Request) -> dict[str, Any]:
    user = _request_user(request)
    _require_csrf(request, user)
    _stop_instance(instance_id, int(user["id"]))
    time.sleep(1)
    instance = _start_instance(instance_id, int(user["id"]))
    return {"ok": True, "instance": instance}


@app.delete("/api/instances/{instance_id}")
async def delete_instance(instance_id: int, request: Request) -> dict[str, bool]:
    user = _request_user(request)
    _require_csrf(request, user)
    _stop_instance(instance_id, int(user["id"]))
    return {"ok": store.delete_instance(instance_id, int(user["id"]))}


def main() -> None:
    file_values = dotenv_values(PROJECT_ROOT / ".env")
    host = os.getenv("CONTROL_HOST") or str(
        file_values.get("CONTROL_HOST", "127.0.0.1")
    )
    port = int(
        os.getenv("CONTROL_PORT")
        or file_values.get("CONTROL_PORT", "8090")
    )
    uvicorn.run(app, host=host, port=port, log_level="info")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    main()
