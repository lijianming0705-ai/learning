#!/usr/bin/env python3
"""MindBubble MCP Server (Stdio & Streamable HTTP) with Antigravity built-in registration."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

# Route all logs strictly to stderr so stdout remains clean for MCP stdio JSON-RPC framing.
logging.basicConfig(
    level=logging.WARNING,
    stream=sys.stderr,
    format="[mindbubble-mcp] %(levelname)s: %(message)s",
)

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from mindbubble_mcp_core import (
    DEFAULT_API_URL,
    DEFAULT_DB_PATH,
    MindBubbleBackend,
    MindBubbleMcpService,
    PROMPT_SPECS,
    RESOURCE_SPECS,
    TOOL_SPECS,
    get_antigravity_registration_status,
    install_into_antigravity,
)

MCP_INSTRUCTIONS = (
    "MindBubble 3D 思维气泡待办看板 MCP 服务。"
    "支持读写气泡任务、按星系（默认星系/lumio/生活家庭等）分类与隔离迁移、"
    "管理 Top-3 核心要务、建立与移除手动连线、生成关键路径与复盘报告，以及 Time Machine 快照查看与恢复。"
)


def create_fastmcp_app(service: MindBubbleMcpService, *, host: str = "127.0.0.1", port: int = 8766) -> FastMCP:
    mcp = FastMCP(
        "mindbubble",
        instructions=MCP_INSTRUCTIONS,
        host=host,
        port=port,
    )

    spec_by_name = {s["name"]: s for s in TOOL_SPECS}

    def _ann(name: str) -> ToolAnnotations:
        raw = (spec_by_name.get(name) or {}).get("annotations") or {}
        return ToolAnnotations(**raw)

    def _desc(name: str) -> str:
        return (spec_by_name.get(name) or {}).get("description") or ""

    @mcp.tool(name="get_workspace_overview", description=_desc("get_workspace_overview"), annotations=_ann("get_workspace_overview"))
    def get_workspace_overview() -> dict[str, Any]:
        return service.get_workspace_overview()

    @mcp.tool(name="list_galaxies", description=_desc("list_galaxies"), annotations=_ann("list_galaxies"))
    def list_galaxies() -> dict[str, Any]:
        return service.list_galaxies()

    @mcp.tool(name="create_galaxy", description=_desc("create_galaxy"), annotations=_ann("create_galaxy"))
    def create_galaxy(
        name: str,
        icon: str = "🚀",
        color: str = "#a78bfa",
        description: str = "",
    ) -> dict[str, Any]:
        return service.create_galaxy(name=name, icon=icon, color=color, description=description)

    @mcp.tool(name="update_galaxy", description=_desc("update_galaxy"), annotations=_ann("update_galaxy"))
    def update_galaxy(
        galaxy: str,
        name: str | None = None,
        icon: str | None = None,
        color: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        return service.update_galaxy(galaxy=galaxy, name=name, icon=icon, color=color, description=description)

    @mcp.tool(name="list_tasks", description=_desc("list_tasks"), annotations=_ann("list_tasks"))
    def list_tasks(
        galaxy: str | None = None,
        status: str = "active",
        tag: str | None = None,
        priority: str | None = None,
        cluster_id: str | None = None,
        top3_only: bool = False,
        search: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        return service.list_tasks(
            galaxy=galaxy,
            status=status,
            tag=tag,
            priority=priority,
            cluster_id=cluster_id,
            top3_only=top3_only,
            search=search,
            limit=limit,
        )

    @mcp.tool(name="get_task", description=_desc("get_task"), annotations=_ann("get_task"))
    def get_task(task_id: str | None = None, title_query: str | None = None) -> dict[str, Any]:
        return service.get_task(task_id=task_id, title_query=title_query)

    @mcp.tool(name="create_task", description=_desc("create_task"), annotations=_ann("create_task"))
    def create_task(
        title: str,
        notes: str = "",
        galaxy: str | None = None,
        tags: list[str] | None = None,
        priority: str = "normal",
        due_at: str | int | float | None = None,
        pin_top3: bool = False,
        top3_slot: int | None = None,
        cluster_id: str | None = None,
        link_to_task_id: str | None = None,
    ) -> dict[str, Any]:
        return service.create_task(
            title=title,
            notes=notes,
            galaxy=galaxy,
            tags=tags,
            priority=priority,
            due_at=due_at,
            pin_top3=pin_top3,
            top3_slot=top3_slot,
            cluster_id=cluster_id,
            link_to_task_id=link_to_task_id,
        )

    @mcp.tool(name="update_task", description=_desc("update_task"), annotations=_ann("update_task"))
    def update_task(
        task_id: str,
        title: str | None = None,
        notes: str | None = None,
        tags: list[str] | None = None,
        priority: str | None = None,
        due_at: str | int | float | None = None,
        clear_due_at: bool = False,
        galaxy: str | None = None,
        cluster_id: str | None = None,
        clear_cluster: bool = False,
        top3_slot: int | None = None,
        clear_top3: bool = False,
        notebook: str | None = None,
        append_notebook: str | None = None,
    ) -> dict[str, Any]:
        resolved_due: Any = None if clear_due_at else (due_at if due_at is not None else "__UNSET__")
        resolved_cluster: Any = None if clear_cluster else (cluster_id if cluster_id is not None else "__UNSET__")
        resolved_top3: Any = None if clear_top3 else (top3_slot if top3_slot is not None else -999)
        return service.update_task(
            task_id=task_id,
            title=title,
            notes=notes,
            tags=tags,
            priority=priority,
            due_at=resolved_due,
            galaxy=galaxy,
            cluster_id=resolved_cluster,
            top3_slot=resolved_top3,
            notebook=notebook,
            append_notebook=append_notebook,
        )

    @mcp.tool(name="complete_task", description=_desc("complete_task"), annotations=_ann("complete_task"))
    def complete_task(
        task_id: str,
        completed: bool = True,
        completed_at: str | int | float | None = None,
    ) -> dict[str, Any]:
        return service.complete_task(task_id=task_id, completed=completed, completed_at=completed_at)

    @mcp.tool(name="delete_task", description=_desc("delete_task"), annotations=_ann("delete_task"))
    def delete_task(task_id: str) -> dict[str, Any]:
        return service.delete_task(task_id=task_id)

    @mcp.tool(name="move_tasks_to_galaxy", description=_desc("move_tasks_to_galaxy"), annotations=_ann("move_tasks_to_galaxy"))
    def move_tasks_to_galaxy(
        task_ids: list[str],
        target_galaxy: str,
        cluster_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        return service.move_tasks_to_galaxy(task_ids=task_ids, target_galaxy=target_galaxy, cluster_ids=cluster_ids)

    @mcp.tool(name="link_tasks", description=_desc("link_tasks"), annotations=_ann("link_tasks"))
    def link_tasks(source_id: str, target_id: str, label: str = "") -> dict[str, Any]:
        return service.link_tasks(source_id=source_id, target_id=target_id, label=label)

    @mcp.tool(name="unlink_tasks", description=_desc("unlink_tasks"), annotations=_ann("unlink_tasks"))
    def unlink_tasks(
        link_id: str | None = None,
        source_id: str | None = None,
        target_id: str | None = None,
    ) -> dict[str, Any]:
        return service.unlink_tasks(link_id=link_id, source_id=source_id, target_id=target_id)

    @mcp.tool(name="get_critical_path", description=_desc("get_critical_path"), annotations=_ann("get_critical_path"))
    def get_critical_path(
        galaxy: str | None = None,
        target_task_id: str | None = None,
        use_ai: bool = False,
    ) -> dict[str, Any]:
        return service.get_critical_path(galaxy=galaxy, target_task_id=target_task_id, use_ai=use_ai)

    @mcp.tool(name="get_daily_report", description=_desc("get_daily_report"), annotations=_ann("get_daily_report"))
    def get_daily_report(date: str | None = None, galaxy: str | None = None) -> dict[str, Any]:
        return service.get_daily_report(date=date, galaxy=galaxy)

    @mcp.tool(name="list_snapshots", description=_desc("list_snapshots"), annotations=_ann("list_snapshots"))
    def list_snapshots(kind: str = "all", limit: int = 30) -> dict[str, Any]:
        return service.list_snapshots(kind=kind, limit=limit)

    @mcp.tool(name="create_snapshot", description=_desc("create_snapshot"), annotations=_ann("create_snapshot"))
    def create_snapshot(label: str = "") -> dict[str, Any]:
        return service.create_snapshot(label=label)

    @mcp.tool(name="restore_snapshot", description=_desc("restore_snapshot"), annotations=_ann("restore_snapshot"))
    def restore_snapshot(snapshot_id: int, confirm: bool = False) -> dict[str, Any]:
        return service.restore_snapshot(snapshot_id=snapshot_id, confirm=confirm)

    # Resources
    @mcp.resource("mindbubble://overview", name=RESOURCE_SPECS[0]["name"], description=RESOURCE_SPECS[0]["description"], mime_type="application/json")
    def resource_overview() -> str:
        return service.read_resource("mindbubble://overview")

    @mcp.resource("mindbubble://galaxies", name=RESOURCE_SPECS[1]["name"], description=RESOURCE_SPECS[1]["description"], mime_type="application/json")
    def resource_galaxies() -> str:
        return service.read_resource("mindbubble://galaxies")

    @mcp.resource("mindbubble://tasks/active", name=RESOURCE_SPECS[2]["name"], description=RESOURCE_SPECS[2]["description"], mime_type="application/json")
    def resource_active_tasks() -> str:
        return service.read_resource("mindbubble://tasks/active")

    # Prompts
    @mcp.prompt(name="daily_planning", description=PROMPT_SPECS[0]["description"])
    def prompt_daily_planning(galaxy: str = "", date: str = "") -> str:
        res = service.get_prompt("daily_planning", {"galaxy": galaxy or None, "date": date or None})
        return res["messages"][0]["content"]["text"]

    @mcp.prompt(name="organize_galaxy", description=PROMPT_SPECS[1]["description"])
    def prompt_organize_galaxy(galaxy: str = "gal_default") -> str:
        res = service.get_prompt("organize_galaxy", {"galaxy": galaxy or "gal_default"})
        return res["messages"][0]["content"]["text"]

    return mcp


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MindBubble MCP Server (Stdio & Streamable HTTP)")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--api-url", default=os.environ.get("MINDBUBBLE_API_URL") or DEFAULT_API_URL)
    parser.add_argument("--db-path", default=os.environ.get("MINDBUBBLE_DB_PATH") or str(DEFAULT_DB_PATH))
    parser.add_argument("--install-antigravity", action="store_true", help="Register MindBubble in ~/.gemini/config/mcp_config.json")
    parser.add_argument("--config-path", default=None, help="Override Antigravity mcp_config.json path")
    parser.add_argument("--status", action="store_true", help="Print registration and backend status JSON")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.install_antigravity:
        res = install_into_antigravity(
            config_path=args.config_path,
            api_url=args.api_url,
            db_path=Path(args.db_path),
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return

    backend = MindBubbleBackend(api_url=args.api_url, db_path=Path(args.db_path))
    service = MindBubbleMcpService(backend)

    if args.status:
        overview = service.get_workspace_overview()
        out = {
            "antigravity": get_antigravity_registration_status(args.config_path),
            "backend": {
                "apiUrl": backend.api_url,
                "dbPath": str(backend.db_path),
                "source": backend.last_source,
                "revision": overview["revision"],
                "counts": overview["counts"],
            },
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    app = create_fastmcp_app(service, host=args.host, port=args.port)
    app.run(transport=args.transport)


if __name__ == "__main__":
    main()
