from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.config import ensure_data_dirs
from app.schemas import (
    GenerateSpeechScriptRequest,
    LocateSpeechRequest,
    LocateSpeechResponse,
    SelectionSpeechRequest,
    SelectionSpeechResponse,
    SpeechScriptResponse,
)
from app.services.agy_service import (
    generate_full_speech_script_with_agy,
    generate_selection_speech_with_agy,
)
from app.services.study_service import (
    get_settings,
    get_source_detail,
    get_speech_segment_record,
    list_speech_segments,
    locate_speech_segment,
    replace_source_speech_segments,
    set_speech_segment_audio_path,
)
from app.services.tts_service import (
    clip_cache_path,
    media_type_for_audio,
    synthesize_to_file,
)

router = APIRouter(prefix="/api", tags=["speech"])

_SAFE_CLIP_NAME_RE = re.compile(r"^sel_[a-f0-9]{16}\.(mp3|m4a)$")


@router.post("/speech/selection", response_model=SelectionSpeechResponse)
async def pronounce_selection(payload: SelectionSpeechRequest) -> SelectionSpeechResponse:
    settings = get_settings()
    model = payload.model or settings.default_model
    generated = await generate_selection_speech_with_agy(
        text=payload.text,
        model=model,
        mode=payload.mode,
    )
    spoken_text = generated["spoken_text"]
    language = generated["language"]
    audio_path = await synthesize_to_file(
        spoken_text,
        language,
        clip_cache_path(spoken_text, language),
    )
    audio_url = f"/api/speech/clips/{audio_path.name}" if audio_path else None
    return SelectionSpeechResponse(
        original_text=payload.text,
        spoken_text=spoken_text,
        language=language,
        ipa_or_guide=generated.get("ipa_or_guide", ""),
        brief_meaning=generated.get("brief_meaning", ""),
        audio_url=audio_url,
    )


@router.get("/speech/clips/{clip_name}")
async def get_speech_clip(clip_name: str):
    if not _SAFE_CLIP_NAME_RE.match(clip_name):
        raise HTTPException(status_code=400, detail="Invalid clip filename")
    path = ensure_data_dirs().speech_dir / clip_name
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Speech clip not found")
    return FileResponse(path, media_type=media_type_for_audio(path))


@router.get("/sources/{source_id}/speech-script", response_model=SpeechScriptResponse)
async def get_source_speech_script(source_id: str) -> SpeechScriptResponse:
    source = get_source_detail(source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    segments = list_speech_segments(source_id)
    return SpeechScriptResponse(
        source_id=source_id,
        source_title=source.title,
        segments=segments,
    )


@router.post("/sources/{source_id}/speech-script", response_model=SpeechScriptResponse)
async def generate_source_speech_script(
    source_id: str,
    payload: GenerateSpeechScriptRequest | None = None,
) -> SpeechScriptResponse:
    source = get_source_detail(source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    req = payload or GenerateSpeechScriptRequest()
    if not req.force_regenerate:
        existing = list_speech_segments(source_id)
        if existing:
            return SpeechScriptResponse(
                source_id=source_id,
                source_title=source.title,
                segments=existing,
            )

    pages: list[tuple[int, str]] = [
        (p.page_number, p.text_content)
        for p in source.pages
        if p.text_content and p.text_content.strip()
    ]
    if not pages and source.plain_text.strip():
        pages = [(1, source.plain_text)]

    if not any(txt.strip() for _, txt in pages):
        raise HTTPException(
            status_code=400,
            detail="该资料暂无可朗读的文本内容（若为扫描件可先框选 OCR 识别）",
        )

    settings = get_settings()
    model = req.model or settings.default_model
    generated_segments = await generate_full_speech_script_with_agy(
        source_id=source_id,
        source_title=source.title,
        pages=pages,
        model=model,
    )
    saved = replace_source_speech_segments(source_id, generated_segments)
    return SpeechScriptResponse(
        source_id=source_id,
        source_title=source.title,
        segments=saved,
    )


@router.get("/speech/segments/{segment_id}/audio")
async def get_speech_segment_audio(segment_id: str):
    seg = get_speech_segment_record(segment_id)
    if not seg:
        raise HTTPException(status_code=404, detail="Speech segment not found")

    existing_raw = seg.get("audio_path")
    if existing_raw:
        existing_path = Path(existing_raw)
        if existing_path.exists() and existing_path.is_file() and existing_path.stat().st_size > 128:
            return FileResponse(existing_path, media_type=media_type_for_audio(existing_path))

    out_base = ensure_data_dirs().speech_dir / f"seg_{segment_id}"
    audio_path = await synthesize_to_file(
        seg["spoken_text"],
        seg.get("language") or "zh-CN",
        out_base,
    )
    if not audio_path:
        raise HTTPException(
            status_code=503,
            detail="本地语音合成引擎不可用，请使用浏览器内置朗读",
        )
    set_speech_segment_audio_path(segment_id, str(audio_path))
    return FileResponse(audio_path, media_type=media_type_for_audio(audio_path))


@router.post("/sources/{source_id}/speech-script/locate", response_model=LocateSpeechResponse)
async def locate_source_speech_segment(
    source_id: str,
    payload: LocateSpeechRequest,
) -> LocateSpeechResponse:
    source = get_source_detail(source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    hit = locate_speech_segment(source_id, payload.text, payload.page_number)
    if not hit:
        raise HTTPException(
            status_code=404,
            detail="当前资料尚未生成全文朗读脚本，请先点击一键生成全文自然朗读",
        )
    return hit
