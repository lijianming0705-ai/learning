#!/usr/bin/env python3
"""Pure-stdlib MCP domain core, JSON-RPC handler, and Antigravity installer for MindBubble."""

from __future__ import annotations

import datetime as _dt
import json
import math
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "mindbubble.sqlite"
DEFAULT_API_URL = "http://127.0.0.1:8765"
MCP_SCRIPT_PATH = BASE_DIR / "mindbubble_mcp.py"
ANTIGRAVITY_MCP_CONFIG_PATH = Path.home() / ".gemini" / "config" / "mcp_config.json"

DEFAULT_GALAXY_ID = "gal_default"
DEFAULT_GALAXY: dict[str, Any] = {
    "id": DEFAULT_GALAXY_ID,
    "name": "默认星系",
    "icon": "🪐",
    "color": "#38bdf8",
    "description": "默认空间 · 自动收纳既有与未归类气泡",
    "createdAt": 0,
    "isDefault": True,
}

MCP_PROTOCOL_VERSION = "2025-03-26"
MCP_SERVER_NAME = "mindbubble"
MCP_SERVER_VERSION = "1.0.0"

PRIORITY_WEIGHTS = {"critical": 3.0, "high": 2.0, "normal": 1.0}


def _default_python_bin() -> str:
    exe = sys.executable or ""
    if exe and Path(exe).exists():
        return exe
    return "python3"


def _uid(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(5)}"


def _now_ms() -> int:
    return int(time.time() * 1000)


def parse_due_at(value: Any) -> int | None:
    """Parse epoch ms, YYYY-MM-DD, YYYY-MM-DD HH:MM, or ISO-8601 into epoch ms."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("due_at cannot be a boolean")
    if isinstance(value, (int, float)):
        num = float(value)
        if not math.isfinite(num) or num <= 0:
            raise ValueError("due_at timestamp must be positive")
        # Treat < 1e11 (~year 5138 in seconds) as seconds and normalize to ms.
        return int(num * 1000) if num < 1e11 else int(num)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return parse_due_at(int(text))
        for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = _dt.datetime.strptime(text, fmt)
                if fmt == "%Y-%m-%d":
                    parsed = parsed.replace(hour=18, minute=0, second=0, microsecond=0)
                return int(parsed.timestamp() * 1000)
            except ValueError:
                pass
        iso = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            parsed_iso = _dt.datetime.fromisoformat(iso)
            return int(parsed_iso.timestamp() * 1000)
        except ValueError as exc:
            raise ValueError(
                f"Invalid due_at format: {value!r}. Expected epoch ms, YYYY-MM-DD, YYYY-MM-DD HH:MM, or ISO-8601."
            ) from exc
    raise ValueError(f"Unsupported due_at type: {type(value).__name__}")


def _format_local_ts(ms: int | None) -> str | None:
    if not ms:
        return None
    return _dt.datetime.fromtimestamp(ms / 1000.0).strftime("%Y-%m-%d %H:%M:%S")


def _local_date_key(ms: int | None) -> str | None:
    if not ms:
        return None
    return _dt.datetime.fromtimestamp(ms / 1000.0).strftime("%Y-%m-%d")


def _sanitize_galaxies(state: dict[str, Any]) -> list[dict[str, Any]]:
    raw = state.get("galaxies")
    if not isinstance(raw, list) or not raw:
        raw = (state.get("prefs") or {}).get("galaxies")
    if not isinstance(raw, list):
        raw = []
    by_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        gid = str(item.get("id") or "").strip()[:64]
        if not gid or gid == "__universe__" or gid in by_id:
            continue
        is_def = gid == DEFAULT_GALAXY_ID
        by_id[gid] = {
            "id": gid,
            "name": str(item.get("name") or (DEFAULT_GALAXY["name"] if is_def else f"星系 {len(order)+1}")).strip()[:32],
            "icon": str(item.get("icon") or (DEFAULT_GALAXY["icon"] if is_def else "✨")).strip()[:8],
            "color": str(item.get("color") or DEFAULT_GALAXY["color"]).strip()[:16],
            "description": str(item.get("description") or (DEFAULT_GALAXY["description"] if is_def else "")).strip()[:120],
            "createdAt": int(item.get("createdAt") or 0),
            "isDefault": is_def,
        }
        order.append(gid)
    default_entry = by_id.get(DEFAULT_GALAXY_ID) or dict(DEFAULT_GALAXY)
    default_entry["id"] = DEFAULT_GALAXY_ID
    default_entry["isDefault"] = True
    result = [default_entry] + [by_id[gid] for gid in order if gid != DEFAULT_GALAXY_ID]
    return result


def _clean_tags(raw_tags: Any) -> list[str]:
    if not isinstance(raw_tags, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw_tags:
        if not isinstance(item, str):
            continue
        clean = item.strip().lstrip("#").strip()[:32]
        if clean and clean.lower() not in seen:
            seen.add(clean.lower())
            out.append(clean)
    return out


class MindBubbleBackend:
    """Dual-mode persistence backend: HTTP-first (live sync) with SQLite WAL fallback."""

    def __init__(
        self,
        *,
        store: Any = None,
        agy_client: Any = None,
        api_url: str | None = None,
        db_path: Path | str | None = None,
    ) -> None:
        self._store = store
        self._agy_client = agy_client
        self.api_url = (api_url or os.environ.get("MINDBUBBLE_API_URL") or DEFAULT_API_URL).rstrip("/")
        self.db_path = Path(db_path or os.environ.get("MINDBUBBLE_DB_PATH") or DEFAULT_DB_PATH)
        self._fallback_store: Any = None
        self._lock = threading.RLock()
        self.last_source: str = "direct" if store is not None else "uninitialized"

    def _get_fallback_store(self) -> Any:
        with self._lock:
            if self._store is not None:
                return self._store
            if self._fallback_store is None:
                from mindbubble_server import MindBubbleStore

                self._fallback_store = MindBubbleStore(self.db_path)
            return self._fallback_store

    def _http_json(
        self,
        path: str,
        method: str = "GET",
        payload: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
    ) -> Any:
        url = f"{self.api_url}{path}"
        req_headers = {"Accept": "application/json"}
        body_bytes: bytes | None = None
        if payload is not None:
            body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req_headers["Content-Type"] = "application/json; charset=utf-8"
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(url, data=body_bytes, headers=req_headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}

    def get_state(self) -> dict[str, Any]:
        if self._store is not None:
            self.last_source = "direct"
            return self._store.get_state()
        try:
            res = self._http_json("/api/state", method="GET")
            self.last_source = "http"
            return res
        except (urllib.error.URLError, OSError, TimeoutError):
            self.last_source = "sqlite"
            return self._get_fallback_store().get_state()

    def save_state(self, state: dict[str, Any], reason: str = "") -> dict[str, Any]:
        if self._store is not None:
            self.last_source = "direct"
            return self._store.save_state(state, reason=reason)
        try:
            hdrs = {"X-MindBubble-Reason": urllib.parse.quote(reason)} if reason else {}
            res = self._http_json("/api/state", method="PUT", payload=state, headers=hdrs)
            self.last_source = "http"
            return res
        except (urllib.error.URLError, OSError, TimeoutError):
            self.last_source = "sqlite"
            return self._get_fallback_store().save_state(state, reason=reason)

    def list_snapshots(self, kind: str = "all", limit: int = 50) -> dict[str, Any]:
        if self._store is not None:
            self.last_source = "direct"
            return self._store.list_snapshots(kind=kind, limit=limit)
        try:
            res = self._http_json(
                f"/api/snapshots?kind={urllib.parse.quote(kind)}&limit={int(limit)}",
                method="GET",
            )
            self.last_source = "http"
            return res
        except (urllib.error.URLError, OSError, TimeoutError):
            self.last_source = "sqlite"
            return self._get_fallback_store().list_snapshots(kind=kind, limit=limit)

    def create_manual_snapshot(self, label: str = "") -> dict[str, Any]:
        if self._store is not None:
            self.last_source = "direct"
            return self._store.create_manual_snapshot(label)
        try:
            res = self._http_json("/api/snapshots", method="POST", payload={"label": label})
            self.last_source = "http"
            return res
        except (urllib.error.URLError, OSError, TimeoutError):
            self.last_source = "sqlite"
            return self._get_fallback_store().create_manual_snapshot(label)

    def restore_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        if self._store is not None:
            self.last_source = "direct"
            return self._store.restore_snapshot(snapshot_id)
        try:
            res = self._http_json(f"/api/snapshots/{int(snapshot_id)}/restore", method="POST", payload={})
            self.last_source = "http"
            return res
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        except (urllib.error.URLError, OSError, TimeoutError):
            self.last_source = "sqlite"
            return self._get_fallback_store().restore_snapshot(snapshot_id)

    def storage_info(self) -> dict[str, Any]:
        if self._store is not None:
            self.last_source = "direct"
            return self._store.storage_info()
        try:
            res = self._http_json("/api/storage/info", method="GET")
            self.last_source = "http"
            return res
        except (urllib.error.URLError, OSError, TimeoutError):
            self.last_source = "sqlite"
            return self._get_fallback_store().storage_info()

    def ai_critical_path(
        self,
        tasks: list[dict[str, Any]],
        manual_links: list[dict[str, Any]],
        ai_links: list[dict[str, Any]],
        target_task_id: str | None = None,
    ) -> dict[str, Any] | None:
        if self._agy_client is not None:
            if not self._agy_client.is_available():
                return None
            return self._agy_client.generate_critical_path(
                tasks=tasks,
                manual_links=manual_links,
                ai_links=ai_links,
                target_task_id=target_task_id,
            )
        try:
            res = self._http_json(
                "/api/ai/critical-path",
                method="POST",
                payload={
                    "tasks": tasks,
                    "manualLinks": manual_links,
                    "aiLinks": ai_links,
                    "targetTaskId": target_task_id,
                },
                timeout=45.0,
            )
            self.last_source = "http"
            return res
        except Exception:
            return None


TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "get_workspace_overview",
        "description": "获取 MindBubble 全局概览：修订号、数据连接来源、各星系气泡统计、Top-3 核心要务及标签目录。",
        "annotations": {"readOnlyHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "list_galaxies",
        "description": "列出所有星系（默认星系及自定义星系）及其任务数、已完成数、进行中数和 Top-3 状态。",
        "annotations": {"readOnlyHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "create_galaxy",
        "description": "创建一个新的 MindBubble 星系用于隔离不同场景或项目。",
        "annotations": {"readOnlyHint": False},
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "星系名称（必填，最多 32 字）"},
                "icon": {"type": "string", "description": "Emoji 图标，默认 🚀"},
                "color": {"type": "string", "description": "主题十六进制颜色，例如 #a78bfa"},
                "description": {"type": "string", "description": "星系描述"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "update_galaxy",
        "description": "更新已有星系的名称、图标、颜色或描述（支持按星系 ID 或名称定位）。",
        "annotations": {"readOnlyHint": False},
        "inputSchema": {
            "type": "object",
            "properties": {
                "galaxy": {"type": "string", "description": "星系 ID 或名称"},
                "name": {"type": "string"},
                "icon": {"type": "string"},
                "color": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["galaxy"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_tasks",
        "description": "按星系、完成状态、标签、优先级、聚类、Top-3 或关键词筛选任务气泡列表。",
        "annotations": {"readOnlyHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {
                "galaxy": {"type": "string", "description": "星系 ID 或名称；留空则跨全部星系筛选"},
                "status": {"type": "string", "enum": ["active", "completed", "all"], "description": "默认 active"},
                "tag": {"type": "string", "description": "按标签名筛选（可不带 #）"},
                "priority": {"type": "string", "enum": ["normal", "high", "critical"]},
                "cluster_id": {"type": "string"},
                "top3_only": {"type": "boolean", "description": "是否仅返回核心要务 Top-3"},
                "search": {"type": "string", "description": "按标题/备注/标签子串筛选"},
                "limit": {"type": "integer", "description": "最多返回条数，默认 100"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_task",
        "description": "获取单个气泡详情，包含关联的连线、所属星系、所属聚类及可读时间戳。",
        "annotations": {"readOnlyHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "气泡 ID"},
                "title_query": {"type": "string", "description": "当不知道 ID 时按标题精确或唯一模糊匹配"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "create_task",
        "description": "创建一个新思维气泡，支持指定星系、标签、优先级、截止时间、置顶 Top-3，以及与已有气泡自动连线。",
        "annotations": {"readOnlyHint": False},
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "气泡标题（必填）"},
                "notes": {"type": "string", "description": "备注正文"},
                "galaxy": {"type": "string", "description": "目标星系 ID 或名称，默认 gal_default"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "标签列表"},
                "priority": {"type": "string", "enum": ["normal", "high", "critical"], "description": "默认 normal"},
                "due_at": {"type": ["string", "number"], "description": "截止时间：毫秒时间戳、YYYY-MM-DD、YYYY-MM-DD HH:MM 或 ISO-8601"},
                "pin_top3": {"type": "boolean", "description": "是否自动占用目标星系空闲 Top-3 槽位"},
                "top3_slot": {"type": "integer", "description": "显式指定 Top-3 槽位 0/1/2"},
                "cluster_id": {"type": "string", "description": "关联的聚类 ID"},
                "link_to_task_id": {"type": "string", "description": "若提供已有气泡 ID，将在其附近生成并自动建立手动连线"},
            },
            "required": ["title"],
            "additionalProperties": False,
        },
    },
    {
        "name": "update_task",
        "description": "更新气泡的标题、备注、标签、优先级、截止时间、星系、聚类或 Top-3 槽位。",
        "annotations": {"readOnlyHint": False},
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "title": {"type": "string"},
                "notes": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "priority": {"type": "string", "enum": ["normal", "high", "critical"]},
                "due_at": {"type": ["string", "number", "null"], "description": "传 null 或空字符串可清除截止时间"},
                "galaxy": {"type": "string"},
                "cluster_id": {"type": ["string", "null"]},
                "top3_slot": {"type": ["integer", "null"], "description": "0/1/2，传 null 可取消置顶"},
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "complete_task",
        "description": "将气泡标记为已完成（自动记录 completedAt 时间戳）或恢复为进行中。",
        "annotations": {"readOnlyHint": False},
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "completed": {"type": "boolean", "description": "true 为完成，false 为恢复进行中，默认 true"},
                "completed_at": {"type": ["string", "number"], "description": "可选自定义完成时间，留空取当前时间"},
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "delete_task",
        "description": "删除指定气泡及其关联连线（后台 Time Machine 会自动留痕保护）。",
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "move_tasks_to_galaxy",
        "description": "批量将气泡（及可选聚类）迁移到目标星系，保留全部手动连线并自动清理跨星系 AI 虚线。",
        "annotations": {"readOnlyHint": False},
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_ids": {"type": "array", "items": {"type": "string"}},
                "target_galaxy": {"type": "string", "description": "目标星系 ID 或名称"},
                "cluster_ids": {"type": "array", "items": {"type": "string"}, "description": "可选同时迁移的聚类 ID 列表"},
            },
            "required": ["task_ids", "target_galaxy"],
            "additionalProperties": False,
        },
    },
    {
        "name": "link_tasks",
        "description": "在两个气泡之间创建手动连线（支持跨星系虫洞连线与自定义连线说明标签）。",
        "annotations": {"readOnlyHint": False},
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_id": {"type": "string"},
                "target_id": {"type": "string"},
                "label": {"type": "string", "description": "可选连线文字标签"},
            },
            "required": ["source_id", "target_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "unlink_tasks",
        "description": "移除两个气泡之间的手动连线，或根据 link_id 直接移除。",
        "annotations": {"readOnlyHint": False},
        "inputSchema": {
            "type": "object",
            "properties": {
                "link_id": {"type": "string"},
                "source_id": {"type": "string"},
                "target_id": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_critical_path",
        "description": "计算指定星系或工作区的核心推进关键路径（支持基于连线与优先级/截止时间的本地分析或 AGY 智能生成）。",
        "annotations": {"readOnlyHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {
                "galaxy": {"type": "string", "description": "星系 ID 或名称；留空包含全部星系"},
                "target_task_id": {"type": "string", "description": "可选重点聚焦目标任务 ID"},
                "use_ai": {"type": "boolean", "description": "是否优先尝试 AGY 智能生成，默认 false"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_daily_report",
        "description": "生成指定日期（默认今天）的复盘报告数据：当日完成气泡、进行中 Top-3、已逾期与即将到期项、按星系分布统计。",
        "annotations": {"readOnlyHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "日期 YYYY-MM-DD，默认当天"},
                "galaxy": {"type": "string", "description": "可选按星系筛选"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "list_snapshots",
        "description": "列出 Time Machine 历史快照列表（支持按 manual/auto/guard/all 筛选）。",
        "annotations": {"readOnlyHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["all", "manual", "auto", "guard"]},
                "limit": {"type": "integer", "description": "默认 30"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "create_snapshot",
        "description": "在后台 SQLite Time Machine 中立即创建一条手动备份快照。",
        "annotations": {"readOnlyHint": False},
        "inputSchema": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "手动备份名称"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "restore_snapshot",
        "description": "恢复 Time Machine 历史快照（恢复前会自动保存保护点；需传入 confirm=true 防误触）。",
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {
                "snapshot_id": {"type": "integer"},
                "confirm": {"type": "boolean", "description": "必须为 true 才会执行恢复"},
            },
            "required": ["snapshot_id", "confirm"],
            "additionalProperties": False,
        },
    },
]

RESOURCE_SPECS: list[dict[str, Any]] = [
    {
        "uri": "mindbubble://overview",
        "name": "MindBubble Workspace Overview",
        "description": "实时工作区统计、星系列表、核心 Top-3 与标签目录",
        "mimeType": "application/json",
    },
    {
        "uri": "mindbubble://galaxies",
        "name": "MindBubble Galaxies",
        "description": "全部星系定义及各星系气泡分布",
        "mimeType": "application/json",
    },
    {
        "uri": "mindbubble://tasks/active",
        "name": "Active Tasks",
        "description": "当前所有进行中气泡列表",
        "mimeType": "application/json",
    },
]

PROMPT_SPECS: list[dict[str, Any]] = [
    {
        "name": "daily_planning",
        "description": "结合 MindBubble 今日复盘数据与关键路径，制定当日行动优先级计划。",
        "arguments": [
            {"name": "galaxy", "description": "可选目标星系 ID 或名称", "required": False},
            {"name": "date", "description": "可选日期 YYYY-MM-DD，默认今天", "required": False},
        ],
    },
    {
        "name": "organize_galaxy",
        "description": "梳理指定星系下的进行中气泡、标签归类、连线关系与 Top-3 占位建议。",
        "arguments": [
            {"name": "galaxy", "description": "星系 ID 或名称，默认 gal_default", "required": False},
        ],
    },
]


class MindBubbleMcpService:
    """Business logic powering all MCP tools, resources, and prompts."""

    def __init__(self, backend: MindBubbleBackend) -> None:
        self.backend = backend

    def _load_bundle(self) -> tuple[dict[str, Any], int, int]:
        bundle = self.backend.get_state()
        state = bundle.get("state") if isinstance(bundle, dict) else None
        if not isinstance(state, dict):
            state = {"tasks": [], "manualLinks": [], "clusters": [], "aiLinks": [], "tagCatalog": [], "prefs": {}}
        galaxies = _sanitize_galaxies(state)
        state = dict(state)
        state["galaxies"] = galaxies
        prefs = dict(state.get("prefs") or {})
        prefs["galaxies"] = galaxies
        state["prefs"] = prefs
        state.setdefault("tasks", [])
        state.setdefault("manualLinks", [])
        state.setdefault("clusters", [])
        state.setdefault("aiLinks", [])
        state.setdefault("tagCatalog", [])
        return state, int(bundle.get("revision") or 0), int(bundle.get("updatedAt") or 0)

    def _save_state(self, state: dict[str, Any], reason: str) -> dict[str, Any]:
        galaxies = _sanitize_galaxies(state)
        state["galaxies"] = galaxies
        prefs = dict(state.get("prefs") or {})
        prefs["galaxies"] = galaxies
        state["prefs"] = prefs
        # Auto-merge tags into tagCatalog
        catalog = _clean_tags(state.get("tagCatalog") or [])
        seen = {c.lower() for c in catalog}
        for t in state.get("tasks") or []:
            for tg in _clean_tags((t or {}).get("tags") if isinstance(t, dict) else []):
                if tg.lower() not in seen and len(catalog) < 200:
                    seen.add(tg.lower())
                    catalog.append(tg)
        state["tagCatalog"] = catalog
        return self.backend.save_state(state, reason=reason)

    @staticmethod
    def _resolve_galaxy(galaxies: list[dict[str, Any]], ref: str | None) -> dict[str, Any] | None:
        if not ref:
            return None
        key = str(ref).strip()
        if not key:
            return None
        for g in galaxies:
            if g["id"] == key:
                return g
        low = key.lower()
        for g in galaxies:
            if str(g.get("name") or "").strip().lower() == low:
                return g
        return None

    @staticmethod
    def _format_task(t: dict[str, Any], galaxy_map: dict[str, dict[str, Any]], cluster_map: dict[str, dict[str, Any]]) -> dict[str, Any]:
        gid = t.get("galaxyId") or DEFAULT_GALAXY_ID
        g = galaxy_map.get(gid) or DEFAULT_GALAXY
        cid = t.get("clusterId")
        cl = cluster_map.get(cid) if cid else None
        return {
            "id": t.get("id"),
            "title": t.get("title") or "",
            "notes": t.get("notes") or "",
            "tags": list(t.get("tags") or []),
            "priority": t.get("priority") or "normal",
            "completed": bool(t.get("completed")),
            "completedAt": t.get("completedAt"),
            "completedAtStr": _format_local_ts(t.get("completedAt")),
            "top3Slot": t.get("top3Slot"),
            "clusterId": cid,
            "clusterName": cl.get("name") if cl else None,
            "galaxyId": gid,
            "galaxyName": g.get("name"),
            "dueAt": t.get("dueAt"),
            "dueAtStr": _format_local_ts(t.get("dueAt")),
            "createdAt": t.get("createdAt"),
            "createdAtStr": _format_local_ts(t.get("createdAt")),
        }

    # ---------- Tools ----------
    def get_workspace_overview(self) -> dict[str, Any]:
        state, rev, updated_at = self._load_bundle()
        galaxies = state["galaxies"]
        tasks = state["tasks"]
        galaxy_map = {g["id"]: g for g in galaxies}
        cluster_map = {c["id"]: c for c in state["clusters"] if isinstance(c, dict) and c.get("id")}
        per_galaxy = []
        for g in galaxies:
            gt = [t for t in tasks if (t.get("galaxyId") or DEFAULT_GALAXY_ID) == g["id"]]
            active = [t for t in gt if not t.get("completed")]
            top3 = sorted(
                [t for t in active if t.get("top3Slot") in (0, 1, 2)],
                key=lambda x: x["top3Slot"],
            )
            per_galaxy.append({
                "id": g["id"],
                "name": g["name"],
                "icon": g["icon"],
                "color": g["color"],
                "totalTasks": len(gt),
                "activeTasks": len(active),
                "completedTasks": len(gt) - len(active),
                "top3": [{"slot": t["top3Slot"], "id": t["id"], "title": t.get("title")} for t in top3],
            })
        top3_all = [
            self._format_task(t, galaxy_map, cluster_map)
            for t in tasks
            if not t.get("completed") and t.get("top3Slot") in (0, 1, 2)
        ]
        top3_all.sort(key=lambda x: (x["galaxyId"], x["top3Slot"]))
        return {
            "revision": rev,
            "updatedAt": updated_at,
            "updatedAtStr": _format_local_ts(updated_at),
            "transportSource": self.backend.last_source,
            "counts": {
                "tasks": len(tasks),
                "active": sum(1 for t in tasks if not t.get("completed")),
                "completed": sum(1 for t in tasks if t.get("completed")),
                "galaxies": len(galaxies),
                "clusters": len(state["clusters"]),
                "manualLinks": len(state["manualLinks"]),
                "aiLinks": len(state["aiLinks"]),
            },
            "tagCatalog": list(state.get("tagCatalog") or []),
            "galaxies": per_galaxy,
            "top3Tasks": top3_all,
        }

    def list_galaxies(self) -> dict[str, Any]:
        state, rev, _ = self._load_bundle()
        galaxies = state["galaxies"]
        tasks = state["tasks"]
        clusters = state["clusters"]
        items = []
        for g in galaxies:
            gt = [t for t in tasks if (t.get("galaxyId") or DEFAULT_GALAXY_ID) == g["id"]]
            active = sum(1 for t in gt if not t.get("completed"))
            g_clusters = [c for c in clusters if (c.get("galaxyId") or DEFAULT_GALAXY_ID) == g["id"]]
            items.append({
                **g,
                "totalTasks": len(gt),
                "activeTasks": active,
                "completedTasks": len(gt) - active,
                "clusterCount": len(g_clusters),
            })
        return {"revision": rev, "transportSource": self.backend.last_source, "galaxies": items}

    def create_galaxy(
        self,
        name: str,
        icon: str = "🚀",
        color: str = "#a78bfa",
        description: str = "",
    ) -> dict[str, Any]:
        clean_name = str(name or "").strip()[:32]
        if not clean_name:
            raise ValueError("Galaxy name is required")
        state, _, _ = self._load_bundle()
        clean_icon = str(icon or "🚀").strip()[:8] or "✨"
        clean_color = str(color or "#a78bfa").strip()[:16]
        if not clean_color.startswith("#"):
            clean_color = "#a78bfa"
        galaxy = {
            "id": _uid("gal"),
            "name": clean_name,
            "icon": clean_icon,
            "color": clean_color,
            "description": str(description or "").strip()[:120],
            "createdAt": _now_ms(),
            "isDefault": False,
        }
        state["galaxies"] = state["galaxies"] + [galaxy]
        saved = self._save_state(state, reason=f"MCP 新建星系「{clean_name[:14]}」")
        return {
            "ok": True,
            "revision": saved.get("revision"),
            "transportSource": self.backend.last_source,
            "galaxy": galaxy,
        }

    def update_galaxy(
        self,
        galaxy: str,
        name: str | None = None,
        icon: str | None = None,
        color: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        state, _, _ = self._load_bundle()
        target = self._resolve_galaxy(state["galaxies"], galaxy)
        if not target:
            raise ValueError(f"Galaxy not found: {galaxy}")
        if isinstance(name, str) and name.strip():
            target["name"] = name.strip()[:32]
        if isinstance(icon, str) and icon.strip():
            target["icon"] = icon.strip()[:8]
        if isinstance(color, str) and color.strip().startswith("#"):
            target["color"] = color.strip()[:16]
        if isinstance(description, str):
            target["description"] = description.strip()[:120]
        saved = self._save_state(state, reason=f"MCP 更新星系「{target['name'][:14]}」")
        return {
            "ok": True,
            "revision": saved.get("revision"),
            "transportSource": self.backend.last_source,
            "galaxy": target,
        }

    def list_tasks(
        self,
        galaxy: str | None = None,
        status: str = "active",
        tag: str | None = None,
        priority: str | None = None,
        cluster_id: str | None = None,
        top3_only: bool = False,
        search: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        state, rev, _ = self._load_bundle()
        galaxies = state["galaxies"]
        galaxy_map = {g["id"]: g for g in galaxies}
        cluster_map = {c["id"]: c for c in state["clusters"] if isinstance(c, dict) and c.get("id")}
        target_gid = None
        if galaxy:
            resolved = self._resolve_galaxy(galaxies, galaxy)
            if not resolved:
                raise ValueError(f"Galaxy not found: {galaxy}")
            target_gid = resolved["id"]

        clean_tag = tag.strip().lstrip("#").strip().lower() if isinstance(tag, str) and tag.strip() else None
        q = search.strip().lower() if isinstance(search, str) and search.strip() else None
        out = []
        for t in state["tasks"]:
            gid = t.get("galaxyId") or DEFAULT_GALAXY_ID
            if target_gid and gid != target_gid:
                continue
            comp = bool(t.get("completed"))
            if status == "active" and comp:
                continue
            if status == "completed" and not comp:
                continue
            if priority and (t.get("priority") or "normal") != priority:
                continue
            if cluster_id and t.get("clusterId") != cluster_id:
                continue
            if top3_only and t.get("top3Slot") not in (0, 1, 2):
                continue
            if clean_tag and not any(str(tg).lower() == clean_tag for tg in (t.get("tags") or [])):
                continue
            if q:
                hay = " ".join([
                    str(t.get("title") or ""),
                    str(t.get("notes") or ""),
                    " ".join(str(tg) for tg in (t.get("tags") or [])),
                ]).lower()
                if q not in hay:
                    continue
            out.append(self._format_task(t, galaxy_map, cluster_map))
        total = len(out)
        cap = max(1, min(int(limit or 100), 500))
        return {
            "revision": rev,
            "transportSource": self.backend.last_source,
            "total": total,
            "tasks": out[:cap],
        }

    def get_task(self, task_id: str | None = None, title_query: str | None = None) -> dict[str, Any]:
        state, rev, _ = self._load_bundle()
        tasks = state["tasks"]
        found = None
        if task_id:
            found = next((t for t in tasks if t.get("id") == task_id), None)
        elif title_query:
            q = title_query.strip().lower()
            exact = [t for t in tasks if str(t.get("title") or "").strip().lower() == q]
            matches = exact or [t for t in tasks if q in str(t.get("title") or "").lower()]
            if len(matches) == 1:
                found = matches[0]
            elif len(matches) > 1:
                raise ValueError(
                    "Multiple tasks matched title_query: "
                    + ", ".join(f"{m.get('id')}:{m.get('title')}" for m in matches[:6])
                )
        else:
            raise ValueError("Either task_id or title_query is required")
        if not found:
            raise ValueError(f"Task not found: {task_id or title_query}")
        galaxy_map = {g["id"]: g for g in state["galaxies"]}
        cluster_map = {c["id"]: c for c in state["clusters"] if isinstance(c, dict) and c.get("id")}
        by_id = {t["id"]: t for t in tasks if isinstance(t, dict) and t.get("id")}
        links = []
        for l in state["manualLinks"]:
            if l.get("source") == found["id"] or l.get("target") == found["id"]:
                other_id = l.get("target") if l.get("source") == found["id"] else l.get("source")
                other = by_id.get(other_id) or {}
                links.append({
                    "linkId": l.get("id"),
                    "direction": "outgoing" if l.get("source") == found["id"] else "incoming",
                    "peerId": other_id,
                    "peerTitle": other.get("title"),
                    "label": l.get("label") or "",
                })
        return {
            "revision": rev,
            "transportSource": self.backend.last_source,
            "task": self._format_task(found, galaxy_map, cluster_map),
            "links": links,
        }

    def create_task(
        self,
        title: str,
        notes: str = "",
        galaxy: str | None = None,
        tags: list[str] | None = None,
        priority: str = "normal",
        due_at: Any = None,
        pin_top3: bool = False,
        top3_slot: int | None = None,
        cluster_id: str | None = None,
        link_to_task_id: str | None = None,
    ) -> dict[str, Any]:
        clean_title = str(title or "").strip()
        if not clean_title:
            raise ValueError("Task title is required")
        if priority not in PRIORITY_WEIGHTS:
            raise ValueError(f"Invalid priority {priority!r}; expected normal|high|critical")

        state, _, _ = self._load_bundle()
        galaxies = state["galaxies"]
        link_parent = None
        if link_to_task_id:
            link_parent = next((t for t in state["tasks"] if t.get("id") == link_to_task_id), None)
            if not link_parent:
                raise ValueError(f"link_to_task_id not found: {link_to_task_id}")

        target_gid = DEFAULT_GALAXY_ID
        if galaxy:
            resolved = self._resolve_galaxy(galaxies, galaxy)
            if not resolved:
                raise ValueError(f"Galaxy not found: {galaxy}")
            target_gid = resolved["id"]
        elif link_parent:
            target_gid = link_parent.get("galaxyId") or DEFAULT_GALAXY_ID

        assigned_slot = None
        if top3_slot is not None:
            if top3_slot not in (0, 1, 2):
                raise ValueError("top3_slot must be 0, 1, or 2")
            assigned_slot = top3_slot
            for existing in state["tasks"]:
                if (existing.get("galaxyId") or DEFAULT_GALAXY_ID) == target_gid and existing.get("top3Slot") == assigned_slot:
                    existing["top3Slot"] = None
        elif pin_top3:
            used = {
                existing.get("top3Slot")
                for existing in state["tasks"]
                if not existing.get("completed")
                and (existing.get("galaxyId") or DEFAULT_GALAXY_ID) == target_gid
                and existing.get("top3Slot") in (0, 1, 2)
            }
            for candidate in (0, 1, 2):
                if candidate not in used:
                    assigned_slot = candidate
                    break

        parsed_due = parse_due_at(due_at)
        seed = secrets.randbelow(10000) / 10000.0
        angle = seed * math.pi * 2.0
        if link_parent is not None:
            px = float(link_parent.get("x") or 0.0)
            py = float(link_parent.get("y") or 0.0)
            pz = float(link_parent.get("z") or 0.0)
            x = round(px + math.cos(angle) * 140.0, 2)
            y = round(py + ((seed - 0.5) * 90.0), 2)
            z = round(pz + math.sin(angle) * 140.0, 2)
        else:
            radius = 220.0 + seed * 180.0
            x = round(math.cos(angle) * radius, 2)
            y = round((seed - 0.5) * 160.0, 2)
            z = round(math.sin(angle) * radius, 2)

        resolved_cluster = cluster_id if cluster_id else (link_parent.get("clusterId") if link_parent and (link_parent.get("galaxyId") or DEFAULT_GALAXY_ID) == target_gid else None)
        new_task = {
            "id": _uid("task"),
            "title": clean_title,
            "notes": str(notes or ""),
            "tags": _clean_tags(tags or []),
            "priority": priority,
            "completed": False,
            "completedAt": None,
            "top3Slot": assigned_slot,
            "clusterId": resolved_cluster,
            "galaxyId": target_gid,
            "dueAt": parsed_due,
            "x": x,
            "y": y,
            "z": z,
            "floatSeed": round(0.1 + seed * 0.85, 6),
            "createdAt": _now_ms(),
        }
        state["tasks"].append(new_task)

        created_link = None
        if link_parent is not None:
            created_link = {
                "id": _uid("ml"),
                "source": link_parent["id"],
                "target": new_task["id"],
                "label": "",
            }
            state["manualLinks"].append(created_link)

        saved = self._save_state(state, reason=f"MCP 新增「{clean_title[:16]}」")
        galaxy_map = {g["id"]: g for g in galaxies}
        cluster_map = {c["id"]: c for c in state["clusters"] if isinstance(c, dict) and c.get("id")}
        return {
            "ok": True,
            "revision": saved.get("revision"),
            "transportSource": self.backend.last_source,
            "task": self._format_task(new_task, galaxy_map, cluster_map),
            "link": created_link,
        }

    def update_task(
        self,
        task_id: str,
        title: str | None = None,
        notes: str | None = None,
        tags: list[str] | None = None,
        priority: str | None = None,
        due_at: Any = "__UNSET__",
        galaxy: str | None = None,
        cluster_id: str | None = "__UNSET__",
        top3_slot: int | None = -999,
    ) -> dict[str, Any]:
        state, _, _ = self._load_bundle()
        task = next((t for t in state["tasks"] if t.get("id") == task_id), None)
        if not task:
            raise ValueError(f"Task not found: {task_id}")
        if title is not None:
            clean = str(title).strip()
            if not clean:
                raise ValueError("title cannot be empty")
            task["title"] = clean
        if notes is not None:
            task["notes"] = str(notes)
        if tags is not None:
            task["tags"] = _clean_tags(tags)
        if priority is not None:
            if priority not in PRIORITY_WEIGHTS:
                raise ValueError(f"Invalid priority {priority!r}")
            task["priority"] = priority
        if due_at != "__UNSET__":
            task["dueAt"] = parse_due_at(due_at)
        if galaxy is not None:
            resolved = self._resolve_galaxy(state["galaxies"], galaxy)
            if not resolved:
                raise ValueError(f"Galaxy not found: {galaxy}")
            task["galaxyId"] = resolved["id"]
        if cluster_id != "__UNSET__":
            task["clusterId"] = cluster_id or None
        if top3_slot != -999:
            if top3_slot is None:
                task["top3Slot"] = None
            else:
                if top3_slot not in (0, 1, 2):
                    raise ValueError("top3_slot must be 0, 1, 2, or null")
                gid = task.get("galaxyId") or DEFAULT_GALAXY_ID
                for other in state["tasks"]:
                    if other.get("id") != task["id"] and (other.get("galaxyId") or DEFAULT_GALAXY_ID) == gid and other.get("top3Slot") == top3_slot:
                        other["top3Slot"] = None
                task["top3Slot"] = top3_slot

        saved = self._save_state(state, reason=f"MCP 更新「{str(task.get('title') or '')[:14]}」")
        galaxy_map = {g["id"]: g for g in state["galaxies"]}
        cluster_map = {c["id"]: c for c in state["clusters"] if isinstance(c, dict) and c.get("id")}
        return {
            "ok": True,
            "revision": saved.get("revision"),
            "transportSource": self.backend.last_source,
            "task": self._format_task(task, galaxy_map, cluster_map),
        }

    def complete_task(
        self,
        task_id: str,
        completed: bool = True,
        completed_at: Any = None,
    ) -> dict[str, Any]:
        state, _, _ = self._load_bundle()
        task = next((t for t in state["tasks"] if t.get("id") == task_id), None)
        if not task:
            raise ValueError(f"Task not found: {task_id}")
        if completed:
            ts = parse_due_at(completed_at) if completed_at is not None else _now_ms()
            task["completed"] = True
            task["completedAt"] = ts
            action_word = "完成"
        else:
            task["completed"] = False
            task["completedAt"] = None
            gid = task.get("galaxyId") or DEFAULT_GALAXY_ID
            if task.get("top3Slot") in (0, 1, 2):
                conflict = any(
                    o.get("id") != task["id"]
                    and not o.get("completed")
                    and (o.get("galaxyId") or DEFAULT_GALAXY_ID) == gid
                    and o.get("top3Slot") == task["top3Slot"]
                    for o in state["tasks"]
                )
                if conflict:
                    task["top3Slot"] = None
            action_word = "恢复"
        saved = self._save_state(state, reason=f"MCP {action_word}「{str(task.get('title') or '')[:14]}」")
        galaxy_map = {g["id"]: g for g in state["galaxies"]}
        cluster_map = {c["id"]: c for c in state["clusters"] if isinstance(c, dict) and c.get("id")}
        return {
            "ok": True,
            "revision": saved.get("revision"),
            "transportSource": self.backend.last_source,
            "task": self._format_task(task, galaxy_map, cluster_map),
        }

    def delete_task(self, task_id: str) -> dict[str, Any]:
        state, _, _ = self._load_bundle()
        target = next((t for t in state["tasks"] if t.get("id") == task_id), None)
        if not target:
            raise ValueError(f"Task not found: {task_id}")
        state["tasks"] = [t for t in state["tasks"] if t.get("id") != task_id]
        before_ml = len(state["manualLinks"])
        state["manualLinks"] = [
            l for l in state["manualLinks"] if l.get("source") != task_id and l.get("target") != task_id
        ]
        state["aiLinks"] = [
            l for l in state["aiLinks"] if l.get("source") != task_id and l.get("target") != task_id
        ]
        saved = self._save_state(state, reason=f"MCP 删除「{str(target.get('title') or '')[:14]}」")
        return {
            "ok": True,
            "revision": saved.get("revision"),
            "transportSource": self.backend.last_source,
            "deletedTaskId": task_id,
            "deletedTitle": target.get("title"),
            "removedLinks": before_ml - len(state["manualLinks"]),
        }

    def move_tasks_to_galaxy(
        self,
        task_ids: list[str],
        target_galaxy: str,
        cluster_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        if not task_ids and not cluster_ids:
            raise ValueError("task_ids or cluster_ids must be non-empty")
        state, _, _ = self._load_bundle()
        resolved = self._resolve_galaxy(state["galaxies"], target_galaxy)
        if not resolved:
            raise ValueError(f"Target galaxy not found: {target_galaxy}")
        target_gid = resolved["id"]
        id_set = set(task_ids or [])
        moved = []
        for t in state["tasks"]:
            if t.get("id") in id_set:
                t["galaxyId"] = target_gid
                moved.append({"id": t.get("id"), "title": t.get("title")})
        missing = sorted(id_set - {m["id"] for m in moved})
        if missing:
            raise ValueError(f"Task IDs not found: {missing}")

        c_set = set(cluster_ids or [])
        moved_clusters = []
        for c in state["clusters"]:
            if isinstance(c, dict) and c.get("id") in c_set:
                c["galaxyId"] = target_gid
                moved_clusters.append({"id": c.get("id"), "name": c.get("name")})

        # Keep intra-galaxy aiLinks clean; keep 100% manualLinks intact
        tgal = {t["id"]: (t.get("galaxyId") or DEFAULT_GALAXY_ID) for t in state["tasks"] if t.get("id")}
        before_ai = len(state["aiLinks"])
        state["aiLinks"] = [
            l for l in state["aiLinks"] if tgal.get(l.get("source")) == tgal.get(l.get("target"))
        ]
        saved = self._save_state(state, reason=f"MCP 迁移 {len(moved)} 个气泡至「{resolved['name']}」")
        return {
            "ok": True,
            "revision": saved.get("revision"),
            "transportSource": self.backend.last_source,
            "targetGalaxy": resolved,
            "movedTasks": moved,
            "movedClusters": moved_clusters,
            "prunedCrossGalaxyAiLinks": before_ai - len(state["aiLinks"]),
            "manualLinksPreserved": len(state["manualLinks"]),
        }

    def link_tasks(self, source_id: str, target_id: str, label: str = "") -> dict[str, Any]:
        if source_id == target_id:
            raise ValueError("source_id and target_id must be distinct")
        state, _, _ = self._load_bundle()
        ids = {t.get("id") for t in state["tasks"]}
        if source_id not in ids or target_id not in ids:
            raise ValueError("Both source_id and target_id must exist")
        existing = next(
            (
                l for l in state["manualLinks"]
                if (l.get("source") == source_id and l.get("target") == target_id)
                or (l.get("source") == target_id and l.get("target") == source_id)
            ),
            None,
        )
        if existing:
            return {
                "ok": True,
                "alreadyExists": True,
                "transportSource": self.backend.last_source,
                "link": existing,
            }
        link = {"id": _uid("ml"), "source": source_id, "target": target_id, "label": str(label or "").strip()[:48]}
        state["manualLinks"].append(link)
        saved = self._save_state(state, reason="MCP 新增气泡连线")
        return {
            "ok": True,
            "alreadyExists": False,
            "revision": saved.get("revision"),
            "transportSource": self.backend.last_source,
            "link": link,
        }

    def unlink_tasks(
        self,
        link_id: str | None = None,
        source_id: str | None = None,
        target_id: str | None = None,
    ) -> dict[str, Any]:
        if not link_id and not (source_id and target_id):
            raise ValueError("Provide link_id or both source_id and target_id")
        state, _, _ = self._load_bundle()
        before = len(state["manualLinks"])
        if link_id:
            state["manualLinks"] = [l for l in state["manualLinks"] if l.get("id") != link_id]
        else:
            state["manualLinks"] = [
                l for l in state["manualLinks"]
                if not (
                    (l.get("source") == source_id and l.get("target") == target_id)
                    or (l.get("source") == target_id and l.get("target") == source_id)
                )
            ]
        removed = before - len(state["manualLinks"])
        if removed == 0:
            return {"ok": True, "removed": 0, "transportSource": self.backend.last_source}
        saved = self._save_state(state, reason="MCP 移除气泡连线")
        return {
            "ok": True,
            "removed": removed,
            "revision": saved.get("revision"),
            "transportSource": self.backend.last_source,
        }

    def get_critical_path(
        self,
        galaxy: str | None = None,
        target_task_id: str | None = None,
        use_ai: bool = False,
    ) -> dict[str, Any]:
        state, rev, _ = self._load_bundle()
        galaxies = state["galaxies"]
        target_gid = None
        if galaxy:
            resolved = self._resolve_galaxy(galaxies, galaxy)
            if not resolved:
                raise ValueError(f"Galaxy not found: {galaxy}")
            target_gid = resolved["id"]

        active = [
            t for t in state["tasks"]
            if not t.get("completed") and (not target_gid or (t.get("galaxyId") or DEFAULT_GALAXY_ID) == target_gid)
        ]
        if not active:
            return {"revision": rev, "transportSource": self.backend.last_source, "steps": [], "summary": "当前无进行中任务"}

        active_ids = {t["id"] for t in active}
        scoped_manual = [l for l in state["manualLinks"] if l.get("source") in active_ids and l.get("target") in active_ids]
        scoped_ai = [l for l in state["aiLinks"] if l.get("source") in active_ids and l.get("target") in active_ids]

        if use_ai:
            ai_res = self.backend.ai_critical_path(active, scoped_manual, scoped_ai, target_task_id=target_task_id)
            if ai_res and isinstance(ai_res.get("steps"), list) and ai_res["steps"]:
                return {"revision": rev, "transportSource": self.backend.last_source, "engine": "agy", **ai_res}

        # Deterministic longest-weighted-path analysis over DAG edges, or Top-3/priority fallback
        task_map = {t["id"]: t for t in active}
        now_ms = _now_ms()

        def node_score(t: dict[str, Any]) -> float:
            w = PRIORITY_WEIGHTS.get(t.get("priority") or "normal", 1.0)
            if t.get("top3Slot") in (0, 1, 2):
                w += 3.0 - 0.5 * float(t["top3Slot"])
            if t.get("dueAt"):
                days_left = (t["dueAt"] - now_ms) / 86400000.0
                if days_left <= 0:
                    w += 3.0
                elif days_left <= 3:
                    w += 2.0
                elif days_left <= 7:
                    w += 1.0
            return w

        adj: dict[str, list[str]] = {tid: [] for tid in active_ids}
        indeg: dict[str, int] = {tid: 0 for tid in active_ids}
        for l in scoped_manual:
            u, v = l.get("source"), l.get("target")
            if u in adj and v in adj and v not in adj[u]:
                adj[u].append(v)
                indeg[v] += 1

        # Kahn's topological order (break cycles cleanly by ignoring remaining back-edges)
        queue = sorted([tid for tid, d in indeg.items() if d == 0], key=lambda x: -node_score(task_map[x]))
        topo: list[str] = []
        seen = set(queue)
        while queue:
            u = queue.pop(0)
            topo.append(u)
            for v in adj[u]:
                indeg[v] -= 1
                if indeg[v] <= 0 and v not in seen:
                    seen.add(v)
                    queue.append(v)
        for tid in active_ids:
            if tid not in seen:
                topo.append(tid)

        dp: dict[str, float] = {tid: node_score(task_map[tid]) for tid in topo}
        parent: dict[str, str | None] = {tid: None for tid in topo}
        pos_in_topo = {tid: idx for idx, tid in enumerate(topo)}
        for u in topo:
            for v in adj[u]:
                if pos_in_topo[u] < pos_in_topo[v]:
                    cand = dp[u] + node_score(task_map[v])
                    if cand > dp[v]:
                        dp[v] = cand
                        parent[v] = u

        end_id = target_task_id if (target_task_id in dp) else max(topo, key=lambda x: dp[x])
        chain: list[str] = []
        cur: str | None = end_id
        guard = set()
        while cur and cur not in guard:
            guard.add(cur)
            chain.append(cur)
            cur = parent.get(cur)
        chain.reverse()

        # If graph had sparse links (chain length < 3) and more tasks exist, append top priority/due tasks
        if len(chain) < min(4, len(active)):
            remaining = [t for t in active if t["id"] not in set(chain)]
            remaining.sort(key=lambda t: (-node_score(t), t.get("dueAt") or 9e18, t.get("createdAt") or 0))
            for t in remaining[: max(0, min(5, len(active)) - len(chain))]:
                chain.append(t["id"])

        steps = []
        for idx, tid in enumerate(chain, 1):
            t = task_map[tid]
            steps.append({
                "order": idx,
                "taskId": tid,
                "title": t.get("title"),
                "priority": t.get("priority") or "normal",
                "top3Slot": t.get("top3Slot"),
                "dueAtStr": _format_local_ts(t.get("dueAt")),
                "reason": "依赖链前置节点" if idx < len(chain) and tid in adj and chain[idx] in adj[tid] else "高优/紧急核心节点",
            })
        return {
            "revision": rev,
            "transportSource": self.backend.last_source,
            "engine": "local-dag",
            "galaxyId": target_gid or "__all__",
            "steps": steps,
            "summary": " → ".join(s["title"] for s in steps[:6]),
        }

    def get_daily_report(self, date: str | None = None, galaxy: str | None = None) -> dict[str, Any]:
        state, rev, _ = self._load_bundle()
        galaxies = state["galaxies"]
        galaxy_map = {g["id"]: g for g in galaxies}
        cluster_map = {c["id"]: c for c in state["clusters"] if isinstance(c, dict) and c.get("id")}
        target_gid = None
        if galaxy:
            resolved = self._resolve_galaxy(galaxies, galaxy)
            if not resolved:
                raise ValueError(f"Galaxy not found: {galaxy}")
            target_gid = resolved["id"]

        if date:
            _dt.datetime.strptime(date.strip(), "%Y-%m-%d")
            date_key = date.strip()
        else:
            date_key = _dt.datetime.now().strftime("%Y-%m-%d")

        scoped = [t for t in state["tasks"] if not target_gid or (t.get("galaxyId") or DEFAULT_GALAXY_ID) == target_gid]
        completed_on_date = [
            self._format_task(t, galaxy_map, cluster_map)
            for t in scoped
            if t.get("completed") and _local_date_key(t.get("completedAt")) == date_key
        ]
        now_ms = _now_ms()
        active_tasks = [t for t in scoped if not t.get("completed")]
        overdue = [
            self._format_task(t, galaxy_map, cluster_map)
            for t in active_tasks
            if t.get("dueAt") and t["dueAt"] < now_ms
        ]
        due_on_date = [
            self._format_task(t, galaxy_map, cluster_map)
            for t in active_tasks
            if t.get("dueAt") and _local_date_key(t["dueAt"]) == date_key
        ]
        top3 = sorted(
            [
                self._format_task(t, galaxy_map, cluster_map)
                for t in active_tasks
                if t.get("top3Slot") in (0, 1, 2)
            ],
            key=lambda x: (x["galaxyId"], x["top3Slot"]),
        )
        return {
            "revision": rev,
            "transportSource": self.backend.last_source,
            "date": date_key,
            "galaxyId": target_gid or "__all__",
            "counts": {
                "totalScoped": len(scoped),
                "completedOnDate": len(completed_on_date),
                "activeTotal": len(active_tasks),
                "overdueActive": len(overdue),
                "dueOnDate": len(due_on_date),
                "top3Active": len(top3),
            },
            "completedOnDate": completed_on_date,
            "top3Active": top3,
            "overdueActive": overdue,
            "dueOnDate": due_on_date,
        }

    def list_snapshots(self, kind: str = "all", limit: int = 30) -> dict[str, Any]:
        res = self.backend.list_snapshots(kind=kind, limit=limit)
        return {**res, "transportSource": self.backend.last_source}

    def create_snapshot(self, label: str = "") -> dict[str, Any]:
        res = self.backend.create_manual_snapshot(label=label or "MCP 手动备份")
        return {**res, "transportSource": self.backend.last_source}

    def restore_snapshot(self, snapshot_id: int, confirm: bool = False) -> dict[str, Any]:
        if not confirm:
            raise ValueError("Refusing to restore snapshot without confirm=true")
        res = self.backend.restore_snapshot(int(snapshot_id))
        if not res:
            raise ValueError(f"Snapshot not found: {snapshot_id}")
        return {
            "ok": True,
            "revision": res.get("revision"),
            "restoredSnapshotId": int(snapshot_id),
            "transportSource": self.backend.last_source,
        }

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        args = dict(arguments or {})
        dispatch = {
            "get_workspace_overview": self.get_workspace_overview,
            "list_galaxies": self.list_galaxies,
            "create_galaxy": self.create_galaxy,
            "update_galaxy": self.update_galaxy,
            "list_tasks": self.list_tasks,
            "get_task": self.get_task,
            "create_task": self.create_task,
            "update_task": self.update_task,
            "complete_task": self.complete_task,
            "delete_task": self.delete_task,
            "move_tasks_to_galaxy": self.move_tasks_to_galaxy,
            "link_tasks": self.link_tasks,
            "unlink_tasks": self.unlink_tasks,
            "get_critical_path": self.get_critical_path,
            "get_daily_report": self.get_daily_report,
            "list_snapshots": self.list_snapshots,
            "create_snapshot": self.create_snapshot,
            "restore_snapshot": self.restore_snapshot,
        }
        fn = dispatch.get(name)
        if not fn:
            raise KeyError(f"Unknown MCP tool: {name}")
        return fn(**args)

    # ---------- Resources ----------
    def read_resource(self, uri: str) -> str:
        if uri == "mindbubble://overview":
            return json.dumps(self.get_workspace_overview(), ensure_ascii=False, indent=2)
        if uri == "mindbubble://galaxies":
            return json.dumps(self.list_galaxies(), ensure_ascii=False, indent=2)
        if uri == "mindbubble://tasks/active":
            return json.dumps(self.list_tasks(status="active", limit=500), ensure_ascii=False, indent=2)
        raise KeyError(f"Unknown resource URI: {uri}")

    # ---------- Prompts ----------
    def get_prompt(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        args = dict(arguments or {})
        if name == "daily_planning":
            galaxy = args.get("galaxy")
            date = args.get("date")
            report = self.get_daily_report(date=date, galaxy=galaxy)
            cpath = self.get_critical_path(galaxy=galaxy)
            text = (
                "请根据以下 MindBubble 实时状态，制定一份今日聚焦执行计划（包含建议的 Top-3 排序、风险逾期提醒、以及关键路径推进步骤）：\n\n"
                f"【今日统计与任务】\n{json.dumps(report, ensure_ascii=False, indent=2)}\n\n"
                f"【当前关键路径】\n{json.dumps(cpath, ensure_ascii=False, indent=2)}"
            )
            return {
                "description": "MindBubble 今日复盘与执行规划提示词",
                "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
            }
        if name == "organize_galaxy":
            galaxy = args.get("galaxy") or DEFAULT_GALAXY_ID
            tasks = self.list_tasks(galaxy=galaxy, status="all", limit=300)
            text = (
                f"请梳理星系 `{galaxy}` 内的所有气泡（总计 {tasks['total']} 项），提出聚类整理、标签归一化、连线补全和 Top-3 置顶建议：\n\n"
                f"{json.dumps(tasks, ensure_ascii=False, indent=2)}"
            )
            return {
                "description": "MindBubble 星系气泡梳理提示词",
                "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
            }
        raise KeyError(f"Unknown prompt: {name}")


class McpJsonRpcHandler:
    """Minimal Streamable HTTP JSON-RPC 2.0 MCP server (spec 2025-03-26)."""

    def __init__(self, service: MindBubbleMcpService) -> None:
        self.service = service
        self._sessions: dict[str, float] = {}
        self._lock = threading.Lock()

    def close_session(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

    def _touch_session(self, session_id: str | None, create: bool = False) -> str:
        with self._lock:
            if session_id and session_id in self._sessions:
                self._sessions[session_id] = time.time()
                return session_id
            if create or not session_id:
                new_id = session_id or uuid.uuid4().hex
                self._sessions[new_id] = time.time()
                return new_id
            self._sessions[session_id] = time.time()
            return session_id

    @staticmethod
    def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

    def handle_request(
        self,
        payload: Any,
        session_id: str | None = None,
    ) -> tuple[int, Any | None, dict[str, str]]:
        if not isinstance(payload, dict):
            return 400, self._error(None, -32600, "Invalid Request: expected JSON object"), {}

        method = payload.get("method")
        req_id = payload.get("id")
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        is_notification = "id" not in payload or req_id is None

        sid = self._touch_session(session_id, create=(method == "initialize"))
        base_headers = {
            "Mcp-Session-Id": sid,
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        }

        if method == "notifications/initialized" or (isinstance(method, str) and method.startswith("notifications/") and is_notification):
            return 202, None, base_headers

        if not isinstance(method, str):
            return 400, self._error(req_id, -32600, "Invalid Request: missing method"), base_headers

        if method == "initialize":
            result = {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                    "prompts": {"listChanged": False},
                },
                "serverInfo": {
                    "name": MCP_SERVER_NAME,
                    "version": MCP_SERVER_VERSION,
                },
                "instructions": "MindBubble 3D 思维气泡待办看板 MCP 服务：支持查询与管理气泡、星系隔离、手动连线、核心 Top-3、关键路径与时光机快照。",
            }
            return 200, {"jsonrpc": "2.0", "id": req_id, "result": result}, base_headers

        if method == "ping":
            return 200, {"jsonrpc": "2.0", "id": req_id, "result": {}}, base_headers

        if method == "tools/list":
            return 200, {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOL_SPECS}}, base_headers

        if method == "tools/call":
            tname = params.get("name")
            targs = params.get("arguments") or {}
            if not isinstance(tname, str):
                return 200, self._error(req_id, -32602, "Missing tool name"), base_headers
            try:
                out = self.service.call_tool(tname, targs)
                return (
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False, indent=2)}],
                            "isError": False,
                        },
                    },
                    base_headers,
                )
            except KeyError as exc:
                return 200, self._error(req_id, -32601, str(exc)), base_headers
            except Exception as exc:
                return (
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [{"type": "text", "text": f"Error: {exc}"}],
                            "isError": True,
                        },
                    },
                    base_headers,
                )

        if method == "resources/list":
            return 200, {"jsonrpc": "2.0", "id": req_id, "result": {"resources": RESOURCE_SPECS}}, base_headers

        if method == "resources/read":
            uri = params.get("uri")
            if not isinstance(uri, str):
                return 200, self._error(req_id, -32602, "Missing resource uri"), base_headers
            try:
                text = self.service.read_resource(uri)
                return (
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "contents": [{"uri": uri, "mimeType": "application/json", "text": text}]
                        },
                    },
                    base_headers,
                )
            except KeyError as exc:
                return 200, self._error(req_id, -32002, str(exc)), base_headers

        if method == "prompts/list":
            return 200, {"jsonrpc": "2.0", "id": req_id, "result": {"prompts": PROMPT_SPECS}}, base_headers

        if method == "prompts/get":
            pname = params.get("name")
            pargs = params.get("arguments") or {}
            if not isinstance(pname, str):
                return 200, self._error(req_id, -32602, "Missing prompt name"), base_headers
            try:
                res = self.service.get_prompt(pname, pargs)
                return 200, {"jsonrpc": "2.0", "id": req_id, "result": res}, base_headers
            except KeyError as exc:
                return 200, self._error(req_id, -32602, str(exc)), base_headers

        return 200, self._error(req_id, -32601, f"Method not found: {method}"), base_headers


# ---------- Antigravity Global MCP Registration ----------
def build_antigravity_mcp_entry(
    *,
    python_bin: str | None = None,
    script_path: Path | str | None = None,
    api_url: str = DEFAULT_API_URL,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    return {
        "command": python_bin or _default_python_bin(),
        "args": [str(script_path or MCP_SCRIPT_PATH)],
        "env": {
            "MINDBUBBLE_API_URL": api_url.rstrip("/"),
            "MINDBUBBLE_DB_PATH": str(db_path or DEFAULT_DB_PATH),
        },
    }


def get_antigravity_registration_status(config_path: Path | str | None = None) -> dict[str, Any]:
    cfg_path = Path(config_path) if config_path else ANTIGRAVITY_MCP_CONFIG_PATH
    info: dict[str, Any] = {
        "configPath": str(cfg_path),
        "configExists": cfg_path.exists(),
        "registered": False,
        "entry": None,
        "otherServers": [],
        "commandExists": False,
        "scriptExists": MCP_SCRIPT_PATH.exists(),
        "error": "",
    }
    if not cfg_path.exists():
        return info
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        servers = raw.get("mcpServers") if isinstance(raw, dict) else None
        if isinstance(servers, dict):
            info["otherServers"] = sorted(k for k in servers.keys() if k != MCP_SERVER_NAME)
            entry = servers.get(MCP_SERVER_NAME)
            if isinstance(entry, dict):
                info["registered"] = True
                info["entry"] = entry
                cmd = str(entry.get("command") or "")
                info["commandExists"] = bool(cmd and Path(cmd).exists())
    except Exception as exc:
        info["error"] = str(exc)
    return info


def install_into_antigravity(
    config_path: Path | str | None = None,
    *,
    python_bin: str | None = None,
    script_path: Path | str | None = None,
    api_url: str = DEFAULT_API_URL,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    cfg_path = Path(config_path) if config_path else ANTIGRAVITY_MCP_CONFIG_PATH
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    if cfg_path.exists():
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"Expected JSON object in {cfg_path}")
    else:
        raw = {}
    servers = raw.get("mcpServers")
    if servers is None:
        servers = {}
    elif not isinstance(servers, dict):
        raise ValueError(f"Expected 'mcpServers' JSON object in {cfg_path}")

    entry = build_antigravity_mcp_entry(
        python_bin=python_bin,
        script_path=script_path,
        api_url=api_url,
        db_path=db_path,
    )
    servers[MCP_SERVER_NAME] = entry
    raw["mcpServers"] = servers

    tmp_path = cfg_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, cfg_path)
    return {
        "ok": True,
        "installed": True,
        **get_antigravity_registration_status(cfg_path),
    }
