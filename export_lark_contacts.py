#!/usr/bin/env python3
"""Export Liuxing (留形) Feishu contacts (Name, Department, Email) via lark-cli and import into 3DBubble (MindBubble)."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_EXPORT_DIR = BASE_DIR / "data"
DEFAULT_API_URL = "http://127.0.0.1:8765"
DEFAULT_DB_PATH = BASE_DIR / "data" / "mindbubble.sqlite"
_PRIORITY_CHAT_RE = re.compile(r"(全员|留形|lumio|company|all\s*staff|全体)", re.IGNORECASE)


def _lark_env() -> dict[str, str]:
    env = os.environ.copy()
    env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
    env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
    return env


def _run_lark_json(args: list[str], timeout: int = 30) -> dict[str, Any]:
    cli = shutil.which("lark-cli")
    if not cli:
        raise RuntimeError("未找到 lark-cli，请先安装并配置 lark-cli")
    proc = subprocess.run(
        [cli, *args],
        env=_lark_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    raw = (proc.stdout or "").strip()
    if not raw and proc.stderr:
        raw = proc.stderr.strip()
    if not raw:
        raise RuntimeError(f"lark-cli {' '.join(args[:3])} 未返回输出 (exit={proc.returncode})")
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
        return {"ok": True, "data": parsed}
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end > start:
            try:
                parsed = json.loads(raw[start : end + 1])
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
        raise RuntimeError(f"无法解析 lark-cli JSON 输出: {raw[:300]}")


def get_lark_auth_status() -> dict[str, Any]:
    """Inspect current lark-cli installation and bot/user auth readiness."""
    cli = shutil.which("lark-cli")
    if not cli:
        return {
            "cliInstalled": False,
            "userReady": False,
            "botReady": False,
            "userName": "",
            "openId": "",
            "tokenStatus": "missing_cli",
            "note": "未检测到 lark-cli",
        }
    try:
        res = _run_lark_json(["auth", "status", "--json", "--verify"], timeout=20)
    except Exception as exc:
        return {
            "cliInstalled": True,
            "userReady": False,
            "botReady": False,
            "userName": "",
            "openId": "",
            "tokenStatus": "error",
            "note": str(exc),
        }

    identities = res.get("identities") if isinstance(res.get("identities"), dict) else {}
    bot_info = identities.get("bot") if isinstance(identities.get("bot"), dict) else {}
    user_info = identities.get("user") if isinstance(identities.get("user"), dict) else {}

    user_ready = bool(user_info.get("available")) and str(user_info.get("status") or "").lower() == "ready"
    bot_ready = bool(bot_info.get("available")) and str(bot_info.get("status") or "").lower() == "ready"
    if res.get("verified") is False and res.get("identity") == "user":
        user_ready = False

    return {
        "cliInstalled": True,
        "userReady": user_ready,
        "botReady": bot_ready,
        "userName": str(user_info.get("userName") or ""),
        "openId": str(user_info.get("openId") or ""),
        "tokenStatus": str(user_info.get("tokenStatus") or user_info.get("status") or ""),
        "note": str(user_info.get("message") or res.get("note") or ""),
        "raw": res,
    }


def _try_fetch_via_org_tree() -> dict[str, Any] | None:
    """Attempt Path 1: direct org tree traversal via bot contact OpenAPI."""
    dept_items: list[dict[str, Any]] = []
    page_token = ""
    while True:
        params: dict[str, Any] = {"fetch_child": True, "page_size": 50}
        if page_token:
            params["page_token"] = page_token
        res = _run_lark_json(
            [
                "api",
                "GET",
                "/open-apis/contact/v3/departments/0/children",
                "--as",
                "bot",
                "--params",
                json.dumps(params, ensure_ascii=False),
            ],
            timeout=30,
        )
        if not res.get("ok"):
            return None
        data = res.get("data") if isinstance(res.get("data"), dict) else {}
        items = data.get("items") if isinstance(data.get("items"), list) else []
        dept_items.extend([d for d in items if isinstance(d, dict)])
        if not data.get("has_more") or not data.get("page_token"):
            break
        page_token = str(data.get("page_token") or "")

    dept_by_id: dict[str, dict[str, Any]] = {}
    for d in dept_items:
        did = str(d.get("open_department_id") or d.get("department_id") or "").strip()
        if did:
            dept_by_id[did] = d

    def _dept_path(did: str) -> str:
        parts: list[str] = []
        seen: set[str] = set()
        cur = did
        while cur and cur != "0" and cur not in seen:
            seen.add(cur)
            info = dept_by_id.get(cur)
            if not info:
                break
            name = str(info.get("name") or "").strip()
            if name:
                parts.append(name)
            cur = str(info.get("parent_department_id") or "").strip()
        return "-".join(reversed(parts))

    dept_ids = ["0"] + list(dept_by_id.keys())
    by_open_id: dict[str, dict[str, str]] = {}
    for did in dept_ids:
        pt = ""
        while True:
            u_params: dict[str, Any] = {"department_id": did, "page_size": 50}
            if pt:
                u_params["page_token"] = pt
            ures = _run_lark_json(
                [
                    "api",
                    "GET",
                    "/open-apis/contact/v3/users/find_by_department",
                    "--as",
                    "bot",
                    "--params",
                    json.dumps(u_params, ensure_ascii=False),
                ],
                timeout=30,
            )
            if not ures.get("ok"):
                break
            udata = ures.get("data") if isinstance(ures.get("data"), dict) else {}
            uitems = udata.get("items") if isinstance(udata.get("items"), list) else []
            for u in uitems:
                if not isinstance(u, dict):
                    continue
                status = u.get("status") if isinstance(u.get("status"), dict) else {}
                if status.get("is_resigned") or status.get("is_frozen"):
                    continue
                oid = str(u.get("open_id") or "").strip()
                name = str(u.get("name") or u.get("en_name") or "").strip()
                if not oid or not name:
                    continue
                d_ids = u.get("department_ids") if isinstance(u.get("department_ids"), list) else []
                dept_paths = [_dept_path(str(x)) for x in d_ids if _dept_path(str(x))]
                dept_str = " / ".join(dict.fromkeys(dept_paths)) or _dept_path(did)
                email = str(u.get("enterprise_email") or u.get("email") or "").strip()
                by_open_id[oid] = {
                    "name": name,
                    "department": dept_str,
                    "email": email,
                    "externalId": oid,
                    "source": "lark",
                }
            if not udata.get("has_more") or not udata.get("page_token"):
                break
            pt = str(udata.get("page_token") or "")

    if not by_open_id:
        return None

    contacts = sorted(by_open_id.values(), key=lambda c: (c["department"], c["name"]))
    return {"contacts": contacts, "sourceChats": [], "method": "org_tree"}


def _extract_user_record(u: dict[str, Any]) -> dict[str, str] | None:
    if u.get("is_cross_tenant") is True:
        return None
    oid = str(u.get("open_id") or "").strip()
    name = str(u.get("localized_name") or u.get("name") or "").strip()
    dept = str(u.get("department") or "").strip()
    email = str(u.get("enterprise_email") or u.get("email") or "").strip()
    if not name or (oid and name == oid and not email and not dept):
        return None
    return {
        "name": name,
        "department": dept,
        "email": email,
        "externalId": oid,
        "source": "lark",
    }


def _fetch_via_user_groups() -> dict[str, Any]:
    """Path 2: aggregate member open_ids across user's group chats + chatted contacts, then batch search-user."""
    chats: list[dict[str, Any]] = []
    page_token = ""
    while True:
        args = ["im", "+chat-list", "--as", "user", "--page-size", "100", "--format", "json"]
        if page_token:
            args.extend(["--page-token", page_token])
        res = _run_lark_json(args, timeout=30)
        if not res.get("ok"):
            err = res.get("error") if isinstance(res.get("error"), dict) else {}
            raise RuntimeError(f"获取飞书群列表失败: {err.get('message') or json.dumps(res, ensure_ascii=False)}")
        data = res.get("data") if isinstance(res.get("data"), dict) else {}
        batch = data.get("chats") or data.get("items") or []
        if isinstance(batch, list):
            chats.extend([c for c in batch if isinstance(c, dict)])
        if not data.get("has_more") or not data.get("page_token"):
            break
        page_token = str(data.get("page_token") or "")

    def _chat_sort_key(c: dict[str, Any]) -> tuple[int, int]:
        cname = str(c.get("name") or "")
        is_priority = 1 if _PRIORITY_CHAT_RE.search(cname) else 0
        raw_cnt = c.get("user_count") or c.get("member_count") or c.get("members_count") or 0
        try:
            cnt = int(raw_cnt)
        except (TypeError, ValueError):
            cnt = 0
        return (is_priority, cnt)

    non_external = [c for c in chats if not c.get("external")]
    if not non_external:
        non_external = chats
    selected_chats = sorted(non_external, key=_chat_sort_key, reverse=True)[:60]

    candidate_ids: list[str] = []
    seen_ids: set[str] = set()
    source_chats: list[dict[str, Any]] = []

    def _add_oid(oid: Any) -> None:
        s = str(oid or "").strip()
        if s and s.startswith("ou_") and s not in seen_ids:
            seen_ids.add(s)
            candidate_ids.append(s)

    for c in selected_chats:
        cid = str(c.get("chat_id") or "").strip()
        if not cid:
            continue
        cname = str(c.get("name") or cid)
        try:
            mres = _run_lark_json(
                [
                    "im",
                    "+chat-members-list",
                    "--chat-id",
                    cid,
                    "--member-types",
                    "user",
                    "--page-all",
                    "--as",
                    "user",
                    "--format",
                    "json",
                ],
                timeout=45,
            )
        except Exception:
            continue
        if not mres.get("ok"):
            continue
        mdata = mres.get("data") if isinstance(mres.get("data"), dict) else {}
        users = mdata.get("users") or mdata.get("items") or []
        added_here = 0
        if isinstance(users, list):
            for u in users:
                if isinstance(u, dict):
                    oid = u.get("open_id") or u.get("member_id")
                    if oid:
                        before = len(seen_ids)
                        _add_oid(oid)
                        if len(seen_ids) > before:
                            added_here += 1
        source_chats.append({"chatId": cid, "name": cname, "memberCount": len(users) if isinstance(users, list) else 0, "newIds": added_here})

    contacts_by_key: dict[str, dict[str, str]] = {}

    def _ingest_user_list(ulist: Any) -> None:
        if not isinstance(ulist, list):
            return
        for u in ulist:
            if not isinstance(u, dict):
                continue
            rec = _extract_user_record(u)
            if not rec:
                continue
            key = rec["externalId"] or rec["email"].lower() or f"{rec['name']}::{rec['department']}"
            existing = contacts_by_key.get(key)
            if not existing:
                contacts_by_key[key] = rec
            else:
                if not existing["department"] and rec["department"]:
                    existing["department"] = rec["department"]
                if not existing["email"] and rec["email"]:
                    existing["email"] = rec["email"]

    for extra_args in (
        ["contact", "+search-user", "--user-ids", "me", "--as", "user", "--format", "json"],
        ["contact", "+search-user", "--has-chatted", "--exclude-external-users", "--page-size", "30", "--as", "user", "--format", "json"],
    ):
        try:
            xres = _run_lark_json(extra_args, timeout=25)
            if xres.get("ok"):
                xdata = xres.get("data") if isinstance(xres.get("data"), dict) else {}
                _ingest_user_list(xdata.get("users"))
        except Exception:
            pass

    for idx in range(0, len(candidate_ids), 100):
        chunk = candidate_ids[idx : idx + 100]
        bres = _run_lark_json(
            [
                "contact",
                "+search-user",
                "--user-ids",
                ",".join(chunk),
                "--exclude-external-users",
                "--as",
                "user",
                "--format",
                "json",
            ],
            timeout=30,
        )
        if not bres.get("ok"):
            err = bres.get("error") if isinstance(bres.get("error"), dict) else {}
            raise RuntimeError(f"批量查询联系人失败: {err.get('message') or json.dumps(bres, ensure_ascii=False)}")
        bdata = bres.get("data") if isinstance(bres.get("data"), dict) else {}
        _ingest_user_list(bdata.get("users"))

    contacts = sorted(contacts_by_key.values(), key=lambda c: (c["department"], c["name"]))
    return {"contacts": contacts, "sourceChats": source_chats, "method": "group_members"}


def fetch_lark_contacts() -> dict[str, Any]:
    """Fetch all accessible Liuxing colleague contacts (Name, Department, Email) via lark-cli."""
    auth = get_lark_auth_status()
    if not auth.get("cliInstalled"):
        raise RuntimeError(auth.get("note") or "未安装 lark-cli")

    if auth.get("botReady"):
        org_res = _try_fetch_via_org_tree()
        if org_res is not None and org_res.get("contacts"):
            return org_res

    if not auth.get("userReady"):
        raise RuntimeError(
            "飞书当前未登录用户身份（user token 已过期或未授权），且应用 Bot 暂无部门通讯录权限。"
            "请先运行 `lark-cli auth login --domain all` 完成授权后再同步。"
        )

    return _fetch_via_user_groups()


def export_contacts_files(contacts: list[dict[str, Any]], out_dir: Path) -> dict[str, str]:
    """Write contacts to CSV (UTF-8 BOM) and JSON files in out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "liuxing_contacts.csv"
    json_path = out_dir / "liuxing_contacts.json"

    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["姓名", "部门", "邮箱", "open_id"])
        for c in contacts:
            writer.writerow(
                [
                    str(c.get("name") or ""),
                    str(c.get("department") or ""),
                    str(c.get("email") or ""),
                    str(c.get("externalId") or ""),
                ]
            )

    payload = {
        "exportedAt": int(time.time() * 1000),
        "count": len(contacts),
        "contacts": contacts,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"csvPath": str(csv_path), "jsonPath": str(json_path)}


def import_to_mindbubble_api(contacts: list[dict[str, Any]], api_url: str, mode: str = "merge") -> dict[str, Any]:
    """POST contacts to a running MindBubble HTTP server."""
    url = f"{api_url.rstrip('/')}/api/contacts/import"
    body = json.dumps({"contacts": contacts, "mode": mode, "source": "lark"}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw)


def import_to_mindbubble_db(contacts: list[dict[str, Any]], db_path: Path, mode: str = "merge") -> dict[str, Any]:
    """Fallback direct SQLite import via MindBubbleStore when HTTP server is offline."""
    from mindbubble_server import MindBubbleStore

    store = MindBubbleStore(db_path)
    try:
        return store.import_contacts(contacts, mode=mode, default_source="lark")
    finally:
        store.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export Feishu contacts via lark-cli and import into 3DBubble (MindBubble).")
    parser.add_argument("--export-dir", type=Path, default=DEFAULT_EXPORT_DIR, help="Directory for CSV/JSON export files")
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help="MindBubble HTTP server URL")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="Fallback MindBubble SQLite DB path")
    parser.add_argument("--mode", choices=("merge", "replace", "replace_source"), default="merge", help="Import mode")
    parser.add_argument("--no-import", action="store_true", help="Only export CSV/JSON without importing into MindBubble")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and print summary without writing files or importing")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = fetch_lark_contacts()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1

    contacts = result["contacts"]
    summary: dict[str, Any] = {
        "ok": True,
        "method": result.get("method"),
        "count": len(contacts),
        "sourceChatsCount": len(result.get("sourceChats") or []),
    }

    if args.dry_run:
        summary["sample"] = contacts[:10]
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    files = export_contacts_files(contacts, args.export_dir)
    summary.update(files)

    if not args.no_import:
        try:
            imp = import_to_mindbubble_api(contacts, args.api_url, mode=args.mode)
            summary["importTarget"] = "api"
            summary["importResult"] = {k: imp.get(k) for k in ("inserted", "updated", "skipped", "total")}
        except Exception as api_exc:
            if args.db:
                imp = import_to_mindbubble_db(contacts, args.db, mode=args.mode)
                summary["importTarget"] = "sqlite"
                summary["apiFallbackReason"] = str(api_exc)
                summary["importResult"] = {k: imp.get(k) for k in ("inserted", "updated", "skipped", "total")}
            else:
                summary["importError"] = str(api_exc)
                print(json.dumps(summary, ensure_ascii=False, indent=2), file=sys.stderr)
                return 2

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
