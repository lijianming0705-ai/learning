from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.config import MAX_UPLOAD_BYTES, ensure_data_dirs
from app.schemas import SourceDetail, SourceSummary, UrlImportRequest
from app.services import study_service
from app.services.source_parser import fetch_and_parse_url, parse_docx, parse_pdf, parse_text_file

router = APIRouter(prefix="/api", tags=["sources"])

_ALLOWED_EXTS = {".pdf", ".docx", ".md", ".markdown", ".txt"}


@router.get("/notebooks/{notebook_id}/sources", response_model=list[SourceSummary])
async def list_sources(notebook_id: str) -> list[SourceSummary]:
    if not study_service.get_notebook(notebook_id):
        raise HTTPException(status_code=404, detail="Notebook not found")
    return study_service.list_sources(notebook_id)


@router.post("/notebooks/{notebook_id}/sources/upload", response_model=list[SourceDetail], status_code=201)
async def upload_sources(notebook_id: str, files: list[UploadFile] = File(...)) -> list[SourceDetail]:
    if not study_service.get_notebook(notebook_id):
        raise HTTPException(status_code=404, detail="Notebook not found")
    if not files:
        raise HTTPException(status_code=400, detail="请至少选择一个文件")

    uploads_dir = ensure_data_dirs().uploads_dir / notebook_id
    uploads_dir.mkdir(parents=True, exist_ok=True)
    created: list[SourceDetail] = []

    for upload in files:
        filename = Path(upload.filename or "untitled").name
        ext = Path(filename).suffix.lower()
        if ext not in _ALLOWED_EXTS:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的文件格式: {filename}（支持 PDF、Word .docx、Markdown、TXT）",
            )

        content = await upload.read()
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail=f"文件过大: {filename}（最大 80MB）")

        source_id = study_service.new_id()
        saved_path = uploads_dir / f"{source_id}{ext}"
        saved_path.write_bytes(content)

        fallback_title = Path(filename).stem or filename
        try:
            if ext == ".pdf":
                parsed = parse_pdf(saved_path, fallback_title)
            elif ext == ".docx":
                parsed = parse_docx(saved_path, fallback_title)
            else:
                parsed = parse_text_file(saved_path, fallback_title)
        except Exception as exc:
            saved_path.unlink(missing_ok=True)
            raise HTTPException(status_code=422, detail=f"解析文件 {filename} 失败: {exc}") from exc

        detail = study_service.save_parsed_source(
            notebook_id=notebook_id,
            source_id=source_id,
            parsed=parsed,
            original_filename=filename,
            file_path=saved_path,
        )
        created.append(detail)

    return created


@router.post("/notebooks/{notebook_id}/sources/url", response_model=SourceDetail, status_code=201)
async def import_url_source(notebook_id: str, payload: UrlImportRequest) -> SourceDetail:
    if not study_service.get_notebook(notebook_id):
        raise HTTPException(status_code=404, detail="Notebook not found")
    try:
        parsed = await fetch_and_parse_url(payload.url, title_override=payload.title)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"抓取网址内容失败: {exc}") from exc

    source_id = study_service.new_id()
    return study_service.save_parsed_source(
        notebook_id=notebook_id,
        source_id=source_id,
        parsed=parsed,
        source_url=payload.url.strip(),
    )


@router.get("/sources/{source_id}", response_model=SourceDetail)
async def get_source(source_id: str) -> SourceDetail:
    src = study_service.get_source_detail(source_id)
    if not src:
        raise HTTPException(status_code=404, detail="Source not found")
    return src


@router.get("/sources/{source_id}/file")
async def get_source_file(source_id: str) -> FileResponse:
    found = study_service.get_source_file_path(source_id)
    if not found:
        raise HTTPException(status_code=404, detail="Source file not found")
    file_path, filename = found
    media_type, _ = mimetypes.guess_type(filename)
    return FileResponse(
        path=file_path,
        media_type=media_type or "application/octet-stream",
        filename=filename,
    )


@router.delete("/sources/{source_id}")
async def delete_source(source_id: str) -> dict[str, bool]:
    ok = study_service.delete_source(source_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Source not found")
    return {"deleted": True}
