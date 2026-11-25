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
from app.services.tts_service import detect_language

_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_MODEL_LINE_RE = re.compile(r"^([a-z0-9][a-z0-9._-]{2,})\s+(.+)$")


def strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text)


def sort_models_fastest_first(models: list[AgyModelOut]) -> list[AgyModelOut]:
    """Order models with the fastest Flash-Low model first, preserving CLI order otherwise."""

    def priority(m: AgyModelOut) -> tuple[int, str]:
        mid = m.id.lower()
        if mid == "gemini-3.8-flash-low":
            return (0, mid)
        if "flash" in mid and mid.endswith("-low"):
            return (1, mid)
        if "flash" in mid and mid.endswith("-medium"):
            return (2, mid)
        if "flash" in mid:
            return (3, mid)
        if mid.endswith("-low"):
            return (4, mid)
        return (5, mid)

    indexed = list(enumerate(models))
    indexed.sort(key=lambda item: (priority(item[1])[0], item[0]))
    return [m for _, m in indexed]


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
    return sort_models_fastest_first(models)


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
                    st_type = su.get("step_type")
                    if st_type == "tool" and su.get("state") == "ACTIVE":
                        yield ("status", "analyzing_image")
                    elif st_type == "agent_response":
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
            return [AgyModelOut(id=DEFAULT_MODEL, name="Gemini 3.8 Flash (Low)")]

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
            AgyModelOut(id="gemini-3.8-flash-low", name="Gemini 3.8 Flash (Low)"),
            AgyModelOut(id="gemini-3.8-flash-medium", name="Gemini 3.8 Flash (Medium)"),
            AgyModelOut(id="gemini-4-neon-high", name="Gemini 4 Neon (High)"),
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
    image_path: str | None = None,
) -> str:
    prior_block = _format_prior_turns(prior_messages)
    if image_path:
        sections = [
            "你是一个具备多模态视觉与 OCR 能力的个人学习 AI 导师。",
            f"用户提供了一张从学习资料框选出来的截图文件，路径为：{image_path}",
            f"请务必先调用 read_file（或 view_file）工具读取查看该图片 {image_path}，识别其中的文字、公式、表格或图表语义，再结合下方信息严谨回答用户问题。",
            "重要约束：除读取上述指定截图外，不要调用任何其他文件写入、Shell 命令、浏览器或代码工具；直接输出 Markdown 格式的学习解答。默认使用与用户提问一致的语言（中文提问则用中文回答）。",
            f"【所在资料】《{highlight.source_title}》 第 {highlight.page_number} 页",
        ]
    else:
        sections = [
            "你是一个个人学习平台的 AI 导师。请直接根据下面提供的学习素材、划线片段、个人笔记与检索到的 RAG 历史记忆来清晰、严谨地回答用户问题。",
            "重要约束：不要调用任何文件读写、Shell 命令、浏览器或代码工具；直接输出 Markdown 格式的学习解答。默认使用与用户提问一致的语言（中文提问则用中文回答）。",
            f"【所在资料】《{highlight.source_title}》 第 {highlight.page_number} 页",
        ]

    if highlight.quote_text:
        sections.append(f"【用户划线/OCR 文本】\n{highlight.quote_text}")
    elif image_path:
        sections.append("【用户划线方式】区域框选截图（请直接查看图片进行 OCR 与语义理解）")
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


def build_classify_sources_prompt(*, notebook_title: str, sources: list[dict[str, str]]) -> str:
    items_block = "\n".join(
        f"- id={s['id']} | type={s['type']} | title={s['title']} | excerpt={s['excerpt'][:260]}"
        for s in sources
    )
    return (
        f"你是笔记本《{notebook_title}》的知识整理助手。请根据下方学习素材的标题和摘要，将所有素材归类为 2 到 6 个简洁清晰的中文主题分类（每个分类名称 2-8 个字，例如：核心论文、原理教程、实战笔记、参考资料、源码解析等）。\n"
        "重要约束：不要调用任何工具；必须仅输出合法的 JSON 对象，不要包含额外解释文字，格式严格如下：\n"
        '{"assignments": [{"id": "<素材id>", "category": "<分类名>"}]}\n\n'
        f"【待分类素材列表】\n{items_block}"
    )


def parse_classification_json(raw: str) -> dict[str, str]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        text = text[start : end + 1]
    data = json.loads(text)
    assignments = data.get("assignments") if isinstance(data, dict) else None
    if not isinstance(assignments, list):
        return {}
    result: dict[str, str] = {}
    for item in assignments:
        if isinstance(item, dict) and item.get("id") and item.get("category"):
            result[str(item["id"]).strip()] = str(item["category"]).strip()
    return result


async def classify_sources_with_agy(
    *,
    notebook_id: str,
    notebook_title: str,
    sources: list[dict[str, str]],
    model: str,
) -> dict[str, str]:
    if not sources:
        return {}
    prompt = build_classify_sources_prompt(notebook_title=notebook_title, sources=sources)
    chunks: list[str] = []
    result_text = ""
    async for kind, data in agy_service.stream_prompt(
        thread_key=f"classify:{notebook_id}",
        model=model,
        prompt=prompt,
    ):
        if kind == "delta":
            chunks.append(str(data))
        elif kind == "result" and isinstance(data, dict):
            result_text = str(data.get("response") or "")
        elif kind == "error":
            raise RuntimeError(str(data))
    raw_output = result_text or "".join(chunks)
    return parse_classification_json(raw_output)


def build_ocr_prompt(*, image_path: str, source_title: str, page_number: int) -> str:
    return (
        "你是一个高精度的学术 OCR 与图文识别助手。\n"
        f"请调用 read_file（或 view_file）工具读取图片：{image_path}（来自《{source_title}》第 {page_number} 页框选区域）。\n"
        "请准确转录图片中的全部可见文字，保持原有的段落、列表或标题结构；若包含数学公式请使用标准 LaTeX 语法（$...$ 或 $$...$$）；"
        "若主要是图表或示意图，请先提取图内所有文字与标签，再用简洁清晰的中文概括其核心含义。\n"
        "重要约束：除读取上述指定图片外不要调用其他工具，也不要输出多余寒暄，直接输出识别结果。"
    )


async def ocr_highlight_image(
    *,
    highlight_id: str,
    image_path: str,
    source_title: str,
    page_number: int,
    model: str,
) -> str:
    prompt = build_ocr_prompt(
        image_path=image_path,
        source_title=source_title,
        page_number=page_number,
    )
    chunks: list[str] = []
    result_text = ""
    async for kind, data in agy_service.stream_prompt(
        thread_key=f"ocr:{highlight_id}",
        model=model,
        prompt=prompt,
    ):
        if kind == "delta":
            chunks.append(str(data))
        elif kind == "result" and isinstance(data, dict):
            result_text = str(data.get("response") or "")
        elif kind == "error":
            raise RuntimeError(str(data))
    return (result_text or "".join(chunks)).strip()


def _extract_json_object(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        text = text[start : end + 1]
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Expected JSON object")
    return data


def _clean_text_for_speech_fallback(text: str) -> str:
    cleaned = re.sub(r"-\s*\n\s*", "", text or "")
    cleaned = re.sub(r"\[\s*\d+(?:\s*[-–,]\s*\d+)*\s*\]", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def build_selection_speech_prompt(*, text: str, mode: str = "natural") -> str:
    mode_hint = (
        "若原文为英文，spoken_text 保留自然地道英文，同时可在 brief_meaning 中给出中文释义；"
        if mode == "natural"
        else "若原文为英文，spoken_text 可先用自然英文朗读核心句再附简短中文顺读衔接；"
    )
    return (
        "你是一个专业的语音文本整理与发音指导助手。\n"
        "请将用户选中的学习文本改写为适合 TTS 语音合成直接朗读的自然口语文本（spoken_text），"
        "需要自动修复 PDF 断词换行、去除多余引用方括号 [1, 2]、将数学公式/符号/常见缩写展开为自然口语读法。\n"
        f"{mode_hint}"
        "如果选中内容主要是英文单词、短语或句子，请补充简洁的音标或连读提示（ipa_or_guide）以及一句简明中文释义（brief_meaning）；"
        "如果是中文内容，spoken_text 保持自然流畅中文，ipa_or_guide 可留空或标注生僻字/外文术语读音，brief_meaning 用一句话概括核心含义。\n"
        "重要约束：不要调用任何工具；必须仅输出严格合法的 JSON 对象，不要包含额外解释文字，格式如下：\n"
        '{"spoken_text": "...", "language": "zh-CN 或 en-US", "ipa_or_guide": "...", "brief_meaning": "..."}\n\n'
        f"【用户选中文本】\n{text[:4000]}"
    )


async def generate_selection_speech_with_agy(
    *,
    text: str,
    model: str,
    mode: str = "natural",
) -> dict[str, str]:
    prompt = build_selection_speech_prompt(text=text, mode=mode)
    chunks: list[str] = []
    result_text = ""
    try:
        async for kind, data in agy_service.stream_prompt(
            thread_key=f"speech_sel:{mode}",
            model=model,
            prompt=prompt,
        ):
            if kind == "delta":
                chunks.append(str(data))
            elif kind == "result" and isinstance(data, dict):
                result_text = str(data.get("response") or "")
            elif kind == "error":
                raise RuntimeError(str(data))
        raw_output = (result_text or "".join(chunks)).strip()
        parsed = _extract_json_object(raw_output)
        spoken = str(parsed.get("spoken_text") or "").strip()
        if not spoken:
            raise ValueError("Empty spoken_text")
        lang = detect_language(spoken, hint=str(parsed.get("language") or ""))
        return {
            "spoken_text": spoken,
            "language": lang,
            "ipa_or_guide": str(parsed.get("ipa_or_guide") or "").strip(),
            "brief_meaning": str(parsed.get("brief_meaning") or "").strip(),
        }
    except Exception:
        cleaned = _clean_text_for_speech_fallback(text)
        return {
            "spoken_text": cleaned,
            "language": detect_language(cleaned),
            "ipa_or_guide": "",
            "brief_meaning": "",
        }


def _split_page_text_chunks(page_number: int, text: str, max_chars: int = 3200) -> list[tuple[int, str]]:
    clean = (text or "").strip()
    if not clean:
        return []
    if len(clean) <= max_chars:
        return [(page_number, clean)]
    chunks: list[tuple[int, str]] = []
    start = 0
    while start < len(clean):
        end = min(len(clean), start + max_chars)
        if end < len(clean):
            split_at = max(clean.rfind("\n", start, end), clean.rfind("。", start, end), clean.rfind(". ", start, end))
            if split_at > start + 600:
                end = split_at + 1
        part = clean[start:end].strip()
        if part:
            chunks.append((page_number, part))
        start = end
    return chunks


def _batch_pages_for_speech(
    pages: list[tuple[int, str]],
    target_chars: int = 3200,
    max_batches: int = 12,
) -> list[list[tuple[int, str]]]:
    expanded: list[tuple[int, str]] = []
    for page_num, txt in pages:
        expanded.extend(_split_page_text_chunks(page_num, txt, max_chars=target_chars))
    batches: list[list[tuple[int, str]]] = []
    current_batch: list[tuple[int, str]] = []
    current_len = 0
    for item in expanded:
        item_len = len(item[1])
        if current_batch and (current_len + item_len > target_chars):
            batches.append(current_batch)
            if len(batches) >= max_batches:
                return batches
            current_batch = []
            current_len = 0
        current_batch.append(item)
        current_len += item_len
    if current_batch and len(batches) < max_batches:
        batches.append(current_batch)
    return batches


def _fallback_segments_for_batch(batch_pages: list[tuple[int, str]]) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for page_num, text in batch_pages:
        cleaned = _clean_text_for_speech_fallback(text)
        if not cleaned:
            continue
        sentences = [s.strip() for s in re.split(r"(?<=[。！？.!?])\s+", cleaned) if s.strip()]
        if not sentences:
            sentences = [cleaned]
        buf = ""
        for sent in sentences:
            if buf and len(buf) + len(sent) > 240:
                segments.append(
                    {
                        "page_number": page_num,
                        "heading": "",
                        "original_excerpt": buf[:48],
                        "spoken_text": buf,
                        "language": detect_language(buf),
                    }
                )
                buf = sent
            else:
                buf = f"{buf} {sent}".strip() if buf else sent
        if buf:
            segments.append(
                {
                    "page_number": page_num,
                    "heading": "",
                    "original_excerpt": buf[:48],
                    "spoken_text": buf,
                    "language": detect_language(buf),
                }
            )
    return segments


def build_full_speech_batch_prompt(
    *,
    source_title: str,
    batch_pages: list[tuple[int, str]],
    batch_index: int,
    total_batches: int,
) -> str:
    pages_block = "\n\n".join(f"--- [Page {p}] ---\n{txt}" for p, txt in batch_pages)
    return (
        f"你是一个高水准的知识播客撰稿人与语音朗读编排助手，正在为学习资料《{source_title}》制作“全文自然朗读脚本”（第 {batch_index + 1}/{total_batches} 批）。\n"
        "请将下方带页码标记的原文改写为适合人耳聆听、连贯自然的口语化分段朗读文本（segments）。\n"
        "编写规则：\n"
        "1. 保留忠实原意，同时去掉页眉页脚、孤立页码、纯 URL 和 `[1, 2]` 等引用编号，修复 PDF 断词换行。\n"
        "2. 数学公式、特殊符号、表格摘要或英文缩写请改写成自然流畅的口语表达，方便直接语音合成播放。\n"
        "3. 每段 spoken_text 控制在 80~280 字（约 2~5 句话），保持原文先后顺序，便于用户按段落播放与定位。\n"
        "4. 每段必须精确标注所属整数 page_number，并在 original_excerpt 中原样摘录该段对应原文开头的 20~55 个字符（用于后续划词定位快速跳转到此段朗读位置）。\n"
        "5. language 严格填 zh-CN 或 en-US。\n"
        "重要约束：不要调用任何工具；必须仅输出严格合法的 JSON 对象，不要包含额外说明文字，格式如下：\n"
        '{"segments": [{"page_number": 1, "heading": "简短小节标题或空字符串", "original_excerpt": "对应原文开头原样摘录", "spoken_text": "自然口语化朗读文本", "language": "zh-CN"}]}\n\n'
        f"【原文内容】\n{pages_block}"
    )


async def generate_full_speech_script_with_agy(
    *,
    source_id: str,
    source_title: str,
    pages: list[tuple[int, str]],
    model: str,
) -> list[dict[str, Any]]:
    batches = _batch_pages_for_speech(pages)
    if not batches:
        return []

    all_segments: list[dict[str, Any]] = []
    total_batches = len(batches)
    for idx, batch_pages in enumerate(batches):
        prompt = build_full_speech_batch_prompt(
            source_title=source_title,
            batch_pages=batch_pages,
            batch_index=idx,
            total_batches=total_batches,
        )
        chunks: list[str] = []
        result_text = ""
        parsed_segments: list[dict[str, Any]] = []
        try:
            async for kind, data in agy_service.stream_prompt(
                thread_key=f"speech_full:{source_id}",
                model=model,
                prompt=prompt,
            ):
                if kind == "delta":
                    chunks.append(str(data))
                elif kind == "result" and isinstance(data, dict):
                    result_text = str(data.get("response") or "")
                elif kind == "error":
                    raise RuntimeError(str(data))
            raw_output = (result_text or "".join(chunks)).strip()
            parsed_obj = _extract_json_object(raw_output)
            raw_segs = parsed_obj.get("segments")
            valid_pages = [p for p, _ in batch_pages]
            default_page = valid_pages[0] if valid_pages else 1
            if isinstance(raw_segs, list):
                for item in raw_segs:
                    if not isinstance(item, dict):
                        continue
                    spoken = str(item.get("spoken_text") or "").strip()
                    if not spoken:
                        continue
                    try:
                        pnum = int(item.get("page_number") or default_page)
                    except (TypeError, ValueError):
                        pnum = default_page
                    if valid_pages and pnum not in valid_pages:
                        pnum = min(valid_pages, key=lambda vp: abs(vp - pnum))
                    parsed_segments.append(
                        {
                            "page_number": max(1, pnum),
                            "heading": str(item.get("heading") or "").strip()[:120],
                            "original_excerpt": str(item.get("original_excerpt") or "").strip()[:160] or spoken[:48],
                            "spoken_text": spoken,
                            "language": detect_language(spoken, hint=str(item.get("language") or "")),
                        }
                    )
        except Exception:
            parsed_segments = []

        if not parsed_segments:
            parsed_segments = _fallback_segments_for_batch(batch_pages)
        all_segments.extend(parsed_segments)

    return all_segments


agy_service = AgyService()
