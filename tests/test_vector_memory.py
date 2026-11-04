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
