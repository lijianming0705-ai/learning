from __future__ import annotations

from fastapi import APIRouter

from app.rag.store import rag_store
from app.schemas import AgyModelOut, HealthOut, SettingsOut, SettingsUpdate
from app.services.agy_service import agy_service
from app.services.ocr_service import ocr_service
from app.services.study_service import get_settings, update_settings
from app.services.tts_service import tts_service

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health", response_model=HealthOut)
async def health() -> HealthOut:
    stats = rag_store.stats()
    return HealthOut(
        status="ok",
        agy_available=agy_service.is_available(),
        agy_path=agy_service.agy_path(),
        agy_warm_workers=agy_service.warm_worker_count(),
        rag_documents=stats["total_documents"],
        local_ocr_available=ocr_service.is_available(),
        local_ocr_engine=ocr_service.engine_name(),
        local_tts_available=tts_service.is_available(),
        local_tts_engine=tts_service.engine_name(),
    )


@router.get("/agy/models", response_model=list[AgyModelOut])
async def list_agy_models() -> list[AgyModelOut]:
    return await agy_service.list_models()


@router.get("/settings", response_model=SettingsOut)
async def read_settings() -> SettingsOut:
    return get_settings()


@router.patch("/settings", response_model=SettingsOut)
async def patch_settings(payload: SettingsUpdate) -> SettingsOut:
    return update_settings(payload)
