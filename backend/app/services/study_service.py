from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.config import DEFAULT_MODEL, ensure_data_dirs
from app.db import DEFAULT_SETTINGS, get_db, utc_now_iso
from app.rag.store import RagHit, rag_store
from app.rag.tokenizer import extract_hybrid_tokens
from app.schemas import (
    CategoryReadingStat,
    ConversationDetail,
    ConversationOut,
    DailyReadingPoint,
    HighlightCreate,
    HighlightOut,
    HighlightRect,
    HighlightUpdate,
    HourlyReadingPoint,
    LocateSpeechResponse,
    MessageOut,
    NotebookCreate,
    NotebookOut,
    NotebookUpdate,
    RagMemoryRef,
    ReadingAnalyticsResponse,
    ReadingBehaviorProfile,
    ReadingHeartbeatRequest,
    ReadingHeartbeatResponse,
    ReadingSummaryMetrics,
    SettingsOut,
    SettingsUpdate,
    SourceDetail,
    SourcePageOut,
    SourceReadingStat,
    SourceSummary,
    SourceUpdate,
    SpeechSegmentOut,
)
from app.services.source_parser import ParsedSource, chunk_text, crop_pdf_region


def new_id() -> str:
    return str(uuid.uuid4())


def get_settings() -> SettingsOut:
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    data = dict(DEFAULT_SETTINGS)
    for r in rows:
        data[r["key"]] = r["value"]
    return SettingsOut(
        default_model=data.get("default_model") or DEFAULT_MODEL,
        rag_top_k=max(1, min(20, int(data.get("rag_top_k", "5")))),
        cross_notebook_rag=data.get("cross_notebook_rag", "true").lower() == "true",
        embedding_provider=data.get("embedding_provider") or "hybrid-local",
        ollama_embedding_model=data.get("ollama_embedding_model") or "",
    )


def update_settings(payload: SettingsUpdate) -> SettingsOut:
    updates: dict[str, str] = {}
    if payload.default_model is not None:
        updates["default_model"] = payload.default_model
    if payload.rag_top_k is not None:
        updates["rag_top_k"] = str(payload.rag_top_k)
    if payload.cross_notebook_rag is not None:
        updates["cross_notebook_rag"] = "true" if payload.cross_notebook_rag else "false"
    if payload.embedding_provider is not None:
        updates["embedding_provider"] = payload.embedding_provider
    if payload.ollama_embedding_model is not None:
        updates["ollama_embedding_model"] = payload.ollama_embedding_model

    if updates:
        with get_db() as conn:
            for k, v in updates.items():
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (k, v),
                )
    settings = get_settings()
    rag_store.configure_embedder(settings.embedding_provider, settings.ollama_embedding_model)
    return settings


# ── Notebooks ──────────────────────────────────────────────────────────────────


def list_notebooks() -> list[NotebookOut]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT n.*,
                   (SELECT COUNT(*) FROM sources s WHERE s.notebook_id = n.id) AS source_count,
                   (SELECT COUNT(*) FROM highlights h WHERE h.notebook_id = n.id) AS highlight_count,
                   (SELECT COUNT(*) FROM messages m
                    JOIN conversations c ON c.id = m.conversation_id
                    WHERE c.notebook_id = n.id AND m.role = 'user') AS qa_count,
                   COALESCE((SELECT SUM(s.reading_seconds) FROM sources s WHERE s.notebook_id = n.id), 0) AS total_reading_seconds,
                   COALESCE((SELECT AVG(s.reading_progress) FROM sources s WHERE s.notebook_id = n.id), 0.0) AS avg_reading_progress
            FROM notebooks n
            ORDER BY n.updated_at DESC
            """
        ).fetchall()
    return [_row_to_notebook(r) for r in rows]


def get_notebook(notebook_id: str) -> NotebookOut | None:
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT n.*,
                   (SELECT COUNT(*) FROM sources s WHERE s.notebook_id = n.id) AS source_count,
                   (SELECT COUNT(*) FROM highlights h WHERE h.notebook_id = n.id) AS highlight_count,
                   (SELECT COUNT(*) FROM messages m
                    JOIN conversations c ON c.id = m.conversation_id
                    WHERE c.notebook_id = n.id AND m.role = 'user') AS qa_count,
                   COALESCE((SELECT SUM(s.reading_seconds) FROM sources s WHERE s.notebook_id = n.id), 0) AS total_reading_seconds,
                   COALESCE((SELECT AVG(s.reading_progress) FROM sources s WHERE s.notebook_id = n.id), 0.0) AS avg_reading_progress
            FROM notebooks n
            WHERE n.id = ?
            """,
            (notebook_id,),
        ).fetchone()
    return _row_to_notebook(row) if row else None


def create_notebook(payload: NotebookCreate) -> NotebookOut:
    nb_id = new_id()
    now = utc_now_iso()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO notebooks (id, title, description, color, icon, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (nb_id, payload.title.strip(), payload.description.strip(), payload.color, payload.icon, now, now),
        )
    return get_notebook(nb_id)  # type: ignore[return-value]


def update_notebook(notebook_id: str, payload: NotebookUpdate) -> NotebookOut | None:
    existing = get_notebook(notebook_id)
    if not existing:
        return None
    now = utc_now_iso()
    with get_db() as conn:
        conn.execute(
            """
            UPDATE notebooks
            SET title = ?, description = ?, color = ?, icon = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                payload.title.strip() if payload.title is not None else existing.title,
                payload.description.strip() if payload.description is not None else existing.description,
                payload.color if payload.color is not None else existing.color,
                payload.icon if payload.icon is not None else existing.icon,
                now,
                notebook_id,
            ),
        )
    return get_notebook(notebook_id)


def delete_notebook(notebook_id: str) -> bool:
    with get_db() as conn:
        file_rows = conn.execute(
            "SELECT file_path FROM sources WHERE notebook_id = ? AND file_path IS NOT NULL",
            (notebook_id,),
        ).fetchall()
        crop_rows = conn.execute(
            "SELECT image_path FROM highlights WHERE notebook_id = ? AND image_path IS NOT NULL",
            (notebook_id,),
        ).fetchall()
        speech_rows = conn.execute(
            """
            SELECT sp.audio_path
            FROM source_speech_segments sp
            JOIN sources s ON s.id = sp.source_id
            WHERE s.notebook_id = ? AND sp.audio_path IS NOT NULL
            """,
            (notebook_id,),
        ).fetchall()
        cur = conn.execute("DELETE FROM notebooks WHERE id = ?", (notebook_id,))
        if cur.rowcount == 0:
            return False

    for r in file_rows:
        fp = Path(r["file_path"])
        if fp.exists():
            fp.unlink(missing_ok=True)
    for cr in crop_rows:
        cp = Path(cr["image_path"])
        if cp.exists():
            cp.unlink(missing_ok=True)
    for sr in speech_rows:
        Path(sr["audio_path"]).unlink(missing_ok=True)
    uploads_nb_dir = ensure_data_dirs().uploads_dir / notebook_id
    if uploads_nb_dir.exists() and not any(uploads_nb_dir.iterdir()):
        uploads_nb_dir.rmdir()
    rag_store.delete_by_filter(notebook_id=notebook_id)
    return True


def _row_to_notebook(row: sqlite3.Row) -> NotebookOut:
    keys = row.keys()
    return NotebookOut(
        id=row["id"],
        title=row["title"],
        description=row["description"],
        color=row["color"],
        icon=row["icon"],
        source_count=int(row["source_count"]),
        highlight_count=int(row["highlight_count"]),
        qa_count=int(row["qa_count"]),
        total_reading_seconds=int(row["total_reading_seconds"]) if "total_reading_seconds" in keys and row["total_reading_seconds"] is not None else 0,
        avg_reading_progress=round(float(row["avg_reading_progress"]), 4) if "avg_reading_progress" in keys and row["avg_reading_progress"] is not None else 0.0,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ── Sources ────────────────────────────────────────────────────────────────────


def list_sources(notebook_id: str) -> list[SourceSummary]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT s.*,
                   (SELECT COUNT(*) FROM highlights h WHERE h.source_id = s.id) AS highlight_count
            FROM sources s
            WHERE s.notebook_id = ?
            ORDER BY s.created_at DESC
            """,
            (notebook_id,),
        ).fetchall()
    return [_row_to_source_summary(r) for r in rows]


def get_source_detail(source_id: str) -> SourceDetail | None:
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT s.*,
                   (SELECT COUNT(*) FROM highlights h WHERE h.source_id = s.id) AS highlight_count
            FROM sources s
            WHERE s.id = ?
            """,
            (source_id,),
        ).fetchone()
        if not row:
            return None
        page_rows = conn.execute(
            "SELECT page_number, text_content FROM source_pages WHERE source_id = ? ORDER BY page_number ASC",
            (source_id,),
        ).fetchall()
    summary = _row_to_source_summary(row)
    return SourceDetail(
        **summary.model_dump(),
        html_content=row["html_content"],
        plain_text=row["plain_text"],
        pages=[SourcePageOut(page_number=p["page_number"], text_content=p["text_content"]) for p in page_rows],
    )


def get_source_file_path(source_id: str) -> tuple[Path, str] | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT file_path, original_filename, title, type FROM sources WHERE id = ?",
            (source_id,),
        ).fetchone()
    if not row or not row["file_path"]:
        return None
    fp = Path(row["file_path"])
    if not fp.exists():
        return None
    return fp, (row["original_filename"] or f"{row['title']}.{row['type']}")


def save_parsed_source(
    *,
    notebook_id: str,
    source_id: str,
    parsed: ParsedSource,
    original_filename: str | None = None,
    source_url: str | None = None,
    file_path: Path | None = None,
) -> SourceDetail:
    now = utc_now_iso()
    nb = get_notebook(notebook_id)
    nb_title = nb.title if nb else ""

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO sources (
                id, notebook_id, type, title, original_filename, source_url, file_path,
                html_content, plain_text, page_count, word_count, is_scanned, status, error_message,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', NULL, ?, ?)
            """,
            (
                source_id,
                notebook_id,
                parsed.type,
                parsed.title,
                original_filename,
                source_url,
                str(file_path) if file_path else None,
                parsed.html_content,
                parsed.plain_text,
                parsed.page_count,
                parsed.word_count,
                1 if parsed.is_scanned else 0,
                now,
                now,
            ),
        )
        for page_num, text_content in parsed.pages:
            conn.execute(
                "INSERT INTO source_pages (id, source_id, page_number, text_content) VALUES (?, ?, ?, ?)",
                (new_id(), source_id, page_num, text_content),
            )
        conn.execute("UPDATE notebooks SET updated_at = ? WHERE id = ?", (now, notebook_id))

    for idx, ch in enumerate(parsed.chunks):
        rag_store.upsert_document(
            doc_id=f"chunk:{source_id}:{idx}",
            doc_type="source_chunk",
            notebook_id=notebook_id,
            notebook_title=nb_title,
            source_id=source_id,
            source_title=parsed.title,
            page_number=ch.page_number,
            title=f"{parsed.title}" + (f" · P.{ch.page_number}" if ch.page_number else ""),
            content=ch.content,
            metadata={"chunk_index": idx, "source_type": parsed.type},
            created_at=now,
        )

    return get_source_detail(source_id)  # type: ignore[return-value]


def update_source(source_id: str, payload: SourceUpdate) -> SourceDetail | None:
    existing = get_source_detail(source_id)
    if not existing:
        return None
    new_title = payload.title.strip() if payload.title is not None else existing.title
    new_category = payload.category.strip() if payload.category is not None else existing.category
    new_last_page = payload.last_page if payload.last_page is not None else existing.last_page
    new_last_scroll = (
        float(payload.last_scroll_ratio)
        if payload.last_scroll_ratio is not None
        else existing.last_scroll_ratio
    )
    new_reading_progress = (
        max(existing.reading_progress, float(payload.reading_progress))
        if payload.reading_progress is not None
        else existing.reading_progress
    )
    meta_changed = (
        (payload.title is not None and new_title != existing.title)
        or (payload.category is not None and new_category != existing.category)
    )
    now = utc_now_iso()
    with get_db() as conn:
        conn.execute(
            """
            UPDATE sources
            SET title = ?, category = ?, last_page = ?, last_scroll_ratio = ?, reading_progress = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                new_title,
                new_category,
                new_last_page,
                new_last_scroll,
                new_reading_progress,
                now if meta_changed else existing.updated_at,
                source_id,
            ),
        )
        if meta_changed:
            conn.execute("UPDATE notebooks SET updated_at = ? WHERE id = ?", (now, existing.notebook_id))
    if payload.title is not None and new_title != existing.title:
        rag_store.update_source_title(source_id, new_title)
    return get_source_detail(source_id)


def list_sources_for_classification(notebook_id: str) -> list[dict[str, str]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, title, type, category, plain_text FROM sources WHERE notebook_id = ? ORDER BY created_at DESC",
            (notebook_id,),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "type": r["type"],
            "category": r["category"] or "",
            "excerpt": " ".join((r["plain_text"] or "").split())[:400],
        }
        for r in rows
    ]


def bulk_update_source_categories(notebook_id: str, mapping: dict[str, str]) -> int:
    if not mapping:
        return 0
    now = utc_now_iso()
    updated = 0
    with get_db() as conn:
        for sid, cat in mapping.items():
            clean_cat = cat.strip()[:60]
            if not clean_cat:
                continue
            cur = conn.execute(
                "UPDATE sources SET category = ?, updated_at = ? WHERE id = ? AND notebook_id = ?",
                (clean_cat, now, sid, notebook_id),
            )
            updated += cur.rowcount
        if updated:
            conn.execute("UPDATE notebooks SET updated_at = ? WHERE id = ?", (now, notebook_id))
    return updated


def delete_source(source_id: str) -> bool:
    with get_db() as conn:
        row = conn.execute("SELECT file_path, notebook_id FROM sources WHERE id = ?", (source_id,)).fetchone()
        if not row:
            return False
        crop_rows = conn.execute(
            "SELECT image_path FROM highlights WHERE source_id = ? AND image_path IS NOT NULL",
            (source_id,),
        ).fetchall()
        speech_rows = conn.execute(
            "SELECT audio_path FROM source_speech_segments WHERE source_id = ? AND audio_path IS NOT NULL",
            (source_id,),
        ).fetchall()
        conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        conn.execute("UPDATE notebooks SET updated_at = ? WHERE id = ?", (utc_now_iso(), row["notebook_id"]))

    if row["file_path"]:
        Path(row["file_path"]).unlink(missing_ok=True)
    for cr in crop_rows:
        Path(cr["image_path"]).unlink(missing_ok=True)
    for sr in speech_rows:
        Path(sr["audio_path"]).unlink(missing_ok=True)
    rag_store.delete_by_filter(source_id=source_id)
    return True


def _row_to_source_summary(row: sqlite3.Row) -> SourceSummary:
    keys = row.keys()
    return SourceSummary(
        id=row["id"],
        notebook_id=row["notebook_id"],
        type=row["type"],
        title=row["title"],
        category=row["category"] if "category" in keys and row["category"] else "",
        original_filename=row["original_filename"],
        source_url=row["source_url"],
        page_count=int(row["page_count"]),
        word_count=int(row["word_count"]),
        highlight_count=int(row["highlight_count"]),
        is_scanned=bool(row["is_scanned"]) if "is_scanned" in keys else False,
        last_page=int(row["last_page"]) if "last_page" in keys and row["last_page"] else 1,
        last_scroll_ratio=float(row["last_scroll_ratio"]) if "last_scroll_ratio" in keys and row["last_scroll_ratio"] is not None else 0.0,
        reading_seconds=int(row["reading_seconds"]) if "reading_seconds" in keys and row["reading_seconds"] is not None else 0,
        reading_progress=round(float(row["reading_progress"]), 4) if "reading_progress" in keys and row["reading_progress"] is not None else 0.0,
        last_read_at=row["last_read_at"] if "last_read_at" in keys else None,
        read_sessions_count=int(row["read_sessions_count"]) if "read_sessions_count" in keys and row["read_sessions_count"] is not None else 0,
        status=row["status"],
        error_message=row["error_message"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ── Highlights & Conversations ─────────────────────────────────────────────────


def list_highlights_for_source(source_id: str) -> list[HighlightOut]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT h.*, s.title AS source_title, s.type AS source_type
            FROM highlights h
            JOIN sources s ON s.id = h.source_id
            WHERE h.source_id = ?
            ORDER BY h.page_number ASC, h.created_at ASC
            """,
            (source_id,),
        ).fetchall()
        return _hydrate_highlights(conn, rows)


def list_highlights_for_notebook(notebook_id: str) -> list[HighlightOut]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT h.*, s.title AS source_title, s.type AS source_type
            FROM highlights h
            JOIN sources s ON s.id = h.source_id
            WHERE h.notebook_id = ?
            ORDER BY h.created_at DESC
            """,
            (notebook_id,),
        ).fetchall()
        return _hydrate_highlights(conn, rows)


def get_highlight(highlight_id: str) -> HighlightOut | None:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT h.*, s.title AS source_title, s.type AS source_type
            FROM highlights h
            JOIN sources s ON s.id = h.source_id
            WHERE h.id = ?
            """,
            (highlight_id,),
        ).fetchall()
        items = _hydrate_highlights(conn, rows)
    return items[0] if items else None


def create_highlight(source_id: str, payload: HighlightCreate) -> HighlightOut | None:
    with get_db() as conn:
        src = conn.execute(
            """
            SELECT s.id, s.notebook_id, s.title, s.type, s.file_path, s.is_scanned, n.title AS notebook_title
            FROM sources s
            JOIN notebooks n ON n.id = s.notebook_id
            WHERE s.id = ?
            """,
            (source_id,),
        ).fetchone()
        if not src:
            return None

        hl_id = new_id()
        now = utc_now_iso()
        rects_list = [r.model_dump() for r in payload.rects]
        rects_json = json.dumps(rects_list)

        image_path_str: str | None = None
        should_crop = (
            src["type"] == "pdf"
            and bool(src["file_path"])
            and bool(rects_list)
            and (payload.anchor_type == "area" or bool(src["is_scanned"]))
        )
        if should_crop:
            pdf_file = Path(src["file_path"])
            if pdf_file.exists():
                crop_out = ensure_data_dirs().crops_dir / f"{hl_id}.png"
                try:
                    crop_pdf_region(pdf_file, payload.page_number, rects_list, crop_out)
                    image_path_str = str(crop_out)
                except Exception:
                    image_path_str = None

        conn.execute(
            """
            INSERT INTO highlights (
                id, notebook_id, source_id, color, quote_text, note_text,
                anchor_type, page_number, rects_json, start_offset, end_offset,
                prefix_text, suffix_text, image_path, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                hl_id,
                src["notebook_id"],
                source_id,
                payload.color,
                payload.quote_text.strip(),
                payload.note_text.strip(),
                payload.anchor_type,
                payload.page_number,
                rects_json,
                payload.start_offset,
                payload.end_offset,
                payload.prefix_text,
                payload.suffix_text,
                image_path_str,
                now,
                now,
            ),
        )
        conn.execute("UPDATE notebooks SET updated_at = ? WHERE id = ?", (now, src["notebook_id"]))

    if payload.note_text.strip() or payload.quote_text.strip():
        _sync_highlight_note_to_rag(
            highlight_id=hl_id,
            notebook_id=src["notebook_id"],
            notebook_title=src["notebook_title"],
            source_id=source_id,
            source_title=src["title"],
            page_number=payload.page_number,
            quote_text=payload.quote_text.strip(),
            note_text=payload.note_text.strip(),
            created_at=now,
        )
    return get_highlight(hl_id)


def update_highlight(highlight_id: str, payload: HighlightUpdate) -> HighlightOut | None:
    existing = get_highlight(highlight_id)
    if not existing:
        return None
    now = utc_now_iso()
    new_color = payload.color if payload.color is not None else existing.color
    new_note = payload.note_text.strip() if payload.note_text is not None else existing.note_text
    new_quote = payload.quote_text.strip() if payload.quote_text is not None else existing.quote_text

    with get_db() as conn:
        conn.execute(
            """
            UPDATE highlights
            SET color = ?, note_text = ?, quote_text = ?, updated_at = ?
            WHERE id = ?
            """,
            (new_color, new_note, new_quote, now, highlight_id),
        )
        nb_row = conn.execute("SELECT title FROM notebooks WHERE id = ?", (existing.notebook_id,)).fetchone()
        nb_title = nb_row["title"] if nb_row else ""

    if new_note or new_quote:
        _sync_highlight_note_to_rag(
            highlight_id=highlight_id,
            notebook_id=existing.notebook_id,
            notebook_title=nb_title,
            source_id=existing.source_id,
            source_title=existing.source_title,
            page_number=existing.page_number,
            quote_text=new_quote,
            note_text=new_note,
            created_at=existing.created_at,
        )
    return get_highlight(highlight_id)


def get_highlight_image_path(highlight_id: str) -> Path | None:
    with get_db() as conn:
        row = conn.execute("SELECT image_path FROM highlights WHERE id = ?", (highlight_id,)).fetchone()
    if not row or not row["image_path"]:
        return None
    p = Path(row["image_path"])
    return p if p.exists() else None


def update_highlight_quote_from_ocr(highlight_id: str, ocr_text: str) -> HighlightOut | None:
    return update_highlight(highlight_id, HighlightUpdate(quote_text=ocr_text))


def delete_highlight(highlight_id: str) -> bool:
    with get_db() as conn:
        row = conn.execute("SELECT image_path FROM highlights WHERE id = ?", (highlight_id,)).fetchone()
        cur = conn.execute("DELETE FROM highlights WHERE id = ?", (highlight_id,))
        if cur.rowcount == 0:
            return False
    if row and row["image_path"]:
        Path(row["image_path"]).unlink(missing_ok=True)
    rag_store.delete_by_filter(highlight_id=highlight_id)
    return True


def _sync_highlight_note_to_rag(
    *,
    highlight_id: str,
    notebook_id: str,
    notebook_title: str,
    source_id: str,
    source_title: str,
    page_number: int,
    quote_text: str,
    note_text: str,
    created_at: str,
) -> None:
    if not note_text and not quote_text:
        return
    parts: list[str] = []
    if quote_text:
        parts.append(f"【划线原文】{quote_text}")
    if note_text:
        parts.append(f"【学习笔记】{note_text}")
    rag_store.upsert_document(
        doc_id=f"note:{highlight_id}",
        doc_type="highlight_note",
        notebook_id=notebook_id,
        notebook_title=notebook_title,
        source_id=source_id,
        source_title=source_title,
        highlight_id=highlight_id,
        page_number=page_number,
        title=f"划线笔记 · {source_title} P.{page_number}",
        content="\n".join(parts),
        metadata={"page_number": page_number},
        created_at=created_at,
    )


def _hydrate_highlights(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[HighlightOut]:
    if not rows:
        return []
    hl_ids = [r["id"] for r in rows]
    placeholders = ",".join("?" for _ in hl_ids)
    conv_rows = conn.execute(
        f"SELECT id, highlight_id FROM conversations WHERE highlight_id IN ({placeholders})",
        hl_ids,
    ).fetchall()
    conv_by_hl = {c["highlight_id"]: c["id"] for c in conv_rows}

    conv_ids = list(conv_by_hl.values())
    msgs_by_conv: dict[str, list[MessageOut]] = {cid: [] for cid in conv_ids}
    if conv_ids:
        c_ph = ",".join("?" for _ in conv_ids)
        msg_rows = conn.execute(
            f"SELECT * FROM messages WHERE conversation_id IN ({c_ph}) ORDER BY created_at ASC",
            conv_ids,
        ).fetchall()
        for mr in msg_rows:
            msgs_by_conv[mr["conversation_id"]].append(_row_to_message(mr))

    result: list[HighlightOut] = []
    for r in rows:
        cid = conv_by_hl.get(r["id"])
        raw_rects = json.loads(r["rects_json"] or "[]")
        rects = [HighlightRect(**item) for item in raw_rects]
        raw_img = r["image_path"] if "image_path" in r.keys() else None
        has_img = bool(raw_img and Path(raw_img).exists())
        result.append(
            HighlightOut(
                id=r["id"],
                notebook_id=r["notebook_id"],
                source_id=r["source_id"],
                source_title=r["source_title"],
                source_type=r["source_type"],
                color=r["color"],
                quote_text=r["quote_text"],
                note_text=r["note_text"],
                anchor_type=r["anchor_type"],
                page_number=int(r["page_number"]),
                rects=rects,
                start_offset=r["start_offset"],
                end_offset=r["end_offset"],
                prefix_text=r["prefix_text"],
                suffix_text=r["suffix_text"],
                image_url=f"/api/highlights/{r['id']}/image" if has_img else None,
                conversation_id=cid,
                messages=msgs_by_conv.get(cid, []) if cid else [],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
        )
    return result


# ── Conversations & Q&A Persistence ────────────────────────────────────────────


def get_or_create_highlight_conversation(highlight_id: str, model: str) -> tuple[str, str | None, list[MessageOut]]:
    with get_db() as conn:
        hl = conn.execute(
            "SELECT id, notebook_id, source_id, quote_text, page_number FROM highlights WHERE id = ?",
            (highlight_id,),
        ).fetchone()
        if not hl:
            raise KeyError("Highlight not found")
        conv = conn.execute(
            "SELECT id, agy_conversation_id FROM conversations WHERE highlight_id = ?",
            (highlight_id,),
        ).fetchone()
        if conv:
            conv_id = conv["id"]
            agy_cid = conv["agy_conversation_id"]
        else:
            conv_id = new_id()
            agy_cid = None
            now = utc_now_iso()
            short_title = (hl["quote_text"] or f"P.{hl['page_number']} 区域划线")[:48]
            conn.execute(
                """
                INSERT INTO conversations (
                    id, notebook_id, source_id, highlight_id, title, agy_conversation_id, model, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)
                """,
                (conv_id, hl["notebook_id"], hl["source_id"], highlight_id, short_title, model, now, now),
            )
        msg_rows = conn.execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at ASC",
            (conv_id,),
        ).fetchall()
    return conv_id, agy_cid, [_row_to_message(m) for m in msg_rows]


def get_or_create_notebook_conversation(
    notebook_id: str,
    conversation_id: str | None,
    source_id: str | None,
    first_prompt: str,
    model: str,
) -> tuple[str, str | None, list[MessageOut]]:
    with get_db() as conn:
        nb = conn.execute("SELECT id FROM notebooks WHERE id = ?", (notebook_id,)).fetchone()
        if not nb:
            raise KeyError("Notebook not found")
        if conversation_id:
            conv = conn.execute(
                "SELECT id, agy_conversation_id FROM conversations WHERE id = ? AND notebook_id = ?",
                (conversation_id, notebook_id),
            ).fetchone()
            if not conv:
                raise KeyError("Conversation not found")
            conv_id = conv["id"]
            agy_cid = conv["agy_conversation_id"]
        else:
            conv_id = new_id()
            agy_cid = None
            now = utc_now_iso()
            title = first_prompt.strip().splitlines()[0][:48] or "笔记本问答"
            conn.execute(
                """
                INSERT INTO conversations (
                    id, notebook_id, source_id, highlight_id, title, agy_conversation_id, model, created_at, updated_at
                ) VALUES (?, ?, ?, NULL, ?, NULL, ?, ?, ?)
                """,
                (conv_id, notebook_id, source_id, title, model, now, now),
            )
        msg_rows = conn.execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at ASC",
            (conv_id,),
        ).fetchall()
    return conv_id, agy_cid, [_row_to_message(m) for m in msg_rows]


def append_message(
    *,
    conversation_id: str,
    role: str,
    content: str,
    model: str | None = None,
    usage: dict[str, Any] | None = None,
    retrieved_memories: list[RagMemoryRef] | None = None,
    duration_seconds: float | None = None,
    agy_conversation_id: str | None = None,
) -> MessageOut:
    msg_id = new_id()
    now = utc_now_iso()
    usage_str = json.dumps(usage, ensure_ascii=False) if usage is not None else None
    mem_str = json.dumps([m.model_dump() for m in (retrieved_memories or [])], ensure_ascii=False)
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO messages (
                id, conversation_id, role, content, model, usage_json,
                retrieved_memories_json, duration_seconds, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (msg_id, conversation_id, role, content, model, usage_str, mem_str, duration_seconds, now),
        )
        if agy_conversation_id:
            conn.execute(
                "UPDATE conversations SET agy_conversation_id = ?, model = COALESCE(?, model), updated_at = ? WHERE id = ?",
                (agy_conversation_id, model, now, conversation_id),
            )
        else:
            conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (msg_id,)).fetchone()
    return _row_to_message(row)


def index_qa_turn_to_rag(
    *,
    message_id: str,
    conversation_id: str,
    notebook_id: str,
    source_id: str | None,
    highlight_id: str | None,
    question: str,
    answer: str,
    created_at: str,
) -> None:
    with get_db() as conn:
        nb = conn.execute("SELECT title FROM notebooks WHERE id = ?", (notebook_id,)).fetchone()
        nb_title = nb["title"] if nb else ""
        src_title = ""
        if source_id:
            src = conn.execute("SELECT title FROM sources WHERE id = ?", (source_id,)).fetchone()
            src_title = src["title"] if src else ""
        page_number: int | None = None
        quote_text = ""
        if highlight_id:
            hl = conn.execute("SELECT page_number, quote_text FROM highlights WHERE id = ?", (highlight_id,)).fetchone()
            if hl:
                page_number = int(hl["page_number"])
                quote_text = hl["quote_text"]

    parts = []
    if quote_text:
        parts.append(f"划线原文：{quote_text}")
    parts.append(f"用户提问：{question}")
    parts.append(f"AI 回答：{answer}")
    title = f"问答记忆 · {src_title or nb_title}" + (f" P.{page_number}" if page_number else "")
    rag_store.upsert_document(
        doc_id=f"qa:{message_id}",
        doc_type="qa_memory",
        notebook_id=notebook_id,
        notebook_title=nb_title,
        source_id=source_id,
        source_title=src_title,
        highlight_id=highlight_id,
        conversation_id=conversation_id,
        page_number=page_number,
        title=title,
        content="\n".join(parts),
        metadata={"question": question[:300]},
        created_at=created_at,
    )


def list_notebook_conversations(notebook_id: str) -> list[ConversationOut]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT c.*,
                   (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count
            FROM conversations c
            WHERE c.notebook_id = ? AND c.highlight_id IS NULL
            ORDER BY c.updated_at DESC
            """,
            (notebook_id,),
        ).fetchall()
    return [
        ConversationOut(
            id=r["id"],
            notebook_id=r["notebook_id"],
            source_id=r["source_id"],
            highlight_id=r["highlight_id"],
            title=r["title"],
            model=r["model"],
            message_count=int(r["message_count"]),
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]


def get_conversation_detail(conversation_id: str) -> ConversationDetail | None:
    with get_db() as conn:
        c = conn.execute(
            """
            SELECT c.*,
                   (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count
            FROM conversations c
            WHERE c.id = ?
            """,
            (conversation_id,),
        ).fetchone()
        if not c:
            return None
        msgs = conn.execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at ASC",
            (conversation_id,),
        ).fetchall()
    return ConversationDetail(
        id=c["id"],
        notebook_id=c["notebook_id"],
        source_id=c["source_id"],
        highlight_id=c["highlight_id"],
        title=c["title"],
        model=c["model"],
        message_count=int(c["message_count"]),
        created_at=c["created_at"],
        updated_at=c["updated_at"],
        messages=[_row_to_message(m) for m in msgs],
    )


def delete_conversation(conversation_id: str) -> bool:
    with get_db() as conn:
        cur = conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        if cur.rowcount == 0:
            return False
    rag_store.delete_by_filter(conversation_id=conversation_id)
    return True


def get_surrounding_page_context(source_id: str, page_number: int) -> str:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT page_number, text_content
            FROM source_pages
            WHERE source_id = ? AND page_number BETWEEN ? AND ?
            ORDER BY page_number ASC
            """,
            (source_id, max(1, page_number - 1), page_number + 1),
        ).fetchall()
    parts = [f"[P.{r['page_number']}]\n{r['text_content'][:1800]}" for r in rows if r["text_content"]]
    return "\n\n".join(parts)[:3800]


def get_source_overview_context(source_id: str) -> str:
    with get_db() as conn:
        row = conn.execute("SELECT title, plain_text FROM sources WHERE id = ?", (source_id,)).fetchone()
    if not row:
        return ""
    return f"当前阅读素材《{row['title']}》节选：\n{row['plain_text'][:3200]}"


def reindex_all_rag() -> dict[str, Any]:
    rag_store.clear_all()
    with get_db() as conn:
        nb_map = {r["id"]: r["title"] for r in conn.execute("SELECT id, title FROM notebooks").fetchall()}
        sources = conn.execute("SELECT * FROM sources").fetchall()
        pages = conn.execute("SELECT * FROM source_pages ORDER BY source_id, page_number").fetchall()
        highlights = conn.execute("SELECT * FROM highlights").fetchall()
        convs = {r["id"]: r for r in conn.execute("SELECT * FROM conversations").fetchall()}
        msgs = conn.execute("SELECT * FROM messages ORDER BY conversation_id, created_at ASC").fetchall()

    src_map = {s["id"]: s for s in sources}
    pages_by_src: dict[str, list[sqlite3.Row]] = {}
    for p in pages:
        pages_by_src.setdefault(p["source_id"], []).append(p)

    for s in sources:
        sid = s["id"]
        nb_id = s["notebook_id"]
        nb_title = nb_map.get(nb_id, "")
        chunk_idx = 0
        for p in pages_by_src.get(sid, []):
            for ch in chunk_text(p["text_content"], page_number=p["page_number"]):
                rag_store.upsert_document(
                    doc_id=f"chunk:{sid}:{chunk_idx}",
                    doc_type="source_chunk",
                    notebook_id=nb_id,
                    notebook_title=nb_title,
                    source_id=sid,
                    source_title=s["title"],
                    page_number=ch.page_number,
                    title=f"{s['title']}" + (f" · P.{ch.page_number}" if ch.page_number else ""),
                    content=ch.content,
                    metadata={"chunk_index": chunk_idx, "source_type": s["type"]},
                    created_at=s["created_at"],
                )
                chunk_idx += 1

    for h in highlights:
        if h["note_text"] or h["quote_text"]:
            src = src_map.get(h["source_id"])
            _sync_highlight_note_to_rag(
                highlight_id=h["id"],
                notebook_id=h["notebook_id"],
                notebook_title=nb_map.get(h["notebook_id"], ""),
                source_id=h["source_id"],
                source_title=src["title"] if src else "",
                page_number=int(h["page_number"]),
                quote_text=h["quote_text"],
                note_text=h["note_text"],
                created_at=h["created_at"],
            )

    msgs_by_conv: dict[str, list[sqlite3.Row]] = {}
    for m in msgs:
        msgs_by_conv.setdefault(m["conversation_id"], []).append(m)

    for cid, conv_msgs in msgs_by_conv.items():
        conv = convs.get(cid)
        if not conv:
            continue
        last_user_text = ""
        for m in conv_msgs:
            if m["role"] == "user":
                last_user_text = m["content"]
            elif m["role"] == "assistant" and last_user_text:
                index_qa_turn_to_rag(
                    message_id=m["id"],
                    conversation_id=cid,
                    notebook_id=conv["notebook_id"],
                    source_id=conv["source_id"],
                    highlight_id=conv["highlight_id"],
                    question=last_user_text,
                    answer=m["content"],
                    created_at=m["created_at"],
                )

    return rag_store.stats()


def rag_hit_to_ref(hit: RagHit) -> RagMemoryRef:
    return RagMemoryRef(
        doc_id=hit.doc_id,
        doc_type=hit.doc_type,  # type: ignore[arg-type]
        score=hit.score,
        notebook_id=hit.notebook_id,
        notebook_title=hit.notebook_title,
        source_id=hit.source_id,
        source_title=hit.source_title,
        highlight_id=hit.highlight_id,
        conversation_id=hit.conversation_id,
        page_number=hit.page_number,
        title=hit.title,
        snippet=hit.snippet,
        content=hit.content,
        created_at=hit.created_at,
    )


def _row_to_message(row: sqlite3.Row) -> MessageOut:
    usage = json.loads(row["usage_json"]) if row["usage_json"] else None
    raw_mem = json.loads(row["retrieved_memories_json"] or "[]")
    memories = [RagMemoryRef(**m) for m in raw_mem]
    return MessageOut(
        id=row["id"],
        conversation_id=row["conversation_id"],
        role=row["role"],  # type: ignore[arg-type]
        content=row["content"],
        model=row["model"],
        usage=usage,
        retrieved_memories=memories,
        duration_seconds=row["duration_seconds"],
        created_at=row["created_at"],
    )


# ── Speech Script & Segment Location ───────────────────────────────────────────


def list_speech_segments(source_id: str) -> list[SpeechSegmentOut]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, source_id, segment_index, page_number, heading, original_excerpt, spoken_text, language
            FROM source_speech_segments
            WHERE source_id = ?
            ORDER BY segment_index ASC
            """,
            (source_id,),
        ).fetchall()
    return [
        SpeechSegmentOut(
            id=r["id"],
            source_id=r["source_id"],
            segment_index=int(r["segment_index"]),
            page_number=int(r["page_number"]),
            heading=r["heading"] or "",
            original_excerpt=r["original_excerpt"] or "",
            spoken_text=r["spoken_text"],
            language=r["language"] or "zh-CN",
            audio_url=f"/api/speech/segments/{r['id']}/audio",
        )
        for r in rows
    ]


def replace_source_speech_segments(source_id: str, segments: list[dict[str, Any]]) -> list[SpeechSegmentOut]:
    now = utc_now_iso()
    with get_db() as conn:
        old_rows = conn.execute(
            "SELECT audio_path FROM source_speech_segments WHERE source_id = ? AND audio_path IS NOT NULL",
            (source_id,),
        ).fetchall()
        conn.execute("DELETE FROM source_speech_segments WHERE source_id = ?", (source_id,))
        for idx, seg in enumerate(segments):
            seg_id = new_id()
            conn.execute(
                """
                INSERT INTO source_speech_segments (
                    id, source_id, segment_index, page_number, heading, original_excerpt,
                    spoken_text, language, audio_path, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    seg_id,
                    source_id,
                    idx,
                    max(1, int(seg.get("page_number") or 1)),
                    str(seg.get("heading") or "")[:120],
                    str(seg.get("original_excerpt") or "")[:160],
                    str(seg.get("spoken_text") or "").strip(),
                    str(seg.get("language") or "zh-CN"),
                    now,
                ),
            )
    for r in old_rows:
        Path(r["audio_path"]).unlink(missing_ok=True)
    return list_speech_segments(source_id)


def get_speech_segment_record(segment_id: str) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM source_speech_segments WHERE id = ?",
            (segment_id,),
        ).fetchone()
    if not row:
        return None
    return dict(row)


def set_speech_segment_audio_path(segment_id: str, audio_path: str) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE source_speech_segments SET audio_path = ? WHERE id = ?",
            (audio_path, segment_id),
        )


_LOCATE_PUNCT_RE = re.compile(r"[\s\W_]+", re.UNICODE)


def _normalize_for_match(text: str) -> str:
    return _LOCATE_PUNCT_RE.sub("", (text or "").lower())


def locate_speech_segment(
    source_id: str,
    selected_text: str,
    page_number: int | None = None,
) -> LocateSpeechResponse | None:
    segments = list_speech_segments(source_id)
    if not segments:
        return None

    query_raw = " ".join((selected_text or "").split()).strip()
    query_compact = _normalize_for_match(query_raw)
    query_tokens = set(extract_hybrid_tokens(query_raw, include_trigrams=True))
    query_trigrams = (
        {query_compact[i : i + 3] for i in range(max(0, len(query_compact) - 2))}
        if len(query_compact) >= 3
        else ({query_compact} if query_compact else set())
    )
    query_prefix = query_compact[:24]

    best_seg = segments[0]
    best_score = -999.0

    for seg in segments:
        score = 0.0
        excerpt_compact = _normalize_for_match(seg.original_excerpt)
        spoken_compact = _normalize_for_match(seg.spoken_text)
        combined_text = f"{seg.heading} {seg.original_excerpt} {seg.spoken_text}"

        if query_compact and len(query_compact) >= 4:
            if query_compact in excerpt_compact or excerpt_compact and excerpt_compact in query_compact:
                score += 12.0
            elif query_prefix and len(query_prefix) >= 6 and query_prefix in excerpt_compact:
                score += 9.5
            if query_compact in spoken_compact or (len(query_prefix) >= 6 and query_prefix in spoken_compact):
                score += 9.0

        if query_tokens:
            seg_tokens = set(extract_hybrid_tokens(combined_text, include_trigrams=True))
            if seg_tokens:
                overlap = len(query_tokens & seg_tokens) / len(query_tokens)
                score += 6.0 * overlap

        if query_trigrams:
            combined_compact = excerpt_compact + spoken_compact
            seg_trigrams = (
                {combined_compact[i : i + 3] for i in range(max(0, len(combined_compact) - 2))}
                if len(combined_compact) >= 3
                else set()
            )
            if seg_trigrams:
                tri_overlap = len(query_trigrams & seg_trigrams) / len(query_trigrams)
                score += 4.0 * tri_overlap

        if page_number is not None:
            diff = abs(seg.page_number - page_number)
            if diff == 0:
                score += 3.0
            elif diff == 1:
                score += 1.0
            else:
                score -= 0.15 * min(diff, 10)

        if score > best_score:
            best_score = score
            best_seg = seg

    return LocateSpeechResponse(
        segment_index=best_seg.segment_index,
        segment_id=best_seg.id,
        page_number=best_seg.page_number,
        score=round(best_score, 3),
    )


# ── Reading Progress, Sessions & Behavior Analytics ────────────────────────────

_DATE_STR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_TYPE_LABELS: dict[str, str] = {
    "pdf": "PDF 文献",
    "docx": "Word 文档",
    "url": "网页文章",
    "text": "纯文本 / Markdown",
}


def _resolve_local_date_hour(local_date: str | None, local_hour: int | None) -> tuple[str, int]:
    now_local = datetime.now()
    clean_date = local_date.strip() if local_date else ""
    if not _DATE_STR_RE.match(clean_date):
        clean_date = now_local.strftime("%Y-%m-%d")
    clean_hour = int(local_hour) if local_hour is not None and 0 <= local_hour <= 23 else now_local.hour
    return clean_date, clean_hour


def record_reading_heartbeat(payload: ReadingHeartbeatRequest) -> ReadingHeartbeatResponse | None:
    now = utc_now_iso()
    local_date, local_hour = _resolve_local_date_hour(payload.local_date, payload.local_hour)

    with get_db() as conn:
        src = conn.execute(
            """
            SELECT id, notebook_id, reading_seconds, reading_progress, read_sessions_count
            FROM sources
            WHERE id = ?
            """,
            (payload.source_id,),
        ).fetchone()
        if not src:
            return None

        prev_seconds = int(src["reading_seconds"] or 0)
        prev_progress = float(src["reading_progress"] or 0.0)
        prev_sessions = int(src["read_sessions_count"] or 0)

        clamped_progress = max(0.0, min(1.0, float(payload.progress)))
        new_progress = max(prev_progress, clamped_progress)
        progress_delta = max(0.0, new_progress - prev_progress)
        new_total_seconds = prev_seconds + payload.delta_seconds

        existing_sess = conn.execute(
            "SELECT id, start_page, duration_seconds, pages_visited, progress_delta FROM reading_sessions WHERE id = ? AND source_id = ?",
            (payload.session_id, payload.source_id),
        ).fetchone()

        if existing_sess:
            session_duration = int(existing_sess["duration_seconds"] or 0) + payload.delta_seconds
            pages_span = abs(payload.current_page - int(existing_sess["start_page"] or 1)) + 1
            new_pages_visited = max(int(existing_sess["pages_visited"] or 1), pages_span)
            new_sess_prog_delta = float(existing_sess["progress_delta"] or 0.0) + progress_delta
            conn.execute(
                """
                UPDATE reading_sessions
                SET ended_at = ?,
                    duration_seconds = ?,
                    end_page = ?,
                    pages_visited = ?,
                    progress_delta = ?
                WHERE id = ?
                """,
                (
                    now,
                    session_duration,
                    payload.current_page,
                    new_pages_visited,
                    new_sess_prog_delta,
                    payload.session_id,
                ),
            )
            new_sessions_count = prev_sessions
        else:
            session_duration = payload.delta_seconds
            conn.execute(
                """
                INSERT INTO reading_sessions (
                    id, notebook_id, source_id, started_at, ended_at,
                    duration_seconds, start_page, end_page, pages_visited,
                    progress_delta, local_date, local_hour
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    payload.session_id,
                    src["notebook_id"],
                    payload.source_id,
                    now,
                    now,
                    session_duration,
                    payload.current_page,
                    payload.current_page,
                    progress_delta,
                    local_date,
                    local_hour,
                ),
            )
            new_sessions_count = prev_sessions + 1

        conn.execute(
            """
            UPDATE sources
            SET reading_seconds = ?,
                reading_progress = ?,
                last_page = ?,
                last_scroll_ratio = ?,
                last_read_at = ?,
                read_sessions_count = ?
            WHERE id = ?
            """,
            (
                new_total_seconds,
                round(new_progress, 4),
                payload.current_page,
                max(0.0, min(1.0, float(payload.scroll_ratio))),
                now,
                new_sessions_count,
                payload.source_id,
            ),
        )

    return ReadingHeartbeatResponse(
        source_id=payload.source_id,
        session_id=payload.session_id,
        reading_seconds=new_total_seconds,
        reading_progress=round(new_progress, 4),
        last_read_at=now,
        session_duration_seconds=session_duration,
    )


def _format_duration_cn(seconds: int) -> str:
    if seconds <= 0:
        return "0分钟"
    if seconds < 60:
        return f"{seconds}秒"
    mins = seconds // 60
    if mins < 60:
        return f"{mins}分钟"
    hrs = mins // 60
    rem_mins = mins % 60
    return f"{hrs}小时{rem_mins}分钟" if rem_mins else f"{hrs}小时"


def _compute_streak_days(active_dates: set[str], today_str: str) -> int:
    if not active_dates:
        return 0
    today_dt = datetime.strptime(today_str, "%Y-%m-%d").date()
    if today_str in active_dates:
        cursor = today_dt
    else:
        yesterday = today_dt - timedelta(days=1)
        if yesterday.strftime("%Y-%m-%d") in active_dates:
            cursor = yesterday
        else:
            return 0
    streak = 0
    while cursor.strftime("%Y-%m-%d") in active_dates:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def _estimate_remaining_seconds(
    *,
    progress: float,
    reading_seconds: int,
    word_count: int,
    page_count: int,
) -> int | None:
    if progress >= 0.95:
        return 0
    if progress >= 0.05 and reading_seconds >= 30:
        est = int(reading_seconds * (1.0 - progress) / progress)
        return min(est, 48 * 3600)
    if word_count > 0:
        return int(max(60, (word_count * (1.0 - progress) / 260.0) * 60))
    if page_count > 0:
        return int(max(60, page_count * (1.0 - progress) * 120))
    return None


def get_reading_analytics(notebook_id: str | None = None, days: int = 30) -> ReadingAnalyticsResponse:
    today_dt = datetime.now().date()
    today_str = today_dt.strftime("%Y-%m-%d")
    start_dt = today_dt - timedelta(days=days - 1)
    start_str = start_dt.strftime("%Y-%m-%d")

    nb_filter_src = "WHERE s.notebook_id = ?" if notebook_id else ""
    nb_filter_sess = "AND rs.notebook_id = ?" if notebook_id else ""
    nb_params: tuple[Any, ...] = (notebook_id,) if notebook_id else ()

    with get_db() as conn:
        src_rows = conn.execute(
            f"""
            SELECT s.id, s.notebook_id, n.title AS notebook_title, s.title, s.type, s.category,
                   s.page_count, s.word_count, s.last_page, s.reading_progress,
                   s.reading_seconds, s.read_sessions_count, s.last_read_at,
                   (SELECT COUNT(*) FROM highlights h WHERE h.source_id = s.id) AS highlight_count,
                   (SELECT COUNT(*) FROM messages m
                    JOIN conversations c ON c.id = m.conversation_id
                    WHERE (c.source_id = s.id OR EXISTS (
                        SELECT 1 FROM highlights h2 WHERE h2.id = c.highlight_id AND h2.source_id = s.id
                    )) AND m.role = 'user') AS qa_count
            FROM sources s
            JOIN notebooks n ON n.id = s.notebook_id
            {nb_filter_src}
            ORDER BY COALESCE(s.last_read_at, s.updated_at) DESC
            """,
            nb_params,
        ).fetchall()

        sess_rows = conn.execute(
            f"""
            SELECT rs.id, rs.source_id, rs.duration_seconds, rs.local_date, rs.local_hour
            FROM reading_sessions rs
            WHERE rs.local_date >= ? {nb_filter_sess}
            ORDER BY rs.local_date ASC
            """,
            (start_str, *nb_params),
        ).fetchall()

        all_dates_rows = conn.execute(
            f"""
            SELECT DISTINCT rs.local_date
            FROM reading_sessions rs
            WHERE 1=1 {nb_filter_sess}
            """,
            nb_params,
        ).fetchall()

        hl_filter = "WHERE notebook_id = ?" if notebook_id else ""
        hl_rows = conn.execute(f"SELECT created_at FROM highlights {hl_filter}", nb_params).fetchall()
        qa_rows = conn.execute(
            f"""
            SELECT m.created_at
            FROM messages m
            JOIN conversations c ON c.id = m.conversation_id
            WHERE m.role = 'user' {"AND c.notebook_id = ?" if notebook_id else ""}
            """,
            nb_params,
        ).fetchall()

    # Daily trend buckets
    daily_map: dict[str, dict[str, int]] = {}
    for i in range(days):
        d_str = (start_dt + timedelta(days=i)).strftime("%Y-%m-%d")
        daily_map[d_str] = {"reading_seconds": 0, "sessions_count": 0, "highlights_count": 0, "qa_count": 0}

    hourly_buckets: list[dict[str, int]] = [{"reading_seconds": 0, "sessions_count": 0} for _ in range(24)]

    window_sessions = len(sess_rows)
    window_longest = 0
    window_total_sess_secs = 0
    for r in sess_rows:
        dur = int(r["duration_seconds"] or 0)
        d_str = r["local_date"]
        hr = max(0, min(23, int(r["local_hour"] or 0)))
        window_total_sess_secs += dur
        if dur > window_longest:
            window_longest = dur
        if d_str in daily_map:
            daily_map[d_str]["reading_seconds"] += dur
            daily_map[d_str]["sessions_count"] += 1
        hourly_buckets[hr]["reading_seconds"] += dur
        hourly_buckets[hr]["sessions_count"] += 1

    for hr_row in hl_rows:
        d_str = str(hr_row["created_at"] or "")[:10]
        if d_str in daily_map:
            daily_map[d_str]["highlights_count"] += 1

    for qr_row in qa_rows:
        d_str = str(qr_row["created_at"] or "")[:10]
        if d_str in daily_map:
            daily_map[d_str]["qa_count"] += 1

    all_active_dates = {r["local_date"] for r in all_dates_rows if r["local_date"]}
    current_streak = _compute_streak_days(all_active_dates, today_str)
    today_seconds = daily_map.get(today_str, {}).get("reading_seconds", 0)

    # Source stats & aggregations
    source_stats: list[SourceReadingStat] = []
    cat_agg: dict[str, dict[str, Any]] = {}
    type_agg: dict[str, dict[str, Any]] = {}

    total_reading_seconds = 0
    total_progress_sum = 0.0
    completed_sources = 0
    in_progress_sources = 0
    unread_sources = 0
    total_highlights = 0
    total_qa_turns = 0
    total_sessions_all_time = 0

    for s in src_rows:
        r_sec = int(s["reading_seconds"] or 0)
        r_prog = float(s["reading_progress"] or 0.0)
        hl_cnt = int(s["highlight_count"] or 0)
        qa_cnt = int(s["qa_count"] or 0)
        sess_cnt = int(s["read_sessions_count"] or 0)
        total_reading_seconds += r_sec
        total_progress_sum += r_prog
        total_highlights += hl_cnt
        total_qa_turns += qa_cnt
        total_sessions_all_time += sess_cnt

        if r_prog >= 0.95:
            completed_sources += 1
        elif r_prog >= 0.03 or r_sec >= 15:
            in_progress_sources += 1
        else:
            unread_sources += 1

        est_rem = _estimate_remaining_seconds(
            progress=r_prog,
            reading_seconds=r_sec,
            word_count=int(s["word_count"] or 0),
            page_count=int(s["page_count"] or 0),
        )
        cat_label = (s["category"] or "").strip() or "未分类"
        c_entry = cat_agg.setdefault(
            cat_label,
            {"reading_seconds": 0, "source_count": 0, "progress_sum": 0.0, "highlight_count": 0},
        )
        c_entry["reading_seconds"] += r_sec
        c_entry["source_count"] += 1
        c_entry["progress_sum"] += r_prog
        c_entry["highlight_count"] += hl_cnt

        t_label = _TYPE_LABELS.get(s["type"], s["type"].upper())
        t_entry = type_agg.setdefault(
            t_label,
            {"reading_seconds": 0, "source_count": 0, "progress_sum": 0.0, "highlight_count": 0},
        )
        t_entry["reading_seconds"] += r_sec
        t_entry["source_count"] += 1
        t_entry["progress_sum"] += r_prog
        t_entry["highlight_count"] += hl_cnt

        source_stats.append(
            SourceReadingStat(
                source_id=s["id"],
                notebook_id=s["notebook_id"],
                notebook_title=s["notebook_title"],
                title=s["title"],
                type=s["type"],
                category=s["category"] or "",
                page_count=int(s["page_count"] or 0),
                word_count=int(s["word_count"] or 0),
                last_page=int(s["last_page"] or 1),
                reading_progress=round(r_prog, 4),
                reading_seconds=r_sec,
                read_sessions_count=sess_cnt,
                highlight_count=hl_cnt,
                qa_count=qa_cnt,
                last_read_at=s["last_read_at"],
                estimated_remaining_seconds=est_rem,
            )
        )

    total_sources = len(src_rows)
    avg_progress = round(total_progress_sum / total_sources, 4) if total_sources > 0 else 0.0
    effective_total_seconds = max(total_reading_seconds, window_total_sess_secs)
    total_sessions = max(window_sessions, total_sessions_all_time)
    avg_session_seconds = int(window_total_sess_secs / window_sessions) if window_sessions > 0 else 0
    hours = effective_total_seconds / 3600.0
    highlights_per_hour = round(total_highlights / hours, 2) if hours >= 0.05 else float(total_highlights)
    qa_per_hour = round(total_qa_turns / hours, 2) if hours >= 0.05 else float(total_qa_turns)

    summary = ReadingSummaryMetrics(
        total_reading_seconds=effective_total_seconds,
        today_reading_seconds=today_seconds,
        avg_session_seconds=avg_session_seconds,
        longest_session_seconds=window_longest,
        total_sessions=total_sessions,
        active_days=len(all_active_dates),
        current_streak_days=current_streak,
        total_sources=total_sources,
        completed_sources=completed_sources,
        in_progress_sources=in_progress_sources,
        unread_sources=unread_sources,
        avg_progress=avg_progress,
        total_highlights=total_highlights,
        total_qa_turns=total_qa_turns,
        highlights_per_hour=highlights_per_hour,
        qa_per_hour=qa_per_hour,
    )

    daily_trend = [
        DailyReadingPoint(
            date=d,
            reading_seconds=vals["reading_seconds"],
            sessions_count=vals["sessions_count"],
            highlights_count=vals["highlights_count"],
            qa_count=vals["qa_count"],
        )
        for d, vals in daily_map.items()
    ]
    hourly_distribution = [
        HourlyReadingPoint(
            hour=h,
            reading_seconds=hourly_buckets[h]["reading_seconds"],
            sessions_count=hourly_buckets[h]["sessions_count"],
        )
        for h in range(24)
    ]

    category_breakdown = sorted(
        [
            CategoryReadingStat(
                label=label,
                reading_seconds=v["reading_seconds"],
                source_count=v["source_count"],
                avg_progress=round(v["progress_sum"] / v["source_count"], 4),
                highlight_count=v["highlight_count"],
            )
            for label, v in cat_agg.items()
        ],
        key=lambda x: (-x.reading_seconds, -x.source_count),
    )
    type_breakdown = sorted(
        [
            CategoryReadingStat(
                label=label,
                reading_seconds=v["reading_seconds"],
                source_count=v["source_count"],
                avg_progress=round(v["progress_sum"] / v["source_count"], 4),
                highlight_count=v["highlight_count"],
            )
            for label, v in type_agg.items()
        ],
        key=lambda x: (-x.reading_seconds, -x.source_count),
    )

    # Build Reading Behavior Profile
    period_buckets = {
        "清晨 (05:00-08:59)": sum(hourly_buckets[h]["reading_seconds"] for h in range(5, 9)),
        "上午 (09:00-11:59)": sum(hourly_buckets[h]["reading_seconds"] for h in range(9, 12)),
        "午后 (12:00-17:59)": sum(hourly_buckets[h]["reading_seconds"] for h in range(12, 18)),
        "晚间 (18:00-22:59)": sum(hourly_buckets[h]["reading_seconds"] for h in range(18, 23)),
        "深夜 (23:00-04:59)": sum(hourly_buckets[h]["reading_seconds"] for h in (23, 0, 1, 2, 3, 4)),
    }
    peak_period_label, peak_secs = max(period_buckets.items(), key=lambda kv: kv[1])
    if peak_secs == 0:
        peak_period_label = "尚无足够时段记录"

    if effective_total_seconds == 0 and total_highlights == 0 and total_qa_turns == 0:
        reader_archetype = "蓄势待发的探索者"
        focus_style = "等待首次深度阅读记录"
        engagement_style = "尚未产生划线与提问互动"
    else:
        if avg_session_seconds >= 1200:
            focus_style = "长时深度沉浸型（平均单次 ≥ 20 分钟）"
        elif avg_session_seconds >= 480:
            focus_style = "稳步专注型（平均单次 8~20 分钟）"
        else:
            focus_style = "高频碎片速读型（平均单次 < 8 分钟）"

        interaction_score = highlights_per_hour + qa_per_hour * 1.5
        if interaction_score >= 8:
            engagement_style = "高互动批注与追问型"
        elif interaction_score >= 2.5:
            engagement_style = "思考平衡型（阅读与笔记并重）"
        else:
            engagement_style = "流畅通读型（偏好先读完再整理）"

        if "深夜" in peak_period_label and avg_session_seconds >= 600:
            reader_archetype = "深夜沉浸研究者"
        elif highlights_per_hour + qa_per_hour >= 8:
            reader_archetype = "高密度批注思辨者"
        elif avg_session_seconds >= 1200:
            reader_archetype = "深度精读学者"
        elif completed_sources > 0 and total_sources > 0 and (completed_sources / total_sources) >= 0.5:
            reader_archetype = "高效推进完结派"
        else:
            reader_archetype = "稳步累积学习者"

    completion_rate = round(completed_sources / total_sources, 4) if total_sources > 0 else 0.0

    insights: list[str] = []
    if effective_total_seconds > 0:
        insights.append(
            f"近 {days} 天累计沉浸阅读 {_format_duration_cn(effective_total_seconds)}，最活跃的黄金时段为 {peak_period_label}。"
        )
    else:
        insights.append("开始打开任意 PDF、Word 或文章并阅读十几秒，系统会自动精准记录你的有效学习时长与阅读进度。")

    if current_streak >= 2:
        insights.append(f"已连续学习 {current_streak} 天，保持稳定的学习节律有助于长期记忆巩固。")
    elif summary.active_days > 0:
        insights.append(f"累计已有 {summary.active_days} 天阅读记录，今天继续保持即可累积连续学习天数。")

    if total_sources > 0:
        near_finish = [s for s in source_stats if 0.35 <= s.reading_progress < 0.95]
        if near_finish:
            top_candidate = sorted(near_finish, key=lambda x: -x.reading_progress)[0]
            pct = int(round(top_candidate.reading_progress * 100))
            insights.append(f"《{top_candidate.title}》已完成 {pct}%，距离完结很近，建议优先收尾。")
        elif unread_sources > 0:
            insights.append(f"当前还有 {unread_sources} 份未开始阅读的素材，可从篇幅较短的一份切入。")

    if effective_total_seconds >= 120:
        insights.append(
            f"平均每小时产生 {highlights_per_hour:.1f} 处划线与 {qa_per_hour:.1f} 次 AI 提问（{engagement_style}）。"
        )

    return ReadingAnalyticsResponse(
        scope_notebook_id=notebook_id,
        days=days,
        summary=summary,
        daily_trend=daily_trend,
        hourly_distribution=hourly_distribution,
        category_breakdown=category_breakdown,
        type_breakdown=type_breakdown,
        source_stats=source_stats,
        behavior_profile=ReadingBehaviorProfile(
            reader_archetype=reader_archetype,
            peak_period_label=peak_period_label,
            focus_style=focus_style,
            engagement_style=engagement_style,
            completion_rate=completion_rate,
            insights=insights,
        ),
    )
