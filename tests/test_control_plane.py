from __future__ import annotations

import sqlite3

import httpx
import pytest

from qq_ai_bot import control_plane
from qq_ai_bot.control_plane import ControlStore, SecretBox


@pytest.mark.asyncio
async def test_control_plane_auth_provider_and_instance(tmp_path, monkeypatch) -> None:
    store = ControlStore(tmp_path / "control.db")
    box = SecretBox()
    monkeypatch.setattr(control_plane, "store", store)
    monkeypatch.setattr(control_plane, "secrets_box", box)
    monkeypatch.setattr(control_plane, "processes", {})
    transport = httpx.ASGITransport(app=control_plane.app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:8090",
    ) as client:
        status = await client.get("/api/auth/status")
        assert status.json()["setup_required"] is True

        setup = await client.post(
            "/api/auth/setup",
            json={"username": "owner", "password": "secure-password"},
        )
        assert setup.status_code == 200
        csrf = setup.json()["csrf_token"]

        provider = await client.post(
            "/api/providers",
            headers={"X-CSRF-Token": csrf},
            json={
                "name": "测试供应商",
                "provider_type": "custom",
                "base_url": "https://api.example.com/v1",
                "model": "example-chat",
                "api_key": "super-secret-key",
            },
        )
        assert provider.status_code == 200
        provider_id = provider.json()["provider"]["id"]
        assert provider.json()["provider"]["api_key_configured"] is True
        assert "super-secret-key" not in provider.text

        providers = await client.get("/api/providers")
        assert "super-secret-key" not in providers.text

        instance = await client.post(
            "/api/instances",
            headers={"X-CSRF-Token": csrf},
            json={
                "name": "测试实例",
                "port": 18081,
                "onebot_api_base": "http://127.0.0.1:13000",
                "onebot_token": "secret-onebot-token",
                "provider_id": provider_id,
                "database_path": "data/instances/test.db",
            },
        )
        assert instance.status_code == 200
        assert instance.json()["instance"]["name"] == "测试实例"

        portal = await client.get("/portal")
        assert portal.status_code == 200
        assert "机器人" in portal.text

    with sqlite3.connect(tmp_path / "control.db") as connection:
        password_hash = connection.execute(
            "SELECT password_hash FROM control_users"
        ).fetchone()[0]
        instance_token = connection.execute(
            "SELECT onebot_token_encrypted FROM bot_instances"
        ).fetchone()[0]
    assert password_hash != "secure-password"
    assert "secure-password" not in password_hash
    assert instance_token != "secret-onebot-token"


@pytest.mark.asyncio
async def test_control_plane_rejects_missing_csrf(tmp_path, monkeypatch) -> None:
    store = ControlStore(tmp_path / "control.db")
    monkeypatch.setattr(control_plane, "store", store)
    monkeypatch.setattr(control_plane, "secrets_box", SecretBox())
    transport = httpx.ASGITransport(app=control_plane.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:8090",
    ) as client:
        await client.post(
            "/api/auth/setup",
            json={"username": "owner", "password": "secure-password"},
        )
        response = await client.post(
            "/api/providers",
            json={
                "name": "untrusted",
                "base_url": "https://api.example.com/v1",
                "model": "model",
                "api_key": "key",
            },
        )
    assert response.status_code == 403
