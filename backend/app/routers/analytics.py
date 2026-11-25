from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.db import utc_now_iso
from app.schemas import (
    ReadingAiReportRequest,
    ReadingAiReportResponse,
    ReadingAnalyticsResponse,
    ReadingHeartbeatRequest,
    ReadingHeartbeatResponse,
)
from app.services.agy_service import generate_reading_behavior_analysis_with_agy
from app.services.study_service import (
    get_reading_analytics,
    get_settings,
    record_reading_heartbeat,
)

router = APIRouter(prefix="/api", tags=["analytics"])


@router.post("/analytics/reading/heartbeat", response_model=ReadingHeartbeatResponse)
async def reading_heartbeat(payload: ReadingHeartbeatRequest) -> ReadingHeartbeatResponse:
    res = record_reading_heartbeat(payload)
    if not res:
        raise HTTPException(status_code=404, detail="Source not found")
    return res


@router.get("/analytics/reading", response_model=ReadingAnalyticsResponse)
async def reading_analytics(
    notebook_id: str | None = Query(default=None),
    days: int = Query(default=30, ge=7, le=365),
) -> ReadingAnalyticsResponse:
    return get_reading_analytics(notebook_id=notebook_id or None, days=days)


@router.post("/analytics/reading/ai-report", response_model=ReadingAiReportResponse)
async def reading_ai_report(payload: ReadingAiReportRequest) -> ReadingAiReportResponse:
    analytics = get_reading_analytics(notebook_id=payload.notebook_id or None, days=payload.days)
    settings = get_settings()
    model = payload.model or settings.default_model
    report = await generate_reading_behavior_analysis_with_agy(
        analytics=analytics,
        model=model,
    )
    return ReadingAiReportResponse(
        report_markdown=report,
        model=model,
        generated_at=utc_now_iso(),
    )
