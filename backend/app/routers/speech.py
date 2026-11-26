from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

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

_SAFE_CLIP_NAME_RE = re.compile(r"^sel_[a-f0-9]{16}\.(wav|mp3|m4a)$")


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
        clip_cache_path(
            spoken_text,
            language,
            engine=settings.tts_engine,
            voice=settings.mlx_tts_voice,
            model=settings.mlx_tts_model,
        ),
        engine=settings.tts_engine,
        voice=settings.mlx_tts_voice,
        model=settings.mlx_tts_model,
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


def _format_sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/sources/{source_id}/speech-script/stream")
async def stream_generate_source_speech_script(
    source_id: str,
    payload: GenerateSpeechScriptRequest | None = None,
):
    source = get_source_detail(source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    req = payload or GenerateSpeechScriptRequest()
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

    async def event_stream():
        if not req.force_regenerate:
            existing = list_speech_segments(source_id)
            if existing:
                yield _format_sse("start", {"source_id": source_id, "total_pages": len(pages)})
                yield _format_sse(
                    "progress",
                    {
                        "stage": "cached",
                        "batch_index": 1,
                        "total_batches": 1,
                        "completed_batches": 1,
                        "page_start": 1,
                        "page_end": len(pages),
                        "segments_so_far": len(existing),
                        "percent": 100,
                    },
                )
                done_res = SpeechScriptResponse(
                    source_id=source_id,
                    source_title=source.title,
                    segments=existing,
                )
                yield _format_sse("done", done_res.model_dump())
                return

        settings = get_settings()
        model = req.model or settings.default_model
        queue: asyncio.Queue[tuple[str, dict] | None] = asyncio.Queue()

        async def _on_progress(evt: dict) -> None:
            await queue.put(("progress", evt))

        async def _worker() -> None:
            try:
                await queue.put(("start", {"source_id": source_id, "total_pages": len(pages)}))
                generated_segments = await generate_full_speech_script_with_agy(
                    source_id=source_id,
                    source_title=source.title,
                    pages=pages,
                    model=model,
                    on_progress=_on_progress,
                )
                saved = replace_source_speech_segments(source_id, generated_segments)
                done_res = SpeechScriptResponse(
                    source_id=source_id,
                    source_title=source.title,
                    segments=saved,
                )
                await queue.put(("done", done_res.model_dump()))
            except Exception as exc:  # noqa: BLE001
                await queue.put(("error", {"message": str(exc)}))
            finally:
                await queue.put(None)

        task = asyncio.create_task(_worker())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                ev_name, ev_data = item
                yield _format_sse(ev_name, ev_data)
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/speech/segments/{segment_id}/audio")
async def get_speech_segment_audio(segment_id: str):
    seg = get_speech_segment_record(segment_id)
    if not seg:
        raise HTTPException(status_code=404, detail="Speech segment not found")

    settings = get_settings()
    engine = settings.tts_engine
    if engine == "mlx-audio":
        voice_slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", settings.mlx_tts_voice).strip("_") or "Vivian"
        model_hash = hashlib.sha256(settings.mlx_tts_model.encode("utf-8")).hexdigest()[:6]
        engine_tag = f"mlx_{voice_slug}_{model_hash}"
    elif engine == "edge-tts":
        engine_tag = "edge"
    else:
        engine_tag = "say"

    out_base = ensure_data_dirs().speech_dir / f"seg_{segment_id}_{engine_tag}"
    existing_raw = seg.get("audio_path")
    if existing_raw:
        existing_path = Path(existing_raw)
        if (
            existing_path.stem == out_base.name
            and existing_path.exists()
            and existing_path.is_file()
            and existing_path.stat().st_size > 128
        ):
            return FileResponse(existing_path, media_type=media_type_for_audio(existing_path))

    audio_path = await synthesize_to_file(
        seg["spoken_text"],
        seg.get("language") or "zh-CN",
        out_base,
        engine=engine,
        voice=settings.mlx_tts_voice,
        model=settings.mlx_tts_model,
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
