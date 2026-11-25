from __future__ import annotations

from fastapi import APIRouter, Query

from app.rag.store import rag_store
from app.schemas import RagMemoryRef, RagStatsOut
from app.services import study_service

router = APIRouter(prefix="/api/rag", tags=["rag"])


@router.get("/search", response_model=list[RagMemoryRef])
async def search_rag(
    q: str = Query(..., min_length=1, max_length=500),
    notebook_id: str | None = Query(default=None),
    source_id: str | None = Query(default=None),
    doc_type: str = Query(default="all"),
    top_k: int = Query(default=10, ge=1, le=50),
) -> list[RagMemoryRef]:
    doc_types = None if doc_type == "all" else [t.strip() for t in doc_type.split(",") if t.strip()]
    hits = rag_store.search(
        q,
        top_k=top_k,
        notebook_id=notebook_id,
        source_id=source_id,
        doc_types=doc_types,
    )
    return [study_service.rag_hit_to_ref(h) for h in hits]


@router.get("/stats", response_model=RagStatsOut)
async def rag_stats() -> RagStatsOut:
    return RagStatsOut(**rag_store.stats())


@router.post("/reindex", response_model=RagStatsOut)
async def rag_reindex() -> RagStatsOut:
    return RagStatsOut(**study_service.reindex_all_rag())
