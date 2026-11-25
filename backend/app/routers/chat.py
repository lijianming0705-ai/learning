from __future__ import annotations

import json
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.rag.store import rag_store
from app.schemas import ConversationDetail, ConversationOut, NotebookChatRequest
from app.services import study_service
from app.services.agy_service import agy_service, build_notebook_chat_prompt

router = APIRouter(prefix="/api", tags=["chat"])


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/notebooks/{notebook_id}/conversations", response_model=list[ConversationOut])
async def list_conversations(notebook_id: str) -> list[ConversationOut]:
    if not study_service.get_notebook(notebook_id):
        raise HTTPException(status_code=404, detail="Notebook not found")
    return study_service.list_notebook_conversations(notebook_id)


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(conversation_id: str) -> ConversationDetail:
    conv = study_service.get_conversation_detail(conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str) -> dict[str, bool]:
    ok = study_service.delete_conversation(conversation_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"deleted": True}


@router.post("/notebooks/{notebook_id}/chat")
async def notebook_chat(notebook_id: str, payload: NotebookChatRequest) -> StreamingResponse:
    nb = study_service.get_notebook(notebook_id)
    if not nb:
        raise HTTPException(status_code=404, detail="Notebook not found")

    settings = study_service.get_settings()
    model = (payload.model or settings.default_model).strip()
    cross_nb = (
        payload.cross_notebook_rag
        if payload.cross_notebook_rag is not None
        else settings.cross_notebook_rag
    )

    try:
        conv_id, _agy_cid, prior_messages = study_service.get_or_create_notebook_conversation(
            notebook_id=notebook_id,
            conversation_id=payload.conversation_id,
            source_id=payload.source_id,
            first_prompt=payload.message,
            model=model,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    user_msg = study_service.append_message(
        conversation_id=conv_id,
        role="user",
        content=payload.message.strip(),
        model=model,
    )

    rag_refs = []
    if payload.use_rag:
        scope_nb = None if cross_nb else notebook_id
        qa_hits = rag_store.search(
            payload.message,
            top_k=max(2, settings.rag_top_k),
            notebook_id=scope_nb,
            doc_types=["qa_memory", "highlight_note"],
            exclude_conversation_id=conv_id,
        )
        chunk_hits = rag_store.search(
            payload.message,
            top_k=4,
            notebook_id=notebook_id,
            source_id=payload.source_id,
            doc_types=["source_chunk"],
        )
        seen_ids: set[str] = set()
        for hit in [*qa_hits, *chunk_hits]:
            if hit.doc_id not in seen_ids:
                seen_ids.add(hit.doc_id)
                rag_refs.append(study_service.rag_hit_to_ref(hit))

    source_ctx = study_service.get_source_overview_context(payload.source_id) if payload.source_id else ""
    prompt = build_notebook_chat_prompt(
        notebook_title=nb.title,
        source_context=source_ctx,
        prior_messages=prior_messages,
        rag_memories=rag_refs,
        question=payload.message.strip(),
        extra_context=payload.extra_context,
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
            yield _sse("error", {"message": "AI 未返回内容，请检查 agy CLI 状态后重试。"})
            return

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
            notebook_id=notebook_id,
            source_id=payload.source_id,
            highlight_id=None,
            question=payload.message.strip(),
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
