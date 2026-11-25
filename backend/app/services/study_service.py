from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from app.config import DEFAULT_MODEL, ensure_data_dirs
from app.db import DEFAULT_SETTINGS, get_db, utc_now_iso
from app.rag.store import RagHit, rag_store
from app.schemas import (
    ConversationDetail,
    ConversationOut,
    HighlightCreate,
    HighlightOut,
    HighlightRect,
    HighlightUpdate,
    MessageOut,
    NotebookCreate,
    NotebookOut,
    NotebookUpdate,
    RagMemoryRef,
    SettingsOut,
    SettingsUpdate,
    SourceDetail,
    SourcePageOut,
    SourceSummary,
    SourceUpdate,
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
                    WHERE c.notebook_id = n.id AND m.role = 'user') AS qa_count
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
                    WHERE c.notebook_id = n.id AND m.role = 'user') AS qa_count
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
    uploads_nb_dir = ensure_data_dirs().uploads_dir / notebook_id
    if uploads_nb_dir.exists() and not any(uploads_nb_dir.iterdir()):
        uploads_nb_dir.rmdir()
    rag_store.delete_by_filter(notebook_id=notebook_id)
    return True


def _row_to_notebook(row: sqlite3.Row) -> NotebookOut:
    return NotebookOut(
        id=row["id"],
        title=row["title"],
        description=row["description"],
        color=row["color"],
        icon=row["icon"],
        source_count=int(row["source_count"]),
        highlight_count=int(row["highlight_count"]),
        qa_count=int(row["qa_count"]),
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
    now = utc_now_iso()
    with get_db() as conn:
        conn.execute(
            "UPDATE sources SET title = ?, category = ?, updated_at = ? WHERE id = ?",
            (new_title, new_category, now, source_id),
        )
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
        conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        conn.execute("UPDATE notebooks SET updated_at = ? WHERE id = ?", (utc_now_iso(), row["notebook_id"]))

    if row["file_path"]:
        Path(row["file_path"]).unlink(missing_ok=True)
    for cr in crop_rows:
        Path(cr["image_path"]).unlink(missing_ok=True)
    rag_store.delete_by_filter(source_id=source_id)
    return True


def _row_to_source_summary(row: sqlite3.Row) -> SourceSummary:
    return SourceSummary(
        id=row["id"],
        notebook_id=row["notebook_id"],
        type=row["type"],
        title=row["title"],
        category=row["category"] if "category" in row.keys() and row["category"] else "",
        original_filename=row["original_filename"],
        source_url=row["source_url"],
        page_count=int(row["page_count"]),
        word_count=int(row["word_count"]),
        highlight_count=int(row["highlight_count"]),
        is_scanned=bool(row["is_scanned"]) if "is_scanned" in row.keys() else False,
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
