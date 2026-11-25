from __future__ import annotations

import json
from pathlib import Path

import fitz


def _parse_sse_events(body: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in body.strip().split("\n\n"):
        if not block.strip():
            continue
        ev_name = "message"
        data_raw = "{}"
        for line in block.splitlines():
            if line.startswith("event:"):
                ev_name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_raw = line[len("data:") :].strip()
        events.append((ev_name, json.loads(data_raw)))
    return events


def test_full_study_workflow_and_rag_persistence(test_client, tmp_path: Path) -> None:
    # 1. Health & agy models & fastest default setting
    h_resp = test_client.get("/api/health")
    assert h_resp.status_code == 200
    m_resp = test_client.get("/api/agy/models")
    assert m_resp.status_code == 200
    assert m_resp.json()[0]["id"] == "gemini-3.8-flash-low"
    s_resp = test_client.get("/api/settings")
    assert s_resp.status_code == 200
    assert s_resp.json()["default_model"] == "gemini-3.8-flash-low"

    # 2. Create notebook
    nb_resp = test_client.post(
        "/api/notebooks",
        json={"title": "Transformer 论文精读", "description": "NLP 基础学习", "color": "#B45309"},
    )
    assert nb_resp.status_code == 201
    nb_id = nb_resp.json()["id"]

    # 3. Generate & upload a PDF source
    pdf_path = tmp_path / "attention.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Scaled Dot-Product Attention uses Query, Key, and Value matrices.", fontsize=12)
    doc.save(str(pdf_path))
    doc.close()

    with open(pdf_path, "rb") as f:
        up_resp = test_client.post(
            f"/api/notebooks/{nb_id}/sources/upload",
            files=[("files", ("attention.pdf", f, "application/pdf"))],
        )
    assert up_resp.status_code == 201
    source = up_resp.json()[0]
    source_id = source["id"]
    assert source["type"] == "pdf"
    assert source["page_count"] == 1

    # Verify raw PDF file can be streamed back for PDF.js
    file_resp = test_client.get(f"/api/sources/{source_id}/file")
    assert file_resp.status_code == 200
    assert file_resp.content.startswith(b"%PDF")

    # 4. Create highlight + note on PDF page 1
    hl_resp = test_client.post(
        f"/api/sources/{source_id}/highlights",
        json={
            "color": "amber",
            "quote_text": "Scaled Dot-Product Attention uses Query, Key, and Value matrices.",
            "note_text": "核心公式：Attention(Q,K,V)",
            "anchor_type": "text",
            "page_number": 1,
            "rects": [{"x": 0.1, "y": 0.1, "width": 0.5, "height": 0.04}],
        },
    )
    assert hl_resp.status_code == 201
    hl_id = hl_resp.json()["id"]

    # 5. Ask AI about the highlight (SSE stream) with explicit model selection
    ask_resp = test_client.post(
        f"/api/highlights/{hl_id}/ask",
        json={"question": "请解释这里的注意力机制为什么需要缩放？", "model": "gemini-4-neon-high", "use_rag": True},
    )
    assert ask_resp.status_code == 200
    sse = _parse_sse_events(ask_resp.text)
    event_names = [name for name, _ in sse]
    assert "rag_context" in event_names
    assert "delta" in event_names
    assert "done" in event_names

    # 6. Verify highlight remarks now persist both user question and AI answer with chosen model
    hl_list_resp = test_client.get(f"/api/sources/{source_id}/highlights")
    assert hl_list_resp.status_code == 200
    highlights = hl_list_resp.json()
    assert len(highlights) == 1
    assert len(highlights[0]["messages"]) == 2
    assert highlights[0]["messages"][0]["role"] == "user"
    assert highlights[0]["messages"][1]["role"] == "assistant"
    assert highlights[0]["messages"][1]["model"] == "gemini-4-neon-high"
    assert "注意力机制" in highlights[0]["messages"][1]["content"]

    # 7. Verify historical AI conversation is saved in RAG DB and retrievable anytime
    rag_resp = test_client.get("/api/rag/search", params={"q": "注意力机制 Query Key Value", "doc_type": "qa_memory"})
    assert rag_resp.status_code == 200
    rag_hits = rag_resp.json()
    assert len(rag_hits) >= 1
    assert rag_hits[0]["highlight_id"] == hl_id
    assert rag_hits[0]["doc_type"] == "qa_memory"

    # 8. Notebook-level chat also works & indexes into RAG
    chat_resp = test_client.post(
        f"/api/notebooks/{nb_id}/chat",
        json={"source_id": source_id, "message": "总结这篇论文的核心贡献", "use_rag": True},
    )
    assert chat_resp.status_code == 200
    chat_sse = _parse_sse_events(chat_resp.text)
    assert any(name == "done" for name, _ in chat_sse)

    # 9. Reindex & stats
    stats_resp = test_client.get("/api/rag/stats")
    assert stats_resp.status_code == 200
    assert stats_resp.json()["total_documents"] >= 3
    reindex_resp = test_client.post("/api/rag/reindex")
    assert reindex_resp.status_code == 200
    assert reindex_resp.json()["total_documents"] >= 3

    # 10. Cascade delete notebook cleans up RAG
    del_resp = test_client.delete(f"/api/notebooks/{nb_id}")
    assert del_resp.status_code == 200
    stats_after = test_client.get("/api/rag/stats").json()
    assert stats_after["total_documents"] == 0
