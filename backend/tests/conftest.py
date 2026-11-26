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
            AgyModelOut(id="gemini-4-hydrogen-medium", name="Gemini 4 Hydrogen (Medium)"),
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
        elif thread_key.startswith("ocr_layout:"):
            import json

            answer = json.dumps(
                {
                    "layout_columns": 1,
                    "blocks": [
                        {"type": "heading", "level": 1, "text": "相对论基础笔记", "align": "center"},
                        {
                            "type": "paragraph",
                            "text": "扫描页正文指出质能等价关系如下：",
                            "indent": True,
                            "align": "left",
                        },
                        {
                            "type": "figure",
                            "bbox": {"x": 0.08, "y": 0.2, "width": 0.82, "height": 0.45},
                            "caption": "图 1 质能公式示意图",
                            "align": "center",
                            "width_pct": 85,
                        },
                        {
                            "type": "columns",
                            "columns": [
                                [{"type": "paragraph", "text": "左栏说明：能量与质量正相关。"}],
                                [{"type": "paragraph", "text": "右栏补充：c 表示真空光速。"}],
                            ],
                        },
                        {
                            "type": "table",
                            "headers": ["物理量", "符号"],
                            "rows": [["能量", "E"], ["质量", "m"]],
                        },
                    ],
                },
                ensure_ascii=False,
            )
        elif thread_key.startswith("speech_sel:"):
            import json

            answer = json.dumps(
                {
                    "spoken_text": "Scaled Dot-Product Attention uses Query, Key, and Value matrices.",
                    "language": "en-US",
                    "ipa_or_guide": "/skeɪld dɒt ˈprɒdʌkt əˈtɛnʃən/",
                    "brief_meaning": "缩放点积注意力使用查询、键和值矩阵。",
                },
                ensure_ascii=False,
            )
        elif thread_key.startswith("speech_full:"):
            import json

            answer = json.dumps(
                {
                    "segments": [
                        {
                            "page_number": 1,
                            "heading": "核心架构",
                            "original_excerpt": "Scaled Dot-Product Attention uses Query",
                            "spoken_text": "第一部分介绍缩放点积注意力机制，它通过 Query、Key 与 Value 三个矩阵完成计算。",
                            "language": "zh-CN",
                        },
                        {
                            "page_number": 1,
                            "heading": "多头并行",
                            "original_excerpt": "Multi-Head Attention allows parallel subspace attention",
                            "spoken_text": "第二部分讲解多头注意力机制，它允许模型并行关注不同子空间中的特征关联。",
                            "language": "zh-CN",
                        },
                    ]
                },
                ensure_ascii=False,
            )
        elif thread_key.startswith("analytics:"):
            answer = (
                "### 1. 阅读习惯与时间画像\n"
                "你在晚间专注度最高。\n\n"
                "### 2. 学习深度与互动效率\n"
                "保持了良好的划线与 AI 问答节奏。\n\n"
                "### 3. 素材推进与瓶颈诊断\n"
                "核心论文正在稳步推进。\n\n"
                "### 4. 下一步可执行改进建议\n"
                "1. 优先读完当前进度较高的资料。"
            )
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

    async def fake_synthesize_to_file(text: str, language: str, out_base_path: Path) -> Path | None:
        out_base_path.parent.mkdir(parents=True, exist_ok=True)
        mp3_path = out_base_path.with_suffix(".mp3")
        mp3_path.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 160)
        return mp3_path

    import app.routers.speech as speech_router
    import app.services.tts_service as tts_service

    monkeypatch.setattr(agy_service, "list_models", fake_list_models)
    monkeypatch.setattr(agy_service, "stream_prompt", fake_stream_prompt)
    monkeypatch.setattr(tts_service, "synthesize_to_file", fake_synthesize_to_file)
    monkeypatch.setattr(speech_router, "synthesize_to_file", fake_synthesize_to_file)

    with TestClient(app) as client:
        yield client
