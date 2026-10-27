from __future__ import annotations

from datetime import datetime, timedelta, timezone

from qq_ai_bot.productivity import (
    ProductivityStore,
    parse_reminder_time,
    split_reminder_argument,
)


def test_note_and_todo_lifecycle(tmp_path) -> None:
    store = ProductivityStore(tmp_path / "tools.db")
    note_id = store.add_note(1, "线性代数", "行列式交换两行会变号")
    assert store.search_notes(1, "行列式")[0].id == note_id
    assert store.list_notes(1)[0].title == "线性代数"

    todo_id = store.add_todo(1, "完成第三章习题")
    assert store.list_todos(1)[0].done is False
    assert store.complete_todo(1, todo_id) is True
    assert store.list_todos(1)[0].done is True

    assert store.delete_note(1, note_id) is True
    assert store.search_notes(1, "行列式") == []


def test_reminder_time_parsing_and_delivery_query(tmp_path) -> None:
    due_at, display = parse_reminder_time(
        "2099-01-02 09:30",
        "Asia/Shanghai",
    )
    assert display == "2099-01-02 09:30"

    store = ProductivityStore(tmp_path / "tools.db")
    reminder_id = store.add_reminder(
        user_id=1,
        message_type="private",
        group_id=None,
        content="复习",
        due_at=due_at,
    )
    assert store.due_reminders(datetime.now(timezone.utc)) == []
    due = store.due_reminders(
        datetime.now(timezone.utc) + timedelta(days=40000)
    )
    assert [reminder.id for reminder in due] == [reminder_id]
    store.mark_reminder_delivered(reminder_id)
    assert store.list_reminders(1) == []


def test_split_reminder_argument() -> None:
    raw_time, content = split_reminder_argument("2026-11-01 08:00 完成报告")
    assert raw_time == "2026-11-01 08:00"
    assert content == "完成报告"
