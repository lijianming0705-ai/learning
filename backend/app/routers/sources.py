from __future__ import annotations

import asyncio
import html
import json
import mimetypes
import re
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse

from app.config import MAX_UPLOAD_BYTES, ensure_data_dirs
from app.schemas import (
    ClassifySourcesRequest,
    ClassifySourcesResponse,
    OcrConvertSourceRequest,
    SourceDetail,
    SourceSummary,
    SourceUpdate,
    UrlImportRequest,
)
from app.services import study_service
from app.services.agy_service import agy_service, classify_sources_with_agy, ocr_pdf_page_layout_with_agy
from app.services.source_parser import (
    ParsedSource,
    SourceChunk,
    build_ocr_layout_page_html,
    build_standalone_export_html,
    chunk_text,
    count_words,
    fetch_and_parse_url,
    html_to_markdown_export,
    parse_docx,
    parse_pdf,
    parse_text_file,
    render_pdf_page_to_png,
)

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


@router.patch("/sources/{source_id}", response_model=SourceDetail)
async def patch_source(source_id: str, payload: SourceUpdate) -> SourceDetail:
    src = study_service.update_source(source_id, payload)
    if not src:
        raise HTTPException(status_code=404, detail="Source not found")
    return src


@router.post("/notebooks/{notebook_id}/sources/classify", response_model=ClassifySourcesResponse)
async def classify_notebook_sources(
    notebook_id: str,
    payload: ClassifySourcesRequest | None = None,
) -> ClassifySourcesResponse:
    nb = study_service.get_notebook(notebook_id)
    if not nb:
        raise HTTPException(status_code=404, detail="Notebook not found")
    items = study_service.list_sources_for_classification(notebook_id)
    if not items:
        return ClassifySourcesResponse(classified_count=0, categories=[], sources=[])

    settings = study_service.get_settings()
    model = ((payload.model if payload else None) or settings.default_model).strip()
    try:
        mapping = await classify_sources_with_agy(
            notebook_id=notebook_id,
            notebook_title=nb.title,
            sources=items,
            model=model,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"AI 分类失败: {exc}") from exc

    classified_count = study_service.bulk_update_source_categories(notebook_id, mapping)
    updated_sources = study_service.list_sources(notebook_id)
    categories = sorted({s.category for s in updated_sources if s.category})
    return ClassifySourcesResponse(
        classified_count=classified_count,
        categories=categories,
        sources=updated_sources,
    )


@router.delete("/sources/{source_id}")
async def delete_source(source_id: str) -> dict[str, bool]:
    ok = study_service.delete_source(source_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Source not found")
    return {"deleted": True}


_SAFE_OCR_ASSET_RE = re.compile(r"^p\d+_fig\d+\.png$")


def _sse_event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/sources/{source_id}/ocr-convert")
async def ocr_convert_pdf_source(
    source_id: str,
    payload: OcrConvertSourceRequest | None = None,
):
    source = study_service.get_source_detail(source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    if source.type != "pdf":
        raise HTTPException(status_code=400, detail="仅支持对 PDF 素材执行版面 OCR 转换")

    found = study_service.get_source_file_path(source_id)
    if not found:
        raise HTTPException(status_code=404, detail="PDF 源文件不存在")
    pdf_path, _ = found

    req = payload or OcrConvertSourceRequest()
    total_pdf_pages = max(1, source.page_count or 1)
    if req.full_book and req.end_page is None:
        start_page = 1
        end_page = total_pdf_pages
    else:
        start_page = max(1, min(total_pdf_pages, req.start_page))
        raw_end = req.end_page if req.end_page is not None else total_pdf_pages
        end_page = max(start_page, min(total_pdf_pages, raw_end))

    settings = study_service.get_settings()
    model = (req.model or settings.default_model).strip()
    output_title = (req.title or f"{source.title}（OCR 文本版）").strip()[:280]
    page_numbers = list(range(start_page, end_page + 1))
    concurrency = max(1, min(3, min(req.concurrency, len(page_numbers))))

    paths = ensure_data_dirs()
    new_source_id = study_service.new_id()
    job_id = study_service.new_id()
    temp_dir = paths.ocr_pages_dir / job_id
    assets_dir = paths.ocr_assets_dir / new_source_id
    temp_dir.mkdir(parents=True, exist_ok=True)
    assets_dir.mkdir(parents=True, exist_ok=True)

    async def event_stream() -> AsyncIterator[str]:
        results_by_page: dict[int, tuple[str, str, int, bool]] = {}
        db_lock = asyncio.Lock()
        saved_created = False
        used_keys: set[str] = set()
        worker_tasks: list[asyncio.Task[None]] = []
        event_queue: asyncio.Queue[tuple[str, dict] | None] = asyncio.Queue()

        def assemble_ordered(with_chunks: bool) -> tuple[str, str, list[tuple[int, str]], list[SourceChunk]]:
            ordered_pages = sorted(results_by_page.keys())
            html_parts = [results_by_page[p][0] for p in ordered_pages]
            pages_list = [(p, results_by_page[p][1]) for p in ordered_pages]
            full_html = "\n".join(html_parts)
            full_plain = "\n\n".join(txt for _, txt in pages_list if txt).strip()
            chunks_list: list[SourceChunk] = []
            if with_chunks:
                for p, txt in pages_list:
                    if txt:
                        chunks_list.extend(chunk_text(txt, page_number=p))
            return full_html, full_plain, pages_list, chunks_list

        async def persist_checkpoint(with_chunks: bool) -> SourceDetail | None:
            nonlocal saved_created
            if not results_by_page:
                return None
            async with db_lock:
                full_html, full_plain, pages_list, chunks_list = assemble_ordered(with_chunks=with_chunks)
                if not saved_created:
                    parsed = ParsedSource(
                        type="text",
                        title=output_title,
                        html_content=full_html,
                        plain_text=full_plain,
                        page_count=len(pages_list),
                        word_count=count_words(full_plain),
                        pages=pages_list,
                        chunks=chunks_list if with_chunks else [],
                        is_scanned=False,
                    )
                    detail = study_service.save_parsed_source(
                        notebook_id=source.notebook_id,
                        source_id=new_source_id,
                        parsed=parsed,
                        original_filename=f"{output_title}.html",
                    )
                    if source.category:
                        updated = study_service.update_source(
                            new_source_id,
                            SourceUpdate(category=source.category),
                        )
                        if updated:
                            detail = updated
                    saved_created = True
                    return detail
                return study_service.update_source_content(
                    new_source_id,
                    html_content=full_html,
                    plain_text=full_plain,
                    pages=pages_list,
                    chunks=chunks_list if with_chunks else None,
                )

        async def process_single_page(
            page_num: int,
            thread_key: str,
        ) -> tuple[str, str, int, bool]:
            page_png = temp_dir / f"page_{page_num}.png"
            try:
                await event_queue.put(
                    (
                        "progress",
                        {
                            "current": len(results_by_page),
                            "total": len(page_numbers),
                            "page_number": page_num,
                            "stage": "rendering",
                            "message": f"正在渲染第 {page_num} 页高清图像...",
                        },
                    )
                )
                render_pdf_page_to_png(pdf_path, page_num, page_png)

                await event_queue.put(
                    (
                        "progress",
                        {
                            "current": len(results_by_page),
                            "total": len(page_numbers),
                            "page_number": page_num,
                            "stage": "ocr",
                            "message": f"agy 正在识别第 {page_num} 页版面与配图...",
                        },
                    )
                )

                last_err: Exception | None = None
                layout_data: dict | None = None
                for attempt in range(2):
                    attempt_key = thread_key if attempt == 0 else f"{thread_key}:retry:{page_num}"
                    used_keys.add(attempt_key)
                    try:
                        layout_data = await ocr_pdf_page_layout_with_agy(
                            job_id=job_id,
                            image_path=str(page_png),
                            source_title=source.title,
                            page_number=page_num,
                            total_pages=total_pdf_pages,
                            model=model,
                            thread_key=attempt_key,
                        )
                        last_err = None
                        break
                    except Exception as exc:
                        last_err = exc
                        await agy_service.release_worker(attempt_key, model)
                        used_keys.discard(attempt_key)

                if layout_data is None:
                    err_txt = html.escape(str(last_err or "未知错误"))
                    placeholder_html = (
                        f'<section class="ocr-page" data-page="{page_num}">'
                        f'<div class="ocr-page-badge">第 {page_num} 页</div>'
                        f'<p class="ocr-align-left"><em>第 {page_num} 页 OCR 识别失败（{err_txt}），可稍后对该页重试。</em></p>'
                        f"</section>"
                    )
                    return placeholder_html, "", 0, True

                page_html, page_plain, fig_count = build_ocr_layout_page_html(
                    layout_data,
                    pdf_path=pdf_path,
                    page_number=page_num,
                    new_source_id=new_source_id,
                    assets_dir=assets_dir,
                )
                return page_html, page_plain, fig_count, False
            finally:
                page_png.unlink(missing_ok=True)

        async def run_all_workers() -> None:
            page_queue: asyncio.Queue[int] = asyncio.Queue()
            for p in page_numbers:
                await page_queue.put(p)

            async def worker_loop(slot: int) -> None:
                handled = 0
                current_batch = 0
                current_key = f"ocr_layout:{job_id}:s{slot}:b{current_batch}"
                used_keys.add(current_key)
                while not page_queue.empty():
                    try:
                        page_num = page_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    try:
                        # Rotate worker every 20 pages to keep context & memory compact
                        if handled > 0 and handled % 20 == 0:
                            old_key = current_key
                            current_batch += 1
                            current_key = f"ocr_layout:{job_id}:s{slot}:b{current_batch}"
                            used_keys.add(current_key)
                            await agy_service.release_worker(old_key, model)
                            used_keys.discard(old_key)

                        page_html, page_plain, fig_count, failed = await process_single_page(page_num, current_key)
                        results_by_page[page_num] = (page_html, page_plain, fig_count, failed)
                        handled += 1

                        # Checkpoint on first page and every 3 pages (or single-thread every page)
                        done_cnt = len(results_by_page)
                        if done_cnt == 1 or concurrency == 1 or done_cnt % 3 == 0:
                            await persist_checkpoint(with_chunks=False)

                        preview = " ".join(page_plain.split())[:100] if page_plain else ("本页识别失败（已跳过）" if failed else "")
                        total_figs_so_far = sum(v[2] for v in results_by_page.values())
                        await event_queue.put(
                            (
                                "progress",
                                {
                                    "current": done_cnt,
                                    "total": len(page_numbers),
                                    "page_number": page_num,
                                    "stage": "done_page",
                                    "figures_count": fig_count,
                                    "total_figures": total_figs_so_far,
                                    "failed": failed,
                                    "preview": preview,
                                    "message": (
                                        f"第 {page_num} 页识别失败，已继续下一页"
                                        if failed
                                        else f"第 {page_num} 页完成（提取配图 {fig_count} 张）"
                                    ),
                                },
                            )
                        )
                    finally:
                        page_queue.task_done()

            try:
                tasks = [asyncio.create_task(worker_loop(s)) for s in range(concurrency)]
                worker_tasks.extend(tasks)
                await asyncio.gather(*tasks)
            finally:
                await event_queue.put(None)

        runner_task: asyncio.Task[None] | None = None
        completed_normally = False
        try:
            yield _sse_event(
                "start",
                {
                    "job_id": job_id,
                    "source_id": source_id,
                    "source_title": source.title,
                    "output_title": output_title,
                    "start_page": start_page,
                    "end_page": end_page,
                    "total_pages": len(page_numbers),
                    "concurrency": concurrency,
                    "model": model,
                },
            )

            runner_task = asyncio.create_task(run_all_workers())
            while True:
                item = await event_queue.get()
                if item is None:
                    break
                ev_name, ev_data = item
                yield _sse_event(ev_name, ev_data)

            await runner_task
            detail = await persist_checkpoint(with_chunks=True)
            if not detail:
                raise RuntimeError("未能识别出任何页面内容")

            total_figures = sum(v[2] for v in results_by_page.values())
            failed_pages = sum(1 for v in results_by_page.values() if v[3])
            completed_normally = True
            yield _sse_event(
                "done",
                {
                    "source": detail.model_dump(),
                    "total_figures": total_figures,
                    "failed_pages": failed_pages,
                },
            )
        except Exception as exc:
            if not saved_created:
                shutil.rmtree(assets_dir, ignore_errors=True)
            yield _sse_event("error", {"message": f"OCR 排版转换失败: {exc}"})
        finally:
            for wt in worker_tasks:
                if not wt.done():
                    wt.cancel()
            if runner_task and not runner_task.done():
                runner_task.cancel()
            # Ensure partial progress is flushed cleanly if cancelled mid-book
            if not completed_normally and results_by_page:
                try:
                    await persist_checkpoint(with_chunks=True)
                except Exception:
                    pass
            for tk in list(used_keys):
                try:
                    await agy_service.release_worker(tk, model)
                except Exception:
                    pass
            shutil.rmtree(temp_dir, ignore_errors=True)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/sources/{source_id}/assets/{asset_name}")
async def get_source_ocr_asset(source_id: str, asset_name: str) -> FileResponse:
    if not _SAFE_OCR_ASSET_RE.match(asset_name):
        raise HTTPException(status_code=400, detail="Invalid asset filename")
    path = ensure_data_dirs().ocr_assets_dir / source_id / asset_name
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="OCR figure asset not found")
    return FileResponse(path, media_type="image/png")


@router.get("/sources/{source_id}/export")
async def export_source_document(
    source_id: str,
    format: Literal["html", "md"] = Query(default="html"),
) -> Response:
    source = study_service.get_source_detail(source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    safe_stem = re.sub(r'[\\/:*?"<>|]+', "_", source.title).strip() or "document"
    if format == "md":
        if source.html_content.strip():
            md_text = html_to_markdown_export(title=source.title, html_content=source.html_content)
        else:
            md_text = f"# {source.title}\n\n{source.plain_text}\n"
        encoded_name = quote(f"{safe_stem}.md")
        return Response(
            content=md_text.encode("utf-8"),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}"},
        )

    assets_dir = ensure_data_dirs().ocr_assets_dir / source_id
    html_doc = build_standalone_export_html(
        title=source.title,
        html_content=source.html_content or f"<p>{source.plain_text}</p>",
        source_id=source_id,
        assets_dir=assets_dir,
    )
    encoded_name = quote(f"{safe_stem}.html")
    return Response(
        content=html_doc.encode("utf-8"),
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}"},
    )
