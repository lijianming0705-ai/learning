from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import FRONTEND_DIST_DIR, ensure_data_dirs
from app.db import init_study_db
from app.rag.store import rag_store
from app.routers import chat, highlights, notebooks, rag, sources, speech, system
from app.services.agy_service import agy_service
from app.services.study_service import get_settings


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_data_dirs()
    init_study_db()
    rag_store.init_db()
    settings = get_settings()
    rag_store.configure_embedder(settings.embedding_provider, settings.ollama_embedding_model)

    prewarm_task: asyncio.Task[None] | None = None
    if os.environ.get("STUDY_DISABLE_PREWARM") != "1":
        prewarm_task = asyncio.create_task(agy_service.prewarm_standby(settings.default_model))

    try:
        yield
    finally:
        if prewarm_task and not prewarm_task.done():
            prewarm_task.cancel()
        await agy_service.shutdown()


app = FastAPI(
    title="InkMind Study API",
    description="Personal web study platform powered by antigravity-cli (agy) and hybrid RAG",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(system.router)
app.include_router(notebooks.router)
app.include_router(sources.router)
app.include_router(highlights.router)
app.include_router(chat.router)
app.include_router(rag.router)
app.include_router(speech.router)

if FRONTEND_DIST_DIR.exists():
    assets_dir = FRONTEND_DIST_DIR / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str):
        candidate = FRONTEND_DIST_DIR / full_path
        if full_path and candidate.exists() and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST_DIR / "index.html")
