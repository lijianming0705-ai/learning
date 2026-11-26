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
    # 1. Health & agy models & default hydrogen medium setting + user selection persistence
    h_resp = test_client.get("/api/health")
    assert h_resp.status_code == 200
    m_resp = test_client.get("/api/agy/models")
    assert m_resp.status_code == 200
    assert m_resp.json()[0]["id"] == "gemini-4-hydrogen-medium"
    s_resp = test_client.get("/api/settings")
    assert s_resp.status_code == 200
    assert s_resp.json()["default_model"] == "gemini-4-hydrogen-medium"

    # Remember user's last selected model across DB re-init
    upd_s = test_client.patch("/api/settings", json={"default_model": "gemini-4-neon-high"})
    assert upd_s.status_code == 200
    assert upd_s.json()["default_model"] == "gemini-4-neon-high"
    from app.db import init_study_db

    init_study_db()
    assert test_client.get("/api/settings").json()["default_model"] == "gemini-4-neon-high"
    # Restore default for remaining assertions
    test_client.patch("/api/settings", json={"default_model": "gemini-4-hydrogen-medium"})

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

    # 9. Source rename + reading position persistence + AI one-click classification
    ren_resp = test_client.patch(
        f"/api/sources/{source_id}",
        json={"title": "Attention Is All You Need (精读版)", "last_page": 1, "last_scroll_ratio": 0.42},
    )
    assert ren_resp.status_code == 200
    assert ren_resp.json()["title"] == "Attention Is All You Need (精读版)"
    assert ren_resp.json()["last_page"] == 1
    assert abs(ren_resp.json()["last_scroll_ratio"] - 0.42) < 1e-6
    get_src_resp = test_client.get(f"/api/sources/{source_id}")
    assert get_src_resp.status_code == 200
    assert abs(get_src_resp.json()["last_scroll_ratio"] - 0.42) < 1e-6
    rag_renamed = test_client.get("/api/rag/search", params={"q": "注意力机制", "doc_type": "qa_memory"}).json()
    assert rag_renamed[0]["source_title"] == "Attention Is All You Need (精读版)"

    cls_resp = test_client.post(f"/api/notebooks/{nb_id}/sources/classify", json={})
    assert cls_resp.status_code == 200
    cls_data = cls_resp.json()
    assert cls_data["classified_count"] == 1
    assert cls_data["categories"] == ["核心论文"] or "核心论文" in cls_data["categories"]
    assert cls_data["sources"][0]["category"] == "核心论文"

    # 9b. Scanned PDF upload -> area crop -> OCR -> visual AI Q&A
    tmp_src = fitz.open()
    tmp_p = tmp_src.new_page(width=420, height=180)
    tmp_p.insert_text((30, 80), "Scanned Formula: E = mc^2", fontsize=14)
    raster_png = tmp_src[0].get_pixmap(matrix=fitz.Matrix(2, 2)).tobytes("png")
    tmp_src.close()

    scanned_pdf = tmp_path / "scanned_book.pdf"
    sdoc = fitz.open()
    spage = sdoc.new_page(width=420, height=180)
    spage.insert_image(spage.rect, stream=raster_png)
    sdoc.save(str(scanned_pdf))
    sdoc.close()

    with open(scanned_pdf, "rb") as f:
        scan_up = test_client.post(
            f"/api/notebooks/{nb_id}/sources/upload",
            files=[("files", ("scanned_book.pdf", f, "application/pdf"))],
        )
    assert scan_up.status_code == 201
    scan_src = scan_up.json()[0]
    assert scan_src["is_scanned"] is True

    area_hl = test_client.post(
        f"/api/sources/{scan_src['id']}/highlights",
        json={
            "color": "sky",
            "quote_text": "",
            "note_text": "",
            "anchor_type": "area",
            "page_number": 1,
            "rects": [{"x": 0.05, "y": 0.15, "width": 0.8, "height": 0.5}],
        },
    )
    assert area_hl.status_code == 201
    area_data = area_hl.json()
    assert area_data["image_url"] == f"/api/highlights/{area_data['id']}/image"

    img_resp = test_client.get(area_data["image_url"])
    assert img_resp.status_code == 200
    assert img_resp.content[:8] == b"\x89PNG\r\n\x1a\n"

    ocr_resp = test_client.post(f"/api/highlights/{area_data['id']}/ocr", json={})
    assert ocr_resp.status_code == 200
    assert "E = mc^2" in ocr_resp.json()["quote_text"]

    scan_ask = test_client.post(
        f"/api/highlights/{area_data['id']}/ask",
        json={"question": "解释框选图片里的公式", "use_rag": True},
    )
    assert scan_ask.status_code == 200
    assert "event: done" in scan_ask.text

    # 9b-2. One-click scanned PDF layout-preserving OCR -> structured text edition + assets + export
    conv_resp = test_client.post(
        f"/api/sources/{scan_src['id']}/ocr-convert",
        json={"start_page": 1, "end_page": 1, "title": "扫描讲义 OCR 排版版"},
    )
    assert conv_resp.status_code == 200
    conv_events = _parse_sse_events(conv_resp.text)
    conv_names = [n for n, _ in conv_events]
    assert "start" in conv_names
    assert "progress" in conv_names
    assert "done" in conv_names
    done_payload = [d for n, d in conv_events if n == "done"][-1]
    assert done_payload["total_figures"] == 1
    ocr_src = done_payload["source"]
    assert ocr_src["type"] == "text"
    assert ocr_src["is_scanned"] is False
    assert "<h1" in ocr_src["html_content"]
    assert "ocr-columns-2" in ocr_src["html_content"]
    assert "<table>" in ocr_src["html_content"]
    expected_asset_url = f"/api/sources/{ocr_src['id']}/assets/p1_fig1.png"
    assert expected_asset_url in ocr_src["html_content"]

    asset_resp = test_client.get(expected_asset_url)
    assert asset_resp.status_code == 200
    assert asset_resp.content[:8] == b"\x89PNG\r\n\x1a\n"

    export_html_resp = test_client.get(f"/api/sources/{ocr_src['id']}/export", params={"format": "html"})
    assert export_html_resp.status_code == 200
    assert "data:image/png;base64," in export_html_resp.text
    assert "相对论基础笔记" in export_html_resp.text

    export_md_resp = test_client.get(f"/api/sources/{ocr_src['id']}/export", params={"format": "md"})
    assert export_md_resp.status_code == 200
    assert "# 相对论基础笔记" in export_md_resp.text

    ocr_rag = test_client.get("/api/rag/search", params={"q": "质能公式示意图 真空光速", "doc_type": "source_chunk"})
    assert ocr_rag.status_code == 200
    assert any(hit["source_id"] == ocr_src["id"] for hit in ocr_rag.json())

    # 9b-3. Full-book multi-page scanned PDF conversion with 2x concurrency
    fullbook_pdf = tmp_path / "fullbook_3p.pdf"
    fb_src_doc = fitz.open()
    for pno in range(1, 4):
        fbp = fb_src_doc.new_page(width=420, height=200)
        fbp.insert_text((30, 80), f"FullBook Scanned Page {pno}", fontsize=14)
    fb_out = fitz.open()
    for pno in range(3):
        pix = fb_src_doc[pno].get_pixmap(matrix=fitz.Matrix(1.5, 1.5)).tobytes("png")
        op = fb_out.new_page(width=420, height=200)
        op.insert_image(op.rect, stream=pix)
    fb_src_doc.close()
    fb_out.save(str(fullbook_pdf))
    fb_out.close()

    with open(fullbook_pdf, "rb") as f:
        fb_up = test_client.post(
            f"/api/notebooks/{nb_id}/sources/upload",
            files=[("files", ("fullbook_3p.pdf", f, "application/pdf"))],
        )
    assert fb_up.status_code == 201
    fb_src = fb_up.json()[0]
    assert fb_src["page_count"] == 3

    fb_conv = test_client.post(
        f"/api/sources/{fb_src['id']}/ocr-convert",
        json={"full_book": True, "concurrency": 2, "title": "全本扫描书 OCR 版"},
    )
    assert fb_conv.status_code == 200
    fb_events = _parse_sse_events(fb_conv.text)
    fb_done = [d for n, d in fb_events if n == "done"][-1]
    assert fb_done["total_figures"] == 3
    assert fb_done["failed_pages"] == 0
    fb_ocr_src = fb_done["source"]
    assert fb_ocr_src["page_count"] == 3
    p1_idx = fb_ocr_src["html_content"].index('data-page="1"')
    p2_idx = fb_ocr_src["html_content"].index('data-page="2"')
    p3_idx = fb_ocr_src["html_content"].index('data-page="3"')
    assert p1_idx < p2_idx < p3_idx
    assert test_client.get(f"/api/sources/{fb_ocr_src['id']}/assets/p3_fig1.png").status_code == 200

    # 9c. Selection pronunciation + full-text natural speech script + select-to-jump
    sel_speech = test_client.post(
        "/api/speech/selection",
        json={
            "text": "Scaled Dot-Product Attention uses Query, Key, and Value matrices.",
            "source_id": source_id,
            "page_number": 1,
        },
    )
    assert sel_speech.status_code == 200
    sel_data = sel_speech.json()
    assert sel_data["language"] == "en-US"
    assert sel_data["audio_url"]
    assert sel_data["ipa_or_guide"]
    clip_resp = test_client.get(sel_data["audio_url"])
    assert clip_resp.status_code == 200
    assert clip_resp.headers["content-type"].startswith("audio/")

    gen_speech = test_client.post(f"/api/sources/{source_id}/speech-script", json={})
    assert gen_speech.status_code == 200
    segs = gen_speech.json()["segments"]
    assert len(segs) == 2

    get_speech = test_client.get(f"/api/sources/{source_id}/speech-script")
    assert get_speech.status_code == 200
    assert len(get_speech.json()["segments"]) == 2

    seg_audio = test_client.get(segs[1]["audio_url"])
    assert seg_audio.status_code == 200
    assert seg_audio.headers["content-type"].startswith("audio/")

    locate_seg2 = test_client.post(
        f"/api/sources/{source_id}/speech-script/locate",
        json={"text": "Multi-Head Attention allows parallel subspace attention", "page_number": 1},
    )
    assert locate_seg2.status_code == 200
    assert locate_seg2.json()["segment_index"] == 1

    locate_seg1 = test_client.post(
        f"/api/sources/{source_id}/speech-script/locate",
        json={"text": "Scaled Dot-Product Attention uses Query", "page_number": 1},
    )
    assert locate_seg1.status_code == 200
    assert locate_seg1.json()["segment_index"] == 0

    # 9d. Reading progress, duration heartbeats & behavior analytics
    from datetime import datetime

    today_str = datetime.now().strftime("%Y-%m-%d")
    hb1 = test_client.post(
        "/api/analytics/reading/heartbeat",
        json={
            "session_id": "sess-test-001",
            "source_id": source_id,
            "delta_seconds": 45,
            "current_page": 1,
            "scroll_ratio": 0.6,
            "progress": 0.6,
            "local_date": today_str,
            "local_hour": 21,
        },
    )
    assert hb1.status_code == 200
    assert hb1.json()["reading_seconds"] == 45
    assert abs(hb1.json()["reading_progress"] - 0.6) < 1e-4

    # Same session updates duration; lower progress should NOT decrease monotonic progress
    hb2 = test_client.post(
        "/api/analytics/reading/heartbeat",
        json={
            "session_id": "sess-test-001",
            "source_id": source_id,
            "delta_seconds": 30,
            "current_page": 1,
            "scroll_ratio": 0.3,
            "progress": 0.3,
            "local_date": today_str,
            "local_hour": 21,
        },
    )
    assert hb2.status_code == 200
    assert hb2.json()["reading_seconds"] == 75
    assert hb2.json()["session_duration_seconds"] == 75
    assert abs(hb2.json()["reading_progress"] - 0.6) < 1e-4

    # New session updates read_sessions_count and notebook rollups
    hb3 = test_client.post(
        "/api/analytics/reading/heartbeat",
        json={
            "session_id": "sess-test-002",
            "source_id": source_id,
            "delta_seconds": 25,
            "current_page": 1,
            "scroll_ratio": 0.96,
            "progress": 1.0,
            "local_date": today_str,
            "local_hour": 22,
        },
    )
    assert hb3.status_code == 200
    assert hb3.json()["reading_seconds"] == 100
    assert abs(hb3.json()["reading_progress"] - 1.0) < 1e-4

    nb_list = test_client.get("/api/notebooks").json()
    target_nb = [n for n in nb_list if n["id"] == nb_id][0]
    assert target_nb["total_reading_seconds"] == 100
    assert abs(target_nb["avg_reading_progress"] - (1.0 / 5.0)) < 0.01

    analytics_resp = test_client.get("/api/analytics/reading", params={"notebook_id": nb_id, "days": 30})
    assert analytics_resp.status_code == 200
    adata = analytics_resp.json()
    assert adata["summary"]["total_reading_seconds"] == 100
    assert adata["summary"]["today_reading_seconds"] == 100
    assert adata["summary"]["current_streak_days"] >= 1
    assert adata["summary"]["total_sessions"] == 2
    assert adata["summary"]["completed_sources"] == 1
    assert len(adata["hourly_distribution"]) == 24
    assert len(adata["daily_trend"]) == 30
    assert "晚间" in adata["behavior_profile"]["peak_period_label"]
    assert len(adata["behavior_profile"]["insights"]) >= 2

    ai_rep = test_client.post("/api/analytics/reading/ai-report", json={"notebook_id": nb_id, "days": 30})
    assert ai_rep.status_code == 200
    assert "阅读习惯与时间画像" in ai_rep.json()["report_markdown"]

    # 10. Reindex & stats
    stats_resp = test_client.get("/api/rag/stats")
    assert stats_resp.status_code == 200
    assert stats_resp.json()["total_documents"] >= 3
    reindex_resp = test_client.post("/api/rag/reindex")
    assert reindex_resp.status_code == 200
    assert reindex_resp.json()["total_documents"] >= 3

    # 11. Cascade delete notebook cleans up RAG
    del_resp = test_client.delete(f"/api/notebooks/{nb_id}")
    assert del_resp.status_code == 200
    stats_after = test_client.get("/api/rag/stats").json()
    assert stats_after["total_documents"] == 0
