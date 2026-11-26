from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
from pathlib import Path

from app.config import (
    DEFAULT_MLX_TTS_MODEL,
    DEFAULT_MLX_TTS_VOICE,
    DEFAULT_TTS_ENGINE,
    ensure_data_dirs,
)

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")
_WHITESPACE_RE = re.compile(r"\s+")
_WORKER_SCRIPT = Path(__file__).resolve().parent / "mlx_tts_worker.py"
_DEFAULT_MLX_VENV_PYTHON = Path.home() / ".local" / "share" / "mlx-audio-tts" / ".venv" / "bin" / "python"

MLX_TTS_VOICES: tuple[str, ...] = (
    "Vivian",
    "Serena",
    "Uncle_Fu",
    "Ryan",
    "Aiden",
    "Ono_Anna",
    "Sohee",
    "Eric",
    "Dylan",
)

EDGE_VOICES: dict[str, str] = {
    "zh-CN": "zh-CN-XiaoxiaoNeural",
    "en-US": "en-US-AriaNeural",
}

MAC_VOICES: dict[str, str] = {
    "zh-CN": "Tingting",
    "en-US": "Samantha",
}


def detect_language(text: str, hint: str | None = None) -> str:
    """Detect zh-CN vs en-US for TTS voice selection."""
    if hint:
        norm = hint.strip().lower()
        if norm.startswith("zh") or norm in {"chinese", "beijing_dialect", "sichuan_dialect"}:
            return "zh-CN"
        if norm.startswith("en") or norm == "english":
            return "en-US"
    compact = re.sub(r"\s+", "", text or "")
    if not compact:
        return "zh-CN"
    cjk_count = len(_CJK_RE.findall(compact))
    return "zh-CN" if (cjk_count / len(compact)) >= 0.15 else "en-US"


def mlx_lang_code(language: str) -> str:
    """Map internal language tags to Qwen3-TTS lang_code values."""
    return "chinese" if language == "zh-CN" else "english"


def clean_speech_text(text: str, max_chars: int = 4000) -> str:
    cleaned = re.sub(r"[`*_#>]+", " ", text or "")
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    return cleaned[:max_chars].strip()


def clip_cache_path(
    spoken_text: str,
    language: str,
    *,
    engine: str = DEFAULT_TTS_ENGINE,
    voice: str = DEFAULT_MLX_TTS_VOICE,
    model: str = DEFAULT_MLX_TTS_MODEL,
) -> Path:
    lang = detect_language(spoken_text, hint=language)
    digest = hashlib.sha256(
        f"{engine}:{voice}:{model}:{lang}:{spoken_text}".encode("utf-8")
    ).hexdigest()[:16]
    return ensure_data_dirs().speech_dir / f"sel_{digest}"


def media_type_for_audio(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".wav":
        return "audio/wav"
    if suffix == ".mp3":
        return "audio/mpeg"
    if suffix == ".m4a":
        return "audio/mp4"
    if suffix == ".aiff":
        return "audio/aiff"
    return "application/octet-stream"


def _preferred_exts_for_engine(engine: str) -> tuple[str, ...]:
    if engine == "mlx-audio":
        return (".wav", ".mp3", ".m4a")
    if engine == "edge-tts":
        return (".mp3", ".m4a", ".wav")
    return (".m4a", ".mp3", ".wav")


def _find_cached_audio(out_base_path: Path, exts: tuple[str, ...] = (".wav", ".mp3", ".m4a")) -> Path | None:
    for ext in exts:
        candidate = out_base_path.with_suffix(ext)
        if candidate.exists() and candidate.is_file() and candidate.stat().st_size > 128:
            return candidate
    return None


class MlxTtsService:
    """Manages a lazy-loaded, warm persistent mlx-audio worker subprocess."""

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    def find_python(self) -> str | None:
        env_py = os.environ.get("STUDY_MLX_TTS_PYTHON", "").strip()
        if env_py:
            p = Path(env_py).expanduser()
            if p.exists() and os.access(p, os.X_OK):
                return str(p)
        if _DEFAULT_MLX_VENV_PYTHON.exists() and os.access(_DEFAULT_MLX_VENV_PYTHON, os.X_OK):
            return str(_DEFAULT_MLX_VENV_PYTHON)
        if importlib.util.find_spec("mlx_audio") is not None:
            return sys.executable
        return None

    def is_available(self) -> bool:
        return self.find_python() is not None and _WORKER_SCRIPT.exists()

    def engine_name(self) -> str:
        if self.is_available():
            return "mlx-audio"
        if importlib.util.find_spec("edge_tts") is not None:
            return "edge-tts"
        if shutil.which("say") and shutil.which("afconvert"):
            return "macos-say"
        return "none"

    async def _ensure_worker(self) -> asyncio.subprocess.Process | None:
        if self._proc is not None and self._proc.returncode is None:
            return self._proc
        py_bin = self.find_python()
        if not py_bin or not _WORKER_SCRIPT.exists():
            return None
        self._proc = await asyncio.create_subprocess_exec(
            py_bin,
            str(_WORKER_SCRIPT),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=4 * 1024 * 1024,
        )
        return self._proc

    async def _reset_worker(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception:
            pass
        if proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    async def synthesize_mlx(
        self,
        text: str,
        lang_code: str,
        out_wav_path: Path,
        *,
        voice: str = DEFAULT_MLX_TTS_VOICE,
        model: str = DEFAULT_MLX_TTS_MODEL,
        timeout: float = 90.0,
    ) -> Path | None:
        async with self._lock:
            proc = await self._ensure_worker()
            if proc is None or proc.stdin is None or proc.stdout is None:
                return None
            req = {
                "text": text,
                "voice": voice or DEFAULT_MLX_TTS_VOICE,
                "lang_code": lang_code,
                "model": model or DEFAULT_MLX_TTS_MODEL,
                "out_path": str(out_wav_path),
            }
            try:
                proc.stdin.write((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
                await proc.stdin.drain()
                raw_resp = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
                if not raw_resp:
                    await self._reset_worker()
                    return None
                resp = json.loads(raw_resp.decode("utf-8"))
                if resp.get("ok") and out_wav_path.exists() and out_wav_path.stat().st_size > 128:
                    return out_wav_path
                return None
            except Exception:
                await self._reset_worker()
                return None

    async def shutdown(self) -> None:
        async with self._lock:
            await self._reset_worker()


tts_service = MlxTtsService()


async def _synthesize_edge(clean_text: str, lang: str, out_base_path: Path) -> Path | None:
    try:
        import edge_tts  # type: ignore[import-untyped]
    except ImportError:
        return None

    mp3_final = out_base_path.with_suffix(".mp3")
    mp3_tmp = out_base_path.with_suffix(".tmp.mp3")
    try:
        voice = EDGE_VOICES.get(lang, EDGE_VOICES["en-US"])
        communicate = edge_tts.Communicate(clean_text, voice=voice)
        await asyncio.wait_for(communicate.save(str(mp3_tmp)), timeout=12.0)
        if mp3_tmp.exists() and mp3_tmp.stat().st_size > 128:
            mp3_tmp.replace(mp3_final)
            return mp3_final
    except Exception:
        mp3_tmp.unlink(missing_ok=True)
    return None


async def _synthesize_macos_say(clean_text: str, lang: str, out_base_path: Path) -> Path | None:
    say_bin = shutil.which("say")
    afconvert_bin = shutil.which("afconvert")
    if not (say_bin and afconvert_bin):
        return None

    txt_tmp = out_base_path.with_suffix(".tmp.txt")
    aiff_tmp = out_base_path.with_suffix(".tmp.aiff")
    m4a_tmp = out_base_path.with_suffix(".tmp.m4a")
    m4a_final = out_base_path.with_suffix(".m4a")
    try:
        txt_tmp.write_text(clean_text, encoding="utf-8")
        voice = MAC_VOICES.get(lang, MAC_VOICES["en-US"])
        proc_say = await asyncio.create_subprocess_exec(
            say_bin,
            "-v",
            voice,
            "-f",
            str(txt_tmp),
            "-o",
            str(aiff_tmp),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc_say.communicate(), timeout=25.0)
        if proc_say.returncode == 0 and aiff_tmp.exists() and aiff_tmp.stat().st_size > 128:
            proc_conv = await asyncio.create_subprocess_exec(
                afconvert_bin,
                "-f",
                "m4af",
                "-d",
                "aac",
                str(aiff_tmp),
                str(m4a_tmp),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc_conv.communicate(), timeout=20.0)
            if proc_conv.returncode == 0 and m4a_tmp.exists() and m4a_tmp.stat().st_size > 128:
                m4a_tmp.replace(m4a_final)
                return m4a_final
    except Exception:
        pass
    finally:
        txt_tmp.unlink(missing_ok=True)
        aiff_tmp.unlink(missing_ok=True)
        m4a_tmp.unlink(missing_ok=True)
    return None


async def synthesize_to_file(
    text: str,
    language: str,
    out_base_path: Path,
    *,
    engine: str | None = None,
    voice: str | None = None,
    model: str | None = None,
) -> Path | None:
    """Synthesize text using mlx-audio (default), edge-tts, or macOS say fallback."""
    clean_text = clean_speech_text(text)
    if not clean_text:
        return None

    selected_engine = (engine or DEFAULT_TTS_ENGINE).strip() or DEFAULT_TTS_ENGINE
    selected_voice = (voice or DEFAULT_MLX_TTS_VOICE).strip() or DEFAULT_MLX_TTS_VOICE
    selected_model = (model or DEFAULT_MLX_TTS_MODEL).strip() or DEFAULT_MLX_TTS_MODEL

    out_base_path.parent.mkdir(parents=True, exist_ok=True)
    cached = _find_cached_audio(out_base_path, _preferred_exts_for_engine(selected_engine))
    if cached is not None:
        return cached

    lang = detect_language(clean_text, hint=language)

    # Engine 1: Local MLX-Audio neural TTS (default)
    if selected_engine == "mlx-audio" and tts_service.is_available():
        wav_path = await tts_service.synthesize_mlx(
            clean_text,
            mlx_lang_code(lang),
            out_base_path.with_suffix(".wav"),
            voice=selected_voice,
            model=selected_model,
        )
        if wav_path is not None:
            return wav_path

    # Engine 2: Microsoft Neural TTS via edge-tts
    if selected_engine in {"mlx-audio", "edge-tts"}:
        mp3_path = await _synthesize_edge(clean_text, lang, out_base_path)
        if mp3_path is not None:
            return mp3_path

    # Engine 3: macOS offline fallback (`say` + `afconvert`)
    return await _synthesize_macos_say(clean_text, lang, out_base_path)
