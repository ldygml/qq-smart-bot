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
        audit = await client.get("/api/audit")
        assert audit.status_code == 200
        assert any(
            event["path"] == "/api/providers"
            for event in audit.json()["events"]
        )
        assert "super-secret-key" not in audit.text

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
        instance_id = instance.json()["instance"]["id"]

        routes = await client.post(
            f"/api/instances/{instance_id}/model-routes",
            headers={"X-CSRF-Token": csrf},
            json={
                "routes": [
                    {
                        "capability": "chat",
                        "primary_provider_id": provider_id,
                        "primary_model": "example-chat",
                        "fallbacks": [
                            {
                                "provider_id": provider_id,
                                "model": "backup-chat",
                            }
                        ],
                    }
                ]
            },
        )
        assert routes.status_code == 200
        assert routes.json()["routes"][0]["capability"] == "chat"
        route_env = control_plane._build_model_routes_env(
            instance_id,
            store.authenticate("owner", "secure-password")["id"],
        )
        assert "backup-chat" in route_env
        assert "super-secret-key" in route_env

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


def test_import_current_config_encrypts_local_secrets(tmp_path, monkeypatch) -> None:
    store = ControlStore(tmp_path / "control.db")
    box = SecretBox()
    monkeypatch.setattr(control_plane, "store", store)
    monkeypatch.setattr(control_plane, "secrets_box", box)
    user_id = store.create_user("owner", "secure-password")
    imported = control_plane._import_current_config(
        user_id,
        {
            "LLM_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "LLM_MODEL": "qwen-plus",
            "LLM_API_KEY": "local-secret-api-key",
            "ONEBOT_API_BASE": "http://127.0.0.1:3000",
            "ONEBOT_TOKEN": "local-onebot-token",
            "APP_PORT": "8080",
            "DATABASE_PATH": "data/bot.db",
        },
    )
    assert imported["name"] == "当前机器人"
    assert imported["port"] == 8080
    assert imported["provider_name"] == "当前机器人配置"
    with sqlite3.connect(tmp_path / "control.db") as connection:
        provider_key = connection.execute(
            "SELECT api_key_encrypted FROM provider_profiles"
        ).fetchone()[0]
        onebot_token = connection.execute(
            "SELECT onebot_token_encrypted FROM bot_instances"
        ).fetchone()[0]
    assert "local-secret-api-key" not in provider_key
    assert "local-onebot-token" not in onebot_token


@pytest.mark.asyncio
async def test_control_plane_proxies_group_settings(tmp_path, monkeypatch) -> None:
    store = ControlStore(tmp_path / "control.db")
    monkeypatch.setattr(control_plane, "store", store)
    monkeypatch.setattr(control_plane, "secrets_box", SecretBox())
    calls = []

    async def fake_instance_admin_json(
        instance_id,
        user_id,
        path,
        *,
        method="GET",
        json_body=None,
    ):
        calls.append((instance_id, user_id, path, method, json_body))
        return {"ok": True, "settings": {"group_require_mention": False}}

    monkeypatch.setattr(
        control_plane,
        "_instance_admin_json",
        fake_instance_admin_json,
    )
    transport = httpx.ASGITransport(app=control_plane.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:8090",
    ) as client:
        setup = await client.post(
            "/api/auth/setup",
            json={"username": "owner", "password": "secure-password"},
        )
        csrf = setup.json()["csrf_token"]
        response = await client.post(
            "/api/instances/9/group-settings",
            headers={"X-CSRF-Token": csrf},
            json={"group_require_mention": False},
        )
        profile_response = await client.post(
            "/api/instances/9/group-profiles/100",
            headers={"X-CSRF-Token": csrf},
            json={"require_mention": True},
        )
        files_response = await client.get(
            "/api/instances/9/group-profiles/100/files",
            params={"folder_id": "folder-1"},
        )
        reset_response = await client.post(
            "/api/instances/9/group-profiles/100/reset",
            headers={"X-CSRF-Token": csrf},
        )
        purge_person = await client.delete(
            "/api/instances/9/people/123/all-memory",
            headers={"X-CSRF-Token": csrf},
        )
        purge_all = await client.delete(
            "/api/instances/9/memory/all",
            headers={"X-CSRF-Token": csrf},
        )
    assert response.status_code == 200
    assert calls[0][0] == 9
    assert calls[0][2] == "/api/admin/group-settings"
    assert calls[0][3] == "PATCH"
    assert calls[0][4] == {"group_require_mention": False}
    assert profile_response.status_code == 200
    assert calls[1][2] == "/api/admin/group-profiles/100"
    assert calls[1][3] == "PATCH"
    assert calls[1][4] == {"require_mention": True}
    assert files_response.status_code == 200
    assert calls[2][2] == (
        "/api/admin/group-profiles/100/files?folder_id=folder-1"
    )
    assert reset_response.status_code == 200
    assert calls[3][2] == "/api/admin/group-profiles/100/reset"
    assert purge_person.status_code == 200
    assert calls[4][2] == "/api/admin/people/123/all-memory"
    assert calls[4][3] == "DELETE"
    assert purge_all.status_code == 200
    assert calls[5][2] == "/api/admin/memory/all"


@pytest.mark.asyncio
async def test_control_plane_proxies_evolution_endpoints(
    tmp_path,
    monkeypatch,
) -> None:
    store = ControlStore(tmp_path / "control.db")
    monkeypatch.setattr(control_plane, "store", store)
    monkeypatch.setattr(control_plane, "secrets_box", SecretBox())
    calls = []

    async def fake_instance_admin_json(
        instance_id,
        user_id,
        path,
        *,
        method="GET",
        json_body=None,
    ):
        calls.append((instance_id, user_id, path, method, json_body))
        return {"ok": True}

    monkeypatch.setattr(
        control_plane,
        "_instance_admin_json",
        fake_instance_admin_json,
    )
    transport = httpx.ASGITransport(app=control_plane.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:8090",
    ) as client:
        setup = await client.post(
            "/api/auth/setup",
            json={"username": "owner", "password": "secure-password"},
        )
        csrf = setup.json()["csrf_token"]
        adaptive = await client.get("/api/instances/9/adaptive")
        presence = await client.get("/api/instances/9/presence")
        updated = await client.post(
            "/api/instances/9/presence",
            headers={"X-CSRF-Token": csrf},
            json={"scope_key": "global", "busy": True},
        )
        topics = await client.get(
            "/api/instances/9/topics",
            params={"scope_key": "user:123"},
        )
    assert adaptive.status_code == 200
    assert presence.status_code == 200
    assert updated.status_code == 200
    assert topics.status_code == 200
    assert calls[0][2] == "/api/admin/adaptive"
    assert calls[1][2] == "/api/admin/presence"
    assert calls[2][2] == "/api/admin/presence"
    assert calls[2][3] == "PATCH"
    assert calls[3][2] == "/api/admin/topics?scope_key=user%3A123"


def test_stop_instance_handles_stored_pid_and_listening_port(
    tmp_path,
    monkeypatch,
) -> None:
    store = ControlStore(tmp_path / "control.db")
    user_id = store.create_user("owner", "secure-password")
    provider_id = store.save_provider(
        user_id=user_id,
        provider_id=None,
        name="provider",
        provider_type="custom",
        base_url="https://api.example.com/v1",
        model="chat",
        api_key_encrypted="encrypted",
    )
    instance_id = store.save_instance(
        user_id=user_id,
        instance_id=None,
        name="instance",
        port=18081,
        onebot_api_base="http://127.0.0.1:13000",
        onebot_token_encrypted="encrypted",
        provider_id=provider_id,
        database_path="data/test.db",
    )
    store.set_instance_pid(instance_id, 12345)
    terminated: list[int] = []

    class FakeProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def children(self, recursive: bool) -> list[FakeProcess]:
            assert recursive is True
            return []

        def terminate(self) -> None:
            terminated.append(self.pid)

    monkeypatch.setattr(control_plane, "store", store)
    monkeypatch.setattr(control_plane, "processes", {})
    monkeypatch.setattr(control_plane.psutil, "Process", FakeProcess)

    assert control_plane._stop_instance(instance_id, user_id) is True
    assert terminated == [12345]
    assert store.instance(instance_id, user_id)["pid"] is None

    second_id = store.save_instance(
        user_id=user_id,
        instance_id=None,
        name="manual-instance",
        port=18082,
        onebot_api_base="http://127.0.0.1:13000",
        onebot_token_encrypted="encrypted",
        provider_id=provider_id,
        database_path="data/test-2.db",
    )
    monkeypatch.setattr(control_plane, "_listening_pid", lambda port: 67890)

    assert control_plane._stop_instance(second_id, user_id) is True
    assert terminated == [12345, 67890]


@pytest.mark.asyncio
async def test_instance_admin_proxy_sends_owner_token(
    tmp_path,
    monkeypatch,
) -> None:
    store = ControlStore(tmp_path / "control.db")
    box = SecretBox()
    user_id = store.create_user("owner", "secure-password")
    provider_id = store.save_provider(
        user_id=user_id,
        provider_id=None,
        name="provider",
        provider_type="custom",
        base_url="https://api.example.com/v1",
        model="chat",
        api_key_encrypted=box.encrypt("key"),
    )
    instance_id = store.save_instance(
        user_id=user_id,
        instance_id=None,
        name="instance",
        port=18081,
        onebot_api_base="http://127.0.0.1:13000",
        onebot_token_encrypted=box.encrypt("owner-token"),
        provider_id=provider_id,
        database_path="data/test.db",
    )
    captured: dict[str, object] = {}

    class FakeResponse:
        is_error = False
        status_code = 200

        def json(self):
            return {"ok": True}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def request(self, method, url, *, json, headers):
            captured.update(
                method=method,
                url=url,
                json=json,
                headers=headers,
            )
            return FakeResponse()

    monkeypatch.setattr(control_plane, "store", store)
    monkeypatch.setattr(control_plane, "secrets_box", box)
    monkeypatch.setattr(control_plane.httpx, "AsyncClient", FakeClient)
    result = await control_plane._instance_admin_json(
        instance_id,
        user_id,
        "/api/admin/overview",
    )
    assert result == {"ok": True}
    assert captured["headers"] == {
        "X-Bot-Admin-Token": "owner-token"
    }
