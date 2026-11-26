from __future__ import annotations

import json
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from app.rag.store import rag_store
from app.schemas import AskHighlightRequest, HighlightCreate, HighlightOut, HighlightUpdate, OcrHighlightRequest
from app.services import study_service
from app.services.agy_service import agy_service, build_highlight_prompt, ocr_highlight_image
from app.services.ocr_service import ocr_service

router = APIRouter(prefix="/api", tags=["highlights"])


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/sources/{source_id}/highlights", response_model=list[HighlightOut])
async def list_source_highlights(source_id: str) -> list[HighlightOut]:
    if not study_service.get_source_detail(source_id):
        raise HTTPException(status_code=404, detail="Source not found")
    return study_service.list_highlights_for_source(source_id)


@router.post("/sources/{source_id}/highlights", response_model=HighlightOut, status_code=201)
async def create_highlight(source_id: str, payload: HighlightCreate) -> HighlightOut:
    hl = study_service.create_highlight(source_id, payload)
    if not hl:
        raise HTTPException(status_code=404, detail="Source not found")
    return hl


@router.patch("/highlights/{highlight_id}", response_model=HighlightOut)
async def patch_highlight(highlight_id: str, payload: HighlightUpdate) -> HighlightOut:
    hl = study_service.update_highlight(highlight_id, payload)
    if not hl:
        raise HTTPException(status_code=404, detail="Highlight not found")
    return hl


@router.delete("/highlights/{highlight_id}")
async def delete_highlight(highlight_id: str) -> dict[str, bool]:
    ok = study_service.delete_highlight(highlight_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Highlight not found")
    return {"deleted": True}


@router.get("/highlights/{highlight_id}/image")
async def get_highlight_image(highlight_id: str) -> FileResponse:
    img_path = study_service.get_highlight_image_path(highlight_id)
    if not img_path:
        raise HTTPException(status_code=404, detail="Highlight image not found")
    return FileResponse(path=img_path, media_type="image/png")


@router.post("/highlights/{highlight_id}/ocr", response_model=HighlightOut)
async def ocr_highlight(
    highlight_id: str,
    payload: OcrHighlightRequest | None = None,
) -> HighlightOut:
    hl = study_service.get_highlight(highlight_id)
    if not hl:
        raise HTTPException(status_code=404, detail="Highlight not found")
    img_path = study_service.get_highlight_image_path(highlight_id)
    if not img_path:
        raise HTTPException(status_code=400, detail="当前划线没有关联截图，请先在 PDF 中使用框选模式截图")

    settings = study_service.get_settings()
    model = ((payload.model if payload else None) or settings.default_model).strip()
    engine = payload.engine if payload else "local"
    ocr_text = ""

    if engine == "local" and ocr_service.is_available():
        try:
            ocr_text = await ocr_service.ocr_image_text(img_path)
        except Exception:
            ocr_text = ""

    if not ocr_text:
        try:
            ocr_text = await ocr_highlight_image(
                highlight_id=highlight_id,
                image_path=str(img_path),
                source_title=hl.source_title,
                page_number=hl.page_number,
                model=model,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"OCR 识别失败: {exc}") from exc

    if not ocr_text:
        raise HTTPException(status_code=422, detail="未识别出有效内容，请尝试放大框选区域或切换模型重试")

    updated = study_service.update_highlight_quote_from_ocr(highlight_id, ocr_text)
    assert updated is not None
    return updated


@router.post("/highlights/{highlight_id}/ask")
async def ask_highlight_ai(highlight_id: str, payload: AskHighlightRequest) -> StreamingResponse:
    hl = study_service.get_highlight(highlight_id)
    if not hl:
        raise HTTPException(status_code=404, detail="Highlight not found")

    settings = study_service.get_settings()
    model = (payload.model or settings.default_model).strip()
    cross_nb = (
        payload.cross_notebook_rag
        if payload.cross_notebook_rag is not None
        else settings.cross_notebook_rag
    )

    conv_id, _agy_cid, prior_messages = study_service.get_or_create_highlight_conversation(highlight_id, model)
    user_msg = study_service.append_message(
        conversation_id=conv_id,
        role="user",
        content=payload.question.strip(),
        model=model,
    )

    # Retrieve hybrid RAG memories (past Q&A memories + notes + source chunks)
    rag_refs = []
    if payload.use_rag:
        rag_query = f"{hl.quote_text}\n{payload.question}".strip()
        scope_nb = None if cross_nb else hl.notebook_id
        qa_hits = rag_store.search(
            rag_query,
            top_k=max(2, settings.rag_top_k),
            notebook_id=scope_nb,
            doc_types=["qa_memory", "highlight_note"],
            exclude_conversation_id=conv_id,
        )
        chunk_hits = rag_store.search(
            payload.question,
            top_k=3,
            notebook_id=hl.notebook_id,
            doc_types=["source_chunk"],
        )
        seen_ids: set[str] = set()
        for hit in [*qa_hits, *chunk_hits]:
            if hit.doc_id not in seen_ids:
                seen_ids.add(hit.doc_id)
                rag_refs.append(study_service.rag_hit_to_ref(hit))

    page_ctx = study_service.get_surrounding_page_context(hl.source_id, hl.page_number)
    img_path = study_service.get_highlight_image_path(highlight_id)
    prompt = build_highlight_prompt(
        highlight=hl,
        page_context=page_ctx,
        prior_messages=prior_messages,
        rag_memories=rag_refs,
        question=payload.question.strip(),
        extra_context=payload.extra_context,
        image_path=str(img_path) if img_path else None,
    )

    async def event_stream() -> AsyncIterator[str]:
        yield _sse(
            "rag_context",
            {
                "conversation_id": conv_id,
                "user_message": user_msg.model_dump(),
                "memories": [m.model_dump() for m in rag_refs],
            },
        )
        chunks: list[str] = []
        result_meta: dict = {}
        had_error = False

        async for kind, data in agy_service.stream_prompt(
            thread_key=f"conv:{conv_id}",
            model=model,
            prompt=prompt,
        ):
            if kind == "status":
                yield _sse("status", {"stage": data})
            elif kind == "delta":
                chunks.append(data)
                yield _sse("delta", {"text": data})
            elif kind == "result":
                result_meta = data or {}
            elif kind == "error":
                had_error = True
                yield _sse("error", {"message": data})

        if had_error and not chunks:
            return

        full_answer = (result_meta.get("response") or "".join(chunks)).strip()
        if not full_answer:
            yield _sse("error", {"message": "AI 未返回内容，请检查 agy CLI 登录状态或重试。"})
            return

        # If agy returned response only in result envelope (no prior delta), emit it
        if not chunks and full_answer:
            yield _sse("delta", {"text": full_answer})

        assistant_msg = study_service.append_message(
            conversation_id=conv_id,
            role="assistant",
            content=full_answer,
            model=model,
            usage=result_meta.get("usage"),
            retrieved_memories=rag_refs,
            duration_seconds=result_meta.get("duration_seconds"),
            agy_conversation_id=result_meta.get("conversation_id"),
        )
        study_service.index_qa_turn_to_rag(
            message_id=assistant_msg.id,
            conversation_id=conv_id,
            notebook_id=hl.notebook_id,
            source_id=hl.source_id,
            highlight_id=hl.id,
            question=payload.question.strip(),
            answer=full_answer,
            created_at=assistant_msg.created_at,
        )
        yield _sse(
            "done",
            {
                "conversation_id": conv_id,
                "message": assistant_msg.model_dump(),
            },
        )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
