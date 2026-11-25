from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def test_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = tmp_path / "study_data"
    monkeypatch.setenv("STUDY_DATA_DIR", str(data_dir))
    monkeypatch.setenv("STUDY_DISABLE_PREWARM", "1")

    from app.main import app
    from app.schemas import AgyModelOut
    from app.services.agy_service import agy_service

    async def fake_list_models(force_refresh: bool = False):
        return [
            AgyModelOut(id="gemini-3.8-flash-low", name="Gemini 3.8 Flash (Low)"),
            AgyModelOut(id="gemini-3.8-flash-medium", name="Gemini 3.8 Flash (Medium)"),
            AgyModelOut(id="gemini-4-neon-high", name="Gemini 4 Neon (High)"),
        ]

    async def fake_stream_prompt(*, thread_key: str, model: str, prompt: str) -> AsyncIterator[tuple[str, Any]]:
        yield ("status", "generating")
        if thread_key.startswith("classify:"):
            import json
            import re

            ids = re.findall(r"id=([^\s|]+)", prompt)
            answer = json.dumps(
                {"assignments": [{"id": sid, "category": "核心论文"} for sid in ids]},
                ensure_ascii=False,
            )
        elif thread_key.startswith("ocr:"):
            answer = "识别文本：E = mc^2（质能等价公式）"
        else:
            answer = f"这是针对问题的精炼解析（模型 {model}）：注意力机制通过 Query、Key、Value 计算相关性权重。"
        yield ("delta", answer[:18])
        yield ("delta", answer[18:])
        yield (
            "result",
            {
                "conversation_id": "agy-conv-test-001",
                "status": "SUCCESS",
                "response": answer,
                "duration_seconds": 0.25,
                "usage": {"input_tokens": 100, "output_tokens": 35, "total_tokens": 135},
            },
        )

    monkeypatch.setattr(agy_service, "list_models", fake_list_models)
    monkeypatch.setattr(agy_service, "stream_prompt", fake_stream_prompt)

    with TestClient(app) as client:
        yield client
