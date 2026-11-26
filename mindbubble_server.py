#!/usr/bin/env python3
"""Lightweight SQLite-backed HTTP service for MindBubble 3D TodoList with Time Machine backups."""

from __future__ import annotations

import argparse
import base64
import copy
import csv
import hashlib
import io
import json
import mimetypes
import os
import queue
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

from mindbubble_rag import MindBubbleRagEngine
from mindbubble_mcp_core import (
    DEFAULT_DB_PATH as _MCP_DEFAULT_DB_PATH,
    McpJsonRpcHandler,
    MindBubbleBackend,
    MindBubbleMcpService,
    PROMPT_SPECS as MCP_PROMPT_SPECS,
    RESOURCE_SPECS as MCP_RESOURCE_SPECS,
    TOOL_SPECS as MCP_TOOL_SPECS,
    build_antigravity_mcp_entry,
    get_antigravity_registration_status,
    install_into_antigravity,
)

MAX_BODY_BYTES = 20 * 1024 * 1024
BASE_DIR = Path(__file__).resolve().parent
HTML_PATH = BASE_DIR / "mind-bubble-todo.html"
SETTINGS_HTML_PATH = BASE_DIR / "mind-bubble-settings.html"
DEFAULT_DB_PATH = BASE_DIR / "data" / "mindbubble.sqlite"
ATTACHMENTS_DIR = BASE_DIR / "data" / "attachments"
MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024
ATT_ID_RE = re.compile(r"^att_[a-f0-9]{12,32}$")
CONTACT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")
_FOCUS_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SCRIPT_TAG_RE = re.compile(r"(?is)<(?:script|iframe|object|embed|style)\b[^>]*>.*?</(?:script|iframe|object|embed|style)>|<(?:script|iframe|object|embed)\b[^>]*?/?>")
_EVENT_HANDLER_RE = re.compile(r"(?is)\s+on[a-z]+\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)")
_JS_PROTO_RE = re.compile(r"(?is)(?:javascript|vbscript)\s*:")
AGY_WORKDIR = BASE_DIR / "data" / "mindbubble-agy-workdir"
DEFAULT_AGY_MODEL = "gemini-pro-orchestrator-neon"
_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_MODEL_LINE_RE = re.compile(r"^([a-z0-9][a-z0-9._-]{2,})\s+(.+)$")

GDRIVE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GDRIVE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GDRIVE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GDRIVE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
GDRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
GDRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
GDRIVE_FILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")

EMPTY_STATE: dict[str, Any] = {
    "tasks": [],
    "manualLinks": [],
    "clusters": [],
    "aiLinks": [],
    "tagCatalog": [],
    "prefs": {},
}

DEFAULT_BACKUP_CONFIG: dict[str, Any] = {
    "autoBackupEnabled": True,
    "minIntervalSec": 0,
    "maxAutoSnapshots": 120,
}


def _sanitize_tag_catalog(raw_catalog: Any, tasks: list[Any]) -> list[str]:
    """Normalize, deduplicate (case-insensitive), and auto-merge task tags up to 200 items."""
    catalog: list[str] = []
    seen: set[str] = set()

    def _add_candidate(item: Any) -> None:
        if not isinstance(item, str) or len(catalog) >= 200:
            return
        clean = item.strip().lstrip("#").strip()[:32]
        if not clean:
            return
        key = clean.lower()
        if key in seen:
            return
        seen.add(key)
        catalog.append(clean)

    if isinstance(raw_catalog, list):
        for item in raw_catalog:
            _add_candidate(item)

    for task in tasks:
        if isinstance(task, dict) and isinstance(task.get("tags"), list):
            for tag in task["tags"]:
                _add_candidate(tag)

    return catalog


DEFAULT_GALAXY_ID = "gal_default"
DEFAULT_GALAXY = {
    "id": DEFAULT_GALAXY_ID,
    "name": "默认星系",
    "icon": "🪐",
    "color": "#38bdf8",
    "description": "默认空间 · 自动收纳既有与未归类气泡",
    "createdAt": 0,
    "isDefault": True,
}


def _sanitize_galaxies(raw_galaxies: Any, fallback_prefs_galaxies: Any = None) -> list[dict[str, Any]]:
    """Normalize Galaxy list and always guarantee built-in Default Galaxy first."""
    candidates: list[Any] = []
    if isinstance(raw_galaxies, list) and len(raw_galaxies) > 0:
        candidates = raw_galaxies
    elif isinstance(fallback_prefs_galaxies, list) and len(fallback_prefs_galaxies) > 0:
        candidates = fallback_prefs_galaxies

    by_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    for item in candidates:
        if not isinstance(item, dict):
            continue
        raw_id = item.get("id")
        if not isinstance(raw_id, str):
            continue
        gid = raw_id.strip()[:64]
        if not gid or gid == "__universe__" or gid in by_id:
            continue
        is_def = gid == DEFAULT_GALAXY_ID
        raw_name = item.get("name")
        name = str(raw_name).strip()[:32] if isinstance(raw_name, str) and raw_name.strip() else (
            DEFAULT_GALAXY["name"] if is_def else f"星系 {len(order) + 1}"
        )
        raw_icon = item.get("icon")
        icon = str(raw_icon).strip()[:8] if isinstance(raw_icon, str) and raw_icon.strip() else (
            DEFAULT_GALAXY["icon"] if is_def else "✨"
        )
        raw_color = item.get("color")
        color = raw_color.strip()[:16] if isinstance(raw_color, str) and raw_color.strip().startswith("#") else DEFAULT_GALAXY["color"]
        raw_desc = item.get("description")
        desc = str(raw_desc).strip()[:120] if isinstance(raw_desc, str) else (DEFAULT_GALAXY["description"] if is_def else "")
        raw_created = item.get("createdAt")
        created_at = int(raw_created) if (isinstance(raw_created, (int, float)) and not isinstance(raw_created, bool) and raw_created > 0) else 0

        by_id[gid] = {
            "id": gid,
            "name": name,
            "icon": icon,
            "color": color,
            "description": desc,
            "createdAt": created_at,
            "isDefault": is_def,
        }
        order.append(gid)

    default_entry = by_id.get(DEFAULT_GALAXY_ID) or dict(DEFAULT_GALAXY)
    default_entry["id"] = DEFAULT_GALAXY_ID
    default_entry["isDefault"] = True

    result = [default_entry]
    for gid in order:
        if gid != DEFAULT_GALAXY_ID:
            result.append(by_id[gid])
    return result


def _sanitize_rich_html_server(raw_html: Any) -> str:
    if not isinstance(raw_html, str) or not raw_html.strip():
        return ""
    cleaned = _SCRIPT_TAG_RE.sub("", raw_html)
    cleaned = _EVENT_HANDLER_RE.sub("", cleaned)
    cleaned = _JS_PROTO_RE.sub("blocked:", cleaned)
    return cleaned[:250_000]


_GBK_PINYIN_BOUNDARIES: tuple[tuple[int, str], ...] = (
    (0xB0A1, "a"),
    (0xB0C5, "b"),
    (0xB2C1, "c"),
    (0xB4EE, "d"),
    (0xB6EA, "e"),
    (0xB7A2, "f"),
    (0xB8C1, "g"),
    (0xB9FE, "h"),
    (0xBBF7, "j"),
    (0xBFA6, "k"),
    (0xC0AC, "l"),
    (0xC2E8, "m"),
    (0xC4C3, "n"),
    (0xC5B6, "o"),
    (0xC5BE, "p"),
    (0xC6DA, "q"),
    (0xC8BB, "r"),
    (0xC8F6, "s"),
    (0xCBFA, "t"),
    (0xCDDA, "w"),
    (0xCEF4, "x"),
    (0xD1B9, "y"),
    (0xD4D1, "z"),
)


def _chinese_pinyin_initials(text: str) -> str:
    """Compute lowercase ASCII initials for Chinese + ASCII text using GB2312 byte boundaries."""
    out: list[str] = []
    for ch in str(text or "").strip():
        if "a" <= ch <= "z" or "A" <= ch <= "Z" or "0" <= ch <= "9":
            out.append(ch.lower())
            continue
        if "\u4e00" <= ch <= "\u9fff":
            try:
                raw = ch.encode("gbk")
            except UnicodeEncodeError:
                continue
            if len(raw) == 2 and raw[0] >= 0xB0:
                code = (raw[0] << 8) + raw[1]
                if code <= 0xD7F9:
                    letter = "z"
                    for idx in range(len(_GBK_PINYIN_BOUNDARIES) - 1, -1, -1):
                        bound, py = _GBK_PINYIN_BOUNDARIES[idx]
                        if code >= bound:
                            letter = py
                            break
                    out.append(letter)
    return "".join(out)


def _extract_email_pinyin_tokens(email: str) -> str:
    local = str(email or "").strip().lower().split("@", 1)[0]
    if not local:
        return ""
    parts = [p for p in re.split(r"[._+\-]+", local) if p]
    tokens = list(dict.fromkeys([*parts, "".join(parts)]))
    return " ".join(tokens)


def _normalize_contact_payload(raw: Any, default_source: str = "manual") -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ValueError("联系人数据必须是 JSON 对象")
    name = str(raw.get("name") or raw.get("localized_name") or "").strip()[:120]
    if not name:
        raise ValueError("联系人姓名不能为空")
    department = str(raw.get("department") or raw.get("dept") or "").strip()[:200]
    email = str(raw.get("email") or raw.get("enterprise_email") or "").strip()[:200]
    aliases = str(raw.get("aliases") or raw.get("alias") or "").strip()[:200]
    raw_pinyin = str(raw.get("pinyin") or "").strip()[:200]
    email_tokens = _extract_email_pinyin_tokens(email)
    pinyin = raw_pinyin or email_tokens
    if raw_pinyin and email_tokens and email_tokens not in raw_pinyin.lower():
        pinyin = f"{raw_pinyin} {email_tokens}".strip()[:200]
    raw_initials = str(raw.get("initials") or "").strip().lower()[:64]
    initials = raw_initials or _chinese_pinyin_initials(name)[:64]
    src_raw = str(raw.get("source") or default_source or "manual").strip().lower()
    source = src_raw if src_raw in ("manual", "lark", "import") else "manual"
    external_id = str(raw.get("externalId") or raw.get("external_id") or raw.get("open_id") or raw.get("openId") or "").strip()[:120]
    notes = str(raw.get("notes") or "").strip()[:500]
    return {
        "name": name,
        "department": department,
        "email": email,
        "pinyin": pinyin,
        "initials": initials,
        "aliases": aliases,
        "source": source,
        "externalId": external_id,
        "notes": notes,
    }


def _parse_contacts_csv(csv_text: str) -> list[dict[str, Any]]:
    text = str(csv_text or "").lstrip("\ufeff").strip()
    if not text:
        return []
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return []

    def _pick(row: dict[str, Any], *keys: str) -> str:
        lower_map = {str(k or "").strip().lower(): v for k, v in row.items() if k is not None}
        for k in keys:
            val = lower_map.get(k.lower())
            if val is not None and str(val).strip():
                return str(val).strip()
        return ""

    items: list[dict[str, Any]] = []
    for row in reader:
        if not isinstance(row, dict):
            continue
        name = _pick(row, "name", "姓名", "名字", "localized_name")
        if not name:
            continue
        items.append(
            {
                "name": name,
                "department": _pick(row, "department", "部门", "dept"),
                "email": _pick(row, "email", "邮箱", "企业邮箱", "enterprise_email", "mail"),
                "aliases": _pick(row, "aliases", "alias", "别名", "昵称"),
                "pinyin": _pick(row, "pinyin", "拼音"),
                "initials": _pick(row, "initials", "拼音首字母", "首字母"),
                "source": _pick(row, "source", "来源") or "import",
                "externalId": _pick(row, "external_id", "externalid", "open_id", "openid"),
                "notes": _pick(row, "notes", "备注"),
            }
        )
    return items


def _sanitize_bubble_workspace_fields(nt: dict[str, Any]) -> None:
    """Normalize per-bubble persistent notebook, rich HTML, focusDate, attachments, checklist, AI history, and activity log."""
    raw_fd = nt.get("focusDate")
    nt["focusDate"] = raw_fd.strip() if isinstance(raw_fd, str) and _FOCUS_DATE_RE.match(raw_fd.strip()) else ""

    raw_nb = nt.get("notebook")
    nt["notebook"] = str(raw_nb)[:100_000] if isinstance(raw_nb, str) else ""
    nt["notebookHtml"] = _sanitize_rich_html_server(nt.get("notebookHtml"))

    clean_atts: list[dict[str, Any]] = []
    raw_atts = nt.get("attachments")
    if isinstance(raw_atts, list):
        for att in raw_atts[:60]:
            if not isinstance(att, dict):
                continue
            aid = str(att.get("id") or "").strip()[:64]
            name = str(att.get("name") or "").strip()[:180]
            if not aid or not name:
                continue
            mime = str(att.get("mimeType") or "application/octet-stream").strip()[:120]
            raw_sz = att.get("size")
            size = int(raw_sz) if isinstance(raw_sz, (int, float)) and not isinstance(raw_sz, bool) and raw_sz >= 0 else 0
            storage = "inline" if att.get("storage") == "inline" else "server"
            url = str(att.get("url") or "").strip()[:512]
            if storage == "server" and url and not url.startswith("/api/attachments/"):
                url = f"/api/attachments/{aid}"
            raw_data_url = att.get("dataUrl")
            data_url = ""
            if storage == "inline" and isinstance(raw_data_url, str) and raw_data_url.startswith("data:") and len(raw_data_url) <= 3_200_000:
                data_url = raw_data_url
            raw_cat = att.get("createdAt")
            created_at = int(raw_cat) if isinstance(raw_cat, (int, float)) and not isinstance(raw_cat, bool) and raw_cat > 0 else 0
            kind = "image" if (att.get("kind") == "image" or mime.startswith("image/")) else "file"
            entry: dict[str, Any] = {
                "id": aid,
                "name": name,
                "mimeType": mime,
                "size": size,
                "url": url,
                "kind": kind,
                "createdAt": created_at,
                "storage": storage,
            }
            if data_url:
                entry["dataUrl"] = data_url
            clean_atts.append(entry)
    nt["attachments"] = clean_atts

    clean_cl: list[dict[str, Any]] = []
    raw_cl = nt.get("checklist")
    if isinstance(raw_cl, list):
        for item in raw_cl[:200]:
            if not isinstance(item, dict):
                continue
            txt = str(item.get("text") or "").strip()[:300]
            if not txt:
                continue
            done = bool(item.get("done"))
            raw_cat = item.get("createdAt")
            created_at = int(raw_cat) if isinstance(raw_cat, (int, float)) and not isinstance(raw_cat, bool) and raw_cat > 0 else 0
            raw_comp = item.get("completedAt")
            comp_at = (
                int(raw_comp)
                if done and isinstance(raw_comp, (int, float)) and not isinstance(raw_comp, bool) and raw_comp > 0
                else None
            )
            clean_cl.append({
                "id": str(item.get("id") or f"chk_{len(clean_cl)+1}")[:64],
                "text": txt,
                "done": done,
                "createdAt": created_at,
                "completedAt": comp_at,
            })
    nt["checklist"] = clean_cl

    clean_ai: list[dict[str, Any]] = []
    raw_ai = nt.get("aiHistory")
    if isinstance(raw_ai, list):
        for msg in raw_ai[-80:]:
            if not isinstance(msg, dict):
                continue
            role = "assistant" if msg.get("role") == "assistant" else "user"
            content = str(msg.get("content") or "").strip()[:20_000]
            if not content:
                continue
            raw_cat = msg.get("createdAt")
            created_at = int(raw_cat) if isinstance(raw_cat, (int, float)) and not isinstance(raw_cat, bool) and raw_cat > 0 else 0
            steps = [
                str(s).strip()[:200]
                for s in (msg.get("suggestedSteps") or [])[:12]
                if isinstance(s, str) and str(s).strip()
            ]
            raw_elapsed = msg.get("elapsedMs")
            elapsed_ms = int(raw_elapsed) if isinstance(raw_elapsed, (int, float)) and not isinstance(raw_elapsed, bool) and raw_elapsed > 0 else 0
            is_err = bool(msg.get("error"))
            clean_rag_sources: list[dict[str, Any]] = []
            raw_rag = msg.get("ragSources")
            if isinstance(raw_rag, list):
                for src in raw_rag[:12]:
                    if not isinstance(src, dict):
                        continue
                    s_title = str(src.get("title") or "").strip()[:160]
                    if not s_title:
                        continue
                    clean_rag_sources.append({
                        "sourceType": str(src.get("sourceType") or "")[:32],
                        "sourceId": str(src.get("sourceId") or "")[:64],
                        "taskId": str(src.get("taskId") or "")[:64],
                        "title": s_title,
                        "galaxyName": str(src.get("galaxyName") or "")[:40],
                        "snippet": str(src.get("snippet") or "")[:200],
                        "matchReasons": [str(r)[:32] for r in (src.get("matchReasons") or [])[:4] if str(r).strip()],
                    })
            clean_ai.append({
                "id": str(msg.get("id") or f"ai_{len(clean_ai)+1}")[:64],
                "role": role,
                "content": content,
                "createdAt": created_at,
                "model": str(msg.get("model") or "")[:80],
                "engine": str(msg.get("engine") or "")[:32],
                "suggestedSteps": steps,
                "conversationId": str(msg.get("conversationId") or "")[:80],
                "elapsedMs": elapsed_ms,
                "error": is_err,
                "retryQuestion": str(msg.get("retryQuestion") or "")[:3000] if is_err else "",
                "ragSources": clean_rag_sources,
            })
    nt["aiHistory"] = clean_ai

    clean_log: list[dict[str, Any]] = []
    raw_log = nt.get("activityLog")
    if isinstance(raw_log, list):
        for entry in raw_log[-150:]:
            if not isinstance(entry, dict):
                continue
            txt = str(entry.get("text") or "").strip()[:300]
            if not txt:
                continue
            raw_cat = entry.get("createdAt")
            created_at = int(raw_cat) if isinstance(raw_cat, (int, float)) and not isinstance(raw_cat, bool) and raw_cat > 0 else 0
            clean_log.append({
                "id": str(entry.get("id") or f"act_{len(clean_log)+1}")[:64],
                "type": str(entry.get("type") or "event")[:40],
                "text": txt,
                "createdAt": created_at,
            })
    nt["activityLog"] = clean_log


def sanitize_state(data: Any) -> dict[str, Any]:
    """Validate and normalize incoming workspace JSON payload."""
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        raise ValueError("Payload must be a JSON object with a 'tasks' list")

    prefs = data.get("prefs") if isinstance(data.get("prefs"), dict) else {}
    clean_prefs = {k: v for k, v in prefs.items() if k != "llmApiKey"}
    galaxies = _sanitize_galaxies(data.get("galaxies"), clean_prefs.get("galaxies"))
    valid_galaxy_ids = {g["id"] for g in galaxies}
    clean_prefs["galaxies"] = galaxies

    tasks: list[Any] = []
    for t in data["tasks"]:
        if isinstance(t, dict):
            nt = dict(t)
            raw_gid = nt.get("galaxyId")
            nt["galaxyId"] = raw_gid if isinstance(raw_gid, str) and raw_gid in valid_galaxy_ids else DEFAULT_GALAXY_ID
            if bool(nt.get("completed")):
                raw_comp = nt.get("completedAt")
                if (
                    isinstance(raw_comp, (int, float))
                    and not isinstance(raw_comp, bool)
                    and (raw_comp - raw_comp == 0)
                    and raw_comp > 0
                ):
                    nt["completedAt"] = int(raw_comp)
                else:
                    nt["completedAt"] = None
            else:
                nt["completedAt"] = None
            _sanitize_bubble_workspace_fields(nt)
            tasks.append(nt)
        else:
            tasks.append(t)

    raw_clusters = data.get("clusters") if isinstance(data.get("clusters"), list) else []
    clusters: list[Any] = []
    for c in raw_clusters:
        if isinstance(c, dict):
            nc = dict(c)
            raw_cgid = nc.get("galaxyId")
            nc["galaxyId"] = raw_cgid if isinstance(raw_cgid, str) and raw_cgid in valid_galaxy_ids else DEFAULT_GALAXY_ID
            clusters.append(nc)
        else:
            clusters.append(c)

    return {
        "tasks": tasks,
        "manualLinks": data.get("manualLinks") if isinstance(data.get("manualLinks"), list) else [],
        "clusters": clusters,
        "aiLinks": data.get("aiLinks") if isinstance(data.get("aiLinks"), list) else [],
        "galaxies": galaxies,
        "tagCatalog": _sanitize_tag_catalog(data.get("tagCatalog"), tasks),
        "prefs": clean_prefs,
    }


def canonical_json(state: dict[str, Any]) -> str:
    return json.dumps(state, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def compute_payload_hash(state: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(state).encode("utf-8")).hexdigest()


def compute_counts(state: dict[str, Any]) -> tuple[int, int, int, int]:
    tasks = state.get("tasks", [])
    task_count = len(tasks)
    completed_count = sum(1 for t in tasks if isinstance(t, dict) and t.get("completed"))
    link_count = len(state.get("manualLinks", []))
    cluster_count = len(state.get("clusters", []))
    return task_count, completed_count, link_count, cluster_count


def _task_signature(t: dict[str, Any]) -> tuple[Any, ...]:
    nb = t.get("notebook") or ""
    nb_html = t.get("notebookHtml") or ""
    atts = tuple(a.get("id") for a in (t.get("attachments") or []) if isinstance(a, dict))
    cl = tuple((c.get("id"), c.get("text"), bool(c.get("done"))) for c in (t.get("checklist") or []) if isinstance(c, dict))
    ai_hist = t.get("aiHistory") or []
    act_log = t.get("activityLog") or []
    return (
        t.get("title"),
        t.get("notes"),
        tuple(t.get("tags") or []),
        t.get("priority"),
        bool(t.get("completed")),
        t.get("completedAt"),
        t.get("top3Slot"),
        t.get("clusterId"),
        t.get("galaxyId"),
        t.get("dueAt"),
        t.get("focusDate") or "",
        hashlib.sha1(str(nb).encode("utf-8", errors="ignore")).hexdigest(),
        hashlib.sha1(str(nb_html).encode("utf-8", errors="ignore")).hexdigest(),
        atts,
        cl,
        len(ai_hist),
        ai_hist[-1].get("id") if ai_hist and isinstance(ai_hist[-1], dict) else None,
        len(act_log),
        act_log[-1].get("id") if act_log and isinstance(act_log[-1], dict) else None,
    )


def compute_diff(base_state: dict[str, Any], target_state: dict[str, Any]) -> dict[str, Any]:
    """Compute structured semantic diff from base_state -> target_state."""
    base_tasks = {t["id"]: t for t in base_state.get("tasks", []) if isinstance(t, dict) and t.get("id")}
    target_tasks = {t["id"]: t for t in target_state.get("tasks", []) if isinstance(t, dict) and t.get("id")}

    added = [str(t.get("title") or "未命名气泡") for tid, t in target_tasks.items() if tid not in base_tasks]
    removed = [str(t.get("title") or "未命名气泡") for tid, t in base_tasks.items() if tid not in target_tasks]
    modified = [
        str(target_tasks[tid].get("title") or "未命名气泡")
        for tid in target_tasks
        if tid in base_tasks and _task_signature(base_tasks[tid]) != _task_signature(target_tasks[tid])
    ]

    b_count, b_comp, b_links, b_clusters = compute_counts(base_state)
    t_count, t_comp, t_links, t_clusters = compute_counts(target_state)

    return {
        "addedTasks": added,
        "removedTasks": removed,
        "modifiedTasks": modified,
        "taskDelta": t_count - b_count,
        "completedDelta": t_comp - b_comp,
        "linkDelta": t_links - b_links,
        "clusterDelta": t_clusters - b_clusters,
        "galaxyDelta": len(target_state.get("galaxies", [])) - len(base_state.get("galaxies", [])),
        "tagCatalogDelta": len(target_state.get("tagCatalog", [])) - len(base_state.get("tagCatalog", [])),
    }


def summarize_diff(diff: dict[str, Any]) -> str:
    parts: list[str] = []
    if diff["addedTasks"]:
        parts.append(f"+{len(diff['addedTasks'])} 新增任务")
    if diff["removedTasks"]:
        parts.append(f"-{len(diff['removedTasks'])} 删除任务")
    if diff["modifiedTasks"]:
        parts.append(f"~{len(diff['modifiedTasks'])} 变更")
    if diff["linkDelta"] > 0:
        parts.append(f"+{diff['linkDelta']} 连线")
    elif diff["linkDelta"] < 0:
        parts.append(f"{diff['linkDelta']} 连线")
    if diff["clusterDelta"] != 0:
        sign = "+" if diff["clusterDelta"] > 0 else ""
        parts.append(f"{sign}{diff['clusterDelta']} 星群")
    if diff.get("tagCatalogDelta", 0) != 0:
        sign = "+" if diff["tagCatalogDelta"] > 0 else ""
        parts.append(f"{sign}{diff['tagCatalogDelta']} 标签")
    if not parts:
        return "位置或偏好微调"
    return " · ".join(parts)


def infer_label(reason: str, diff: dict[str, Any]) -> str:
    if reason:
        return reason[:48]
    if len(diff["addedTasks"]) == 1 and not diff["removedTasks"]:
        return f"新增「{diff['addedTasks'][0][:16]}」"
    if len(diff["removedTasks"]) == 1 and not diff["addedTasks"]:
        return f"删除「{diff['removedTasks'][0][:16]}」"
    if diff["addedTasks"]:
        return f"批量新增 {len(diff['addedTasks'])} 个任务"
    if diff["removedTasks"]:
        return f"移除 {len(diff['removedTasks'])} 个任务"
    if len(diff["modifiedTasks"]) == 1:
        return f"更新「{diff['modifiedTasks'][0][:16]}」"
    if diff["modifiedTasks"]:
        return f"更新 {len(diff['modifiedTasks'])} 个任务"
    if diff["linkDelta"] != 0:
        return "调整气泡连线"
    if diff["clusterDelta"] != 0:
        return "AI 星群结构更新"
    return "工作区自动快照"


class MindBubbleStore:
    """Thread-safe SQLite WAL persistence & Time Machine store for MindBubble."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._on_change_callbacks: list[Callable[[str], None]] = []
        self._init_db()

    def add_change_listener(self, cb: Callable[[str], None]) -> None:
        if callable(cb) and cb not in self._on_change_callbacks:
            self._on_change_callbacks.append(cb)

    def _notify_changed(self, reason: str = "") -> None:
        for cb in list(self._on_change_callbacks):
            try:
                cb(reason)
            except Exception:
                pass

    def _init_db(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workspace_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    payload_json TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    revision INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    label TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    task_count INTEGER NOT NULL DEFAULT 0,
                    completed_count INTEGER NOT NULL DEFAULT 0,
                    link_count INTEGER NOT NULL DEFAULT 0,
                    cluster_count INTEGER NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_snapshots_created ON snapshots(created_at DESC, id DESC);"
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS backup_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    auto_backup_enabled INTEGER NOT NULL DEFAULT 1,
                    min_interval_sec INTEGER NOT NULL DEFAULT 0,
                    max_auto_snapshots INTEGER NOT NULL DEFAULT 120
                );
                """
            )
            self._conn.execute(
                """
                INSERT OR IGNORE INTO backup_config (id, auto_backup_enabled, min_interval_sec, max_auto_snapshots)
                VALUES (1, 1, 0, 120);
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gdrive_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    client_id TEXT NOT NULL DEFAULT '',
                    client_secret TEXT NOT NULL DEFAULT '',
                    folder_name TEXT NOT NULL DEFAULT 'MindBubble Backups',
                    folder_id TEXT NOT NULL DEFAULT '',
                    scope_mode TEXT NOT NULL DEFAULT 'drive.file',
                    access_token TEXT NOT NULL DEFAULT '',
                    refresh_token TEXT NOT NULL DEFAULT '',
                    token_expires_at INTEGER NOT NULL DEFAULT 0,
                    account_email TEXT NOT NULL DEFAULT '',
                    account_name TEXT NOT NULL DEFAULT '',
                    last_backup_at INTEGER NOT NULL DEFAULT 0,
                    last_backup_file TEXT NOT NULL DEFAULT '',
                    updated_at INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO gdrive_config (id) VALUES (1);"
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS attachments (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    rel_path TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS contacts (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    department TEXT NOT NULL DEFAULT '',
                    email TEXT NOT NULL DEFAULT '',
                    pinyin TEXT NOT NULL DEFAULT '',
                    initials TEXT NOT NULL DEFAULT '',
                    aliases TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'manual',
                    external_id TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_contacts_name ON contacts(name);")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_contacts_email ON contacts(email);")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_contacts_ext ON contacts(external_id);")
            self._conn.commit()

    def _get_config_locked(self) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT auto_backup_enabled, min_interval_sec, max_auto_snapshots FROM backup_config WHERE id = 1"
        ).fetchone()
        if not row:
            return dict(DEFAULT_BACKUP_CONFIG)
        return {
            "autoBackupEnabled": bool(row[0]),
            "minIntervalSec": int(row[1]),
            "maxAutoSnapshots": int(row[2]),
        }

    def _get_live_state_locked(self) -> tuple[dict[str, Any], int, int]:
        row = self._conn.execute(
            "SELECT payload_json, revision, updated_at FROM workspace_state WHERE id = 1"
        ).fetchone()
        if not row:
            return copy.deepcopy(EMPTY_STATE), 0, 0
        try:
            payload = sanitize_state(json.loads(row[0]))
        except Exception:
            payload = copy.deepcopy(EMPTY_STATE)
        return payload, int(row[1]), int(row[2])

    def _record_snapshot_locked(
        self,
        revision: int,
        state: dict[str, Any],
        kind: str,
        label: str,
        summary: str | None = None,
        force: bool = False,
        created_at: int | None = None,
    ) -> int | None:
        cfg = self._get_config_locked()
        payload_json = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        payload_hash = compute_payload_hash(state)
        now_ms = int(created_at if created_at is not None else time.time() * 1000)

        if kind == "auto":
            if not cfg["autoBackupEnabled"]:
                return None
            latest = self._conn.execute(
                "SELECT payload_hash, created_at FROM snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if latest and not force and latest[0] == payload_hash:
                return None
            if cfg["minIntervalSec"] > 0 and not force:
                latest_auto = self._conn.execute(
                    "SELECT created_at FROM snapshots WHERE kind = 'auto' ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if latest_auto and (now_ms - int(latest_auto[0])) < cfg["minIntervalSec"] * 1000:
                    return None

        t_count, comp_count, l_count, c_count = compute_counts(state)
        final_summary = (
            summary
            if summary is not None
            else f"{t_count} 任务 ({comp_count} 已完成) · {l_count} 连线 · {c_count} 星群"
        )

        cur = self._conn.execute(
            """
            INSERT INTO snapshots (
                revision, kind, label, summary,
                task_count, completed_count, link_count, cluster_count,
                payload_json, payload_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                revision,
                kind,
                label[:80],
                final_summary[:180],
                t_count,
                comp_count,
                l_count,
                c_count,
                payload_json,
                payload_hash,
                now_ms,
            ),
        )
        snap_id = int(cur.lastrowid)

        if kind == "auto":
            max_keep = max(10, int(cfg["maxAutoSnapshots"]))
            self._conn.execute(
                """
                DELETE FROM snapshots
                WHERE kind = 'auto'
                  AND id NOT IN (
                      SELECT id FROM snapshots WHERE kind = 'auto' ORDER BY id DESC LIMIT ?
                  )
                """,
                (max_keep,),
            )

        return snap_id

    def _upsert_live_locked(self, state: dict[str, Any], now_ms: int | None = None) -> tuple[int, int]:
        payload_json = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        ts = int(now_ms if now_ms is not None else time.time() * 1000)
        self._conn.execute(
            """
            INSERT INTO workspace_state (id, payload_json, revision, updated_at)
            VALUES (1, ?, 1, ?)
            ON CONFLICT(id) DO UPDATE SET
                payload_json = excluded.payload_json,
                revision = workspace_state.revision + 1,
                updated_at = excluded.updated_at
            """,
            (payload_json, ts),
        )
        row = self._conn.execute(
            "SELECT revision, updated_at FROM workspace_state WHERE id = 1"
        ).fetchone()
        return int(row[0]), int(row[1])

    def health(self) -> dict[str, Any]:
        with self._lock:
            _, rev, updated_at = self._get_live_state_locked()
            snap_count = int(self._conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0])
            contacts_count = int(self._conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0])
        return {
            "ok": True,
            "db": str(self.db_path),
            "revision": rev,
            "updatedAt": updated_at,
            "snapshotCount": snap_count,
            "contactsCount": contacts_count,
        }

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            payload, rev, updated_at = self._get_live_state_locked()
        return {
            "state": payload,
            "revision": rev,
            "updatedAt": updated_at,
        }

    def save_state(self, data: Any, reason: str = "") -> dict[str, Any]:
        clean = sanitize_state(data)
        now_ms = int(time.time() * 1000)
        with self._lock:
            prev_state, _, _ = self._get_live_state_locked()
            diff = compute_diff(prev_state, clean)
            new_rev, updated_at = self._upsert_live_locked(clean, now_ms)
            label = infer_label(reason.strip(), diff)
            summary = summarize_diff(diff)
            snap_id = self._record_snapshot_locked(
                revision=new_rev,
                state=clean,
                kind="auto",
                label=label,
                summary=summary,
                force=False,
                created_at=now_ms,
            )
            self._conn.commit()
        self._notify_changed(label)
        return {
            "ok": True,
            "revision": new_rev,
            "updatedAt": updated_at,
            "snapshotId": snap_id,
        }

    def clear_state(self, save_guard: bool = True) -> dict[str, Any]:
        now_ms = int(time.time() * 1000)
        with self._lock:
            prev_state, prev_rev, _ = self._get_live_state_locked()
            if save_guard and prev_state.get("tasks"):
                self._record_snapshot_locked(
                    revision=prev_rev,
                    state=prev_state,
                    kind="pre_clear",
                    label="清空前自动保护",
                    force=True,
                    created_at=now_ms,
                )
            empty = copy.deepcopy(EMPTY_STATE)
            new_rev, updated_at = self._upsert_live_locked(empty, now_ms)
            self._conn.commit()
        self._notify_changed("清空工作区")
        return {
            "ok": True,
            "revision": new_rev,
            "updatedAt": updated_at,
        }

    def create_manual_snapshot(self, label: str = "") -> dict[str, Any]:
        clean_label = (label or "").strip() or "手动备份"
        with self._lock:
            live, rev, _ = self._get_live_state_locked()
            snap_id = self._record_snapshot_locked(
                revision=rev,
                state=live,
                kind="manual",
                label=clean_label,
                force=True,
            )
            self._conn.commit()
            row = self._conn.execute(
                """
                SELECT id, revision, kind, label, summary, task_count, completed_count,
                       link_count, cluster_count, payload_hash, created_at
                FROM snapshots WHERE id = ?
                """,
                (snap_id,),
            ).fetchone()
        return {"ok": True, "snapshot": self._row_to_meta(row)}

    @staticmethod
    def _row_to_meta(row: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "id": int(row[0]),
            "revision": int(row[1]),
            "kind": row[2],
            "label": row[3],
            "summary": row[4],
            "taskCount": int(row[5]),
            "completedCount": int(row[6]),
            "linkCount": int(row[7]),
            "clusterCount": int(row[8]),
            "payloadHash": row[9],
            "createdAt": int(row[10]),
        }

    def list_snapshots(self, kind: str = "all", limit: int = 200) -> dict[str, Any]:
        lim = max(1, min(int(limit), 1000))
        where = ""
        params: list[Any] = []
        if kind == "auto":
            where = "WHERE kind = 'auto'"
        elif kind == "manual":
            where = "WHERE kind = 'manual'"
        elif kind == "guard":
            where = "WHERE kind LIKE 'pre_%'"

        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT id, revision, kind, label, summary, task_count, completed_count,
                       link_count, cluster_count, payload_hash, created_at
                FROM snapshots
                {where}
                ORDER BY id DESC
                LIMIT ?
                """,
                (*params, lim),
            ).fetchall()
            _, rev, _ = self._get_live_state_locked()

        return {
            "revision": rev,
            "snapshots": [self._row_to_meta(r) for r in rows],
        }

    def get_snapshot_detail(self, snapshot_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT id, revision, kind, label, summary, task_count, completed_count,
                       link_count, cluster_count, payload_hash, created_at, payload_json
                FROM snapshots WHERE id = ?
                """,
                (snapshot_id,),
            ).fetchone()
            if not row:
                return None
            live_state, live_rev, _ = self._get_live_state_locked()

        snap_state = sanitize_state(json.loads(row[11]))
        meta = self._row_to_meta(row[:11])
        return {
            "snapshot": meta,
            "state": snap_state,
            "currentRevision": live_rev,
            "diffAgainstCurrent": compute_diff(live_state, snap_state),
        }

    def restore_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        now_ms = int(time.time() * 1000)
        with self._lock:
            row = self._conn.execute(
                "SELECT revision, label, payload_json, payload_hash FROM snapshots WHERE id = ?",
                (snapshot_id,),
            ).fetchone()
            if not row:
                return None
            target_rev = int(row[0])
            target_state = sanitize_state(json.loads(row[2]))
            target_hash = row[3]

            live_state, live_rev, _ = self._get_live_state_locked()
            live_hash = compute_payload_hash(live_state)

            guard_id = None
            if live_hash != target_hash:
                guard_id = self._record_snapshot_locked(
                    revision=live_rev,
                    state=live_state,
                    kind="pre_restore",
                    label=f"恢复至 #{target_rev} 前自动保护",
                    force=True,
                    created_at=now_ms,
                )

            new_rev, updated_at = self._upsert_live_locked(target_state, now_ms + 1)
            self._conn.commit()

        self._notify_changed(f"恢复快照 #{snapshot_id}")
        return {
            "ok": True,
            "revision": new_rev,
            "updatedAt": updated_at,
            "restoredFromId": snapshot_id,
            "guardSnapshotId": guard_id,
            "state": target_state,
        }

    def delete_snapshot(self, snapshot_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM snapshots WHERE id = ?", (snapshot_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def purge_snapshots(self, kind: str = "auto") -> dict[str, Any]:
        with self._lock:
            if kind == "all":
                cur = self._conn.execute("DELETE FROM snapshots")
            elif kind == "auto":
                cur = self._conn.execute("DELETE FROM snapshots WHERE kind = 'auto'")
            else:
                raise ValueError("kind must be 'auto' or 'all'")
            self._conn.commit()
            deleted = int(cur.rowcount)
        return {"ok": True, "deleted": deleted}

    def update_config(self, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise ValueError("Config payload must be a JSON object")
        auto_enabled = 1 if data.get("autoBackupEnabled", True) else 0
        min_interval = max(0, min(int(data.get("minIntervalSec", 0)), 3600))
        max_auto = max(10, min(int(data.get("maxAutoSnapshots", 120)), 2000))
        with self._lock:
            self._conn.execute(
                """
                UPDATE backup_config
                SET auto_backup_enabled = ?, min_interval_sec = ?, max_auto_snapshots = ?
                WHERE id = 1
                """,
                (auto_enabled, min_interval, max_auto),
            )
            self._conn.commit()
            cfg = self._get_config_locked()
        return {"ok": True, "backupConfig": cfg}

    def storage_info(self) -> dict[str, Any]:
        with self._lock:
            live_state, rev, updated_at = self._get_live_state_locked()
            cfg = self._get_config_locked()
            counts = self._conn.execute(
                """
                SELECT
                    COUNT(*),
                    SUM(CASE WHEN kind = 'auto' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN kind = 'manual' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN kind LIKE 'pre_%' THEN 1 ELSE 0 END),
                    MIN(created_at),
                    MAX(created_at)
                FROM snapshots
                """
            ).fetchone()
            contacts_count = int(self._conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0])

        db_bytes = 0
        for suffix in ("", "-wal", "-shm"):
            p = Path(f"{self.db_path}{suffix}")
            if p.exists():
                db_bytes += p.stat().st_size

        t_count, comp_count, l_count, c_count = compute_counts(live_state)
        return {
            "dbPath": str(self.db_path),
            "dbSizeBytes": db_bytes,
            "revision": rev,
            "updatedAt": updated_at,
            "contactsCount": contacts_count,
            "liveCounts": {
                "tasks": t_count,
                "completed": comp_count,
                "manualLinks": l_count,
                "clusters": c_count,
            },
            "snapshotCounts": {
                "total": int(counts[0] or 0),
                "auto": int(counts[1] or 0),
                "manual": int(counts[2] or 0),
                "guard": int(counts[3] or 0),
                "oldestAt": int(counts[4]) if counts[4] else None,
                "newestAt": int(counts[5]) if counts[5] else None,
            },
            "backupConfig": cfg,
        }

    def vacuum(self) -> dict[str, Any]:
        with self._lock:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            self._conn.execute("VACUUM;")
        return {"ok": True, "storage": self.storage_info()}

    def export_backup(self, include_history: bool = True, snapshot_id: int | None = None) -> dict[str, Any] | None:
        now_ms = int(time.time() * 1000)
        if snapshot_id is not None:
            detail = self.get_snapshot_detail(snapshot_id)
            if not detail:
                return None
            return {
                "format": "mindbubble-backup",
                "version": 2,
                "mode": "snapshot",
                "exportedAt": now_ms,
                "snapshotMeta": detail["snapshot"],
                "currentState": detail["state"],
                "snapshots": [],
            }

        with self._lock:
            live_state, rev, _ = self._get_live_state_locked()
            cfg = self._get_config_locked()
            snaps_out: list[dict[str, Any]] = []
            if include_history:
                rows = self._conn.execute(
                    """
                    SELECT id, revision, kind, label, summary, task_count, completed_count,
                           link_count, cluster_count, payload_hash, created_at, payload_json
                    FROM snapshots
                    ORDER BY id ASC
                    """
                ).fetchall()
                for r in rows:
                    meta = self._row_to_meta(r[:11])
                    meta["state"] = sanitize_state(json.loads(r[11]))
                    snaps_out.append(meta)

        contacts_out = self.list_contacts().get("contacts", [])
        return {
            "format": "mindbubble-backup",
            "version": 2,
            "mode": "full" if include_history else "workspace",
            "exportedAt": now_ms,
            "revision": rev,
            "backupConfig": cfg,
            "currentState": live_state,
            "snapshots": snaps_out,
            "contacts": contacts_out,
        }

    def import_backup(self, data: Any, mode: str = "merge") -> dict[str, Any]:
        if mode not in ("merge", "workspace_only", "replace_all"):
            raise ValueError("mode must be 'merge', 'workspace_only', or 'replace_all'")
        if not isinstance(data, dict):
            raise ValueError("Backup payload must be a JSON object")

        if data.get("format") == "mindbubble-backup" and isinstance(data.get("currentState"), dict):
            target_state = sanitize_state(data["currentState"])
            raw_snaps = data.get("snapshots") if isinstance(data.get("snapshots"), list) else []
        elif isinstance(data.get("tasks"), list):
            target_state = sanitize_state(data)
            raw_snaps = []
        else:
            raise ValueError("Unrecognized backup format (expected v2 mindbubble-backup or v1 workspace JSON)")

        now_ms = int(time.time() * 1000)
        imported_snaps = 0

        with self._lock:
            live_state, live_rev, _ = self._get_live_state_locked()

            if mode == "replace_all":
                self._conn.execute("DELETE FROM snapshots")
            elif live_state.get("tasks"):
                self._record_snapshot_locked(
                    revision=live_rev,
                    state=live_state,
                    kind="pre_import",
                    label="导入备份前自动保护",
                    force=True,
                    created_at=now_ms,
                )

            if mode in ("merge", "replace_all") and raw_snaps:
                existing_keys = {
                    (r[0], int(r[1]))
                    for r in self._conn.execute("SELECT payload_hash, created_at FROM snapshots").fetchall()
                }
                for item in raw_snaps:
                    if not isinstance(item, dict) or not isinstance(item.get("state"), dict):
                        continue
                    try:
                        snap_state = sanitize_state(item["state"])
                    except ValueError:
                        continue
                    s_hash = compute_payload_hash(snap_state)
                    s_created = int(item.get("createdAt") or now_ms)
                    if mode == "merge" and (s_hash, s_created) in existing_keys:
                        continue
                    existing_keys.add((s_hash, s_created))
                    s_kind = item.get("kind") if item.get("kind") in ("auto", "manual", "pre_restore", "pre_import", "pre_clear") else "manual"
                    s_label = str(item.get("label") or "导入快照")
                    s_summary = str(item.get("summary") or "") or None
                    s_rev = int(item.get("revision") or 1)
                    self._record_snapshot_locked(
                        revision=s_rev,
                        state=snap_state,
                        kind=s_kind,
                        label=s_label,
                        summary=s_summary,
                        force=True,
                        created_at=s_created,
                    )
                    imported_snaps += 1

            new_rev, updated_at = self._upsert_live_locked(target_state, now_ms + 1)
            self._record_snapshot_locked(
                revision=new_rev,
                state=target_state,
                kind="manual" if mode == "workspace_only" else "auto",
                label="导入工作区备份",
                force=True,
                created_at=now_ms + 1,
            )
            self._conn.commit()

        raw_contacts = data.get("contacts") if isinstance(data.get("contacts"), list) else []
        if raw_contacts:
            self.import_contacts(raw_contacts, mode="merge", default_source="import")

        return {
            "ok": True,
            "revision": new_rev,
            "updatedAt": updated_at,
            "importedTasks": len(target_state.get("tasks", [])),
            "importedSnapshots": imported_snaps,
            "state": target_state,
        }

    _GDRIVE_COLUMNS = (
        "client_id",
        "client_secret",
        "folder_name",
        "folder_id",
        "scope_mode",
        "access_token",
        "refresh_token",
        "token_expires_at",
        "account_email",
        "account_name",
        "last_backup_at",
        "last_backup_file",
        "updated_at",
    )

    def get_gdrive_row(self) -> dict[str, Any]:
        with self._lock:
            cols_sql = ", ".join(self._GDRIVE_COLUMNS)
            row = self._conn.execute(
                f"SELECT {cols_sql} FROM gdrive_config WHERE id = 1"
            ).fetchone()
            if not row:
                return {
                    "client_id": "",
                    "client_secret": "",
                    "folder_name": "MindBubble Backups",
                    "folder_id": "",
                    "scope_mode": "drive.file",
                    "access_token": "",
                    "refresh_token": "",
                    "token_expires_at": 0,
                    "account_email": "",
                    "account_name": "",
                    "last_backup_at": 0,
                    "last_backup_file": "",
                    "updated_at": 0,
                }
            return {col: row[idx] for idx, col in enumerate(self._GDRIVE_COLUMNS)}

    def update_gdrive_row(self, **fields: Any) -> dict[str, Any]:
        allowed = set(self._GDRIVE_COLUMNS)
        updates = {k: v for k, v in fields.items() if k in allowed}
        with self._lock:
            if updates:
                updates.setdefault("updated_at", int(time.time() * 1000))
                assignments = ", ".join(f"{col} = ?" for col in updates.keys())
                params = list(updates.values())
                self._conn.execute(
                    f"UPDATE gdrive_config SET {assignments} WHERE id = 1",
                    params,
                )
                self._conn.commit()
            cols_sql = ", ".join(self._GDRIVE_COLUMNS)
            row = self._conn.execute(
                f"SELECT {cols_sql} FROM gdrive_config WHERE id = 1"
            ).fetchone()
            return {col: row[idx] for idx, col in enumerate(self._GDRIVE_COLUMNS)}

    def save_attachment(
        self,
        name: str,
        mime_type: str,
        raw_bytes: bytes,
        task_id: str = "",
    ) -> dict[str, Any]:
        if not isinstance(raw_bytes, (bytes, bytearray)) or len(raw_bytes) == 0:
            raise ValueError("Attachment data is empty")
        if len(raw_bytes) > MAX_ATTACHMENT_BYTES:
            raise OverflowError(f"Attachment exceeds {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB limit")

        clean_name = Path(str(name or "attachment")).name.strip()[:180] or "attachment"
        clean_mime = str(mime_type or "").split(";")[0].strip()[:120]
        if not clean_mime:
            clean_mime = mimetypes.guess_type(clean_name)[0] or "application/octet-stream"

        att_id = f"att_{secrets.token_hex(8)}"
        ext = Path(clean_name).suffix[:16]
        if not re.match(r"^\.[A-Za-z0-9_-]{1,15}$", ext):
            guessed_ext = mimetypes.guess_extension(clean_mime) or ""
            ext = guessed_ext if re.match(r"^\.[A-Za-z0-9_-]{1,15}$", guessed_ext) else ".bin"

        att_dir = self.db_path.parent / "attachments"
        att_dir.mkdir(parents=True, exist_ok=True)
        rel_name = f"{att_id}{ext}"
        dest_path = att_dir / rel_name
        tmp_path = att_dir / f"{rel_name}.tmp"
        tmp_path.write_bytes(bytes(raw_bytes))
        os.replace(tmp_path, dest_path)

        now_ms = int(time.time() * 1000)
        size = len(raw_bytes)
        clean_task_id = str(task_id or "").strip()[:64]
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO attachments (id, task_id, name, mime_type, size, rel_path, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (att_id, clean_task_id, clean_name, clean_mime, size, rel_name, now_ms),
            )
            self._conn.commit()

        self._notify_changed(f"上传附件 {clean_name}")
        kind = "image" if clean_mime.startswith("image/") else "file"
        return {
            "id": att_id,
            "name": clean_name,
            "mimeType": clean_mime,
            "size": size,
            "url": f"/api/attachments/{att_id}",
            "kind": kind,
            "createdAt": now_ms,
            "storage": "server",
        }

    def get_attachment(self, att_id: str) -> tuple[dict[str, Any], bytes] | None:
        if not isinstance(att_id, str) or not ATT_ID_RE.match(att_id):
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT id, task_id, name, mime_type, size, rel_path, created_at FROM attachments WHERE id = ?",
                (att_id,),
            ).fetchone()
        if not row:
            return None
        file_path = self.db_path.parent / "attachments" / row[5]
        if not file_path.exists():
            return None
        meta = {
            "id": row[0],
            "taskId": row[1],
            "name": row[2],
            "mimeType": row[3],
            "size": row[4],
            "createdAt": row[6],
        }
        return meta, file_path.read_bytes()

    def delete_attachment(self, att_id: str) -> bool:
        if not isinstance(att_id, str) or not ATT_ID_RE.match(att_id):
            return False
        with self._lock:
            row = self._conn.execute("SELECT rel_path FROM attachments WHERE id = ?", (att_id,)).fetchone()
            if not row:
                return False
            rel_path = row[0]
            self._conn.execute("DELETE FROM attachments WHERE id = ?", (att_id,))
            self._conn.commit()
        file_path = self.db_path.parent / "attachments" / rel_path
        if file_path.exists():
            try:
                file_path.unlink()
            except OSError:
                pass
        self._notify_changed(f"删除附件 {att_id}")
        return True

    @staticmethod
    def _row_to_contact(row: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "id": str(row[0]),
            "name": str(row[1]),
            "department": str(row[2] or ""),
            "email": str(row[3] or ""),
            "pinyin": str(row[4] or ""),
            "initials": str(row[5] or ""),
            "aliases": str(row[6] or ""),
            "source": str(row[7] or "manual"),
            "externalId": str(row[8] or ""),
            "notes": str(row[9] or ""),
            "createdAt": int(row[10] or 0),
            "updatedAt": int(row[11] or 0),
        }

    def list_contacts(self, query: str = "", department: str = "") -> dict[str, Any]:
        q = str(query or "").strip().lower()
        dept_filter = str(department or "").strip()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, name, department, email, pinyin, initials, aliases, source, external_id, notes, created_at, updated_at
                FROM contacts
                ORDER BY department COLLATE NOCASE ASC, name COLLATE NOCASE ASC
                """
            ).fetchall()

        all_items = [self._row_to_contact(r) for r in rows]
        departments = sorted({c["department"] for c in all_items if c["department"]})

        filtered: list[dict[str, Any]] = []
        for c in all_items:
            if dept_filter and c["department"] != dept_filter:
                continue
            if q:
                hay = " ".join(
                    [c["name"], c["department"], c["email"], c["pinyin"], c["initials"], c["aliases"], c["notes"]]
                ).lower()
                if q not in hay:
                    continue
            filtered.append(c)

        return {
            "ok": True,
            "contacts": filtered,
            "total": len(all_items),
            "filtered": len(filtered),
            "departments": departments,
        }

    def upsert_contact(self, data: Any, contact_id: str | None = None) -> dict[str, Any]:
        norm = _normalize_contact_payload(data, default_source="manual")
        now_ms = int(time.time() * 1000)
        with self._lock:
            if contact_id is not None:
                if not CONTACT_ID_RE.match(contact_id):
                    raise ValueError("Invalid contact ID")
                existing = self._conn.execute(
                    "SELECT created_at FROM contacts WHERE id = ?",
                    (contact_id,),
                ).fetchone()
                if not existing:
                    raise LookupError("Contact not found")
                self._conn.execute(
                    """
                    UPDATE contacts
                    SET name = ?, department = ?, email = ?, pinyin = ?, initials = ?,
                        aliases = ?, source = ?, external_id = ?, notes = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        norm["name"],
                        norm["department"],
                        norm["email"],
                        norm["pinyin"],
                        norm["initials"],
                        norm["aliases"],
                        norm["source"],
                        norm["externalId"],
                        norm["notes"],
                        now_ms,
                        contact_id,
                    ),
                )
                cid = contact_id
            else:
                cid = f"ct_{secrets.token_hex(6)}"
                self._conn.execute(
                    """
                    INSERT INTO contacts (
                        id, name, department, email, pinyin, initials, aliases,
                        source, external_id, notes, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cid,
                        norm["name"],
                        norm["department"],
                        norm["email"],
                        norm["pinyin"],
                        norm["initials"],
                        norm["aliases"],
                        norm["source"],
                        norm["externalId"],
                        norm["notes"],
                        now_ms,
                        now_ms,
                    ),
                )
            self._conn.commit()
            row = self._conn.execute(
                """
                SELECT id, name, department, email, pinyin, initials, aliases, source, external_id, notes, created_at, updated_at
                FROM contacts WHERE id = ?
                """,
                (cid,),
            ).fetchone()
        self._notify_changed(f"更新联系人 {norm['name']}")
        return {"ok": True, "contact": self._row_to_contact(row)}

    def import_contacts(
        self,
        items: list[dict[str, Any]],
        mode: str = "merge",
        default_source: str = "import",
    ) -> dict[str, Any]:
        if mode not in ("merge", "replace", "replace_source"):
            raise ValueError("mode must be 'merge', 'replace', or 'replace_source'")
        if not isinstance(items, list):
            raise ValueError("contacts must be a list")

        now_ms = int(time.time() * 1000)
        inserted = 0
        updated = 0
        skipped = 0

        with self._lock:
            if mode == "replace":
                self._conn.execute("DELETE FROM contacts")
            elif mode == "replace_source":
                src_clean = default_source if default_source in ("manual", "lark", "import") else "import"
                self._conn.execute("DELETE FROM contacts WHERE source = ?", (src_clean,))

            existing_rows = self._conn.execute(
                """
                SELECT id, name, department, email, pinyin, initials, aliases, source, external_id, notes, created_at, updated_at
                FROM contacts
                """
            ).fetchall()
            by_ext: dict[str, dict[str, Any]] = {}
            by_email: dict[str, dict[str, Any]] = {}
            by_name_dept: dict[tuple[str, str], dict[str, Any]] = {}

            def _index_contact(c: dict[str, Any]) -> None:
                if c["externalId"]:
                    by_ext[c["externalId"]] = c
                if c["email"]:
                    by_email[c["email"].lower()] = c
                by_name_dept[(c["name"].lower(), c["department"].lower())] = c

            for r in existing_rows:
                _index_contact(self._row_to_contact(r))

            for raw in items[:5000]:
                try:
                    norm = _normalize_contact_payload(raw, default_source=default_source)
                except ValueError:
                    skipped += 1
                    continue

                match: dict[str, Any] | None = None
                if norm["externalId"]:
                    match = by_ext.get(norm["externalId"])
                if match is None and norm["email"]:
                    match = by_email.get(norm["email"].lower())
                if match is None:
                    match = by_name_dept.get((norm["name"].lower(), norm["department"].lower()))

                if match is not None:
                    merged_aliases = match["aliases"] or norm["aliases"]
                    if norm["aliases"] and match["aliases"] and norm["aliases"] != match["aliases"]:
                        merged_aliases = match["aliases"]
                    merged_notes = match["notes"] or norm["notes"]
                    merged_dept = norm["department"] or match["department"]
                    merged_email = norm["email"] or match["email"]
                    merged_ext = norm["externalId"] or match["externalId"]
                    merged_pinyin = norm["pinyin"] or match["pinyin"]
                    merged_initials = norm["initials"] or match["initials"]
                    merged_src = norm["source"] if norm["source"] == "lark" else (match["source"] or norm["source"])

                    self._conn.execute(
                        """
                        UPDATE contacts
                        SET name = ?, department = ?, email = ?, pinyin = ?, initials = ?,
                            aliases = ?, source = ?, external_id = ?, notes = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            norm["name"],
                            merged_dept,
                            merged_email,
                            merged_pinyin,
                            merged_initials,
                            merged_aliases,
                            merged_src,
                            merged_ext,
                            merged_notes,
                            now_ms,
                            match["id"],
                        ),
                    )
                    updated_obj = {
                        **match,
                        "name": norm["name"],
                        "department": merged_dept,
                        "email": merged_email,
                        "pinyin": merged_pinyin,
                        "initials": merged_initials,
                        "aliases": merged_aliases,
                        "source": merged_src,
                        "externalId": merged_ext,
                        "notes": merged_notes,
                        "updatedAt": now_ms,
                    }
                    _index_contact(updated_obj)
                    updated += 1
                else:
                    cid = f"ct_{secrets.token_hex(6)}"
                    self._conn.execute(
                        """
                        INSERT INTO contacts (
                            id, name, department, email, pinyin, initials, aliases,
                            source, external_id, notes, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            cid,
                            norm["name"],
                            norm["department"],
                            norm["email"],
                            norm["pinyin"],
                            norm["initials"],
                            norm["aliases"],
                            norm["source"],
                            norm["externalId"],
                            norm["notes"],
                            now_ms,
                            now_ms,
                        ),
                    )
                    created_obj = {
                        "id": cid,
                        **norm,
                        "createdAt": now_ms,
                        "updatedAt": now_ms,
                    }
                    _index_contact(created_obj)
                    inserted += 1

            self._conn.commit()
            total = int(self._conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0])

        res = self.list_contacts()
        self._notify_changed("导入联系人")
        return {
            "ok": True,
            "inserted": inserted,
            "updated": updated,
            "skipped": skipped,
            "total": total,
            "contacts": res["contacts"],
            "departments": res["departments"],
        }

    def delete_contact(self, contact_id: str) -> bool:
        if not isinstance(contact_id, str) or not CONTACT_ID_RE.match(contact_id):
            return False
        with self._lock:
            cur = self._conn.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
            self._conn.commit()
            deleted = cur.rowcount > 0
        if deleted:
            self._notify_changed(f"删除联系人 {contact_id}")
        return deleted

    def clear_contacts(self, source: str = "") -> dict[str, Any]:
        src = str(source or "").strip().lower()
        with self._lock:
            if src:
                if src not in ("manual", "lark", "import"):
                    raise ValueError("source must be 'manual', 'lark', or 'import'")
                cur = self._conn.execute("DELETE FROM contacts WHERE source = ?", (src,))
            else:
                cur = self._conn.execute("DELETE FROM contacts")
            self._conn.commit()
            deleted = int(cur.rowcount)
        if deleted > 0:
            self._notify_changed("清空联系人")
        return {"ok": True, "deleted": deleted}

    def export_contacts_csv(self) -> str:
        items = self.list_contacts().get("contacts", [])
        buf = io.StringIO()
        buf.write("\ufeff")
        writer = csv.writer(buf)
        writer.writerow(["姓名", "部门", "邮箱", "拼音首字母", "别名", "来源", "external_id", "备注"])
        for c in items:
            writer.writerow(
                [
                    c["name"],
                    c["department"],
                    c["email"],
                    c["initials"],
                    c["aliases"],
                    c["source"],
                    c["externalId"],
                    c["notes"],
                ]
            )
        return buf.getvalue()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text)


def _find_agy_bin() -> str | None:
    found = shutil.which("agy")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "agy"
    if fallback.exists():
        return str(fallback)
    return None


def _sort_models_fastest_first(models: list[dict[str, str]]) -> list[dict[str, str]]:
    def priority(m: dict[str, str]) -> tuple[int, str]:
        mid = m["id"].lower()
        if mid == "gemini-pro-orchestrator-neon":
            return (0, mid)
        if mid == "gemini-4-neon-medium":
            return (1, mid)
        if "orchestrator" in mid:
            return (2, mid)
        if "neon" in mid:
            return (3, mid)
        if mid == "gemini-4-hydrogen-medium" or "hydrogen" in mid:
            return (4, mid)
        if "flash" in mid:
            return (5, mid)
        return (6, mid)

    indexed = list(enumerate(models))
    indexed.sort(key=lambda item: (priority(item[1])[0], item[0]))
    return [m for _, m in indexed]


class AgyWorker:
    """Persistent stream-json worker process for antigravity-cli (`agy`)."""

    def __init__(self, agy_bin: str, model: str, workdir: Path) -> None:
        self.model = model
        self.conversation_id: str = ""
        self.last_used: float = time.monotonic()
        workdir.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(
            [
                agy_bin,
                "--input-format",
                "stream-json",
                "--output-format",
                "stream-json",
                "--disable-slash-commands",
                "--model",
                model,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(workdir),
        )
        self._lock = threading.Lock()
        self._stdout_q: queue.Queue[bytes | None] = queue.Queue()
        self.stderr_lines: list[str] = []
        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        try:
            while True:
                line = self.proc.stdout.readline()
                if not line:
                    self._stdout_q.put(None)
                    break
                self._stdout_q.put(line)
        except Exception:
            self._stdout_q.put(None)

    def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        try:
            while True:
                line = self.proc.stderr.readline()
                if not line:
                    break
                decoded = _strip_ansi(line.decode("utf-8", errors="replace")).strip()
                if decoded:
                    self.stderr_lines.append(decoded)
                    if len(self.stderr_lines) > 30:
                        self.stderr_lines.pop(0)
        except Exception:
            pass

    def is_alive(self) -> bool:
        return self.proc.poll() is None and self.proc.stdin is not None

    def ask(self, prompt: str, timeout: float = 90.0) -> str:
        if not self.is_alive():
            raise RuntimeError("agy worker process is not alive")
        with self._lock:
            self.last_used = time.monotonic()
            assert self.proc.stdin is not None
            payload = json.dumps({"event": "user", "message": {"content": prompt}}, ensure_ascii=False) + "\n"
            self.proc.stdin.write(payload.encode("utf-8"))
            self.proc.stdin.flush()

            deadline = time.monotonic() + timeout
            chunks: list[str] = []
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"agy timed out after {timeout:.0f}s")
                try:
                    raw_line = self._stdout_q.get(timeout=remaining)
                except queue.Empty as exc:
                    raise TimeoutError(f"agy timed out after {timeout:.0f}s") from exc

                if raw_line is None:
                    err_tail = " | ".join(self.stderr_lines[-5:])
                    raise RuntimeError(f"agy exited unexpectedly ({self.proc.returncode}): {err_tail}")

                line_str = raw_line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue
                try:
                    ev = json.loads(line_str)
                except json.JSONDecodeError:
                    continue

                if ev.get("conversation_id"):
                    self.conversation_id = str(ev["conversation_id"]).strip()

                ev_type = ev.get("event")
                if ev_type == "step_update":
                    su = ev.get("step_update") or {}
                    if su.get("step_type") == "agent_response":
                        delta = su.get("text_delta") or ""
                        if delta:
                            chunks.append(delta)
                elif ev_type == "result":
                    res = ev.get("result") or {}
                    if res.get("conversation_id"):
                        self.conversation_id = str(res["conversation_id"]).strip()
                    if res.get("status") != "SUCCESS":
                        raise RuntimeError(res.get("error") or f"agy returned status {res.get('status')}")
                    text = "".join(chunks).strip()
                    if not text:
                        text = str(res.get("response") or res.get("text") or "").strip()
                    self.last_used = time.monotonic()
                    return text

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        if self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2.5)
            except Exception:
                try:
                    self.proc.kill()
                    self.proc.wait(timeout=1.5)
                except Exception:
                    pass


class AgyClient:
    """Manages `agy` model discovery, warm worker reuse, and task clustering."""

    def __init__(self, workdir: Path = AGY_WORKDIR) -> None:
        self.workdir = workdir
        self._pool_lock = threading.Lock()
        self._worker: AgyWorker | None = None
        self._bubble_workers: dict[str, AgyWorker] = {}
        self._models_cache: list[dict[str, str]] | None = None
        self._models_cached_at: float = 0.0

    def is_available(self) -> bool:
        return _find_agy_bin() is not None

    def list_models(self, force_refresh: bool = False) -> dict[str, Any]:
        agy_bin = _find_agy_bin()
        now = time.monotonic()
        if agy_bin and (force_refresh or not self._models_cache or (now - self._models_cached_at > 300)):
            try:
                proc = subprocess.run(
                    [agy_bin, "models"],
                    capture_output=True,
                    text=True,
                    timeout=20.0,
                )
                cleaned = _strip_ansi(proc.stdout + "\n" + proc.stderr).replace("\r", "\n")
                parsed: list[dict[str, str]] = []
                seen: set[str] = set()
                for line in cleaned.splitlines():
                    line = re.sub(r"^.*?Fetching available models\.\.\.\s*", "", line).strip()
                    if not line:
                        continue
                    m = _MODEL_LINE_RE.match(line)
                    if m:
                        mid, name = m.group(1).strip(), m.group(2).strip()
                        if mid == "gemini-pro-orchestrator-neon":
                            name = "Orchestrator (Neon · Gemini 4 Neon Medium)"
                        if mid not in seen:
                            seen.add(mid)
                            parsed.append({"id": mid, "name": name})
                if parsed:
                    self._models_cache = _sort_models_fastest_first(parsed)
                    self._models_cached_at = now
            except Exception:
                pass

        fallback_models = [
            {"id": "gemini-pro-orchestrator-neon", "name": "Orchestrator (Neon · Gemini 4 Neon Medium)"},
            {"id": "gemini-4-neon-medium", "name": "Gemini 4 Neon (Medium)"},
            {"id": "gemini-4-neon-high", "name": "Gemini 4 Neon (High)"},
            {"id": "gemini-4-hydrogen-medium", "name": "Gemini 4 Hydrogen (Medium)"},
            {"id": "gemini-3.8-flash-low", "name": "Gemini 3.8 Flash (Low)"},
        ]
        return {
            "available": agy_bin is not None,
            "agyPath": agy_bin,
            "defaultModel": DEFAULT_AGY_MODEL,
            "bubbleChatSupported": True,
            "models": self._models_cache or fallback_models,
        }

    def prewarm(self, model: str = DEFAULT_AGY_MODEL) -> None:
        agy_bin = _find_agy_bin()
        if not agy_bin:
            return
        with self._pool_lock:
            if self._worker and self._worker.is_alive() and self._worker.model == model:
                return
            if self._worker:
                self._worker.close()
                self._worker = None
            try:
                self._worker = AgyWorker(agy_bin, model, self.workdir)
            except Exception:
                self._worker = None

    def _ask_with_retry(self, prompt: str, model: str, timeout: float = 90.0) -> str:
        agy_bin = _find_agy_bin()
        if not agy_bin:
            raise RuntimeError("未检测到 agy 命令，请先安装并登录 antigravity-cli")

        for attempt in range(2):
            with self._pool_lock:
                if not self._worker or not self._worker.is_alive() or self._worker.model != model:
                    if self._worker:
                        self._worker.close()
                    self._worker = AgyWorker(agy_bin, model, self.workdir)
                worker = self._worker

            try:
                return worker.ask(prompt, timeout=timeout)
            except Exception:
                with self._pool_lock:
                    if self._worker is worker:
                        self._worker.close()
                        self._worker = None
                if attempt == 1:
                    raise

        raise RuntimeError("agy invocation failed")

    def _ask_bubble_with_retry(
        self,
        task_id: str,
        prompt: str,
        model: str,
        reset_session: bool = False,
        timeout: float = 120.0,
    ) -> tuple[str, str]:
        agy_bin = _find_agy_bin()
        if not agy_bin:
            raise RuntimeError("未检测到 agy 命令，请先安装并登录 antigravity-cli")

        key = str(task_id or "").strip()[:64] or "__default_bubble__"
        for attempt in range(2):
            with self._pool_lock:
                existing = self._bubble_workers.get(key)
                if reset_session or not existing or not existing.is_alive() or existing.model != model:
                    if existing:
                        existing.close()
                        self._bubble_workers.pop(key, None)
                    while len(self._bubble_workers) >= 3:
                        oldest_key = min(
                            self._bubble_workers,
                            key=lambda k: self._bubble_workers[k].last_used,
                        )
                        oldest_worker = self._bubble_workers.pop(oldest_key)
                        oldest_worker.close()
                    self._bubble_workers[key] = AgyWorker(agy_bin, model, self.workdir)
                worker = self._bubble_workers[key]

            try:
                text = worker.ask(prompt, timeout=timeout)
                return text, worker.conversation_id
            except Exception:
                with self._pool_lock:
                    if self._bubble_workers.get(key) is worker:
                        self._bubble_workers.pop(key, None)
                        worker.close()
                if attempt == 1:
                    raise

        raise RuntimeError("agy bubble invocation failed")

    def cluster_tasks(self, tasks: Any, manual_links: Any, model: str | None = None) -> dict[str, Any]:
        if not isinstance(tasks, list) or not tasks:
            raise ValueError("tasks must be a non-empty list")

        chosen_model = (model or "").strip() or DEFAULT_AGY_MODEL
        compact_tasks: list[dict[str, Any]] = []
        valid_ids: list[str] = []
        for t in tasks:
            if not isinstance(t, dict) or not t.get("id"):
                continue
            tid = str(t["id"])
            valid_ids.append(tid)
            item: dict[str, Any] = {
                "id": tid,
                "title": str(t.get("title") or "")[:120],
                "tags": [str(x)[:24] for x in (t.get("tags") or [])[:6]],
                "notes": str(t.get("notes") or "")[:120],
                "priority": str(t.get("priority") or "normal"),
            }
            if t.get("dueAt"):
                item["hasDue"] = True
            compact_tasks.append(item)

        if not valid_ids:
            raise ValueError("No valid task IDs provided")

        compact_links = []
        if isinstance(manual_links, list):
            for l in manual_links[:40]:
                if isinstance(l, dict) and l.get("source") and l.get("target"):
                    compact_links.append({"source": str(l["source"]), "target": str(l["target"]), "label": str(l.get("label") or "")[:20]})

        prompt = (
            "你是一个知识整理与任务星系聚类 AI。不要调用任何工具，也不要读取任何文件，只根据下方任务 JSON 进行语义聚类。\n"
            "请将任务按主题聚为 2 到 5 个星系（若任务少于 3 个可聚为 1 到 2 个），并为强相关任务生成跨任务关联。\n"
            "严格只输出一段合法 JSON，不要包含 Markdown 代码块或任何解释文字，格式如下：\n"
            '{"clusters":[{"name":"简短中文星系名(4-10字)","summary":"16字以内聚类主题说明","taskIds":["id1","id2"]}],"aiLinks":[{"source":"id1","target":"id2","reason":"8字以内关联说明"}]}\n'
            "要求：输入里的每一个任务 id 都必须且只能出现在一个 cluster.taskIds 中。\n"
            f"任务列表：{json.dumps(compact_tasks, ensure_ascii=False)}\n"
            f"已有手动连线：{json.dumps(compact_links, ensure_ascii=False)}"
        )

        started = time.monotonic()
        raw_text = self._ask_with_retry(prompt, chosen_model)
        cleaned = raw_text.replace("```json", "").replace("```", "").strip()
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if not match:
            raise RuntimeError("AGY 未返回合法 JSON 结构")
        parsed = json.loads(match.group(0))
        raw_clusters = parsed.get("clusters")
        if not isinstance(raw_clusters, list) or not raw_clusters:
            raise RuntimeError("AGY 返回的聚类结果为空")

        valid_id_set = set(valid_ids)
        assigned: set[str] = set()
        clusters_out: list[dict[str, Any]] = []
        for idx, c in enumerate(raw_clusters):
            if not isinstance(c, dict):
                continue
            c_ids = [str(tid) for tid in (c.get("taskIds") or []) if str(tid) in valid_id_set and str(tid) not in assigned]
            if not c_ids:
                continue
            assigned.update(c_ids)
            clusters_out.append({
                "name": str(c.get("name") or f"星系 {len(clusters_out) + 1}")[:24],
                "summary": str(c.get("summary") or "AGY 智能语义聚类")[:48],
                "taskIds": c_ids,
            })

        leftover = [tid for tid in valid_ids if tid not in assigned]
        if leftover:
            if clusters_out:
                clusters_out[0]["taskIds"].extend(leftover)
            else:
                clusters_out.append({"name": "待整理星系", "summary": "AGY 自动归类", "taskIds": leftover})

        manual_pair_set: set[str] = set()
        for l in compact_links:
            manual_pair_set.add(f"{l['source']}|{l['target']}")
            manual_pair_set.add(f"{l['target']}|{l['source']}")

        ai_links_out: list[dict[str, Any]] = []
        seen_ai: set[str] = set()
        for l in parsed.get("aiLinks") or []:
            if not isinstance(l, dict):
                continue
            s = str(l.get("source") or "")
            t = str(l.get("target") or "")
            if s in valid_id_set and t in valid_id_set and s != t:
                pair = "|".join(sorted((s, t)))
                if f"{s}|{t}" not in manual_pair_set and pair not in seen_ai:
                    seen_ai.add(pair)
                    ai_links_out.append({
                        "source": s,
                        "target": t,
                        "reason": str(l.get("reason") or "AGY 语义关联")[:24],
                    })

        return {
            "ok": True,
            "engine": "agy",
            "model": chosen_model,
            "clusters": clusters_out,
            "aiLinks": ai_links_out,
            "elapsedMs": int((time.monotonic() - started) * 1000),
        }

    def generate_daily_report(self, report_context: Any, model: str | None = None) -> dict[str, Any]:
        if not isinstance(report_context, dict):
            raise ValueError("reportContext must be a JSON object")

        chosen_model = (model or "").strip() or DEFAULT_AGY_MODEL

        def _compact_tasks(items: Any, limit: int) -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            if not isinstance(items, list):
                return out
            for t in items[:limit]:
                if not isinstance(t, dict):
                    continue
                out.append({
                    "title": str(t.get("title") or "")[:100],
                    "priority": str(t.get("priority") or "normal"),
                    "tags": [str(x)[:24] for x in (t.get("tags") or [])[:5]],
                    "notes": str(t.get("notes") or "")[:100],
                    "time": str(t.get("time") or "")[:32],
                    "top3": bool(t.get("top3")),
                })
            return out

        compact_ctx = {
            "date": str(report_context.get("date") or "")[:20],
            "weekday": str(report_context.get("weekday") or "")[:12],
            "scopeLabel": str(report_context.get("scopeLabel") or "全部标签")[:40],
            "stats": report_context.get("stats") if isinstance(report_context.get("stats"), dict) else {},
            "completedTasks": _compact_tasks(report_context.get("completedTasks"), 40),
            "createdTasks": _compact_tasks(report_context.get("createdTasks"), 30),
            "top3Active": _compact_tasks(report_context.get("top3Active"), 3),
            "overdueTasks": _compact_tasks(report_context.get("overdueTasks"), 15),
            "dueSoonTasks": _compact_tasks(report_context.get("dueSoonTasks"), 15),
            "tagBreakdown": (report_context.get("tagBreakdown") or [])[:12]
            if isinstance(report_context.get("tagBreakdown"), list)
            else [],
            "clusterBreakdown": (report_context.get("clusterBreakdown") or [])[:8]
            if isinstance(report_context.get("clusterBreakdown"), list)
            else [],
        }

        prompt = (
            "你是一个高效且洞察力强的个人任务复盘助手。不要调用任何工具，也不要读取任何文件，只根据下方今日任务统计 JSON 生成简洁务实的中文今日报告。\n"
            "严格只输出一段合法 JSON，不要包含 Markdown 代码块或任何额外解释文字，格式如下：\n"
            '{"overview":"60-120字今日复盘总评（结合完成情况、核心推进与焦点分布）",'
            '"highlights":["关键成果或亮点1","亮点2"],'
            '"risks":["潜在风险、逾期或阻塞提醒（若无风险给出简短正面判断）"],'
            '"nextActions":["明日或下一步优先行动建议1","建议2","建议3"]}\n'
            f"今日数据：{json.dumps(compact_ctx, ensure_ascii=False)}"
        )

        started = time.monotonic()
        raw_text = self._ask_with_retry(prompt, chosen_model)
        cleaned = raw_text.replace("```json", "").replace("```", "").strip()
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if not match:
            raise RuntimeError("AGY 未返回合法 JSON 结构")
        parsed = json.loads(match.group(0))
        if not isinstance(parsed, dict):
            raise RuntimeError("AGY 返回格式异常")

        def _norm_list(val: Any) -> list[str]:
            if not isinstance(val, list):
                return []
            res: list[str] = []
            for item in val:
                s = str(item or "").strip()
                if s:
                    res.append(s[:140])
                if len(res) >= 6:
                    break
            return res

        overview = str(parsed.get("overview") or "").strip()[:400]
        highlights = _norm_list(parsed.get("highlights"))
        risks = _norm_list(parsed.get("risks"))
        next_actions = _norm_list(parsed.get("nextActions"))

        if not overview and not highlights and not next_actions:
            raise RuntimeError("AGY 返回的报告内容为空")

        return {
            "ok": True,
            "engine": "agy",
            "model": chosen_model,
            "summary": {
                "overview": overview,
                "highlights": highlights,
                "risks": risks,
                "nextActions": next_actions,
            },
            "elapsedMs": int((time.monotonic() - started) * 1000),
        }

    def generate_critical_path(
        self,
        tasks: Any,
        manual_links: Any = None,
        ai_links: Any = None,
        target_task_id: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(tasks, list) or not tasks:
            raise ValueError("tasks must be a non-empty list")

        chosen_model = (model or "").strip() or DEFAULT_AGY_MODEL

        compact_tasks: list[dict[str, Any]] = []
        valid_ids: set[str] = set()
        for t in tasks[:80]:
            if not isinstance(t, dict):
                continue
            tid = str(t.get("id") or "").strip()
            if not tid:
                continue
            valid_ids.add(tid)
            compact_tasks.append({
                "id": tid,
                "title": str(t.get("title") or "")[:100],
                "priority": str(t.get("priority") or "normal"),
                "top3Slot": t.get("top3Slot"),
                "dueAt": t.get("dueAt"),
                "completed": bool(t.get("completed")),
                "tags": [str(x)[:24] for x in (t.get("tags") or [])[:5]],
                "notes": str(t.get("notes") or "")[:100],
                "clusterId": t.get("clusterId"),
            })

        if not compact_tasks:
            raise ValueError("tasks contains no valid items")

        compact_manual: list[dict[str, str]] = []
        if isinstance(manual_links, list):
            for l in manual_links[:120]:
                if not isinstance(l, dict):
                    continue
                src = str(l.get("source") or "").strip()
                dst = str(l.get("target") or "").strip()
                if src in valid_ids and dst in valid_ids and src != dst:
                    compact_manual.append({
                        "source": src,
                        "target": dst,
                        "label": str(l.get("label") or "")[:30],
                    })

        compact_ai: list[dict[str, str]] = []
        if isinstance(ai_links, list):
            for l in ai_links[:120]:
                if not isinstance(l, dict):
                    continue
                src = str(l.get("source") or "").strip()
                dst = str(l.get("target") or "").strip()
                if src in valid_ids and dst in valid_ids and src != dst:
                    compact_ai.append({
                        "source": src,
                        "target": dst,
                        "reason": str(l.get("reason") or "")[:30],
                    })

        clean_target = str(target_task_id or "").strip()
        if clean_target not in valid_ids:
            clean_target = ""

        payload_ctx = {
            "targetTaskId": clean_target or None,
            "tasks": compact_tasks,
            "manualLinks": compact_manual,
            "aiLinks": compact_ai,
        }
        prompt = (
            "你是一个专业的项目管理与关键路径(CPM)规划专家。不要调用任何工具，也不要读取任何文件，只根据下方任务图数据推导出最重要的一条有序关键路径（3到7步，从前置铺垫逐步推进到终局目标"
            + (f" {clean_target}" if clean_target else "（或最具价值的核心 Top3/关键里程碑）")
            + "）。如已有连线稀疏，可结合语义补全前后置依赖。\n"
            "严格只输出合法 JSON，不要包含 Markdown 代码块或任何额外解释，格式如下：\n"
            '{"goalTitle":"终局目标标题","summary":"60-120字关键路径洞察与推进建议","bottleneckId":"task_id",'
            '"steps":[{"taskId":"task_id","reason":"为何排在此步或对下一步的支撑","risk":"潜在阻塞或执行提醒"}],'
            '"suggestedLinks":[{"source":"前置taskId","target":"后置taskId","reason":"依赖原因"}]}\n'
            f"任务图数据：{json.dumps(payload_ctx, ensure_ascii=False)}"
        )

        started = time.monotonic()
        raw_text = self._ask_with_retry(prompt, chosen_model)
        cleaned = raw_text.replace("```json", "").replace("```", "").strip()
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if not match:
            raise RuntimeError("AGY 未返回合法 JSON 结构")
        parsed = json.loads(match.group(0))
        if not isinstance(parsed, dict):
            raise RuntimeError("AGY 返回格式异常")

        steps_out: list[dict[str, str]] = []
        seen_steps: set[str] = set()
        raw_steps = parsed.get("steps")
        if isinstance(raw_steps, list):
            for st in raw_steps:
                if not isinstance(st, dict):
                    continue
                tid = str(st.get("taskId") or "").strip()
                if not tid or tid not in valid_ids or tid in seen_steps:
                    continue
                seen_steps.add(tid)
                steps_out.append({
                    "taskId": tid,
                    "reason": str(st.get("reason") or "").strip()[:120],
                    "risk": str(st.get("risk") or "").strip()[:120],
                })
                if len(steps_out) >= 10:
                    break

        if not steps_out:
            raise RuntimeError("AGY 返回的关键路径步骤为空或无效")

        bottleneck_id = str(parsed.get("bottleneckId") or "").strip()
        if bottleneck_id not in seen_steps:
            bottleneck_id = steps_out[0]["taskId"]

        suggested_out: list[dict[str, str]] = []
        seen_pairs: set[str] = set()
        raw_suggested = parsed.get("suggestedLinks")
        if isinstance(raw_suggested, list):
            for lk in raw_suggested:
                if not isinstance(lk, dict):
                    continue
                src = str(lk.get("source") or "").strip()
                dst = str(lk.get("target") or "").strip()
                if not src or not dst or src == dst or src not in valid_ids or dst not in valid_ids:
                    continue
                pk = f"{src}|{dst}"
                if pk in seen_pairs:
                    continue
                seen_pairs.add(pk)
                suggested_out.append({
                    "source": src,
                    "target": dst,
                    "reason": str(lk.get("reason") or "AI 推导前置依赖").strip()[:48],
                })
                if len(suggested_out) >= 12:
                    break

        goal_title = str(parsed.get("goalTitle") or "").strip()[:100]
        summary = str(parsed.get("summary") or "").strip()[:400]

        return {
            "ok": True,
            "engine": "agy",
            "model": chosen_model,
            "path": {
                "goalTitle": goal_title,
                "summary": summary,
                "bottleneckId": bottleneck_id,
                "steps": steps_out,
                "suggestedLinks": suggested_out,
            },
            "elapsedMs": int((time.monotonic() - started) * 1000),
        }

    def bubble_chat(
        self,
        task_context: Any,
        messages: Any = None,
        question: str = "",
        model: str | None = None,
        task_id: str = "",
        reset_session: bool = False,
        rag_bundle: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(task_context, dict):
            raise ValueError("taskContext must be a JSON object")
        clean_question = str(question or "").strip()
        if not clean_question:
            raise ValueError("question must not be empty")

        chosen_model = (model or "").strip() or DEFAULT_AGY_MODEL
        clean_task_id = str(task_id or task_context.get("id") or "").strip()[:64]
        compact_cl = []
        for item in (task_context.get("checklist") or [])[:40]:
            if isinstance(item, dict) and item.get("text"):
                compact_cl.append({"text": str(item["text"])[:160], "done": bool(item.get("done"))})
        compact_links = []
        for lk in (task_context.get("linkedTasks") or [])[:20]:
            if isinstance(lk, dict) and lk.get("title"):
                compact_links.append({
                    "title": str(lk["title"])[:100],
                    "completed": bool(lk.get("completed")),
                    "label": str(lk.get("label") or "")[:40],
                })
        compact_ctx = {
            "title": str(task_context.get("title") or "")[:160],
            "galaxyName": str(task_context.get("galaxyName") or "默认星系")[:40],
            "clusterName": str(task_context.get("clusterName") or "")[:60],
            "priority": str(task_context.get("priority") or "normal"),
            "completed": bool(task_context.get("completed")),
            "dueAtStr": str(task_context.get("dueAtStr") or "")[:32],
            "tags": [str(x)[:24] for x in (task_context.get("tags") or [])[:8]],
            "notes": str(task_context.get("notes") or "")[:1000],
            "notebook": str(task_context.get("notebook") or "")[:6000],
            "attachments": [
                str(a.get("name") or "")[:80]
                for a in (task_context.get("attachments") or [])[:20]
                if isinstance(a, dict) and a.get("name")
            ],
            "checklist": compact_cl,
            "linkedTasks": compact_links,
        }
        compact_msgs = []
        if isinstance(messages, list):
            for m in messages[-12:]:
                if isinstance(m, dict) and m.get("content"):
                    compact_msgs.append({
                        "role": "assistant" if m.get("role") == "assistant" else "user",
                        "content": str(m["content"])[:1500],
                    })

        rag_sources = list((rag_bundle or {}).get("sources") or []) if isinstance(rag_bundle, dict) else []
        rag_stats = dict((rag_bundle or {}).get("stats") or {}) if isinstance(rag_bundle, dict) else {}
        rag_section = ""
        if isinstance(rag_bundle, dict):
            ws_sum = str(rag_bundle.get("workspaceSummary") or "").strip()
            hits = rag_bundle.get("hits") or []
            hit_blocks = []
            for idx, h in enumerate(hits, 1):
                reasons = ",".join(h.get("matchReasons") or [])
                hit_blocks.append(
                    f"[{idx}] ({h.get('sourceType')} | {h.get('galaxyName') or '工作区'}"
                    + (f" | {reasons}" if reasons else "")
                    + f") {h.get('title')}:\n{str(h.get('content') or '').strip()}"
                )
            rag_section = (
                (f"\n系统全局概览：\n{ws_sum}\n" if ws_sum else "")
                + ("\n自动 RAG 检索到的全系统相关上下文（包含其他气泡、笔记本、AI历史、附件正文与联系人；请优先围绕当前气泡回答，并在相关时自然吸收这些跨系统信息）：\n" + "\n\n".join(hit_blocks) + "\n" if hit_blocks else "")
            )

        prompt = (
            "你是 MindBubble 气泡工作舱的专属 AI 助手。不要调用任何工具，也不要读取任何文件，只结合下方任务上下文、全系统 RAG 上下文与近期对话回答用户问题。\n"
            "请尽量输出清晰、可直接落地的中文 Markdown，并优先输出合法 JSON："
            '{"reply":"Markdown 格式回答正文","suggestedSteps":["可选可添加到子任务清单的具体行动步1","行动步2"]}\n'
            f"任务上下文：{json.dumps(compact_ctx, ensure_ascii=False)}\n"
            f"{rag_section}"
            f"历史对话：{json.dumps(compact_msgs, ensure_ascii=False)}\n"
            f"用户问题：{clean_question[:3000]}"
        )

        started = time.monotonic()
        raw_text, conv_id = self._ask_bubble_with_retry(
            clean_task_id,
            prompt,
            chosen_model,
            reset_session=bool(reset_session),
            timeout=120.0,
        )
        raw_text = raw_text.strip()
        reply_text = raw_text
        suggested_steps: list[str] = []

        cleaned = raw_text
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        elif cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        match = re.search(r"\{[\s\S]*\}", cleaned)
        parsed_ok = False
        if match:
            try:
                parsed = json.loads(match.group(0))
                if isinstance(parsed, dict) and isinstance(parsed.get("reply"), str) and parsed["reply"].strip():
                    reply_text = parsed["reply"].strip()[:20_000]
                    raw_steps = parsed.get("suggestedSteps")
                    if isinstance(raw_steps, list):
                        for st in raw_steps[:12]:
                            s = str(st or "").strip()[:200]
                            if s:
                                suggested_steps.append(s)
                    parsed_ok = True
            except Exception:
                pass

        if not parsed_ok:
            reply_text = raw_text[:20_000]
            for line in reply_text.splitlines():
                m_line = re.match(r"^\s*(?:[-*•]|\d+[.)、])\s+(?:\[[ xX]\]\s+)?(.+)$", line)
                if m_line:
                    step = m_line.group(1).strip()[:200]
                    if step and len(suggested_steps) < 8:
                        suggested_steps.append(step)

        if not reply_text:
            raise RuntimeError("AGY 未返回有效回复内容")

        elapsed_ms = int((time.monotonic() - started) * 1000)
        print(
            f"[bubble-chat] task={clean_task_id or '-'} model={chosen_model} conv={conv_id or '-'} rag={len(rag_sources)} elapsed={elapsed_ms}ms",
            flush=True,
        )

        return {
            "ok": True,
            "engine": "agy",
            "cli": "antigravity-cli",
            "agyPath": _find_agy_bin(),
            "model": chosen_model,
            "conversationId": conv_id,
            "reply": reply_text,
            "suggestedSteps": suggested_steps,
            "ragSources": rag_sources,
            "ragStats": rag_stats,
            "elapsedMs": elapsed_ms,
        }

    def close(self) -> None:
        with self._pool_lock:
            if self._worker:
                self._worker.close()
                self._worker = None
            for worker in list(self._bubble_workers.values()):
                worker.close()
            self._bubble_workers.clear()


class GoogleDriveClient:
    """Stdlib-only Google Drive OAuth 2.0 + Drive v3 backup/restore client."""

    def __init__(self, store: MindBubbleStore) -> None:
        self.store = store
        self._lock = threading.Lock()
        self._pending_states: dict[str, dict[str, Any]] = {}

    def _http_request(
        self,
        url: str,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: int = 30,
    ) -> tuple[int, bytes]:
        req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return int(resp.status), resp.read()
        except urllib.error.HTTPError as exc:
            err_body = exc.read()
            raise ValueError(self._extract_google_error(exc.code, err_body)) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"无法连接 Google 服务: {exc.reason}") from exc

    @staticmethod
    def _extract_google_error(status_code: int, raw: bytes) -> str:
        text = raw.decode("utf-8", errors="replace").strip()
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                if isinstance(obj.get("error_description"), str):
                    return f"Google API ({status_code}): {obj['error_description']}"
                err = obj.get("error")
                if isinstance(err, dict) and err.get("message"):
                    return f"Google API ({status_code}): {err['message']}"
                if isinstance(err, str):
                    return f"Google API ({status_code}): {err}"
        except Exception:
            pass
        return f"Google API HTTP {status_code}: {text[:240] or '请求失败'}"

    def _http_json(
        self,
        url: str,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: int = 30,
    ) -> Any:
        _status, raw = self._http_request(url, method=method, headers=headers, body=body, timeout=timeout)
        return json.loads(raw.decode("utf-8")) if raw else {}

    @staticmethod
    def parse_credentials_json(raw: Any) -> tuple[str, str]:
        obj = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(obj, dict):
            raise ValueError("credentials.json 必须是 JSON 对象")
        section = obj.get("web") or obj.get("installed") or obj
        if not isinstance(section, dict):
            raise ValueError("无法识别的 Google OAuth credentials.json 结构")
        cid = str(section.get("client_id") or "").strip()
        csec = str(section.get("client_secret") or "").strip()
        if not cid or not csec:
            raise ValueError("credentials.json 中缺少 client_id 或 client_secret")
        return cid, csec

    @staticmethod
    def _build_scopes(scope_mode: str) -> str:
        base = "https://www.googleapis.com/auth/drive.file https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/userinfo.profile"
        if scope_mode == "drive.file+readonly":
            return f"https://www.googleapis.com/auth/drive.readonly {base}"
        return base

    def get_status(self, origin: str) -> dict[str, Any]:
        row = self.store.get_gdrive_row()
        cid = str(row.get("client_id") or "")
        csec = str(row.get("client_secret") or "")
        has_refresh = bool(row.get("refresh_token"))
        has_live_access = bool(row.get("access_token")) and int(row.get("token_expires_at") or 0) > int(time.time() * 1000) + 15000
        return {
            "ok": True,
            "backendSupported": True,
            "configured": bool(cid and csec),
            "hasClientSecret": bool(csec),
            "clientId": cid,
            "folderName": str(row.get("folder_name") or "MindBubble Backups"),
            "folderId": str(row.get("folder_id") or ""),
            "scopeMode": str(row.get("scope_mode") or "drive.file"),
            "connected": bool(cid and csec and (has_refresh or has_live_access)),
            "hasRefreshToken": has_refresh,
            "accountEmail": str(row.get("account_email") or ""),
            "accountName": str(row.get("account_name") or ""),
            "tokenExpiresAt": int(row.get("token_expires_at") or 0),
            "lastBackupAt": int(row.get("last_backup_at") or 0),
            "lastBackupFile": str(row.get("last_backup_file") or ""),
            "redirectUri": f"{origin.rstrip('/')}/api/gdrive/callback",
            "jsOrigin": origin.rstrip("/"),
        }

    def save_config(self, payload: Any, origin: str = "http://localhost:8765") -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("Payload must be a JSON object")
        cur = self.store.get_gdrive_row()
        prev_cid = str(cur.get("client_id") or "")
        prev_folder = str(cur.get("folder_name") or "MindBubble Backups")

        if payload.get("credentialsJson"):
            cid, csec = self.parse_credentials_json(payload["credentialsJson"])
        else:
            cid = str(payload.get("clientId") if "clientId" in payload else prev_cid).strip()
            if payload.get("clearSecret"):
                csec = ""
            elif "clientSecret" in payload and str(payload.get("clientSecret") or "").strip():
                csec = str(payload["clientSecret"]).strip()
            elif cid == prev_cid:
                csec = str(cur.get("client_secret") or "")
            else:
                csec = ""

        folder_name = str(payload.get("folderName") or prev_folder).strip()[:80] or "MindBubble Backups"
        scope_mode = "drive.file+readonly" if payload.get("scopeMode") == "drive.file+readonly" else "drive.file"

        updates: dict[str, Any] = {
            "client_id": cid,
            "client_secret": csec,
            "folder_name": folder_name,
            "scope_mode": scope_mode,
        }
        if cid != prev_cid:
            updates.update({
                "access_token": "",
                "refresh_token": "",
                "token_expires_at": 0,
                "account_email": "",
                "account_name": "",
                "folder_id": "",
            })
        elif folder_name != prev_folder:
            updates["folder_id"] = ""

        self.store.update_gdrive_row(**updates)
        return self.get_status(origin)

    def start_auth(self, origin: str) -> dict[str, Any]:
        row = self.store.get_gdrive_row()
        cid = str(row.get("client_id") or "").strip()
        csec = str(row.get("client_secret") or "").strip()
        if not cid or not csec:
            raise ValueError("请先保存 Google OAuth Client ID 与 Client Secret")

        redirect_uri = f"{origin.rstrip('/')}/api/gdrive/callback"
        state_tok = secrets.token_urlsafe(24)
        now = time.time()
        with self._lock:
            expired = [k for k, v in self._pending_states.items() if now - float(v.get("createdAt", 0)) > 600]
            for k in expired:
                self._pending_states.pop(k, None)
            self._pending_states[state_tok] = {"createdAt": now, "redirectUri": redirect_uri}

        params = {
            "client_id": cid,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": self._build_scopes(str(row.get("scope_mode") or "drive.file")),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state_tok,
        }
        auth_url = f"{GDRIVE_AUTH_URL}?{urlencode(params)}"
        return {"ok": True, "authUrl": auth_url, "redirectUri": redirect_uri}

    def handle_callback(self, code: str, state: str, error: str = "") -> dict[str, Any]:
        if error:
            raise ValueError(f"Google 授权未完成: {error}")
        if not code or not state:
            raise ValueError("缺少授权 code 或 state 参数")

        with self._lock:
            pending = self._pending_states.pop(state, None)
        if not pending or time.time() - float(pending.get("createdAt", 0)) > 600:
            raise ValueError("授权 state 无效或已过期，请重新发起授权")

        row = self.store.get_gdrive_row()
        cid = str(row.get("client_id") or "").strip()
        csec = str(row.get("client_secret") or "").strip()
        if not cid or not csec:
            raise ValueError("OAuth 配置不完整，缺少 Client ID / Secret")

        form_bytes = urlencode({
            "code": code,
            "client_id": cid,
            "client_secret": csec,
            "redirect_uri": str(pending["redirectUri"]),
            "grant_type": "authorization_code",
        }).encode("utf-8")

        tok = self._http_json(
            GDRIVE_TOKEN_URL,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=form_bytes,
        )
        access_token = str(tok.get("access_token") or "").strip()
        if not access_token:
            raise ValueError("Google 未返回 access_token")
        refresh_token = str(tok.get("refresh_token") or row.get("refresh_token") or "").strip()
        expires_in = int(tok.get("expires_in") or 3600)
        expires_at = int(time.time() * 1000) + max(60, expires_in) * 1000

        email = str(row.get("account_email") or "")
        name = str(row.get("account_name") or "")
        try:
            profile = self._http_json(
                GDRIVE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            email = str(profile.get("email") or email)
            name = str(profile.get("name") or email or name)
        except Exception:
            pass

        self.store.update_gdrive_row(
            access_token=access_token,
            refresh_token=refresh_token,
            token_expires_at=expires_at,
            account_email=email,
            account_name=name,
        )
        return {"ok": True, "accountEmail": email, "accountName": name}

    def _ensure_access_token(self) -> str:
        row = self.store.get_gdrive_row()
        now_ms = int(time.time() * 1000)
        cur_access = str(row.get("access_token") or "").strip()
        if cur_access and int(row.get("token_expires_at") or 0) > now_ms + 60000:
            return cur_access

        refresh_token = str(row.get("refresh_token") or "").strip()
        cid = str(row.get("client_id") or "").strip()
        csec = str(row.get("client_secret") or "").strip()
        if not refresh_token or not cid or not csec:
            raise ValueError("Google Drive 授权已过期或未授权，请重新点击授权")

        form_bytes = urlencode({
            "client_id": cid,
            "client_secret": csec,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }).encode("utf-8")
        tok = self._http_json(
            GDRIVE_TOKEN_URL,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=form_bytes,
        )
        new_access = str(tok.get("access_token") or "").strip()
        if not new_access:
            raise ValueError("刷新 Google Drive 访问令牌失败，请重新授权")
        expires_in = int(tok.get("expires_in") or 3600)
        expires_at = now_ms + max(60, expires_in) * 1000
        self.store.update_gdrive_row(access_token=new_access, token_expires_at=expires_at)
        return new_access

    def _ensure_backup_folder(self, token: str) -> tuple[str, str]:
        row = self.store.get_gdrive_row()
        folder_name = str(row.get("folder_name") or "MindBubble Backups").strip() or "MindBubble Backups"
        cached_id = str(row.get("folder_id") or "").strip()
        headers = {"Authorization": f"Bearer {token}"}

        if cached_id and GDRIVE_FILE_ID_RE.match(cached_id):
            try:
                meta = self._http_json(
                    f"{GDRIVE_FILES_URL}/{quote(cached_id)}?fields=id,name,trashed",
                    headers=headers,
                )
                if meta.get("id") and not meta.get("trashed"):
                    return str(meta["id"]), str(meta.get("name") or folder_name)
            except Exception:
                pass

        escaped = folder_name.replace("\\", "\\\\").replace("'", "\\'")
        q = f"name='{escaped}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        search_url = f"{GDRIVE_FILES_URL}?{urlencode({'q': q, 'fields': 'files(id,name)', 'pageSize': 5, 'spaces': 'drive'})}"
        found = self._http_json(search_url, headers=headers)
        files = found.get("files") if isinstance(found, dict) else []
        if isinstance(files, list) and files:
            fid = str(files[0]["id"])
            self.store.update_gdrive_row(folder_id=fid)
            return fid, str(files[0].get("name") or folder_name)

        created = self._http_json(
            f"{GDRIVE_FILES_URL}?fields=id,name",
            method="POST",
            headers={**headers, "Content-Type": "application/json; charset=utf-8"},
            body=json.dumps({
                "name": folder_name,
                "mimeType": "application/vnd.google-apps.folder",
            }, ensure_ascii=False).encode("utf-8"),
        )
        fid = str(created.get("id") or "")
        if not fid:
            raise ValueError("创建 Google Drive 备份文件夹失败")
        self.store.update_gdrive_row(folder_id=fid)
        return fid, folder_name

    def upload_backup(self, include_history: bool = True, custom_name: str = "") -> dict[str, Any]:
        token = self._ensure_access_token()
        folder_id, folder_name = self._ensure_backup_folder(token)
        payload = self.store.export_backup(include_history=include_history)
        if not payload:
            raise ValueError("无法生成备份数据")

        date_str = time.strftime("%Y%m%d-%H%M%S")
        suffix = "full" if include_history else "workspace"
        fname = str(custom_name or "").strip()[:120] or f"mindbubble-backup-{suffix}-{date_str}.json"
        if not fname.lower().endswith(".json"):
            fname += ".json"

        cur_state = payload.get("currentState") or {}
        metadata = {
            "name": fname,
            "parents": [folder_id],
            "mimeType": "application/json",
            "appProperties": {
                "app": "mindbubble",
                "mode": "full" if include_history else "workspace",
                "tasks": str(len(cur_state.get("tasks") or [])),
                "galaxies": str(len(cur_state.get("galaxies") or [])),
                "snapshots": str(len(payload.get("snapshots") or [])),
                "revision": str(payload.get("revision") or 0),
            },
        }
        boundary = f"mindbubble_{uuid.uuid4().hex}"
        body = (
            f"--{boundary}\r\n"
            f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(metadata, ensure_ascii=False)}\r\n"
            f"--{boundary}\r\n"
            f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(payload, ensure_ascii=False)}\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")

        uploaded = self._http_json(
            f"{GDRIVE_UPLOAD_URL}?uploadType=multipart&fields=id,name,size,modifiedTime,webViewLink,appProperties",
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": f"multipart/related; boundary={boundary}",
            },
            body=body,
            timeout=60,
        )
        now_ms = int(time.time() * 1000)
        self.store.update_gdrive_row(last_backup_at=now_ms, last_backup_file=str(uploaded.get("name") or fname))
        return {"ok": True, "file": uploaded, "folderId": folder_id, "folderName": folder_name, "lastBackupAt": now_ms}

    def list_files(self, search_all: bool = False, limit: int = 30) -> dict[str, Any]:
        token = self._ensure_access_token()
        row = self.store.get_gdrive_row()
        folder_id = ""
        folder_name = str(row.get("folder_name") or "MindBubble Backups")
        if not search_all:
            folder_id, folder_name = self._ensure_backup_folder(token)
            q = f"'{folder_id}' in parents and trashed=false and (mimeType='application/json' or name contains '.json')"
        else:
            q = "trashed=false and mimeType!='application/vnd.google-apps.folder' and (mimeType='application/json' or name contains '.json')"

        params = urlencode({
            "q": q,
            "orderBy": "modifiedTime desc",
            "pageSize": str(max(1, min(100, int(limit)))),
            "fields": "files(id,name,size,modifiedTime,webViewLink,appProperties)",
            "spaces": "drive",
        })
        res = self._http_json(f"{GDRIVE_FILES_URL}?{params}", headers={"Authorization": f"Bearer {token}"})
        files = res.get("files") if isinstance(res, dict) and isinstance(res.get("files"), list) else []
        return {"ok": True, "files": files, "folderId": folder_id, "folderName": folder_name}

    @staticmethod
    def _validate_file_id(file_id: str) -> str:
        clean = str(file_id or "").strip()
        if not GDRIVE_FILE_ID_RE.match(clean):
            raise ValueError("无效的 Google Drive 文件 ID")
        return clean

    def download_backup(self, file_id: str) -> dict[str, Any]:
        fid = self._validate_file_id(file_id)
        token = self._ensure_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        meta = self._http_json(
            f"{GDRIVE_FILES_URL}/{quote(fid)}?fields=id,name,size,modifiedTime,webViewLink,appProperties",
            headers=headers,
        )
        _status, raw = self._http_request(
            f"{GDRIVE_FILES_URL}/{quote(fid)}?alt=media",
            headers=headers,
            timeout=60,
        )
        if len(raw) > MAX_BODY_BYTES:
            raise OverflowError("Google Drive 备份文件超过 20 MB 限制")
        parsed = json.loads(raw.decode("utf-8"))
        if isinstance(parsed, dict) and parsed.get("format") == "mindbubble-backup" and isinstance(parsed.get("currentState"), dict):
            ws = parsed["currentState"]
            snaps = parsed.get("snapshots") if isinstance(parsed.get("snapshots"), list) else []
            fmt = "v2-snapshot" if parsed.get("mode") == "snapshot" else "v2-full"
        elif isinstance(parsed, dict) and isinstance(parsed.get("tasks"), list):
            ws = parsed
            snaps = []
            fmt = "v1-workspace"
        else:
            raise ValueError("所选 Google Drive 文件不是有效的 MindBubble 备份格式")

        summary = {
            "format": fmt,
            "tasks": len(ws.get("tasks") or []),
            "galaxies": len(ws.get("galaxies") or (ws.get("prefs") or {}).get("galaxies") or []),
            "manualLinks": len(ws.get("manualLinks") or []),
            "clusters": len(ws.get("clusters") or []),
            "snapshots": len(snaps),
            "exportedAt": parsed.get("exportedAt"),
        }
        return {"ok": True, "file": meta, "summary": summary, "backup": parsed}

    def import_from_drive(self, file_id: str, mode: str = "merge") -> dict[str, Any]:
        downloaded = self.download_backup(file_id)
        res = self.store.import_backup(downloaded["backup"], mode=mode)
        return {"ok": True, "sourceFile": downloaded["file"], **res}

    def delete_file(self, file_id: str) -> dict[str, Any]:
        fid = self._validate_file_id(file_id)
        token = self._ensure_access_token()
        self._http_json(
            f"{GDRIVE_FILES_URL}/{quote(fid)}?fields=id,trashed",
            method="PATCH",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            body=json.dumps({"trashed": True}).encode("utf-8"),
        )
        return {"ok": True, "fileId": fid}

    def disconnect(self, origin: str = "http://localhost:8765") -> dict[str, Any]:
        row = self.store.get_gdrive_row()
        tok = str(row.get("refresh_token") or row.get("access_token") or "").strip()
        if tok:
            try:
                self._http_request(
                    GDRIVE_REVOKE_URL,
                    method="POST",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    body=urlencode({"token": tok}).encode("utf-8"),
                    timeout=10,
                )
            except Exception:
                pass
        self.store.update_gdrive_row(
            access_token="",
            refresh_token="",
            token_expires_at=0,
            account_email="",
            account_name="",
            folder_id="",
        )
        return self.get_status(origin)


def make_handler(
    store: MindBubbleStore,
    agy_client: AgyClient,
    gdrive_client: GoogleDriveClient | None = None,
    rag_engine: MindBubbleRagEngine | None = None,
) -> type[BaseHTTPRequestHandler]:
    gdrive = gdrive_client or GoogleDriveClient(store)
    rag = rag_engine or MindBubbleRagEngine(store.db_path)
    if rag_engine is None:
        store.add_change_listener(lambda reason: rag.schedule_sync(store, reason=reason))
    mcp_service = MindBubbleMcpService(MindBubbleBackend(store=store, agy_client=agy_client))
    mcp_rpc = McpJsonRpcHandler(mcp_service)

    class MindBubbleHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        @staticmethod
        def _mcp_cors_headers() -> dict[str, str]:
            return {
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Accept, Mcp-Session-Id, MCP-Protocol-Version",
                "Access-Control-Expose-Headers": "Mcp-Session-Id, MCP-Protocol-Version",
            }

        def _send_empty(self, status: int, extra_headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            if extra_headers:
                for k, v in extra_headers.items():
                    self.send_header(k, v)
            self.end_headers()

        def do_OPTIONS(self) -> None:
            path = urlparse(self.path).path
            if path == "/mcp":
                self._send_empty(HTTPStatus.NO_CONTENT, self._mcp_cors_headers())
                return
            self._send_empty(HTTPStatus.NO_CONTENT)

        def _request_origin(self) -> str:
            proto = (self.headers.get("X-Forwarded-Proto") or "http").split(",")[0].strip() or "http"
            host = (self.headers.get("Host") or "localhost:8765").strip()
            return f"{proto}://{host}"

        @staticmethod
        def _parse_gdrive_file_id(path: str, prefix: str, suffix: str = "") -> str | None:
            if not path.startswith(prefix) or (suffix and not path.endswith(suffix)):
                return None
            mid = path[len(prefix) : len(path) - len(suffix) if suffix else len(path)]
            if mid and "/" not in mid and GDRIVE_FILE_ID_RE.match(mid):
                return mid
            return None

        def log_message(self, format: str, *args: Any) -> None:
            ts = time.strftime("%H:%M:%S")
            sys.stderr.write(f"[{ts}] {self.address_string()} - {format % args}\n")

        def _send_bytes(
            self,
            status: int,
            content_type: str,
            body: bytes,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if extra_headers:
                for k, v in extra_headers.items():
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _send_json(
            self,
            status: int,
            data: dict[str, Any],
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self._send_bytes(status, "application/json; charset=utf-8", body, extra_headers)

        def _read_json_body(self) -> Any:
            raw_len = self.headers.get("Content-Length")
            if raw_len is None:
                raise ValueError("Missing Content-Length header")
            try:
                length = int(raw_len)
            except ValueError as exc:
                raise ValueError("Invalid Content-Length header") from exc
            if length < 0:
                raise ValueError("Invalid Content-Length header")
            if length > MAX_BODY_BYTES:
                raise OverflowError("Payload exceeds 20 MB limit")
            raw = self.rfile.read(length) if length > 0 else b"{}"
            return json.loads(raw.decode("utf-8"))

        @staticmethod
        def _parse_snapshot_id(path: str, prefix: str, suffix: str = "") -> int | None:
            if not path.startswith(prefix) or not path.endswith(suffix):
                return None
            mid = path[len(prefix) : len(path) - len(suffix) if suffix else len(path)]
            if mid.isdigit():
                return int(mid)
            return None

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)

            if path in ("/", "/mind-bubble-todo.html"):
                if not HTML_PATH.exists():
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "mind-bubble-todo.html not found"})
                    return
                self._send_bytes(HTTPStatus.OK, "text/html; charset=utf-8", HTML_PATH.read_bytes())
                return

            if path in ("/settings", "/mind-bubble-settings.html"):
                if not SETTINGS_HTML_PATH.exists():
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "mind-bubble-settings.html not found"})
                    return
                self._send_bytes(HTTPStatus.OK, "text/html; charset=utf-8", SETTINGS_HTML_PATH.read_bytes())
                return

            if path == "/api/health":
                health = store.health()
                health["agyAvailable"] = agy_client.is_available()
                health["bubbleChatSupported"] = True
                health["attachmentsSupported"] = True
                health["contactsSupported"] = True
                health["ragSupported"] = True
                health["rag"] = rag.get_status()
                self._send_json(HTTPStatus.OK, health)
                return

            if path == "/api/rag/status":
                self._send_json(HTTPStatus.OK, {"ok": True, **rag.get_status()})
                return

            if path == "/api/rag/search":
                q = (qs.get("q") or [""])[0]
                tid = (qs.get("taskId") or [""])[0]
                tk_str = (qs.get("topK") or ["8"])[0]
                top_k = max(1, min(int(tk_str) if tk_str.isdigit() else 8, 20))
                hits = rag.retrieve(store, query=q, current_task_id=tid, top_k=top_k)
                self._send_json(HTTPStatus.OK, {"ok": True, "query": q, "hits": hits, "status": rag.get_status()})
                return

            if path == "/api/contacts":
                q = (qs.get("q") or [""])[0]
                dept = (qs.get("department") or [""])[0]
                self._send_json(HTTPStatus.OK, store.list_contacts(query=q, department=dept))
                return

            if path == "/api/contacts/export":
                fmt = (qs.get("format") or ["csv"])[0].lower()
                date_str = time.strftime("%Y%m%d-%H%M%S")
                if fmt == "json":
                    data = store.list_contacts()
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "ok": True,
                            "exportedAt": int(time.time() * 1000),
                            "count": data["total"],
                            "contacts": data["contacts"],
                        },
                        {"Content-Disposition": f'attachment; filename="mindbubble-contacts-{date_str}.json"'},
                    )
                else:
                    csv_text = store.export_contacts_csv()
                    self._send_bytes(
                        HTTPStatus.OK,
                        "text/csv; charset=utf-8",
                        csv_text.encode("utf-8"),
                        {"Content-Disposition": f'attachment; filename="mindbubble-contacts-{date_str}.csv"'},
                    )
                return

            if path == "/api/contacts/lark/status":
                try:
                    from export_lark_contacts import get_lark_auth_status

                    st = get_lark_auth_status()
                    st.pop("raw", None)
                    self._send_json(HTTPStatus.OK, {"ok": True, **st})
                except Exception as exc:
                    self._send_json(HTTPStatus.OK, {"ok": False, "cliInstalled": False, "userReady": False, "botReady": False, "note": str(exc)})
                return

            if path.startswith("/api/attachments/"):
                att_id = path[len("/api/attachments/"):].strip("/")
                if not ATT_ID_RE.match(att_id):
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid attachment ID"})
                    return
                res = store.get_attachment(att_id)
                if not res:
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Attachment not found"})
                    return
                meta, raw_bytes = res
                force_dl = (qs.get("download") or ["0"])[0] == "1"
                disp = "attachment" if force_dl else "inline"
                extra_hdrs = {
                    "X-Content-Type-Options": "nosniff",
                    "Content-Disposition": f"{disp}; filename*=UTF-8''{quote(meta['name'])}",
                }
                self._send_bytes(HTTPStatus.OK, meta["mimeType"], raw_bytes, extra_hdrs)
                return

            if path == "/api/ai/models":
                force = (qs.get("refresh") or ["0"])[0] == "1"
                self._send_json(HTTPStatus.OK, agy_client.list_models(force_refresh=force))
                return

            if path == "/api/state":
                self._send_json(HTTPStatus.OK, store.get_state())
                return

            if path == "/api/storage/info":
                self._send_json(HTTPStatus.OK, store.storage_info())
                return

            if path == "/api/snapshots":
                kind = (qs.get("kind") or ["all"])[0]
                limit_str = (qs.get("limit") or ["200"])[0]
                limit = int(limit_str) if limit_str.isdigit() else 200
                self._send_json(HTTPStatus.OK, store.list_snapshots(kind=kind, limit=limit))
                return

            snap_export_id = self._parse_snapshot_id(path, "/api/snapshots/", "/export")
            if snap_export_id is not None:
                payload = store.export_backup(include_history=False, snapshot_id=snap_export_id)
                if not payload:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "Snapshot not found"})
                    return
                fname = f"mindbubble-snapshot-{snap_export_id}.json"
                self._send_json(
                    HTTPStatus.OK,
                    payload,
                    {"Content-Disposition": f'attachment; filename="{fname}"'},
                )
                return

            snap_id = self._parse_snapshot_id(path, "/api/snapshots/")
            if snap_id is not None:
                detail = store.get_snapshot_detail(snap_id)
                if not detail:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "Snapshot not found"})
                    return
                self._send_json(HTTPStatus.OK, detail)
                return

            if path == "/api/backup/export":
                inc = (qs.get("includeHistory") or ["1"])[0] != "0"
                payload = store.export_backup(include_history=inc)
                date_str = time.strftime("%Y%m%d-%H%M%S")
                suffix = "full" if inc else "workspace"
                fname = f"mindbubble-backup-{suffix}-{date_str}.json"
                self._send_json(
                    HTTPStatus.OK,
                    payload or {},
                    {"Content-Disposition": f'attachment; filename="{fname}"'},
                )
                return

            if path == "/api/gdrive/status":
                self._send_json(HTTPStatus.OK, gdrive.get_status(self._request_origin()))
                return

            if path == "/api/gdrive/auth/start":
                try:
                    self._send_json(HTTPStatus.OK, gdrive.start_auth(self._request_origin()))
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                return

            if path == "/api/gdrive/callback":
                code = (qs.get("code") or [""])[0]
                state_tok = (qs.get("state") or [""])[0]
                err_param = (qs.get("error") or [""])[0]
                try:
                    res = gdrive.handle_callback(code=code, state=state_tok, error=err_param)
                    payload_js = json.dumps({
                        "type": "mindbubble-gdrive-auth",
                        "ok": True,
                        "accountEmail": res.get("accountEmail", ""),
                        "accountName": res.get("accountName", ""),
                    }, ensure_ascii=False)
                    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>Google Drive 授权成功</title></head><body style="margin:0;padding:36px;background:#070d1c;color:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,sans-serif;text-align:center;"><h3>✓ Google Drive 授权成功</h3><p style="color:#94a3b8;">正在返回 MindBubble 设置页...</p><p><a href="/settings#backup" style="color:#38bdf8;">返回设置页</a></p><script>try{{if(window.opener){{window.opener.postMessage({payload_js},'*');window.close();}}else{{window.location.replace('/settings#backup');}}}}catch(e){{window.location.replace('/settings#backup');}}</script></body></html>"""
                    self._send_bytes(HTTPStatus.OK, "text/html; charset=utf-8", html.encode("utf-8"))
                except Exception as exc:
                    payload_js = json.dumps({
                        "type": "mindbubble-gdrive-auth",
                        "ok": False,
                        "error": str(exc),
                    }, ensure_ascii=False)
                    err_html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>Google Drive 授权失败</title></head><body style="margin:0;padding:36px;background:#070d1c;color:#fda4af;font-family:-apple-system,BlinkMacSystemFont,sans-serif;text-align:center;"><h3>✕ Google Drive 授权失败</h3><p style="color:#cbd5e1;">{str(exc).replace('<','&lt;')}</p><p><a href="/settings#backup" style="color:#38bdf8;">返回设置页</a></p><script>try{{if(window.opener){{window.opener.postMessage({payload_js},'*');}}}}catch(e){{}}</script></body></html>"""
                    self._send_bytes(HTTPStatus.BAD_REQUEST, "text/html; charset=utf-8", err_html.encode("utf-8"))
                return

            if path == "/api/gdrive/files":
                search_all = (qs.get("all") or ["0"])[0] == "1"
                limit_str = (qs.get("limit") or ["30"])[0]
                limit = int(limit_str) if limit_str.isdigit() else 30
                try:
                    self._send_json(HTTPStatus.OK, gdrive.list_files(search_all=search_all, limit=limit))
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_GATEWAY, {"ok": False, "error": str(exc)})
                return

            if path == "/api/mcp/info":
                origin = self._request_origin()
                stdio_entry = build_antigravity_mcp_entry(api_url=origin, db_path=store.db_path if hasattr(store, "db_path") else _MCP_DEFAULT_DB_PATH)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "serverName": "mindbubble",
                        "protocolVersion": "2025-03-26",
                        "streamableHttpUrl": f"{origin}/mcp",
                        "antigravity": get_antigravity_registration_status(),
                        "stdioConfig": {"mcpServers": {"mindbubble": stdio_entry}},
                        "httpConfig": {"mcpServers": {"mindbubble": {"url": f"{origin}/mcp"}}},
                        "tools": [
                            {"name": t["name"], "description": t["description"], "annotations": t.get("annotations", {})}
                            for t in MCP_TOOL_SPECS
                        ],
                        "resources": MCP_RESOURCE_SPECS,
                        "prompts": MCP_PROMPT_SPECS,
                    },
                )
                return

            if path == "/mcp":
                hdrs = {"Allow": "POST, DELETE, OPTIONS", **self._mcp_cors_headers()}
                self._send_json(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    {"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": "Use POST /mcp for JSON-RPC requests"}},
                    hdrs,
                )
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

        def _handle_state_upsert(self) -> None:
            try:
                payload = self._read_json_body()
                header_reason = unquote(self.headers.get("X-MindBubble-Reason", "") or "")
                body_reason = ""
                if isinstance(payload, dict) and isinstance(payload.get("_reason"), str):
                    body_reason = payload.pop("_reason")
                result = store.save_state(payload, reason=header_reason or body_reason)
                self._send_json(HTTPStatus.OK, result)
            except OverflowError as exc:
                self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": str(exc)})
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        def do_PUT(self) -> None:
            path = urlparse(self.path).path
            if path == "/api/state":
                self._handle_state_upsert()
                return
            if path == "/api/backup/config":
                try:
                    payload = self._read_json_body()
                    self._send_json(HTTPStatus.OK, store.update_config(payload))
                except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if path == "/api/gdrive/config":
                try:
                    payload = self._read_json_body()
                    self._send_json(HTTPStatus.OK, gdrive.save_config(payload, origin=self._request_origin()))
                except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                return
            if path.startswith("/api/contacts/"):
                cid = path[len("/api/contacts/"):].strip("/")
                try:
                    payload = self._read_json_body()
                    self._send_json(HTTPStatus.OK, store.upsert_contact(payload, contact_id=cid))
                except LookupError:
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Contact not found"})
                except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if path == "/api/state":
                self._handle_state_upsert()
                return
            if path == "/api/snapshots":
                try:
                    payload = self._read_json_body()
                    label = str(payload.get("label") or "") if isinstance(payload, dict) else ""
                    self._send_json(HTTPStatus.OK, store.create_manual_snapshot(label))
                except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return

            restore_id = self._parse_snapshot_id(path, "/api/snapshots/", "/restore")
            if restore_id is not None:
                res = store.restore_snapshot(restore_id)
                if not res:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "Snapshot not found"})
                    return
                self._send_json(HTTPStatus.OK, res)
                return

            if path == "/api/backup/import":
                try:
                    payload = self._read_json_body()
                    mode = "merge"
                    backup_data = payload
                    if isinstance(payload, dict) and "backup" in payload:
                        backup_data = payload["backup"]
                        mode = str(payload.get("mode") or "merge")
                    res = store.import_backup(backup_data, mode=mode)
                    self._send_json(HTTPStatus.OK, res)
                except OverflowError as exc:
                    self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": str(exc)})
                except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return

            if path == "/api/gdrive/disconnect":
                self._send_json(HTTPStatus.OK, gdrive.disconnect(origin=self._request_origin()))
                return

            if path == "/api/gdrive/backup":
                try:
                    payload = self._read_json_body() if self.headers.get("Content-Length") else {}
                    inc = bool(payload.get("includeHistory", True)) if isinstance(payload, dict) else True
                    cname = str(payload.get("fileName") or "") if isinstance(payload, dict) else ""
                    self._send_json(HTTPStatus.OK, gdrive.upload_backup(include_history=inc, custom_name=cname))
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_GATEWAY, {"ok": False, "error": str(exc)})
                return

            gd_preview_id = self._parse_gdrive_file_id(path, "/api/gdrive/files/", "/preview")
            if gd_preview_id is not None:
                try:
                    self._send_json(HTTPStatus.OK, gdrive.download_backup(gd_preview_id))
                except OverflowError as exc:
                    self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "error": str(exc)})
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_GATEWAY, {"ok": False, "error": str(exc)})
                return

            gd_import_id = self._parse_gdrive_file_id(path, "/api/gdrive/files/", "/import")
            if gd_import_id is not None:
                try:
                    payload = self._read_json_body() if self.headers.get("Content-Length") else {}
                    mode = str(payload.get("mode") or "merge") if isinstance(payload, dict) else "merge"
                    self._send_json(HTTPStatus.OK, gdrive.import_from_drive(gd_import_id, mode=mode))
                except OverflowError as exc:
                    self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "error": str(exc)})
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_GATEWAY, {"ok": False, "error": str(exc)})
                return

            if path.startswith("/api/gdrive/files/") and (path.endswith("/preview") or path.endswith("/import")):
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "无效的 Google Drive 文件 ID"})
                return

            if path == "/api/storage/vacuum":
                self._send_json(HTTPStatus.OK, store.vacuum())
                return

            if path == "/api/ai/cluster":
                try:
                    payload = self._read_json_body()
                    if not isinstance(payload, dict):
                        raise ValueError("Payload must be a JSON object")
                    res = agy_client.cluster_tasks(
                        tasks=payload.get("tasks"),
                        manual_links=payload.get("manualLinks"),
                        model=payload.get("model"),
                    )
                    self._send_json(HTTPStatus.OK, res)
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_GATEWAY, {"ok": False, "error": str(exc)})
                return

            if path == "/api/ai/daily-report":
                try:
                    payload = self._read_json_body()
                    if not isinstance(payload, dict):
                        raise ValueError("Payload must be a JSON object")
                    res = agy_client.generate_daily_report(
                        report_context=payload.get("reportContext"),
                        model=payload.get("model"),
                    )
                    self._send_json(HTTPStatus.OK, res)
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_GATEWAY, {"ok": False, "error": str(exc)})
                return

            if path == "/api/ai/critical-path":
                try:
                    payload = self._read_json_body()
                    if not isinstance(payload, dict):
                        raise ValueError("Payload must be a JSON object")
                    res = agy_client.generate_critical_path(
                        tasks=payload.get("tasks"),
                        manual_links=payload.get("manualLinks"),
                        ai_links=payload.get("aiLinks"),
                        target_task_id=payload.get("targetTaskId"),
                        model=payload.get("model"),
                    )
                    self._send_json(HTTPStatus.OK, res)
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_GATEWAY, {"ok": False, "error": str(exc)})
                return

            if path == "/api/rag/reindex":
                try:
                    status = rag.sync_all(store, force=True)
                    self._send_json(HTTPStatus.OK, {"ok": True, **status})
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                return

            if path == "/api/ai/bubble-chat":
                try:
                    payload = self._read_json_body()
                    if not isinstance(payload, dict):
                        raise ValueError("Payload must be a JSON object")
                    raw_ctx = payload.get("taskContext")
                    req_task_id = str(
                        payload.get("taskId")
                        or (raw_ctx.get("id") if isinstance(raw_ctx, dict) else "")
                        or ""
                    )
                    question_str = str(payload.get("question") or "")
                    use_rag = payload.get("useRag", True) is not False
                    rag_bundle = (
                        rag.build_bubble_rag_bundle(
                            store=store,
                            task_id=req_task_id,
                            task_context=raw_ctx if isinstance(raw_ctx, dict) else {},
                            question=question_str,
                            top_k=10,
                        )
                        if use_rag
                        else None
                    )
                    res = agy_client.bubble_chat(
                        task_context=raw_ctx,
                        messages=payload.get("messages"),
                        question=question_str,
                        model=payload.get("model"),
                        task_id=req_task_id,
                        reset_session=bool(payload.get("resetSession")),
                        rag_bundle=rag_bundle,
                    )
                    self._send_json(HTTPStatus.OK, res)
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_GATEWAY, {"ok": False, "error": str(exc)})
                return

            if path == "/api/attachments":
                try:
                    payload = self._read_json_body()
                    if not isinstance(payload, dict):
                        raise ValueError("Payload must be a JSON object")
                    b64 = str(payload.get("dataBase64") or "").strip()
                    if b64.startswith("data:") and "," in b64:
                        b64 = b64.split(",", 1)[1]
                    raw_bytes = base64.b64decode(b64, validate=True)
                    att = store.save_attachment(
                        name=str(payload.get("name") or "attachment"),
                        mime_type=str(payload.get("mimeType") or ""),
                        raw_bytes=raw_bytes,
                        task_id=str(payload.get("taskId") or ""),
                    )
                    self._send_json(HTTPStatus.OK, {"ok": True, "attachment": att})
                except OverflowError as exc:
                    self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "error": str(exc)})
                except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                return

            if path == "/api/mcp/install-antigravity":
                try:
                    origin = self._request_origin()
                    db_p = store.db_path if hasattr(store, "db_path") else _MCP_DEFAULT_DB_PATH
                    res = install_into_antigravity(api_url=origin, db_path=db_p)
                    self._send_json(HTTPStatus.OK, res)
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                return

            if path == "/api/contacts":
                try:
                    payload = self._read_json_body()
                    self._send_json(HTTPStatus.OK, store.upsert_contact(payload))
                except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                return

            if path == "/api/contacts/import":
                try:
                    payload = self._read_json_body()
                    mode = "merge"
                    default_source = "import"
                    items: list[dict[str, Any]] = []
                    if isinstance(payload, list):
                        items = [x for x in payload if isinstance(x, dict)]
                    elif isinstance(payload, dict):
                        mode = str(payload.get("mode") or "merge")
                        default_source = str(payload.get("source") or "import")
                        if isinstance(payload.get("csvText"), str):
                            items = _parse_contacts_csv(payload["csvText"])
                        elif isinstance(payload.get("contacts"), list):
                            items = [x for x in payload["contacts"] if isinstance(x, dict)]
                        else:
                            raise ValueError("Payload must include 'contacts' list or 'csvText'")
                    else:
                        raise ValueError("Invalid contacts import payload")
                    res = store.import_contacts(items, mode=mode, default_source=default_source)
                    self._send_json(HTTPStatus.OK, res)
                except OverflowError as exc:
                    self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "error": str(exc)})
                except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                return

            if path == "/api/contacts/lark/sync":
                try:
                    from export_lark_contacts import export_contacts_files, fetch_lark_contacts

                    lark_res = fetch_lark_contacts()
                    contacts = lark_res["contacts"]
                    export_dir = store.db_path.parent if hasattr(store, "db_path") else (BASE_DIR / "data")
                    files = export_contacts_files(contacts, export_dir)
                    imp = store.import_contacts(contacts, mode="merge", default_source="lark")
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            **imp,
                            "method": lark_res.get("method"),
                            "sourceChatsCount": len(lark_res.get("sourceChats") or []),
                            **files,
                        },
                    )
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                return

            if path == "/mcp":
                cors = self._mcp_cors_headers()
                try:
                    payload = self._read_json_body()
                except OverflowError as exc:
                    self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": str(exc)}}, cors)
                    return
                except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}, cors)
                    return
                status, body_obj, rpc_headers = mcp_rpc.handle_request(
                    payload,
                    session_id=self.headers.get("Mcp-Session-Id"),
                )
                merged_headers = {**cors, **rpc_headers}
                if body_obj is None:
                    self._send_empty(status, merged_headers)
                else:
                    self._send_json(status, body_obj, merged_headers)
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

        def do_DELETE(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)

            if path == "/api/state":
                guard = (qs.get("guard") or ["1"])[0] != "0"
                self._send_json(HTTPStatus.OK, store.clear_state(save_guard=guard))
                return

            if path == "/api/snapshots":
                kind = (qs.get("kind") or ["auto"])[0]
                try:
                    self._send_json(HTTPStatus.OK, store.purge_snapshots(kind))
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return

            snap_id = self._parse_snapshot_id(path, "/api/snapshots/")
            if snap_id is not None:
                ok = store.delete_snapshot(snap_id)
                if not ok:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "Snapshot not found"})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, "deletedId": snap_id})
                return

            gd_del_id = self._parse_gdrive_file_id(path, "/api/gdrive/files/")
            if gd_del_id is not None:
                try:
                    self._send_json(HTTPStatus.OK, gdrive.delete_file(gd_del_id))
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_GATEWAY, {"ok": False, "error": str(exc)})
                return

            if path.startswith("/api/gdrive/files/"):
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "无效的 Google Drive 文件 ID"})
                return

            if path.startswith("/api/attachments/"):
                att_id = path[len("/api/attachments/"):].strip("/")
                if not ATT_ID_RE.match(att_id):
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid attachment ID"})
                    return
                if not store.delete_attachment(att_id):
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Attachment not found"})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, "deletedId": att_id})
                return

            if path == "/mcp":
                mcp_rpc.close_session(self.headers.get("Mcp-Session-Id"))
                self._send_empty(HTTPStatus.NO_CONTENT, self._mcp_cors_headers())
                return

            if path == "/api/contacts":
                src = (qs.get("source") or [""])[0]
                try:
                    self._send_json(HTTPStatus.OK, store.clear_contacts(source=src))
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                return

            if path.startswith("/api/contacts/"):
                cid = path[len("/api/contacts/"):].strip("/")
                if not CONTACT_ID_RE.match(cid):
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid contact ID"})
                    return
                if not store.delete_contact(cid):
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Contact not found"})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, "deletedId": cid})
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    return MindBubbleHandler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MindBubble 3D TodoList SQLite Server")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Bind port (default: 8765)")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite DB file path")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    store = MindBubbleStore(args.db)
    agy_client = AgyClient()
    gdrive_client = GoogleDriveClient(store)
    rag_engine = MindBubbleRagEngine(args.db)
    store.add_change_listener(lambda reason: rag_engine.schedule_sync(store, reason=reason))
    threading.Thread(target=rag_engine.sync_all, args=(store,), daemon=True).start()
    threading.Thread(target=agy_client.prewarm, args=(DEFAULT_AGY_MODEL,), daemon=True).start()

    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((args.host, args.port), make_handler(store, agy_client, gdrive_client, rag_engine))

    def shutdown_handler(signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    print(
        f"MindBubble server listening on http://{args.host}:{args.port} (db: {args.db}, agy: {agy_client.is_available()})",
        flush=True,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
        rag_engine.close()
        agy_client.close()
        store.close()


if __name__ == "__main__":
    main()
