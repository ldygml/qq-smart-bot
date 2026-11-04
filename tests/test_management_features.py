from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

import httpx
import pytest
from docx import Document as WordDocument
from pydantic import SecretStr

from qq_ai_bot.app import create_app, _detect_natural_generation
from qq_ai_bot.config import Settings
from qq_ai_bot.content_tools import (
    generate_chart,
    generate_code,
    generate_document,
    generate_file,
)
from qq_ai_bot.control_plane import (
    build_encrypted_backup,
    decrypt_backup,
    encrypt_backup,
    restore_encrypted_backup,
)
from qq_ai_bot.group_behavior import GroupBehaviorStore
from qq_ai_bot.group_profiles import GroupProfileStore
from qq_ai_bot.onebot import StickerSource
from qq_ai_bot.stickers import StickerStore
from qq_ai_bot.usage import MeteredChatClient, UsageStore


class FeatureChat:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, *, model=None, extra_payload=None) -> str:
        self.calls += 1
        prompt = str(messages[-1].get("content", ""))
        if "series" in prompt:
            return (
                '{"title":"每周学习时长","kind":"bar","unit":"小时",'
                '"series":[{"label":"周一","value":2},'
                '{"label":"周二","value":1.5}]}'
            )
        if "sections" in prompt:
            return (
                '{"title":"学习计划","subtitle":"本周",'
                '"sections":[{"heading":"目标","paragraphs":["完成复盘。"]}]}'
            )
        if '"files"' in prompt:
            return (
                '{"project_name":"grade_tool","summary":"成绩统计脚本",'
                '"files":[{"path":"main.py","content":"print(42)\\n"}]}'
            )
        if "format" in prompt:
            return '{"title":"打卡记录","format":"csv","content":"日期,时长"}'
        return "完成"

    async def close(self) -> None:
        return None


class NoopSender:
    async def send_text(self, target, text) -> None:
        return None

    async def close(self) -> None:
        return None


class MultiCodeChat(FeatureChat):
    async def chat(self, messages, *, model=None, extra_payload=None) -> str:
        return (
            '{"project_name":"todo_web","summary":"待办网页",'
            '"files":['
            '{"path":"index.html","content":"<h1>Todo</h1>"},'
            '{"path":"src/app.js","content":"console.log(1);\\n"}'
            "]}"
        )


class QualityDocumentChat:
    async def chat(self, messages, *, model=None, extra_payload=None) -> str:
        prompt = str(messages[-1].get("content", ""))
        if extra_payload and extra_payload.get("enable_search"):
            return (
                '{"facts":[{"claim":"歌曲发行信息已核对",'
                '"source_url":"https://example.com/song",'
                '"quote":"发行信息"}],"uncertain":[]}'
            )
        assert "检索核对材料" in prompt or "待审校 JSON" in prompt
        return (
            '{"title":"晴天.docx","subtitle":"歌曲资料",'
            '"sections":['
            '{"heading":"歌词正文（完整版）","paragraphs":['
            '"故事的小黄花","从出生那年就飘着","童年的荡秋千",'
            '"随记忆一直晃到现在","吹着前奏望着天空","想起花瓣试着掉落",'
            '"为你翘课的那一天","花落的那一天"]},'
            '{"heading":"主题分析","paragraphs":['
            '"歌曲围绕青春记忆与遗憾展开，这是分析文字。",'
            '"资料中的发行信息需要以可核验来源为准。"]}'
            '],"sources":["https://example.com/song",'
            '"https://invented.example/fake"]}'
        )


class NamedFileChat(FeatureChat):
    async def chat(self, messages, *, model=None, extra_payload=None) -> str:
        return (
            '{"title":"打卡记录.csv","format":"csv",'
            '"content":"日期,时长"}'
        )


class RepairCodeChat(FeatureChat):
    async def chat(self, messages, *, model=None, extra_payload=None) -> str:
        prompt = str(messages[-1].get("content", ""))
        content = "def broken(:\n    pass\n" if "检测到的错误" not in prompt else "def ok():\n    return 1\n"
        return json.dumps(
            {
                "project_name": "repair_demo",
                "summary": "修复后的脚本",
                "files": [{"path": "main.py", "content": content}],
            },
            ensure_ascii=False,
        )


@pytest.mark.parametrize(
    ("text", "command"),
    [
        ("收集晴天歌词，然后发doxc给我", "/生成文档"),
        ("整理本周学习资料，做成一份Word发我", "/生成文档"),
        ("把这些数据做成柱状图发给我", "/生成图表"),
        ("把打卡记录导出为CSV给我", "/生成文件"),
        ("写个 Python 脚本发给我", "/生成代码"),
        ("实现一个待办网页并生成代码文件", "/生成代码"),
        ("写个 C++文件，输出helloworld", "/生成代码"),
        ("给我一份可以输出helloworld的cpp文件", "/生成代码"),
        ("生成一个包含多个文件的代码项目并发给我", "/生成代码"),
    ],
)
def test_natural_language_generation_intents(
    text: str,
    command: str,
) -> None:
    detected = _detect_natural_generation(text)
    assert detected is not None
    assert detected[0] == command


def test_question_about_charts_does_not_trigger_generation() -> None:
    assert _detect_natural_generation("这个图表怎么做才好看") is None
    assert _detect_natural_generation("这段 Python 代码怎么写") is None


@pytest.mark.asyncio
async def test_group_settings_api_is_persistent_and_reloadable(tmp_path) -> None:
    database = tmp_path / "bot.db"

    def settings() -> Settings:
        return Settings(
            _env_file=None,
            onebot_token=SecretStr("test"),
            database_path=database,
            enable_group=True,
            group_require_mention=True,
            vector_memory_enabled=False,
        )

    app = create_app(
        settings=settings(),
        chat_client=FeatureChat(),
        onebot_client=NoopSender(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        current = await client.get("/api/admin/group-settings")
        assert current.status_code == 200
        assert current.json()["settings"]["group_require_mention"] is True

        updated = await client.patch(
            "/api/admin/group-settings",
            json={
                "group_require_mention": False,
                "group_smart_participation": True,
                "group_behavior_learning": True,
                "group_reply_cooldown_seconds": 45,
                "group_min_reply_confidence": 0.72,
                "group_allowlist": "100,200",
                "group_trigger_prefixes": "",
                "quote_replies": False,
            },
        )
        assert updated.status_code == 200
        assert updated.json()["settings"]["group_allowlist"] == "100,200"

        invalid = await client.patch(
            "/api/admin/group-settings",
            json={"group_allowlist": "not-a-group"},
        )
        assert invalid.status_code == 400

        created_group = await client.post(
            "/api/admin/group-profiles",
            json={"group_id": 90001, "name": "测试群"},
        )
        assert created_group.status_code == 200
        changed_group = await client.patch(
            "/api/admin/group-profiles/90001",
            json={
                "enabled": False,
                "require_mention": True,
                "hourly_reply_limit": 3,
                "avoid_last_word": True,
                "custom_prompt": "这个群只回答明确问题。",
            },
        )
        assert changed_group.status_code == 200
        profiles = await client.get("/api/admin/group-profiles")
        profile = next(
            item
            for item in profiles.json()["groups"]
            if item["group_id"] == 90001
        )
        assert profile["enabled"] is False
        assert profile["hourly_reply_limit"] == 3

    reloaded = create_app(
        settings=settings(),
        chat_client=FeatureChat(),
        onebot_client=NoopSender(),
    )
    loaded = reloaded.state.settings
    assert loaded.group_require_mention is False
    assert loaded.group_allowlist == "100,200"
    assert loaded.group_ids == {100, 200}
    assert loaded.group_reply_cooldown_seconds == 45
    assert loaded.group_min_reply_confidence == 0.72
    assert loaded.quote_replies is False
    assert loaded.trigger_prefixes == ()
    transport = httpx.ASGITransport(app=reloaded)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        persisted_profiles = await client.get(
            "/api/admin/group-profiles"
        )
    persisted = next(
        item
        for item in persisted_profiles.json()["groups"]
        if item["group_id"] == 90001
    )
    assert persisted["require_mention"] is True
    assert persisted["custom_prompt"] == "这个群只回答明确问题。"


@pytest.mark.asyncio
async def test_content_generation_creates_real_artifacts(tmp_path) -> None:
    client = FeatureChat()
    chart = await generate_chart(
        client,
        model="test",
        prompt="周一 2 小时，周二 1.5 小时",
        output_dir=tmp_path,
    )
    document = await generate_document(
        client,
        model="test",
        prompt="写一个学习计划",
        output_dir=tmp_path,
    )
    data_file = await generate_file(
        client,
        model="test",
        prompt="生成打卡 CSV",
        output_dir=tmp_path,
    )
    code_file = await generate_code(
        client,
        model="test",
        prompt="写一个 Python 成绩统计脚本",
        output_dir=tmp_path,
    )
    assert chart.path.read_bytes().startswith(b"\x89PNG")
    assert document.path.suffix == ".docx"
    assert document.path.stat().st_size > 1000
    assert data_file.path.read_text(encoding="utf-8") == "日期,时长"
    assert code_file.name == "main.py"
    assert code_file.path.read_text(encoding="utf-8") == "print(42)\n"


@pytest.mark.asyncio
async def test_multi_file_code_generation_is_packaged_as_zip(tmp_path) -> None:
    artifact = await generate_code(
        MultiCodeChat(),
        model="test",
        prompt="实现一个待办网页",
        output_dir=tmp_path,
    )
    assert artifact.kind == "archive"
    assert artifact.path.suffix == ".zip"
    with zipfile.ZipFile(artifact.path) as archive:
        assert sorted(archive.namelist()) == [
            "todo_web/index.html",
            "todo_web/src/app.js",
        ]


@pytest.mark.asyncio
async def test_file_generation_does_not_duplicate_suffix(tmp_path) -> None:
    artifact = await generate_file(
        NamedFileChat(),
        model="test",
        prompt="生成打卡记录",
        output_dir=tmp_path,
    )
    assert artifact.name == "打卡记录.csv"
    assert artifact.path.suffix == ".csv"


@pytest.mark.asyncio
async def test_document_quality_filters_lyrics_and_unverified_sources(tmp_path) -> None:
    artifact = await generate_document(
        QualityDocumentChat(),
        model="test",
        prompt="整理歌曲资料和歌词",
        output_dir=tmp_path,
    )
    assert artifact.name == "晴天.docx"
    document = WordDocument(artifact.path)
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert "故事的小黄花" not in text
    assert "主题分析" in text
    assert "https://example.com/song" in text
    assert "invented.example" not in text


@pytest.mark.asyncio
async def test_code_generation_repairs_python_syntax_error(tmp_path) -> None:
    artifact = await generate_code(
        RepairCodeChat(),
        model="test",
        prompt="写一个 Python 函数",
        output_dir=tmp_path,
    )
    assert "def ok()" in artifact.path.read_text(encoding="utf-8")


def test_sticker_management_and_disabled_filter(tmp_path) -> None:
    store = StickerStore(tmp_path / "bot.db")
    store.record(
        StickerSource(
            summary="无语",
            emoji_id="1",
            image_source="https://example.com/sticker.png",
        )
    )
    item = store.list_recent()[0]
    sticker_id = int(item["id"])
    assert item["image_source"].endswith("sticker.png")
    assert store.update(
        sticker_id,
        category="反应",
        usage_hint="对方说了离谱的事情",
        enabled=False,
    )
    assert store.count() == 0
    assert store.list_recent() == []
    assert store.get_by_id(sticker_id) is None
    assert store.delete(sticker_id)


@pytest.mark.asyncio
async def test_usage_metrics_capture_latency_and_errors(tmp_path) -> None:
    class FailingChat(FeatureChat):
        async def chat(self, messages, *, model=None, extra_payload=None) -> str:
            raise RuntimeError("upstream failed")

    usage = UsageStore(tmp_path / "bot.db")
    successful = MeteredChatClient(FeatureChat(), usage)
    await successful.chat([{"role": "user", "content": "你好"}], model="chat-a")
    failing = MeteredChatClient(FailingChat(), usage)
    with pytest.raises(RuntimeError):
        await failing.chat([{"role": "user", "content": "报错"}], model="chat-b")
    summary = usage.summary(
        input_price_per_million=1,
        output_price_per_million=2,
    )
    assert summary["totals"]["calls"] == 2
    assert summary["totals"]["errors"] == 1
    assert summary["totals"]["input_tokens"] > 0


def test_group_behavior_learns_silence_and_resets(tmp_path) -> None:
    store = GroupBehaviorStore(tmp_path / "bot.db")
    for _ in range(12):
        store.observe_message(100)
        store.record_ordinary_ignored(100)
    policy = store.policy(100)
    assert policy["silence_bias"] > 0.9
    assert not store.should_attempt_ordinary(100, cooldown_seconds=0)
    store.record_reply(100)
    store.record_reply(100)
    assert store.policy(100)["replies_last_hour"] == 2
    assert not store.can_reply_now(100, 2)
    assert store.reset(100) == 1
    assert store.policy(100)["silence_bias"] == 0
    assert store.policy(100)["replies_last_hour"] == 0


def test_group_profiles_are_independent(tmp_path) -> None:
    store = GroupProfileStore(tmp_path / "bot.db")
    defaults = {
        "enabled": True,
        "require_mention": False,
        "smart_participation": True,
        "behavior_learning": True,
        "min_reply_confidence": 0.65,
        "reply_cooldown_seconds": 90,
        "hourly_reply_limit": 20,
        "participation_model": "qwen3.8-flash",
        "trigger_prefixes": "/ai",
        "quote_replies": True,
        "quote_counts_as_mention": True,
        "auto_sticker_enabled": True,
        "avoid_last_word": True,
        "custom_prompt": "",
    }
    first = store.ensure(100, defaults=defaults, name="技术群")
    second = store.ensure(200, defaults=defaults, name="闲聊群")
    assert first.require_mention is False
    assert second.require_mention is False
    updated = store.update(
        100,
        values={
            "require_mention": True,
            "reply_cooldown_seconds": 300,
            "custom_prompt": "只回答技术问题，不讲笑话。",
        },
    )
    assert updated is not None
    assert updated.require_mention is True
    assert updated.reply_cooldown_seconds == 300
    assert store.get(200).require_mention is False
    assert len(store.list_all()) == 2


def test_encrypted_backup_round_trip_and_wrong_passphrase(tmp_path) -> None:
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    (source_root / "data").mkdir(parents=True)
    (source_root / ".runtime").mkdir()
    (target_root / "data").mkdir(parents=True)
    control_path = source_root / "data" / "control.db"
    instance_path = source_root / "data" / "instance.db"
    with sqlite3.connect(control_path) as connection:
        connection.execute("CREATE TABLE demo (value TEXT)")
        connection.execute("INSERT INTO demo VALUES ('control-secret')")
    with sqlite3.connect(instance_path) as connection:
        connection.execute("CREATE TABLE demo (value TEXT)")
        connection.execute("INSERT INTO demo VALUES ('instance-memory')")
    (source_root / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (source_root / ".runtime" / "control-master.key").write_text(
        "test-key",
        encoding="utf-8",
    )
    encrypted = build_encrypted_backup(
        instances=[
            {
                "database_path": "data/instance.db",
            }
        ],
        username="owner",
        passphrase="correct-password",
        project_root=source_root,
        control_path=control_path,
    )
    assert b"instance-memory" not in encrypted
    with pytest.raises(ValueError):
        decrypt_backup(encrypted, "wrong-password")
    result = restore_encrypted_backup(
        encrypted=encrypted,
        passphrase="correct-password",
        project_root=target_root,
        control_path=target_root / "data" / "control.db",
    )
    assert result["restored"]
    with sqlite3.connect(target_root / "data" / "instance.db") as connection:
        value = connection.execute("SELECT value FROM demo").fetchone()[0]
    assert value == "instance-memory"
    assert (target_root / ".env").read_text(encoding="utf-8") == "TOKEN=secret\n"
