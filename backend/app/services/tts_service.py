from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
from pathlib import Path

from app.config import ensure_data_dirs

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")
_WHITESPACE_RE = re.compile(r"\s+")

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
        if norm.startswith("zh"):
            return "zh-CN"
        if norm.startswith("en"):
            return "en-US"
    compact = re.sub(r"\s+", "", text or "")
    if not compact:
        return "zh-CN"
    cjk_count = len(_CJK_RE.findall(compact))
    return "zh-CN" if (cjk_count / len(compact)) >= 0.15 else "en-US"


def clean_speech_text(text: str, max_chars: int = 4000) -> str:
    cleaned = re.sub(r"[`*_#>]+", " ", text or "")
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    return cleaned[:max_chars].strip()


def clip_cache_path(spoken_text: str, language: str) -> Path:
    lang = detect_language(spoken_text, hint=language)
    digest = hashlib.sha256(f"{lang}:{spoken_text}".encode("utf-8")).hexdigest()[:16]
    return ensure_data_dirs().speech_dir / f"sel_{digest}"


def media_type_for_audio(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".mp3":
        return "audio/mpeg"
    if suffix == ".m4a":
        return "audio/mp4"
    if suffix == ".aiff":
        return "audio/aiff"
    return "application/octet-stream"


def _find_cached_audio(out_base_path: Path) -> Path | None:
    for ext in (".mp3", ".m4a"):
        candidate = out_base_path.with_suffix(ext)
        if candidate.exists() and candidate.is_file() and candidate.stat().st_size > 128:
            return candidate
    return None


async def synthesize_to_file(text: str, language: str, out_base_path: Path) -> Path | None:
    """Synthesize text to .mp3 (edge-tts) or .m4a (macOS say + afconvert)."""
    clean_text = clean_speech_text(text)
    if not clean_text:
        return None

    out_base_path.parent.mkdir(parents=True, exist_ok=True)
    cached = _find_cached_audio(out_base_path)
    if cached is not None:
        return cached

    lang = detect_language(clean_text, hint=language)

    # Engine 1: Microsoft Neural TTS via edge-tts
    try:
        import edge_tts  # type: ignore[import-untyped]

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
    except ImportError:
        pass

    # Engine 2: macOS offline fallback (`say` + `afconvert`)
    say_bin = shutil.which("say")
    afconvert_bin = shutil.which("afconvert")
    if say_bin and afconvert_bin:
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
