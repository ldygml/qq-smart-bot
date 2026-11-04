from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from qq_ai_bot.content_tools import (
    generate_chart,
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
        if "format" in prompt:
            return '{"title":"打卡记录","format":"csv","content":"日期,时长"}'
        return "完成"

    async def close(self) -> None:
        return None


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
    assert chart.path.read_bytes().startswith(b"\x89PNG")
    assert document.path.suffix == ".docx"
    assert document.path.stat().st_size > 1000
    assert data_file.path.read_text(encoding="utf-8") == "日期,时长"


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
    assert store.reset(100) == 1
    assert store.policy(100)["silence_bias"] == 0


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
