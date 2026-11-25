from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIST_DIR = PROJECT_ROOT / "frontend" / "dist"


@dataclass(frozen=True)
class AppPaths:
    data_dir: Path
    study_db_path: Path
    rag_db_path: Path
    uploads_dir: Path
    agy_workdir: Path


def get_paths() -> AppPaths:
    raw_data = os.environ.get("STUDY_DATA_DIR")
    data_dir = Path(raw_data).expanduser().resolve() if raw_data else (PROJECT_ROOT / "data")
    return AppPaths(
        data_dir=data_dir,
        study_db_path=data_dir / "study.sqlite",
        rag_db_path=data_dir / "rag.sqlite",
        uploads_dir=data_dir / "uploads",
        agy_workdir=data_dir / "agy-workdir",
    )


def ensure_data_dirs() -> AppPaths:
    paths = get_paths()
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.uploads_dir.mkdir(parents=True, exist_ok=True)
    paths.agy_workdir.mkdir(parents=True, exist_ok=True)
    return paths


AGY_BIN = os.environ.get("STUDY_AGY_BIN", "agy")
DEFAULT_MODEL = os.environ.get("STUDY_DEFAULT_MODEL", "gemini-3.8-flash-low")
OLLAMA_URL = os.environ.get("STUDY_OLLAMA_URL", "http://localhost:11434")
MAX_UPLOAD_BYTES = 80 * 1024 * 1024  # 80 MB
