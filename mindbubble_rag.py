#!/usr/bin/env python3
"""Zero-dependency SQLite FTS5 + bilingual Hybrid RAG engine for MindBubble."""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import html as _html
import io
import json
import math
import re
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

DEFAULT_GALAXY_ID = "gal_default"

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")
_EN_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_+#.-]{1,47}")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_SAFE_FTS_TOKEN_RE = re.compile(r"^[a-z0-9\u4e00-\u9fff_]{1,48}$")
_MENTION_RE = re.compile(r"@([\w\u4e00-\u9fff.-]{1,32})")


def strip_html_to_text(raw_html: Any) -> str:
    """Convert rich HTML into clean plain text preserving block line breaks."""
    if not isinstance(raw_html, str) or not raw_html.strip():
        return ""
    text = re.sub(r"(?i)<br\s*/?>", "\n", raw_html)
    text = re.sub(r"(?i)</(?:p|div|h[1-6]|li|tr|blockquote|details|summary|figure|figcaption)>", "\n", text)
    text = re.sub(r"(?i)</t[dh]>", " | ", text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = _html.unescape(text)
    lines = [_WS_RE.sub(" ", ln).strip() for ln in text.splitlines()]
    joined = "\n".join(ln for ln in lines if ln)
    return _MULTI_NL_RE.sub("\n\n", joined).strip()


def tokenize_bilingual(text: Any, max_tokens: int = 360) -> str:
    """Produce space-separated normalized English tokens + overlapping CJK 1/2/3-grams."""
    if not isinstance(text, str) or not text.strip():
        return ""
    low = text.lower()
    tokens: list[str] = []
    seen: set[str] = set()

    def _add(tok: str) -> None:
        if len(tokens) >= max_tokens:
            return
        if tok and tok not in seen:
            seen.add(tok)
            tokens.append(tok)

    for m in _EN_TOKEN_RE.finditer(low):
        raw = m.group(0).strip("._-")
        if len(raw) >= 2:
            _add(raw)
            if any(ch in raw for ch in ("_", "-", ".", "/")):
                for sub in re.split(r"[._/-]+", raw):
                    if len(sub) >= 2:
                        _add(sub)

    for m in _CJK_RUN_RE.finditer(low):
        run = m.group(0)
        n = len(run)
        if n == 1:
            _add(run)
            continue
        for i in range(n - 1):
            _add(run[i : i + 2])
        if n >= 3:
            for i in range(n - 2):
                _add(run[i : i + 3])
        if n <= 8:
            _add(run)
        # Single CJK characters for short/rare queries if budget allows
        if n <= 12:
            for ch in run:
                _add(ch)

    return " ".join(tokens)


def chunk_text(
    text: str,
    max_chars: int = 650,
    overlap: int = 90,
    max_chunks: int = 20,
) -> list[str]:
    """Split text along Markdown headings/paragraphs/sentences with bounded overlap."""
    clean = _MULTI_NL_RE.sub("\n\n", str(text or "").strip())
    if not clean:
        return []
    if len(clean) <= max_chars:
        return [clean]

    # Prefer splitting on blank lines or headings first
    blocks = re.split(r"(?:\n\s*\n|(?=\n#{1,3}\s))", clean)
    pieces: list[str] = []
    for blk in blocks:
        b = blk.strip()
        if not b:
            continue
        if len(b) <= max_chars:
            pieces.append(b)
            continue
        # Further split oversized blocks by lines / Chinese & English sentence punctuation
        sub_parts = re.split(r"(?<=[。！？.!?\n])", b)
        cur = ""
        for sp in sub_parts:
            if not sp:
                continue
            if len(cur) + len(sp) <= max_chars:
                cur += sp
            else:
                if cur.strip():
                    pieces.append(cur.strip())
                if len(sp) > max_chars:
                    step = max(120, max_chars - overlap)
                    for i in range(0, len(sp), step):
                        seg = sp[i : i + max_chars].strip()
                        if seg:
                            pieces.append(seg)
                    cur = ""
                else:
                    cur = sp
        if cur.strip():
            pieces.append(cur.strip())

    chunks: list[str] = []
    cur = ""
    for piece in pieces:
        if not cur:
            cur = piece
        elif len(cur) + 2 + len(piece) <= max_chars:
            cur = f"{cur}\n\n{piece}"
        else:
            chunks.append(cur)
            if len(chunks) >= max_chunks:
                break
            tail = cur[-overlap:] if overlap > 0 and len(cur) > overlap else ""
            cur = f"{tail}\n{piece}".strip() if tail else piece
            if len(cur) > max_chars:
                cur = cur[:max_chars]
    if cur and len(chunks) < max_chunks:
        chunks.append(cur)
    return chunks[:max_chunks]


def _format_ts(ts_ms: Any) -> str:
    if not isinstance(ts_ms, (int, float)) or isinstance(ts_ms, bool) or ts_ms <= 0:
        return ""
    try:
        return _dt.datetime.fromtimestamp(float(ts_ms) / 1000.0).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def _decode_text_bytes(raw_bytes: bytes) -> str:
    for enc in ("utf-8", "utf-8-sig", "gb18030", "latin-1"):
        try:
            return raw_bytes.decode(enc)
        except Exception:
            continue
    return ""


def extract_attachment_text(name: str, mime_type: str, raw_bytes: bytes | None) -> str:
    """Pure-stdlib attachment text extractor supporting text, code, office XML, PDF strings, and fallback meta."""
    clean_name = str(name or "attachment").strip()
    clean_mime = str(mime_type or "").lower().split(";")[0].strip()
    ext = Path(clean_name).suffix.lower()
    size = len(raw_bytes) if raw_bytes else 0

    if not raw_bytes:
        return f"[附件: {clean_name} | {clean_mime or 'unknown'} | {size} bytes]"

    # Binary images
    if clean_mime.startswith("image/") or ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico"}:
        if ext == ".svg" or clean_mime == "image/svg+xml":
            svg_txt = strip_html_to_text(_decode_text_bytes(raw_bytes[:200_000]))
            if svg_txt:
                return f"[SVG 图像: {clean_name}]\n{svg_txt[:6000]}"
        return f"[图片附件: {clean_name} | 类型 {clean_mime or ext} | {size} bytes]"

    text_exts = {
        ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml",
        ".toml", ".ini", ".cfg", ".conf", ".log", ".sql", ".py", ".js", ".ts", ".tsx",
        ".jsx", ".html", ".htm", ".xml", ".css", ".scss", ".sh", ".zsh", ".env", ".rst",
    }
    if clean_mime.startswith("text/") or ext in text_exts or clean_mime in {
        "application/json", "application/xml", "application/javascript", "text/csv",
    }:
        txt = _decode_text_bytes(raw_bytes[:600_000])
        if ext in {".html", ".htm", ".xml"} or clean_mime in {"text/html", "application/xml", "text/xml"}:
            txt = strip_html_to_text(txt)
        txt = _MULTI_NL_RE.sub("\n\n", txt).strip()
        if txt:
            return f"[文档附件: {clean_name}]\n{txt[:24_000]}"

    # Office OpenXML (.docx / .pptx / .xlsx) via stdlib zipfile + ElementTree
    if ext in {".docx", ".pptx", ".xlsx"}:
        try:
            with zipfile.ZipFile(io.BytesIO(raw_bytes[:8_000_000])) as zf:
                parts: list[str] = []
                if ext == ".docx" and "word/document.xml" in zf.namelist():
                    root = ET.fromstring(zf.read("word/document.xml"))
                    for elem in root.iter():
                        tag = elem.tag.split("}")[-1]
                        if tag in {"t", "instrText"} and elem.text:
                            parts.append(elem.text)
                        elif tag in {"p", "tr", "br"}:
                            parts.append("\n")
                elif ext == ".pptx":
                    slides = sorted(
                        n for n in zf.namelist()
                        if re.match(r"^ppt/slides/slide\d+\.xml$", n)
                    )
                    for sname in slides[:60]:
                        root = ET.fromstring(zf.read(sname))
                        s_texts = [el.text.strip() for el in root.iter() if el.tag.split("}")[-1] == "t" and el.text and el.text.strip()]
                        if s_texts:
                            parts.append(f"[{Path(sname).stem}] " + " ".join(s_texts))
                elif ext == ".xlsx":
                    shared: list[str] = []
                    if "xl/sharedStrings.xml" in zf.namelist():
                        sroot = ET.fromstring(zf.read("xl/sharedStrings.xml"))
                        for si in sroot:
                            txt = "".join(el.text or "" for el in si.iter() if el.tag.split("}")[-1] == "t")
                            shared.append(txt)
                    sheets = sorted(
                        n for n in zf.namelist()
                        if re.match(r"^xl/worksheets/sheet\d+\.xml$", n)
                    )
                    for sh in sheets[:20]:
                        root = ET.fromstring(zf.read(sh))
                        vals: list[str] = []
                        for c in root.iter():
                            if c.tag.split("}")[-1] != "c":
                                continue
                            ctype = c.attrib.get("t", "")
                            v_el = next((ch for ch in c if ch.tag.split("}")[-1] == "v"), None)
                            if v_el is not None and v_el.text is not None:
                                val = v_el.text
                                if ctype == "s" and val.isdigit() and int(val) < len(shared):
                                    vals.append(shared[int(val)])
                                else:
                                    vals.append(val)
                        if vals:
                            parts.append(f"[{Path(sh).stem}] " + " | ".join(vals[:400]))
                joined = "".join(parts) if ext == ".docx" else "\n".join(parts)
                joined = _MULTI_NL_RE.sub("\n\n", joined).strip()
                if joined:
                    return f"[Office 附件: {clean_name}]\n{joined[:24_000]}"
        except Exception:
            pass

    # PDF fallback: bounded printable UTF-8/ASCII literal scan
    if ext == ".pdf" or clean_mime == "application/pdf":
        try:
            sample = raw_bytes[:2_500_000].decode("latin-1", errors="ignore")
            paren_runs = re.findall(r"\(([A-Za-z0-9\s,.;:!?/_-]{4,180})\)", sample)
            filtered = [r.strip() for r in paren_runs if not re.match(r"^[0-9\s.-]+$", r.strip())]
            if filtered:
                return f"[PDF 附件: {clean_name}]\n" + " ".join(filtered[:300])[:8000]
        except Exception:
            pass

    return f"[附件: {clean_name} | 类型 {clean_mime or ext or 'binary'} | {size} bytes]"


class MindBubbleRagEngine:
    """Automatic SQLite FTS5 + bilingual Hybrid RAG engine across all MindBubble data."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._dirty: bool = True
        self._indexing: bool = False
        self._sync_timer: threading.Timer | None = None
        self._timer_lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.execute("PRAGMA busy_timeout=5000;")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rag_docs (
                    doc_key TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    task_id TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL,
                    galaxy_id TEXT NOT NULL DEFAULT '',
                    galaxy_name TEXT NOT NULL DEFAULT '',
                    cluster_id TEXT NOT NULL DEFAULT '',
                    content_hash TEXT NOT NULL,
                    updated_at INTEGER NOT NULL DEFAULT 0,
                    meta_json TEXT NOT NULL DEFAULT '{}'
                );
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rag_chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doc_key TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    task_id TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL,
                    galaxy_id TEXT NOT NULL DEFAULT '',
                    galaxy_name TEXT NOT NULL DEFAULT '',
                    cluster_id TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    search_tokens TEXT NOT NULL,
                    meta_json TEXT NOT NULL DEFAULT '{}'
                );
                """
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_rag_chunks_doc ON rag_chunks(doc_key);")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_rag_chunks_source ON rag_chunks(source_type, source_id);")
            self._conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS rag_chunks_fts USING fts5(
                    chunk_id UNINDEXED,
                    title_tokens,
                    content_tokens,
                    tokenize='unicode61'
                );
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rag_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            self._conn.commit()

    def mark_dirty(self, reason: str = "") -> None:
        self._dirty = True

    def schedule_sync(self, store: Any, delay: float = 0.35, reason: str = "") -> None:
        self.mark_dirty(reason)
        with self._timer_lock:
            if self._sync_timer is not None:
                try:
                    self._sync_timer.cancel()
                except Exception:
                    pass
                self._sync_timer = None

            def _run() -> None:
                with self._timer_lock:
                    self._sync_timer = None
                try:
                    self.sync_all(store, force=False)
                except Exception:
                    pass

            timer = threading.Timer(max(0.05, float(delay)), _run)
            timer.daemon = True
            self._sync_timer = timer
            timer.start()

    def ensure_fresh(self, store: Any) -> None:
        if self._dirty:
            self.sync_all(store, force=False)
            return
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM rag_docs").fetchone()
            if not row or row[0] == 0:
                self._dirty = True
        if self._dirty:
            self.sync_all(store, force=False)

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            doc_cnt = int(self._conn.execute("SELECT COUNT(*) FROM rag_docs").fetchone()[0] or 0)
            chunk_cnt = int(self._conn.execute("SELECT COUNT(*) FROM rag_chunks").fetchone()[0] or 0)
            by_type_rows = self._conn.execute(
                "SELECT source_type, COUNT(*) FROM rag_docs GROUP BY source_type"
            ).fetchall()
            meta_rows = self._conn.execute("SELECT key, value FROM rag_meta").fetchall()
        meta_map = {str(k): str(v) for k, v in meta_rows}
        return {
            "docCount": doc_cnt,
            "chunkCount": chunk_cnt,
            "byType": {str(st): int(cnt) for st, cnt in by_type_rows},
            "lastIndexedAt": int(meta_map.get("last_indexed_at") or 0),
            "lastRevision": int(meta_map.get("last_revision") or 0),
            "dirty": bool(self._dirty),
            "indexing": bool(self._indexing),
        }

    def _read_attachment_rows(self) -> list[tuple[Any, ...]]:
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='attachments'"
            ).fetchone()
            if not exists:
                return []
            return self._conn.execute(
                "SELECT id, task_id, name, mime_type, size, rel_path, created_at FROM attachments"
            ).fetchall()

    def _build_all_docs(
        self,
        store: Any,
        existing_hashes: dict[str, str],
    ) -> tuple[list[dict[str, Any]], int]:
        bundle = store.get_state()
        state = bundle.get("state") or {}
        revision = int(bundle.get("revision") or 0)
        tasks = [t for t in (state.get("tasks") or []) if isinstance(t, dict) and t.get("id")]
        galaxies = [g for g in (state.get("galaxies") or []) if isinstance(g, dict) and g.get("id")]
        clusters = [c for c in (state.get("clusters") or []) if isinstance(c, dict) and c.get("id")]
        manual_links = [l for l in (state.get("manualLinks") or []) if isinstance(l, dict)]
        ai_links = [l for l in (state.get("aiLinks") or []) if isinstance(l, dict)]

        galaxy_map = {g["id"]: g for g in galaxies}
        cluster_map = {c["id"]: c for c in clusters}
        task_map = {t["id"]: t for t in tasks}

        # Adjacency map for link descriptions
        links_by_task: dict[str, list[str]] = {}
        for lk in manual_links:
            s = str(lk.get("source") or "")
            t = str(lk.get("target") or "")
            lbl = str(lk.get("label") or "").strip()
            if s in task_map and t in task_map and s != t:
                links_by_task.setdefault(s, []).append(
                    f"{task_map[t].get('title', t)}" + (f" ({lbl})" if lbl else "")
                )
                links_by_task.setdefault(t, []).append(
                    f"{task_map[s].get('title', s)}" + (f" ({lbl})" if lbl else "")
                )

        docs: list[dict[str, Any]] = []
        now_ms = int(time.time() * 1000)

        # 1-4: Task docs (task_meta, task_notebook, task_ai, task_activity) + inline attachments
        seen_att_ids: set[str] = set()
        for task in tasks:
            tid = str(task["id"])
            title = str(task.get("title") or "未命名气泡").strip()
            gid = str(task.get("galaxyId") or DEFAULT_GALAXY_ID)
            gal = galaxy_map.get(gid) or {"id": gid, "name": "默认星系"}
            gname = str(gal.get("name") or "默认星系")
            cid = str(task.get("clusterId") or "")
            cl = cluster_map.get(cid)
            cname = str(cl.get("name") or "") if cl else ""

            tags = [str(x).strip() for x in (task.get("tags") or []) if str(x).strip()]
            prio = str(task.get("priority") or "normal")
            status_str = f"已完成 ({_format_ts(task.get('completedAt'))})" if task.get("completed") else "进行中"
            top3 = f"Top {int(task['top3Slot']) + 1}" if task.get("top3Slot") in (0, 1, 2) and not task.get("completed") else ""
            focus_date = str(task.get("focusDate") or "")
            due_str = _format_ts(task.get("dueAt"))
            notes = str(task.get("notes") or "").strip()
            cl_lines = []
            for item in (task.get("checklist") or []):
                if isinstance(item, dict) and str(item.get("text") or "").strip():
                    mark = "[x]" if item.get("done") else "[ ]"
                    cl_lines.append(f"- {mark} {str(item['text']).strip()}")
            peer_strs = links_by_task.get(tid, [])

            meta_lines = [
                f"气泡标题: {title}",
                f"所属星系: {gname}" + (f" | 聚类: {cname}" if cname else ""),
                f"状态: {status_str} | 优先级: {prio}" + (f" | {top3}" if top3 else "") + (f" | 今日重点: {focus_date}" if focus_date else ""),
            ]
            if due_str:
                meta_lines.append(f"截止时间: {due_str}")
            if tags:
                meta_lines.append("标签: " + ", ".join(f"#{tg}" for tg in tags))
            if notes:
                meta_lines.append(f"备注:\n{notes}")
            if cl_lines:
                meta_lines.append("子任务清单:\n" + "\n".join(cl_lines))
            if peer_strs:
                meta_lines.append("关联气泡: " + "；".join(peer_strs[:20]))
            meta_text = "\n".join(meta_lines).strip()
            docs.append({
                "doc_key": f"task_meta:{tid}",
                "source_type": "task_meta",
                "source_id": tid,
                "task_id": tid,
                "title": title,
                "galaxy_id": gid,
                "galaxy_name": gname,
                "cluster_id": cid,
                "updated_at": int(task.get("completedAt") or task.get("createdAt") or now_ms),
                "text": meta_text,
                "meta": {"priority": prio, "completed": bool(task.get("completed")), "tags": tags},
            })

            # task_notebook
            nb_md = str(task.get("notebook") or "").strip()
            nb_html_txt = strip_html_to_text(task.get("notebookHtml") or "")
            if nb_md and nb_html_txt:
                # Merge if HTML has extra blocks/text not captured in Markdown
                nb_text = nb_md if len(nb_md) >= len(nb_html_txt) * 0.85 else f"{nb_md}\n\n{nb_html_txt}"
            else:
                nb_text = nb_md or nb_html_txt
            if nb_text.strip():
                docs.append({
                    "doc_key": f"task_nb:{tid}",
                    "source_type": "task_notebook",
                    "source_id": tid,
                    "task_id": tid,
                    "title": f"{title} · 笔记本",
                    "galaxy_id": gid,
                    "galaxy_name": gname,
                    "cluster_id": cid,
                    "updated_at": now_ms,
                    "text": nb_text.strip(),
                    "meta": {"parentTitle": title},
                })

            # task_ai
            ai_hist = [
                m for m in (task.get("aiHistory") or [])
                if isinstance(m, dict) and not m.get("error") and m.get("engine") != "agy-error" and str(m.get("content") or "").strip()
            ]
            if ai_hist:
                ai_lines = []
                for m in ai_hist[-20:]:
                    role_lbl = "用户" if m.get("role") == "user" else "AI助手"
                    ai_lines.append(f"[{role_lbl}] {str(m['content']).strip()[:2000]}")
                docs.append({
                    "doc_key": f"task_ai:{tid}",
                    "source_type": "task_ai",
                    "source_id": tid,
                    "task_id": tid,
                    "title": f"{title} · AI对话记录",
                    "galaxy_id": gid,
                    "galaxy_name": gname,
                    "cluster_id": cid,
                    "updated_at": now_ms,
                    "text": "\n\n".join(ai_lines),
                    "meta": {"parentTitle": title},
                })

            # task_activity
            act_log = [
                a for a in (task.get("activityLog") or [])
                if isinstance(a, dict) and str(a.get("text") or "").strip()
            ]
            if act_log:
                act_lines = [
                    f"{_format_ts(a.get('createdAt'))} {str(a['text']).strip()}"
                    for a in act_log[-25:]
                ]
                docs.append({
                    "doc_key": f"task_act:{tid}",
                    "source_type": "task_activity",
                    "source_id": tid,
                    "task_id": tid,
                    "title": f"{title} · 操作留痕",
                    "galaxy_id": gid,
                    "galaxy_name": gname,
                    "cluster_id": cid,
                    "updated_at": now_ms,
                    "text": "\n".join(act_lines),
                    "meta": {"parentTitle": title},
                })

            # Inline attachments on task
            for att in (task.get("attachments") or []):
                if not isinstance(att, dict) or not att.get("id"):
                    continue
                aid = str(att["id"])
                if aid in seen_att_ids or att.get("storage") != "inline":
                    continue
                seen_att_ids.add(aid)
                aname = str(att.get("name") or "attachment")
                amime = str(att.get("mimeType") or "application/octet-stream")
                data_url = str(att.get("dataUrl") or "")
                raw_bytes: bytes | None = None
                if data_url.startswith("data:") and "," in data_url:
                    try:
                        raw_bytes = base64.b64decode(data_url.split(",", 1)[1], validate=False)
                    except Exception:
                        raw_bytes = None
                sig_hash = hashlib.sha1(f"{tid}|{aname}|{amime}|{att.get('size', 0)}|{len(data_url)}".encode()).hexdigest()
                doc_key = f"att:{aid}"
                if existing_hashes.get(doc_key) == sig_hash:
                    docs.append({
                        "doc_key": doc_key,
                        "source_type": "attachment",
                        "source_id": aid,
                        "task_id": tid,
                        "title": f"附件: {aname}",
                        "galaxy_id": gid,
                        "galaxy_name": gname,
                        "cluster_id": cid,
                        "updated_at": int(att.get("createdAt") or now_ms),
                        "precomputed_hash": sig_hash,
                        "text": "",
                        "meta": {"name": aname, "mimeType": amime, "parentTitle": title},
                    })
                else:
                    extracted = extract_attachment_text(aname, amime, raw_bytes)
                    docs.append({
                        "doc_key": doc_key,
                        "source_type": "attachment",
                        "source_id": aid,
                        "task_id": tid,
                        "title": f"附件: {aname}",
                        "galaxy_id": gid,
                        "galaxy_name": gname,
                        "cluster_id": cid,
                        "updated_at": int(att.get("createdAt") or now_ms),
                        "precomputed_hash": sig_hash,
                        "text": f"所属气泡: {title} ({gname})\n{extracted}",
                        "meta": {"name": aname, "mimeType": amime, "parentTitle": title},
                    })

        # 5: Galaxy & cluster overview docs
        for g in galaxies:
            gid = str(g["id"])
            gname = str(g.get("name") or "默认星系")
            gtasks = [t for t in tasks if (t.get("galaxyId") or DEFAULT_GALAXY_ID) == gid]
            active_titles = [str(t.get("title") or "") for t in gtasks if not t.get("completed")]
            g_text = (
                f"星系名称: {gname}\n"
                f"说明: {str(g.get('description') or '')}\n"
                f"气泡统计: 进行中 {len(active_titles)} 个 / 总计 {len(gtasks)} 个\n"
                + ("进行中气泡列表: " + "、".join(active_titles[:40]) if active_titles else "")
            ).strip()
            docs.append({
                "doc_key": f"galaxy:{gid}",
                "source_type": "galaxy_cluster",
                "source_id": gid,
                "task_id": "",
                "title": f"星系: {gname}",
                "galaxy_id": gid,
                "galaxy_name": gname,
                "cluster_id": "",
                "updated_at": now_ms,
                "text": g_text,
                "meta": {"kind": "galaxy"},
            })

        for cl in clusters:
            cid = str(cl["id"])
            cname = str(cl.get("name") or "未命名聚类")
            gid = str(cl.get("galaxyId") or DEFAULT_GALAXY_ID)
            gname = str((galaxy_map.get(gid) or {}).get("name") or "默认星系")
            ctasks = [t for t in tasks if t.get("clusterId") == cid]
            ctitles = [str(t.get("title") or "") for t in ctasks]
            c_text = (
                f"星群主题: {cname} ({gname})\n"
                f"摘要: {str(cl.get('summary') or '')}\n"
                + ("包含气泡: " + "、".join(ctitles[:40]) if ctitles else "")
            ).strip()
            docs.append({
                "doc_key": f"cluster:{cid}",
                "source_type": "galaxy_cluster",
                "source_id": cid,
                "task_id": "",
                "title": f"星群: {cname}",
                "galaxy_id": gid,
                "galaxy_name": gname,
                "cluster_id": cid,
                "updated_at": now_ms,
                "text": c_text,
                "meta": {"kind": "cluster"},
            })

        # 6: Disk-backed attachments table
        att_dir = self.db_path.parent / "attachments"
        for aid, tid, aname, amime, asize, rel_path, created_at in self._read_attachment_rows():
            aid_s = str(aid)
            if aid_s in seen_att_ids:
                continue
            seen_att_ids.add(aid_s)
            tid_s = str(tid or "")
            parent = task_map.get(tid_s)
            gid = str((parent or {}).get("galaxyId") or DEFAULT_GALAXY_ID)
            gname = str((galaxy_map.get(gid) or {}).get("name") or "默认星系")
            cid = str((parent or {}).get("clusterId") or "")
            parent_title = str((parent or {}).get("title") or "工作区附件")
            sig_hash = hashlib.sha1(f"{tid_s}|{aname}|{amime}|{asize}|{rel_path}|{created_at}".encode()).hexdigest()
            doc_key = f"att:{aid_s}"
            if existing_hashes.get(doc_key) == sig_hash:
                docs.append({
                    "doc_key": doc_key,
                    "source_type": "attachment",
                    "source_id": aid_s,
                    "task_id": tid_s,
                    "title": f"附件: {aname}",
                    "galaxy_id": gid,
                    "galaxy_name": gname,
                    "cluster_id": cid,
                    "updated_at": int(created_at or now_ms),
                    "precomputed_hash": sig_hash,
                    "text": "",
                    "meta": {"name": aname, "mimeType": amime, "parentTitle": parent_title},
                })
            else:
                fpath = att_dir / str(rel_path)
                raw_bytes = None
                try:
                    if fpath.exists() and fpath.stat().st_size <= 15 * 1024 * 1024:
                        raw_bytes = fpath.read_bytes()
                except Exception:
                    raw_bytes = None
                extracted = extract_attachment_text(str(aname), str(amime), raw_bytes)
                docs.append({
                    "doc_key": doc_key,
                    "source_type": "attachment",
                    "source_id": aid_s,
                    "task_id": tid_s,
                    "title": f"附件: {aname}",
                    "galaxy_id": gid,
                    "galaxy_name": gname,
                    "cluster_id": cid,
                    "updated_at": int(created_at or now_ms),
                    "precomputed_hash": sig_hash,
                    "text": f"所属气泡: {parent_title} ({gname})\n{extracted}",
                    "meta": {"name": aname, "mimeType": amime, "parentTitle": parent_title},
                })

        # 7: Contacts table
        try:
            contacts_res = store.list_contacts()
            contacts_list = contacts_res.get("contacts") or []
        except Exception:
            contacts_list = []
        for c in contacts_list:
            if not isinstance(c, dict) or not c.get("id"):
                continue
            cid = str(c["id"])
            cname = str(c.get("name") or "").strip()
            if not cname:
                continue
            c_parts = [f"联系人: {cname}"]
            for field, label in (
                ("enName", "英文名"),
                ("nickname", "昵称"),
                ("department", "部门"),
                ("title", "职位"),
                ("email", "邮箱"),
                ("mobile", "手机"),
                ("source", "来源"),
                ("notes", "备注"),
            ):
                val = str(c.get(field) or "").strip()
                if val:
                    c_parts.append(f"{label}: {val}")
            docs.append({
                "doc_key": f"contact:{cid}",
                "source_type": "contact",
                "source_id": cid,
                "task_id": "",
                "title": f"联系人: {cname}" + (f" ({c.get('department')})" if c.get("department") else ""),
                "galaxy_id": "",
                "galaxy_name": "通讯录",
                "cluster_id": "",
                "updated_at": int(c.get("updatedAt") or now_ms),
                "text": "\n".join(c_parts),
                "meta": {"name": cname, "department": str(c.get("department") or ""), "title": str(c.get("title") or "")},
            })

        return docs, revision

    def sync_all(self, store: Any, force: bool = False) -> dict[str, Any]:
        with self._lock:
            self._indexing = True
        try:
            with self._lock:
                existing_rows = self._conn.execute("SELECT doc_key, content_hash FROM rag_docs").fetchall()
                existing_hashes = {str(k): str(h) for k, h in existing_rows}

            docs, revision = self._build_all_docs(store, {} if force else existing_hashes)

            prepared_upserts: list[dict[str, Any]] = []
            current_keys: set[str] = set()
            for d in docs:
                key = d["doc_key"]
                current_keys.add(key)
                chash = d.get("precomputed_hash") or hashlib.sha1(
                    f"{d['title']}\n{d['galaxy_name']}\n{d['cluster_id']}\n{d['text']}".encode("utf-8", errors="ignore")
                ).hexdigest()
                if not force and existing_hashes.get(key) == chash:
                    continue
                d["content_hash"] = chash
                d["chunks"] = chunk_text(d["text"])
                d["title_tokens"] = tokenize_bilingual(d["title"], max_tokens=80)
                d["chunk_tokens"] = [tokenize_bilingual(f"{d['title']} {ch}", max_tokens=360) for ch in d["chunks"]]
                prepared_upserts.append(d)

            deleted_keys = [k for k in existing_hashes if k not in current_keys]
            now_ms = int(time.time() * 1000)

            with self._lock:
                for dk in deleted_keys:
                    chunk_ids = [r[0] for r in self._conn.execute("SELECT id FROM rag_chunks WHERE doc_key = ?", (dk,)).fetchall()]
                    if chunk_ids:
                        placeholders = ",".join("?" for _ in chunk_ids)
                        self._conn.execute(f"DELETE FROM rag_chunks_fts WHERE chunk_id IN ({placeholders})", chunk_ids)
                    self._conn.execute("DELETE FROM rag_chunks WHERE doc_key = ?", (dk,))
                    self._conn.execute("DELETE FROM rag_docs WHERE doc_key = ?", (dk,))

                for d in prepared_upserts:
                    dk = d["doc_key"]
                    old_ids = [r[0] for r in self._conn.execute("SELECT id FROM rag_chunks WHERE doc_key = ?", (dk,)).fetchall()]
                    if old_ids:
                        placeholders = ",".join("?" for _ in old_ids)
                        self._conn.execute(f"DELETE FROM rag_chunks_fts WHERE chunk_id IN ({placeholders})", old_ids)
                    self._conn.execute("DELETE FROM rag_chunks WHERE doc_key = ?", (dk,))
                    self._conn.execute(
                        """
                        INSERT OR REPLACE INTO rag_docs
                        (doc_key, source_type, source_id, task_id, title, galaxy_id, galaxy_name, cluster_id, content_hash, updated_at, meta_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            dk,
                            d["source_type"],
                            d["source_id"],
                            d["task_id"],
                            d["title"],
                            d["galaxy_id"],
                            d["galaxy_name"],
                            d["cluster_id"],
                            d["content_hash"],
                            d["updated_at"],
                            json.dumps(d.get("meta") or {}, ensure_ascii=False),
                        ),
                    )
                    meta_str = json.dumps(d.get("meta") or {}, ensure_ascii=False)
                    for idx, ch_text in enumerate(d["chunks"]):
                        ch_tokens = d["chunk_tokens"][idx]
                        cur = self._conn.execute(
                            """
                            INSERT INTO rag_chunks
                            (doc_key, chunk_index, source_type, source_id, task_id, title, galaxy_id, galaxy_name, cluster_id, content, search_tokens, meta_json)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                dk,
                                idx,
                                d["source_type"],
                                d["source_id"],
                                d["task_id"],
                                d["title"],
                                d["galaxy_id"],
                                d["galaxy_name"],
                                d["cluster_id"],
                                ch_text,
                                ch_tokens,
                                meta_str,
                            ),
                        )
                        chunk_id = cur.lastrowid
                        self._conn.execute(
                            "INSERT INTO rag_chunks_fts (chunk_id, title_tokens, content_tokens) VALUES (?, ?, ?)",
                            (chunk_id, d["title_tokens"], ch_tokens),
                        )

                self._conn.execute("INSERT OR REPLACE INTO rag_meta (key, value) VALUES ('last_indexed_at', ?)", (str(now_ms),))
                self._conn.execute("INSERT OR REPLACE INTO rag_meta (key, value) VALUES ('last_revision', ?)", (str(revision),))
                self._conn.commit()
                self._dirty = False

            return self.get_status()
        finally:
            with self._lock:
                self._indexing = False

    def retrieve(
        self,
        store: Any,
        query: str,
        current_task_id: str = "",
        top_k: int = 10,
        exclude_current_core: bool = False,
    ) -> list[dict[str, Any]]:
        self.ensure_fresh(store)
        top_k = max(1, min(int(top_k or 10), 25))
        bundle = store.get_state()
        state = bundle.get("state") or {}
        tasks = [t for t in (state.get("tasks") or []) if isinstance(t, dict) and t.get("id")]
        task_map = {str(t["id"]): t for t in tasks}
        current_task = task_map.get(str(current_task_id or "").strip())

        # Build query text enriched with current task title/tags and @mentions
        q_parts = [str(query or "").strip()]
        mention_names: set[str] = {m.lower() for m in _MENTION_RE.findall(str(query or ""))}
        if current_task:
            q_parts.append(str(current_task.get("title") or ""))
            q_parts.extend(str(tg) for tg in (current_task.get("tags") or [])[:6])
            for fld in (current_task.get("notes") or "", current_task.get("notebook") or ""):
                for m in _MENTION_RE.findall(str(fld)[:2000]):
                    mention_names.add(m.lower())

        raw_q_tokens = tokenize_bilingual(" ".join(q_parts), max_tokens=36).split()
        fts_tokens = [tok for tok in raw_q_tokens if _SAFE_FTS_TOKEN_RE.match(tok)][:24]
        q_token_set = set(raw_q_tokens)

        # Graph proximity sets
        manual_neighbors: set[str] = set()
        ai_neighbors: set[str] = set()
        cur_cid = str((current_task or {}).get("clusterId") or "")
        cur_gid = str((current_task or {}).get("galaxyId") or "")
        if current_task_id:
            for lk in (state.get("manualLinks") or []):
                if not isinstance(lk, dict):
                    continue
                s, t = str(lk.get("source") or ""), str(lk.get("target") or "")
                if s == current_task_id and t:
                    manual_neighbors.add(t)
                elif t == current_task_id and s:
                    manual_neighbors.add(s)
            for lk in (state.get("aiLinks") or []):
                if not isinstance(lk, dict):
                    continue
                s, t = str(lk.get("source") or ""), str(lk.get("target") or "")
                if s == current_task_id and t:
                    ai_neighbors.add(t)
                elif t == current_task_id and s:
                    ai_neighbors.add(s)

        fts_scores: dict[int, float] = {}
        with self._lock:
            if fts_tokens:
                match_expr = " OR ".join(f'"{t}"' for t in fts_tokens)
                try:
                    fts_rows = self._conn.execute(
                        """
                        SELECT CAST(chunk_id AS INTEGER), bm25(rag_chunks_fts, 2.5, 1.0) AS rank_score
                        FROM rag_chunks_fts
                        WHERE rag_chunks_fts MATCH ?
                        ORDER BY rank_score ASC
                        LIMIT 60
                        """,
                        (match_expr,),
                    ).fetchall()
                    for cid_int, rank_score in fts_rows:
                        # bm25 in SQLite FTS5 is negative (more negative = better)
                        fts_scores[int(cid_int)] = max(0.0, -float(rank_score))
                except Exception:
                    fts_scores = {}

            all_chunks = self._conn.execute(
                """
                SELECT id, doc_key, chunk_index, source_type, source_id, task_id, title,
                       galaxy_id, galaxy_name, cluster_id, content, search_tokens, meta_json
                FROM rag_chunks
                """
            ).fetchall()

        max_fts = max(fts_scores.values()) if fts_scores else 1.0
        if max_fts <= 0:
            max_fts = 1.0

        scored: list[dict[str, Any]] = []
        for row in all_chunks:
            (
                chunk_id, _doc_key, _ch_idx, src_type, src_id, row_tid,
                title, gid, gname, cid, content, search_tokens, meta_json,
            ) = row

            if exclude_current_core and current_task_id and row_tid == current_task_id and src_type in {"task_meta", "task_notebook"}:
                continue

            c_tokens = set(str(search_tokens or "").split())
            overlap_cnt = len(q_token_set & c_tokens) if q_token_set else 0
            lex_score = (overlap_cnt / max(1.0, math.sqrt(len(q_token_set) * max(1, len(c_tokens))))) if overlap_cnt else 0.0
            fts_norm = (fts_scores.get(int(chunk_id), 0.0) / max_fts) if chunk_id in fts_scores else 0.0

            reasons: list[str] = []
            if fts_norm > 0 or overlap_cnt > 0:
                reasons.append("关键词匹配")

            graph_boost = 0.0
            if row_tid and row_tid in manual_neighbors:
                graph_boost += 0.45
                reasons.append("直接连线气泡")
            elif row_tid and row_tid in ai_neighbors:
                graph_boost += 0.22
                reasons.append("AI语义关联")
            if cur_cid and cid == cur_cid and row_tid != current_task_id:
                graph_boost += 0.14
                reasons.append("同星群")
            elif cur_gid and gid == cur_gid and row_tid != current_task_id:
                graph_boost += 0.06

            if src_type == "attachment" and current_task_id and row_tid == current_task_id:
                graph_boost += 0.55
                reasons.append("本气泡附件正文")

            if src_type == "contact" and mention_names:
                title_low = str(title or "").lower()
                if any(mn in title_low or mn in str(content or "").lower() for mn in mention_names):
                    graph_boost += 0.55
                    reasons.append("@提及联系人")

            total_score = (0.45 * fts_norm) + (0.40 * lex_score) + graph_boost
            if total_score <= 0.08:
                continue

            snippet = re.sub(r"\s+", " ", str(content or "")).strip()[:160]
            scored.append({
                "sourceType": str(src_type),
                "sourceId": str(src_id),
                "taskId": str(row_tid),
                "title": str(title),
                "galaxyName": str(gname),
                "snippet": snippet,
                "content": str(content),
                "score": round(float(total_score), 4),
                "matchReasons": reasons[:4] if reasons else ["关联上下文"],
            })

        scored.sort(key=lambda x: (-x["score"], x["title"]))

        # Diversity cap: max 2 chunks per (sourceType, sourceId)
        picked: list[dict[str, Any]] = []
        per_src_cnt: dict[tuple[str, str], int] = {}
        for item in scored:
            sk = (item["sourceType"], item["sourceId"])
            cnt = per_src_cnt.get(sk, 0)
            if cnt >= 2:
                continue
            per_src_cnt[sk] = cnt + 1
            picked.append(item)
            if len(picked) >= top_k:
                break
        return picked

    @staticmethod
    def build_workspace_digest(state: dict[str, Any], contacts_total: int = 0, attachments_total: int = 0) -> str:
        tasks = [t for t in (state.get("tasks") or []) if isinstance(t, dict)]
        galaxies = [g for g in (state.get("galaxies") or []) if isinstance(g, dict)]
        galaxy_map = {str(g.get("id")): g for g in galaxies if g.get("id")}
        today_str = _dt.datetime.now().strftime("%Y-%m-%d")

        per_gal: list[str] = []
        for g in galaxies:
            gid = str(g.get("id") or DEFAULT_GALAXY_ID)
            gtasks = [t for t in tasks if (t.get("galaxyId") or DEFAULT_GALAXY_ID) == gid]
            act = sum(1 for t in gtasks if not t.get("completed"))
            comp = sum(1 for t in gtasks if t.get("completed"))
            per_gal.append(f"{g.get('name', gid)}({act}进行中/{comp}完成)")

        top3_titles = [
            f"[{galaxy_map.get(str(t.get('galaxyId')), {}).get('name', '默认')}] {t.get('title')}"
            for t in tasks
            if not t.get("completed") and t.get("top3Slot") in (0, 1, 2)
        ][:6]
        focus_titles = [
            str(t.get("title") or "")
            for t in tasks
            if not t.get("completed") and t.get("focusDate") == today_str
        ][:6]
        lines = [
            f"全系统工作区概览：共 {len(tasks)} 个气泡、{len(galaxies)} 个星系、{attachments_total} 个附件、{contacts_total} 位联系人。",
            "星系分布：" + "，".join(per_gal[:8]),
        ]
        if top3_titles:
            lines.append("当前 Top3：" + "；".join(top3_titles))
        if focus_titles:
            lines.append("今日重点关注：" + "；".join(focus_titles))
        return "\n".join(lines)[:700]

    def build_bubble_rag_bundle(
        self,
        store: Any,
        task_id: str,
        task_context: dict[str, Any] | None,
        question: str,
        top_k: int = 10,
    ) -> dict[str, Any]:
        self.ensure_fresh(store)
        hits = self.retrieve(
            store,
            query=question,
            current_task_id=task_id,
            top_k=top_k,
            exclude_current_core=True,
        )

        # Bound total injected chunk chars to ~7500
        bounded_hits: list[dict[str, Any]] = []
        total_chars = 0
        for h in hits:
            ch = str(h.get("content") or "")
            if total_chars + len(ch) > 7500 and bounded_hits:
                rem = 7500 - total_chars
                if rem >= 180:
                    h_copy = dict(h)
                    h_copy["content"] = ch[:rem] + "..."
                    bounded_hits.append(h_copy)
                break
            bounded_hits.append(h)
            total_chars += len(ch)

        state = (store.get_state().get("state") or {})
        status = self.get_status()
        by_type = status.get("byType") or {}
        digest = self.build_workspace_digest(
            state,
            contacts_total=int(by_type.get("contact") or 0),
            attachments_total=int(by_type.get("attachment") or 0),
        )

        sources = [
            {
                "sourceType": h["sourceType"],
                "sourceId": h["sourceId"],
                "taskId": h["taskId"],
                "title": h["title"],
                "galaxyName": h["galaxyName"],
                "snippet": h["snippet"],
                "matchReasons": h["matchReasons"],
            }
            for h in bounded_hits
        ]
        return {
            "workspaceSummary": digest,
            "hits": bounded_hits,
            "sources": sources,
            "stats": status,
        }

    def close(self) -> None:
        with self._timer_lock:
            if self._sync_timer is not None:
                try:
                    self._sync_timer.cancel()
                except Exception:
                    pass
                self._sync_timer = None
        with self._lock:
            self._conn.close()
