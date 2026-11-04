from __future__ import annotations

import pytest

from qq_ai_bot.vector_memory import VectorChunk, VectorMemoryStore


def test_vector_search_returns_best_match(tmp_path) -> None:
    store = VectorMemoryStore(tmp_path / "vectors.db")
    store.upsert_chunks(
        [
            VectorChunk("memory", "1", "小明", "长期记忆", "喜欢足球"),
            VectorChunk("memory", "2", "小明", "长期记忆", "喜欢咖啡"),
        ],
        [[1.0, 0.0], [0.0, 1.0]],
    )
    matches = store.search([0.9, 0.1], namespaces=("memory",), limit=1)
    assert matches[0].content == "喜欢足球"
    assert matches[0].score > 0.9


def test_vector_source_count_and_deduplication(tmp_path) -> None:
    store = VectorMemoryStore(tmp_path / "vectors.db")
    chunk = VectorChunk("document", "doc-1", "报告", "第 1 页", "内容")
    assert store.upsert_chunks([chunk], [[1.0, 0.0]]) == 1
    assert store.upsert_chunks([chunk], [[1.0, 0.0]]) == 0
    assert store.count("document") == 1


def test_vector_memory_can_delete_user_chunks_or_namespace(tmp_path) -> None:
    store = VectorMemoryStore(tmp_path / "vectors.db")
    store.upsert_chunks(
        [
            VectorChunk("memory", "memory:1", "甲", "长期记忆", "甲的偏好"),
            VectorChunk("episode", "episode:1", "甲", "私聊", "甲的对话"),
        ],
        [[1.0, 0.0], [1.0, 0.0]],
        metadata={"user_id": 1},
    )
    store.upsert_chunks(
        [VectorChunk("memory", "memory:2", "乙", "长期记忆", "乙的偏好")],
        [[0.0, 1.0]],
        metadata={"user_id": 2},
    )
    assert store.delete_by_metadata_user("memory", 1) == 1
    assert store.delete_by_metadata_user("episode", 1) == 1
    assert store.count("memory") == 1
    assert store.count("episode") == 0
    assert store.delete_namespace("memory") == 1
    assert store.count("memory") == 0
