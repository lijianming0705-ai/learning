from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from app.config import AGY_BIN, DEFAULT_MODEL, ensure_data_dirs
from app.schemas import AgyModelOut, HighlightOut, MessageOut, RagMemoryRef

_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_MODEL_LINE_RE = re.compile(r"^([a-z0-9][a-z0-9._-]{2,})\s+(.+)$")


def strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text)


def parse_agy_models_output(raw: str) -> list[AgyModelOut]:
    cleaned = strip_ansi(raw).replace("\r", "\n")
    models: list[AgyModelOut] = []
    seen: set[str] = set()
    for line in cleaned.splitlines():
        line = re.sub(r"^.*?Fetching available models\.\.\.\s*", "", line).strip()
        if not line:
            continue
        m = _MODEL_LINE_RE.match(line)
        if m:
            model_id, name = m.group(1).strip(), m.group(2).strip()
            if model_id not in seen:
                seen.add(model_id)
                models.append(AgyModelOut(id=model_id, name=name))
    return models


@dataclass
class AgyWorker:
    model: str
    proc: asyncio.subprocess.Process
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = field(default_factory=time.monotonic)
    agy_conversation_id: str | None = None
    stderr_lines: list[str] = field(default_factory=list)
    _stderr_task: asyncio.Task[None] | None = None
    turn_count: int = 0

    @classmethod
    async def spawn(cls, model: str) -> AgyWorker:
        paths = ensure_data_dirs()
        proc = await asyncio.create_subprocess_exec(
            AGY_BIN,
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--disable-slash-commands",
            "--model",
            model,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(paths.agy_workdir),
            limit=4 * 1024 * 1024,
        )
        worker = cls(model=model, proc=proc)
        worker._stderr_task = asyncio.create_task(worker._drain_stderr())
        return worker

    async def _drain_stderr(self) -> None:
        assert self.proc.stderr is not None
        try:
            while True:
                line = await self.proc.stderr.readline()
                if not line:
                    break
                decoded = strip_ansi(line.decode("utf-8", errors="replace")).strip()
                if decoded:
                    self.stderr_lines.append(decoded)
                    if len(self.stderr_lines) > 30:
                        self.stderr_lines.pop(0)
        except Exception:
            pass

    def is_alive(self) -> bool:
        return self.proc.returncode is None and self.proc.stdin is not None and self.proc.stdout is not None

    async def ask_stream(self, prompt: str, timeout: float = 240.0) -> AsyncIterator[tuple[str, Any]]:
        if not self.is_alive():
            raise RuntimeError("agy worker process is not alive")

        async with self.lock:
            assert self.proc.stdin is not None
            assert self.proc.stdout is not None
            self.last_used = time.monotonic()

            if self.turn_count == 0:
                yield ("status", "connecting_agy")
            else:
                yield ("status", "generating")

            payload = json.dumps({"event": "user", "message": {"content": prompt}}, ensure_ascii=False) + "\n"
            self.proc.stdin.write(payload.encode("utf-8"))
            await self.proc.stdin.drain()

            emitted_generating = self.turn_count > 0
            while True:
                raw_line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=timeout)
                if not raw_line:
                    err_tail = " | ".join(self.stderr_lines[-5:])
                    raise RuntimeError(f"agy process exited unexpectedly ({self.proc.returncode}): {err_tail}")

                line_str = raw_line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue
                try:
                    event_obj = json.loads(line_str)
                except json.JSONDecodeError:
                    continue

                ev_type = event_obj.get("event")
                if ev_type == "init":
                    self.agy_conversation_id = event_obj.get("conversation_id") or self.agy_conversation_id
                    if not emitted_generating:
                        emitted_generating = True
                        yield ("status", "generating")
                elif ev_type == "step_update":
                    su = event_obj.get("step_update") or {}
                    if su.get("step_type") == "agent_response":
                        if not emitted_generating:
                            emitted_generating = True
                            yield ("status", "generating")
                        delta = su.get("text_delta") or ""
                        if delta:
                            yield ("delta", delta)
                elif ev_type == "result":
                    res = event_obj.get("result") or {}
                    self.turn_count += 1
                    self.last_used = time.monotonic()
                    if res.get("conversation_id"):
                        self.agy_conversation_id = res["conversation_id"]
                    if res.get("status") != "SUCCESS":
                        err_msg = res.get("error") or f"agy returned status {res.get('status')}"
                        raise RuntimeError(err_msg)
                    yield ("result", res)
                    return

    async def close(self) -> None:
        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
        if self.proc.returncode is None:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), timeout=3.0)
            except Exception:
                try:
                    self.proc.kill()
                    await self.proc.wait()
                except Exception:
                    pass


class AgyService:
    """Manages antigravity-cli (`agy`) model discovery and pooled stream-json workers."""

    def __init__(self, max_workers: int = 4, idle_ttl_seconds: float = 600.0) -> None:
        self.max_workers = max_workers
        self.idle_ttl_seconds = idle_ttl_seconds
        self._workers: OrderedDict[tuple[str, str], AgyWorker] = OrderedDict()
        self._standby: AgyWorker | None = None
        self._standby_lock = asyncio.Lock()
        self._pool_lock = asyncio.Lock()
        self._models_cache: list[AgyModelOut] | None = None
        self._models_cached_at: float = 0.0

    def agy_path(self) -> str | None:
        return shutil.which(AGY_BIN)

    def is_available(self) -> bool:
        return self.agy_path() is not None

    def warm_worker_count(self) -> int:
        count = sum(1 for w in self._workers.values() if w.is_alive())
        if self._standby and self._standby.is_alive():
            count += 1
        return count

    async def list_models(self, force_refresh: bool = False) -> list[AgyModelOut]:
        now = time.monotonic()
        if not force_refresh and self._models_cache and (now - self._models_cached_at < 300):
            return self._models_cache

        if not self.is_available():
            return [AgyModelOut(id=DEFAULT_MODEL, name="Gemini 3.8 Flash (Medium)")]

        proc = await asyncio.create_subprocess_exec(
            AGY_BIN,
            "models",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20.0)
            parsed = parse_agy_models_output(stdout.decode("utf-8", errors="replace"))
            if parsed:
                self._models_cache = parsed
                self._models_cached_at = now
                return parsed
        except Exception:
            if proc.returncode is None:
                proc.kill()

        return self._models_cache or [
            AgyModelOut(id="gemini-4-neon-high", name="Gemini 4 Neon (High)"),
            AgyModelOut(id="gemini-3.8-flash-medium", name="Gemini 3.8 Flash (Medium)"),
            AgyModelOut(id="gemini-3.8-flash-low", name="Gemini 3.8 Flash (Low)"),
        ]

    async def prewarm_standby(self, model: str = DEFAULT_MODEL) -> None:
        if not self.is_available():
            return
        async with self._standby_lock:
            if self._standby and self._standby.is_alive() and self._standby.model == model:
                return
            if self._standby:
                await self._standby.close()
                self._standby = None
            try:
                self._standby = await AgyWorker.spawn(model)
            except Exception:
                self._standby = None

    async def _acquire_worker(self, thread_key: str, model: str) -> AgyWorker:
        key = (thread_key, model)
        async with self._pool_lock:
            await self._cleanup_expired_locked()
            existing = self._workers.get(key)
            if existing and existing.is_alive():
                self._workers.move_to_end(key)
                return existing
            if existing:
                await existing.close()
                self._workers.pop(key, None)

        # Try claiming standby worker if model matches
        async with self._standby_lock:
            if self._standby and self._standby.is_alive() and self._standby.model == model:
                worker = self._standby
                self._standby = None
                asyncio.create_task(self.prewarm_standby(model))
                async with self._pool_lock:
                    await self._insert_worker_locked(key, worker)
                return worker

        worker = await AgyWorker.spawn(model)
        async with self._pool_lock:
            await self._insert_worker_locked(key, worker)
        return worker

    async def _insert_worker_locked(self, key: tuple[str, str], worker: AgyWorker) -> None:
        while len(self._workers) >= self.max_workers:
            _, oldest = self._workers.popitem(last=False)
            await oldest.close()
        self._workers[key] = worker

    async def _cleanup_expired_locked(self) -> None:
        now = time.monotonic()
        expired_keys = [
            k for k, w in self._workers.items() if (not w.is_alive()) or (now - w.last_used > self.idle_ttl_seconds)
        ]
        for k in expired_keys:
            w = self._workers.pop(k)
            await w.close()

    async def _evict_worker(self, thread_key: str, model: str) -> None:
        key = (thread_key, model)
        async with self._pool_lock:
            w = self._workers.pop(key, None)
        if w:
            await w.close()

    async def stream_prompt(
        self,
        *,
        thread_key: str,
        model: str,
        prompt: str,
    ) -> AsyncIterator[tuple[str, Any]]:
        if not self.is_available():
            yield ("error", f"未检测到 `{AGY_BIN}` 命令，请先安装并登录 antigravity-cli。")
            return

        try:
            worker = await self._acquire_worker(thread_key, model)
            async for ev in worker.ask_stream(prompt):
                yield ev
        except Exception as exc:
            await self._evict_worker(thread_key, model)
            yield ("error", f"调用 agy 失败: {exc}")

    async def shutdown(self) -> None:
        async with self._standby_lock:
            if self._standby:
                await self._standby.close()
                self._standby = None
        async with self._pool_lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for w in workers:
            await w.close()


def _format_rag_memories(memories: list[RagMemoryRef]) -> str:
    if not memories:
        return "（本次未检索到相关历史记忆）"
    lines: list[str] = []
    for idx, mem in enumerate(memories, start=1):
        label = {
            "qa_memory": "历史对话记忆",
            "highlight_note": "历史划线笔记",
            "source_chunk": "关联原文片段",
        }.get(mem.doc_type, mem.doc_type)
        loc = f"《{mem.source_title}》" if mem.source_title else f"笔记本《{mem.notebook_title}》"
        if mem.page_number:
            loc += f" P.{mem.page_number}"
        lines.append(f"[{label} #{idx}] {loc} (相关度 {mem.score:.2f})\n{mem.content[:900]}")
    return "\n\n".join(lines)


def _format_prior_turns(messages: list[MessageOut], limit: int = 8) -> str:
    if not messages:
        return ""
    recent = messages[-limit:]
    parts = []
    for m in recent:
        role = "用户" if m.role == "user" else "AI助手"
        parts.append(f"{role}：{m.content[:1200]}")
    return "\n\n".join(parts)


def build_highlight_prompt(
    *,
    highlight: HighlightOut,
    page_context: str,
    prior_messages: list[MessageOut],
    rag_memories: list[RagMemoryRef],
    question: str,
    extra_context: str | None = None,
) -> str:
    prior_block = _format_prior_turns(prior_messages)
    sections = [
        "你是一个个人学习平台的 AI 导师。请直接根据下面提供的学习素材、划线片段、个人笔记与检索到的 RAG 历史记忆来清晰、严谨地回答用户问题。",
        "重要约束：不要调用任何文件读写、Shell 命令、浏览器或代码工具；直接输出 Markdown 格式的学习解答。默认使用与用户提问一致的语言（中文提问则用中文回答）。",
        f"【所在资料】《{highlight.source_title}》 第 {highlight.page_number} 页",
    ]
    if highlight.quote_text:
        sections.append(f"【用户划线片段】\n{highlight.quote_text}")
    else:
        sections.append("【用户划线方式】PDF 框选区域（请结合所在页面上下文理解）")

    if highlight.note_text:
        sections.append(f"【用户自写笔记】\n{highlight.note_text}")
    if page_context:
        sections.append(f"【周边页面上下文】\n{page_context}")
    if extra_context:
        sections.append(f"【用户补充引用】\n{extra_context}")
    if rag_memories:
        sections.append(f"【RAG 检索到的历史学习记忆与相关内容】\n{_format_rag_memories(rag_memories)}")
    if prior_block:
        sections.append(f"【本条划线的历史问答记录】\n{prior_block}")
    sections.append(f"【当前提问】\n{question}")
    return "\n\n".join(sections)


def build_notebook_chat_prompt(
    *,
    notebook_title: str,
    source_context: str,
    prior_messages: list[MessageOut],
    rag_memories: list[RagMemoryRef],
    question: str,
    extra_context: str | None = None,
) -> str:
    prior_block = _format_prior_turns(prior_messages)
    sections = [
        f"你是笔记本《{notebook_title}》的个人学习 AI 助手。请结合资料内容与 RAG 检索到的历史对话记忆回答用户问题。",
        "重要约束：不要调用任何文件读写、Shell 命令、浏览器或代码工具；直接输出结构清晰的 Markdown 回答。默认使用与用户提问一致的语言。",
    ]
    if source_context:
        sections.append(f"【当前打开资料】\n{source_context}")
    if extra_context:
        sections.append(f"【用户引用记忆】\n{extra_context}")
    if rag_memories:
        sections.append(f"【RAG 检索到的历史对话与原文片段】\n{_format_rag_memories(rag_memories)}")
    if prior_block:
        sections.append(f"【历史对话上下文】\n{prior_block}")
    sections.append(f"【当前提问】\n{question}")
    return "\n\n".join(sections)


agy_service = AgyService()
