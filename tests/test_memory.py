from __future__ import annotations

from qq_ai_bot.memory import ChatMemory


def test_memory_persists_and_resets(tmp_path) -> None:
    path = tmp_path / "chat.db"
    memory = ChatMemory(path)
    memory.append_exchange("private:1", "你好", "你好呀", max_rows=10)

    reopened = ChatMemory(path)
    assert reopened.history("private:1", 10) == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好呀"},
    ]
    assert reopened.reset("private:1") == 2
    assert reopened.history("private:1", 10) == []


def test_memory_prunes_old_rows(tmp_path) -> None:
    memory = ChatMemory(tmp_path / "chat.db")
    for index in range(6):
        memory.append_exchange(
            "private:1",
            f"user-{index}",
            f"assistant-{index}",
            max_rows=4,
        )

    history = memory.history("private:1", 10)
    assert len(history) == 4
    assert history[0]["content"] == "user-4"
    assert history[-1]["content"] == "assistant-5"

