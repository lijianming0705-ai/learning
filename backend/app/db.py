from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from app.config import DEFAULT_MODEL, ensure_data_dirs, get_paths

DEFAULT_SETTINGS: dict[str, str] = {
    "default_model": DEFAULT_MODEL,
    "rag_top_k": "5",
    "cross_notebook_rag": "true",
    "embedding_provider": "hybrid-local",
    "ollama_embedding_model": "",
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS notebooks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    color TEXT NOT NULL DEFAULT '#B45309',
    icon TEXT NOT NULL DEFAULT 'book',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    original_filename TEXT,
    source_url TEXT,
    file_path TEXT,
    html_content TEXT NOT NULL DEFAULT '',
    plain_text TEXT NOT NULL DEFAULT '',
    page_count INTEGER NOT NULL DEFAULT 0,
    word_count INTEGER NOT NULL DEFAULT 0,
    is_scanned INTEGER NOT NULL DEFAULT 0,
    last_page INTEGER NOT NULL DEFAULT 1,
    last_scroll_ratio REAL NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL DEFAULT 'ready',
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sources_notebook ON sources(notebook_id, created_at);

CREATE TABLE IF NOT EXISTS source_pages (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL,
    text_content TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_source_pages_source ON source_pages(source_id, page_number);

CREATE TABLE IF NOT EXISTS highlights (
    id TEXT PRIMARY KEY,
    notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    color TEXT NOT NULL DEFAULT 'amber',
    quote_text TEXT NOT NULL DEFAULT '',
    note_text TEXT NOT NULL DEFAULT '',
    anchor_type TEXT NOT NULL DEFAULT 'text',
    page_number INTEGER NOT NULL DEFAULT 1,
    rects_json TEXT NOT NULL DEFAULT '[]',
    start_offset INTEGER,
    end_offset INTEGER,
    prefix_text TEXT NOT NULL DEFAULT '',
    suffix_text TEXT NOT NULL DEFAULT '',
    image_path TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_highlights_source ON highlights(source_id, page_number, created_at);
CREATE INDEX IF NOT EXISTS idx_highlights_notebook ON highlights(notebook_id, created_at);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
    source_id TEXT REFERENCES sources(id) ON DELETE SET NULL,
    highlight_id TEXT UNIQUE REFERENCES highlights(id) ON DELETE CASCADE,
    title TEXT NOT NULL DEFAULT '',
    agy_conversation_id TEXT,
    model TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversations_notebook ON conversations(notebook_id, updated_at);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    model TEXT,
    usage_json TEXT,
    retrieved_memories_json TEXT NOT NULL DEFAULT '[]',
    duration_seconds REAL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, created_at);

CREATE TABLE IF NOT EXISTS source_speech_segments (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    segment_index INTEGER NOT NULL,
    page_number INTEGER NOT NULL DEFAULT 1,
    heading TEXT NOT NULL DEFAULT '',
    original_excerpt TEXT NOT NULL DEFAULT '',
    spoken_text TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'zh-CN',
    audio_path TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_speech_source ON source_speech_segments(source_id, segment_index);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def open_study_connection() -> sqlite3.Connection:
    paths = ensure_data_dirs()
    conn = sqlite3.connect(str(paths.study_db_path), check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    conn = open_study_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_study_db() -> None:
    ensure_data_dirs()
    with get_db() as conn:
        conn.executescript(SCHEMA_SQL)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(sources)").fetchall()}
        if "category" not in cols:
            conn.execute("ALTER TABLE sources ADD COLUMN category TEXT NOT NULL DEFAULT ''")
        if "is_scanned" not in cols:
            conn.execute("ALTER TABLE sources ADD COLUMN is_scanned INTEGER NOT NULL DEFAULT 0")
        if "last_page" not in cols:
            conn.execute("ALTER TABLE sources ADD COLUMN last_page INTEGER NOT NULL DEFAULT 1")
        if "last_scroll_ratio" not in cols:
            conn.execute("ALTER TABLE sources ADD COLUMN last_scroll_ratio REAL NOT NULL DEFAULT 0.0")
        hl_cols = {r["name"] for r in conn.execute("PRAGMA table_info(highlights)").fetchall()}
        if "image_path" not in hl_cols:
            conn.execute("ALTER TABLE highlights ADD COLUMN image_path TEXT")
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )
    # Touch path check for clarity
    _ = get_paths()
