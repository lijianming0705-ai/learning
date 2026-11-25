from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

import numpy as np

from app.config import ensure_data_dirs, get_paths
from app.db import utc_now_iso
from app.rag.embeddings import Embedder, LocalMultilingualEmbedder, OllamaEmbedder
from app.rag.tokenizer import fts_query_expression, fts_tokenize

RAG_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS rag_documents (
    doc_id TEXT PRIMARY KEY,
    doc_type TEXT NOT NULL,
    notebook_id TEXT NOT NULL,
    notebook_title TEXT NOT NULL DEFAULT '',
    source_id TEXT,
    source_title TEXT NOT NULL DEFAULT '',
    highlight_id TEXT,
    conversation_id TEXT,
    page_number INTEGER,
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    embedding BLOB NOT NULL,
    embedding_dim INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rag_notebook ON rag_documents(notebook_id, doc_type);
CREATE INDEX IF NOT EXISTS idx_rag_source ON rag_documents(source_id);
CREATE INDEX IF NOT EXISTS idx_rag_highlight ON rag_documents(highlight_id);
CREATE INDEX IF NOT EXISTS idx_rag_conversation ON rag_documents(conversation_id);

CREATE VIRTUAL TABLE IF NOT EXISTS rag_fts USING fts5(
    doc_id UNINDEXED,
    title_tokens,
    content_tokens
);
"""


@dataclass
class RagHit:
    doc_id: str
    doc_type: str
    score: float
    notebook_id: str
    notebook_title: str
    source_id: str | None
    source_title: str
    highlight_id: str | None
    conversation_id: str | None
    page_number: int | None
    title: str
    snippet: str
    content: str
    created_at: str


class RagStore:
    """Dedicated SQLite-backed Hybrid Vector + FTS5 RAG engine."""

    def __init__(self) -> None:
        self._local_embedder = LocalMultilingualEmbedder(dim=512)
        self._embedder: Embedder = self._local_embedder

    def configure_embedder(self, provider: str = "hybrid-local", ollama_model: str = "") -> None:
        if provider == "ollama" and ollama_model.strip():
            self._embedder = OllamaEmbedder(ollama_model.strip(), fallback=self._local_embedder)
        else:
            self._embedder = self._local_embedder

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        paths = ensure_data_dirs()
        conn = sqlite3.connect(str(paths.rag_db_path), check_same_thread=False, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(RAG_SCHEMA_SQL)

    def upsert_document(
        self,
        *,
        doc_id: str,
        doc_type: str,
        notebook_id: str,
        notebook_title: str = "",
        source_id: str | None = None,
        source_title: str = "",
        highlight_id: str | None = None,
        conversation_id: str | None = None,
        page_number: int | None = None,
        title: str = "",
        content: str,
        metadata: dict[str, Any] | None = None,
        created_at: str | None = None,
    ) -> None:
        text_for_embedding = f"{title}\n{content}".strip()
        vec = self._embedder.embed(text_for_embedding)
        now = utc_now_iso()
        created = created_at or now
        meta_str = json.dumps(metadata or {}, ensure_ascii=False)
        title_tokens = fts_tokenize(title)
        content_tokens = fts_tokenize(content)

        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO rag_documents (
                    doc_id, doc_type, notebook_id, notebook_title, source_id, source_title,
                    highlight_id, conversation_id, page_number, title, content, metadata_json,
                    embedding, embedding_dim, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(doc_id) DO UPDATE SET
                    doc_type=excluded.doc_type,
                    notebook_id=excluded.notebook_id,
                    notebook_title=excluded.notebook_title,
                    source_id=excluded.source_id,
                    source_title=excluded.source_title,
                    highlight_id=excluded.highlight_id,
                    conversation_id=excluded.conversation_id,
                    page_number=excluded.page_number,
                    title=excluded.title,
                    content=excluded.content,
                    metadata_json=excluded.metadata_json,
                    embedding=excluded.embedding,
                    embedding_dim=excluded.embedding_dim,
                    updated_at=excluded.updated_at
                """,
                (
                    doc_id,
                    doc_type,
                    notebook_id,
                    notebook_title,
                    source_id,
                    source_title,
                    highlight_id,
                    conversation_id,
                    page_number,
                    title,
                    content,
                    meta_str,
                    vec.tobytes(),
                    int(vec.shape[0]),
                    created,
                    now,
                ),
            )
            conn.execute("DELETE FROM rag_fts WHERE doc_id = ?", (doc_id,))
            conn.execute(
                "INSERT INTO rag_fts (doc_id, title_tokens, content_tokens) VALUES (?, ?, ?)",
                (doc_id, title_tokens, content_tokens),
            )

    def update_source_title(self, source_id: str, new_title: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE rag_documents SET source_title = ?, updated_at = ? WHERE source_id = ?",
                (new_title, utc_now_iso(), source_id),
            )

    def delete_by_filter(
        self,
        *,
        doc_id: str | None = None,
        notebook_id: str | None = None,
        source_id: str | None = None,
        highlight_id: str | None = None,
        conversation_id: str | None = None,
    ) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if doc_id is not None:
            clauses.append("doc_id = ?")
            params.append(doc_id)
        if notebook_id is not None:
            clauses.append("notebook_id = ?")
            params.append(notebook_id)
        if source_id is not None:
            clauses.append("source_id = ?")
            params.append(source_id)
        if highlight_id is not None:
            clauses.append("highlight_id = ?")
            params.append(highlight_id)
        if conversation_id is not None:
            clauses.append("conversation_id = ?")
            params.append(conversation_id)
        if not clauses:
            return 0

        where_sql = " AND ".join(clauses)
        with self._conn() as conn:
            rows = conn.execute(f"SELECT doc_id FROM rag_documents WHERE {where_sql}", params).fetchall()
            doc_ids = [r["doc_id"] for r in rows]
            if not doc_ids:
                return 0
            placeholders = ",".join("?" for _ in doc_ids)
            conn.execute(f"DELETE FROM rag_fts WHERE doc_id IN ({placeholders})", doc_ids)
            conn.execute(f"DELETE FROM rag_documents WHERE doc_id IN ({placeholders})", doc_ids)
            return len(doc_ids)

    def clear_all(self) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM rag_fts")
            conn.execute("DELETE FROM rag_documents")

    def search(
        self,
        query: str,
        *,
        top_k: int = 6,
        notebook_id: str | None = None,
        source_id: str | None = None,
        doc_types: list[str] | None = None,
        exclude_conversation_id: str | None = None,
        min_score: float = 0.08,
    ) -> list[RagHit]:
        query = query.strip()
        if not query:
            return []

        where_clauses: list[str] = []
        params: list[Any] = []
        if notebook_id:
            where_clauses.append("notebook_id = ?")
            params.append(notebook_id)
        if source_id:
            where_clauses.append("source_id = ?")
            params.append(source_id)
        if doc_types:
            placeholders = ",".join("?" for _ in doc_types)
            where_clauses.append(f"doc_type IN ({placeholders})")
            params.extend(doc_types)
        if exclude_conversation_id:
            where_clauses.append("(conversation_id IS NULL OR conversation_id != ?)")
            params.append(exclude_conversation_id)

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT doc_id, doc_type, notebook_id, notebook_title, source_id, source_title,
                       highlight_id, conversation_id, page_number, title, content,
                       embedding, embedding_dim, created_at
                FROM rag_documents
                {where_sql}
                """,
                params,
            ).fetchall()

            if not rows:
                return []

            query_vec = self._embedder.embed(query)
            q_dim = int(query_vec.shape[0])

            # 1) Dense vector cosine similarity
            vec_scores: dict[str, float] = {}
            row_by_id: dict[str, sqlite3.Row] = {}
            same_dim_ids: list[str] = []
            same_dim_vecs: list[np.ndarray] = []

            for row in rows:
                did = row["doc_id"]
                row_by_id[did] = row
                if row["embedding_dim"] == q_dim:
                    arr = np.frombuffer(row["embedding"], dtype=np.float32)
                    if arr.shape[0] == q_dim:
                        same_dim_ids.append(did)
                        same_dim_vecs.append(arr)

            if same_dim_vecs:
                mat = np.vstack(same_dim_vecs)
                sims = mat @ query_vec
                for did, sim in zip(same_dim_ids, sims.tolist()):
                    vec_scores[did] = float(max(0.0, sim))

            # 2) Sparse FTS5 BM25 ranking
            fts_rank_map: dict[str, int] = {}
            fts_expr = fts_query_expression(query)
            if fts_expr:
                try:
                    fts_rows = conn.execute(
                        """
                        SELECT doc_id, bm25(rag_fts, 2.0, 1.0) AS rank_score
                        FROM rag_fts
                        WHERE rag_fts MATCH ?
                        ORDER BY rank_score ASC
                        LIMIT 100
                        """,
                        (fts_expr,),
                    ).fetchall()
                    rank_idx = 0
                    for frow in fts_rows:
                        did = frow["doc_id"]
                        if did in row_by_id:
                            rank_idx += 1
                            fts_rank_map[did] = rank_idx
                except sqlite3.OperationalError:
                    fts_rank_map = {}

            # 3) Reciprocal Rank Fusion + similarity blend
            sorted_vec = sorted(vec_scores.items(), key=lambda x: x[1], reverse=True)
            vec_rank_map = {did: idx + 1 for idx, (did, sc) in enumerate(sorted_vec) if sc > 0.05}

            candidates: list[tuple[str, float]] = []
            for did in row_by_id:
                v_sim = vec_scores.get(did, 0.0)
                in_fts = did in fts_rank_map
                if v_sim < min_score and not in_fts:
                    continue
                rrf = 0.0
                if did in vec_rank_map:
                    rrf += 1.0 / (40.0 + vec_rank_map[did])
                if in_fts:
                    rrf += 1.15 / (40.0 + fts_rank_map[did])
                # Slight boost for exact qa_memory when similarity is strong
                doc_type_bonus = 0.04 if row_by_id[did]["doc_type"] == "qa_memory" else 0.0
                final_score = round(min(0.999, v_sim * 0.65 + rrf * 12.0 + doc_type_bonus), 4)
                candidates.append((did, final_score))

            candidates.sort(key=lambda item: item[1], reverse=True)
            hits: list[RagHit] = []
            for did, score in candidates[:top_k]:
                row = row_by_id[did]
                hits.append(
                    RagHit(
                        doc_id=did,
                        doc_type=row["doc_type"],
                        score=score,
                        notebook_id=row["notebook_id"],
                        notebook_title=row["notebook_title"],
                        source_id=row["source_id"],
                        source_title=row["source_title"],
                        highlight_id=row["highlight_id"],
                        conversation_id=row["conversation_id"],
                        page_number=row["page_number"],
                        title=row["title"],
                        snippet=_make_snippet(row["content"], query),
                        content=row["content"],
                        created_at=row["created_at"],
                    )
                )
            return hits

    def stats(self) -> dict[str, Any]:
        paths = get_paths()
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM rag_documents").fetchone()[0]
            rows = conn.execute(
                "SELECT doc_type, COUNT(*) AS cnt FROM rag_documents GROUP BY doc_type"
            ).fetchall()
        counts = {r["doc_type"]: int(r["cnt"]) for r in rows}
        size = paths.rag_db_path.stat().st_size if paths.rag_db_path.exists() else 0
        return {
            "total_documents": int(total),
            "counts_by_type": counts,
            "embedding_provider": self._embedder.name,
            "embedding_dim": self._embedder.dim,
            "db_size_bytes": size,
        }


def _make_snippet(content: str, query: str, max_len: int = 240) -> str:
    compact = " ".join(content.split())
    if len(compact) <= max_len:
        return compact
    lowered = compact.lower()
    pos = -1
    for tok in query.lower().split():
        idx = lowered.find(tok)
        if idx != -1:
            pos = idx
            break
    if pos == -1 and len(query) >= 2:
        for i in range(len(query) - 1):
            idx = lowered.find(query[i : i + 2].lower())
            if idx != -1:
                pos = idx
                break
    if pos <= 40:
        return compact[:max_len] + "…"
    start = max(0, pos - 60)
    end = min(len(compact), start + max_len)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(compact) else ""
    return prefix + compact[start:end] + suffix


rag_store = RagStore()
