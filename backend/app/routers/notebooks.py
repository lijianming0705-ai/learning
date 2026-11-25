from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.schemas import HighlightOut, NotebookCreate, NotebookOut, NotebookUpdate
from app.services import study_service

router = APIRouter(prefix="/api/notebooks", tags=["notebooks"])


@router.get("", response_model=list[NotebookOut])
async def list_notebooks() -> list[NotebookOut]:
    return study_service.list_notebooks()


@router.post("", response_model=NotebookOut, status_code=201)
async def create_notebook(payload: NotebookCreate) -> NotebookOut:
    return study_service.create_notebook(payload)


@router.get("/{notebook_id}", response_model=NotebookOut)
async def get_notebook(notebook_id: str) -> NotebookOut:
    nb = study_service.get_notebook(notebook_id)
    if not nb:
        raise HTTPException(status_code=404, detail="Notebook not found")
    return nb


@router.patch("/{notebook_id}", response_model=NotebookOut)
async def patch_notebook(notebook_id: str, payload: NotebookUpdate) -> NotebookOut:
    nb = study_service.update_notebook(notebook_id, payload)
    if not nb:
        raise HTTPException(status_code=404, detail="Notebook not found")
    return nb


@router.delete("/{notebook_id}")
async def delete_notebook(notebook_id: str) -> dict[str, bool]:
    ok = study_service.delete_notebook(notebook_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Notebook not found")
    return {"deleted": True}


@router.get("/{notebook_id}/highlights", response_model=list[HighlightOut])
async def list_notebook_highlights(notebook_id: str) -> list[HighlightOut]:
    if not study_service.get_notebook(notebook_id):
        raise HTTPException(status_code=404, detail="Notebook not found")
    return study_service.list_highlights_for_notebook(notebook_id)
