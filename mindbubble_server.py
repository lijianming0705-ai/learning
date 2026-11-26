#!/usr/bin/env python3
"""Lightweight SQLite-backed HTTP service for MindBubble 3D TodoList with Time Machine backups."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import queue
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

MAX_BODY_BYTES = 20 * 1024 * 1024
BASE_DIR = Path(__file__).resolve().parent
HTML_PATH = BASE_DIR / "mind-bubble-todo.html"
SETTINGS_HTML_PATH = BASE_DIR / "mind-bubble-settings.html"
DEFAULT_DB_PATH = BASE_DIR / "data" / "mindbubble.sqlite"
AGY_WORKDIR = BASE_DIR / "data" / "mindbubble-agy-workdir"
DEFAULT_AGY_MODEL = "gemini-4-hydrogen-medium"
_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_MODEL_LINE_RE = re.compile(r"^([a-z0-9][a-z0-9._-]{2,})\s+(.+)$")

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


def sanitize_state(data: Any) -> dict[str, Any]:
    """Validate and normalize incoming workspace JSON payload."""
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        raise ValueError("Payload must be a JSON object with a 'tasks' list")

    prefs = data.get("prefs") if isinstance(data.get("prefs"), dict) else {}
    clean_prefs = {k: v for k, v in prefs.items() if k != "llmApiKey"}
    tasks = data["tasks"]

    return {
        "tasks": tasks,
        "manualLinks": data.get("manualLinks") if isinstance(data.get("manualLinks"), list) else [],
        "clusters": data.get("clusters") if isinstance(data.get("clusters"), list) else [],
        "aiLinks": data.get("aiLinks") if isinstance(data.get("aiLinks"), list) else [],
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
    return (
        t.get("title"),
        t.get("notes"),
        tuple(t.get("tags") or []),
        t.get("priority"),
        bool(t.get("completed")),
        t.get("top3Slot"),
        t.get("clusterId"),
        t.get("dueAt"),
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
        self._init_db()

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
        return {
            "ok": True,
            "db": str(self.db_path),
            "revision": rev,
            "updatedAt": updated_at,
            "snapshotCount": snap_count,
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

        return {
            "format": "mindbubble-backup",
            "version": 2,
            "mode": "full" if include_history else "workspace",
            "exportedAt": now_ms,
            "revision": rev,
            "backupConfig": cfg,
            "currentState": live_state,
            "snapshots": snaps_out,
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

        return {
            "ok": True,
            "revision": new_rev,
            "updatedAt": updated_at,
            "importedTasks": len(target_state.get("tasks", [])),
            "importedSnapshots": imported_snaps,
            "state": target_state,
        }

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
        if mid == "gemini-4-hydrogen-medium":
            return (0, mid)
        if mid == "gemini-3.8-flash-low":
            return (1, mid)
        if "hydrogen" in mid:
            return (2, mid)
        if "flash" in mid and mid.endswith("-low"):
            return (3, mid)
        if "flash" in mid:
            return (4, mid)
        return (5, mid)

    indexed = list(enumerate(models))
    indexed.sort(key=lambda item: (priority(item[1])[0], item[0]))
    return [m for _, m in indexed]


class AgyWorker:
    """Persistent stream-json worker process for antigravity-cli (`agy`)."""

    def __init__(self, agy_bin: str, model: str, workdir: Path) -> None:
        self.model = model
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

                ev_type = ev.get("event")
                if ev_type == "step_update":
                    su = ev.get("step_update") or {}
                    if su.get("step_type") == "agent_response":
                        delta = su.get("text_delta") or ""
                        if delta:
                            chunks.append(delta)
                elif ev_type == "result":
                    res = ev.get("result") or {}
                    if res.get("status") != "SUCCESS":
                        raise RuntimeError(res.get("error") or f"agy returned status {res.get('status')}")
                    text = "".join(chunks).strip()
                    if not text:
                        text = str(res.get("response") or res.get("text") or "").strip()
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
                        if mid not in seen:
                            seen.add(mid)
                            parsed.append({"id": mid, "name": name})
                if parsed:
                    self._models_cache = _sort_models_fastest_first(parsed)
                    self._models_cached_at = now
            except Exception:
                pass

        fallback_models = [
            {"id": "gemini-4-hydrogen-medium", "name": "Gemini 4 Hydrogen (Medium)"},
            {"id": "gemini-3.8-flash-low", "name": "Gemini 3.8 Flash (Low)"},
            {"id": "gemini-4-neon-high", "name": "Gemini 4 Neon (High)"},
        ]
        return {
            "available": agy_bin is not None,
            "agyPath": agy_bin,
            "defaultModel": DEFAULT_AGY_MODEL,
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

    def _ask_with_retry(self, prompt: str, model: str) -> str:
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
                return worker.ask(prompt, timeout=90.0)
            except Exception:
                with self._pool_lock:
                    if self._worker is worker:
                        self._worker.close()
                        self._worker = None
                if attempt == 1:
                    raise

        raise RuntimeError("agy invocation failed")

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

    def close(self) -> None:
        with self._pool_lock:
            if self._worker:
                self._worker.close()
                self._worker = None


def make_handler(store: MindBubbleStore, agy_client: AgyClient) -> type[BaseHTTPRequestHandler]:
    class MindBubbleHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

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
                self._send_json(HTTPStatus.OK, health)
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
    threading.Thread(target=agy_client.prewarm, args=(DEFAULT_AGY_MODEL,), daemon=True).start()

    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((args.host, args.port), make_handler(store, agy_client))

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
        agy_client.close()
        store.close()


if __name__ == "__main__":
    main()
