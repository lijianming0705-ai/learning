from __future__ import annotations

from pathlib import Path

import pytest

from app.rag.store import RagStore


def test_rag_cjk_and_english_hybrid_retrieval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDY_DATA_DIR", str(tmp_path / "rag_data"))
    store = RagStore()
    store.init_db()

    store.upsert_document(
        doc_id="qa:1",
        doc_type="qa_memory",
        notebook_id="nb-ai",
        notebook_title="深度学习笔记",
        source_id="src-1",
        source_title="Attention Is All You Need",
        highlight_id="hl-1",
        conversation_id="conv-1",
        page_number=3,
        title="问答记忆 · Attention P.3",
        content="用户提问：Transformer 中的自注意力机制原理是什么？\nAI 回答：Self-Attention 通过 Q、K 内积缩放并经过 Softmax 得到权重，再对 V 加权求和。",
    )
    store.upsert_document(
        doc_id="qa:2",
        doc_type="qa_memory",
        notebook_id="nb-hist",
        notebook_title="中国近代史",
        source_id="src-2",
        source_title="历史讲义",
        conversation_id="conv-2",
        page_number=1,
        title="问答记忆 · 历史讲义 P.1",
        content="用户提问：洋务运动的核心口号是什么？\nAI 回答：初期以自强为目标，后期提出求富口号。",
    )

    hits = store.search("自注意力机制 Q K Softmax 原理", top_k=3)
    assert hits, "Expected RAG search to return matching Chinese + English Q&A memory"
    assert hits[0].doc_id == "qa:1"
    assert hits[0].highlight_id == "hl-1"
    assert hits[0].page_number == 3

    # Verify exclusion filter works for ongoing conversation
    filtered = store.search("自注意力机制", top_k=3, exclude_conversation_id="conv-1")
    assert all(h.conversation_id != "conv-1" for h in filtered)

    # Verify deletion removes both table and FTS entry
    deleted = store.delete_by_filter(highlight_id="hl-1")
    assert deleted == 1
    after_del = store.search("自注意力机制 Softmax", top_k=3)
    assert all(h.doc_id != "qa:1" for h in after_del)
