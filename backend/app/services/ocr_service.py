from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import threading
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF
import numpy as np

from app.config import ensure_data_dirs

_SWIFT_SOURCE = Path(__file__).resolve().with_name("vision_ocr.swift")

_CJK_CHAR_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\u3000-\u303f\uff00-\uffef]")
_HEADER_PAGE_RE = re.compile(
    r"^\s*(?:第?\s*\d+\s*页|\d{1,4}|[IVXLCDMivxlcdm]{1,7}|[-—–·|\s]*\d{1,4}[-—–·|\s]*)\s*$"
)
_CHAPTER_RE = re.compile(
    r"^(?:第[一二三四五六七八九十百千万零0-9]+[章节部分篇回讲]|[（(]?[一二三四五六七八九十]+[)）]?[、.\s]\s*\S|Chapter\s+\d+|Part\s+[IVX\d]+|Section\s+\d+)",
    re.IGNORECASE,
)
_SECTION_NUM_RE = re.compile(r"^\d+(?:\.\d+){0,2}\s+\S")
_BULLET_RE = re.compile(r"^(?:[•·▪◦●■◆▶]\s*|[-–—*]\s+)")
_ORDERED_RE = re.compile(r"^(?:(?:\d{1,3}|[a-zA-Z])[.、)）]\s+|[（(]\d{1,3}[)）]\s*|[①②③④⑤⑥⑦⑧⑨⑩]\s*)")
_CAPTION_RE = re.compile(r"^(?:图|表|Figure|Fig\.?|Table)\s*[\d一二三四五六七八九十.:：-]*", re.IGNORECASE)
_FORMULA_CHARS_RE = re.compile(r"[∑∫√≈≠≤≥∞∂∇±×÷αβγδθλμσΔΩ→⇒∀∃∈⊂∉⊆∪∩∏]")


@dataclass(slots=True)
class OcrLine:
    text: str
    x: float
    y: float
    w: float
    h: float
    confidence: float = 1.0

    @property
    def x_right(self) -> float:
        return self.x + self.w

    @property
    def y_bottom(self) -> float:
        return self.y + self.h

    @property
    def x_center(self) -> float:
        return self.x + self.w * 0.5

    @property
    def y_center(self) -> float:
        return self.y + self.h * 0.5


def _is_cjk_edge(ch: str) -> bool:
    return bool(ch and _CJK_CHAR_RE.match(ch))


def smart_join_fragments(parts: list[str]) -> str:
    if not parts:
        return ""
    buf = parts[0].strip()
    for nxt_raw in parts[1:]:
        nxt = nxt_raw.strip()
        if not nxt:
            continue
        if not buf:
            buf = nxt
            continue
        if buf.endswith("-") and len(buf) >= 2 and buf[-2].isalpha() and nxt[0].isalpha():
            buf = buf[:-1] + nxt
        elif _is_cjk_edge(buf[-1]) or _is_cjk_edge(nxt[0]):
            buf += nxt
        else:
            buf += " " + nxt
    return buf.strip()


def _compress_page_ranges(pages: list[int]) -> str:
    if not pages:
        return ""
    ordered = sorted(set(pages))
    ranges: list[str] = []
    start = ordered[0]
    prev = ordered[0]
    for p in ordered[1:]:
        if p == prev + 1:
            prev = p
        else:
            ranges.append(f"{start}-{prev}" if start < prev else str(start))
            start = prev = p
    ranges.append(f"{start}-{prev}" if start < prev else str(start))
    return ",".join(ranges)


def _merge_baseline_fragments(lines: list[OcrLine]) -> list[OcrLine]:
    if not lines:
        return []
    sorted_lines = sorted(lines, key=lambda ln: (ln.y_center, ln.x))
    rows: list[list[OcrLine]] = []
    for ln in sorted_lines:
        placed = False
        for row in reversed(rows):
            ref = row[0]
            overlap = min(ref.y_bottom, ln.y_bottom) - max(ref.y, ln.y)
            min_h = max(1e-4, min(ref.h, ln.h))
            if overlap >= 0.52 * min_h or abs(ref.y_center - ln.y_center) <= 0.38 * min_h:
                row.append(ln)
                placed = True
                break
        if not placed:
            rows.append([ln])

    merged: list[OcrLine] = []
    for row in rows:
        row.sort(key=lambda r: r.x)
        cur = row[0]
        for nxt in row[1:]:
            gap = nxt.x - cur.x_right
            max_h = max(cur.h, nxt.h, 0.01)
            gap_threshold = max(0.045, 2.2 * max_h)
            if gap <= gap_threshold:
                joined_text = smart_join_fragments([cur.text, nxt.text])
                new_x = min(cur.x, nxt.x)
                new_y = min(cur.y, nxt.y)
                new_xr = max(cur.x_right, nxt.x_right)
                new_yb = max(cur.y_bottom, nxt.y_bottom)
                cur = OcrLine(
                    text=joined_text,
                    x=new_x,
                    y=new_y,
                    w=max(1e-4, new_xr - new_x),
                    h=max(1e-4, new_yb - new_y),
                    confidence=(cur.confidence + nxt.confidence) * 0.5,
                )
            else:
                merged.append(cur)
                cur = nxt
        merged.append(cur)

    merged.sort(key=lambda ln: (ln.y, ln.x))
    return merged


def _strip_running_headers_footers(lines: list[OcrLine], h_med: float) -> list[OcrLine]:
    if len(lines) < 6:
        return lines
    keep = list(lines)

    # Strip isolated top header / page number
    if len(keep) >= 6:
        top = keep[0]
        nxt = keep[1]
        gap = nxt.y - top.y_bottom
        txt = top.text.strip()
        has_cue = (
            "|" in txt
            or "｜" in txt
            or bool(_HEADER_PAGE_RE.match(txt))
            or bool(re.search(r"(?:^\d+\s+|\s+\d+$)", txt))
        )
        if top.y < 0.085 and len(txt) <= 48 and top.h <= h_med * 1.18 and gap > h_med * 0.85 and has_cue:
            keep.pop(0)

    # Strip isolated bottom page number
    if len(keep) >= 5:
        bot = keep[-1]
        prev = keep[-2]
        gap = bot.y - prev.y_bottom
        txt = bot.text.strip()
        if bot.y > 0.89 and len(txt) <= 18 and gap > h_med * 0.6 and _HEADER_PAGE_RE.match(txt):
            keep.pop()

    return keep


def _match_figure_captions(
    lines: list[OcrLine],
    figures: list[dict[str, Any]],
) -> tuple[list[OcrLine], list[dict[str, Any]]]:
    if not figures:
        return lines, []

    used_line_indices: set[int] = set()
    annotated_figs: list[dict[str, Any]] = []

    for fig in figures:
        bbox = fig.get("bbox")
        if not isinstance(bbox, dict):
            continue
        bx = float(bbox.get("x", 0.0))
        by = float(bbox.get("y", 0.0))
        bw = float(bbox.get("width", 0.0))
        bh = float(bbox.get("height", 0.0))
        fig_bottom = by + bh
        fig_center = bx + bw * 0.5

        best_idx: int | None = None
        best_dist = 999.0

        for idx, ln in enumerate(lines):
            if idx in used_line_indices:
                continue
            txt = ln.text.strip()
            if not txt or len(txt) > 65:
                continue
            # Check overlap horizontally
            horiz_overlap = min(bx + bw + 0.08, ln.x_right) - max(bx - 0.08, ln.x)
            if horiz_overlap <= 0:
                continue

            below_dist = ln.y - fig_bottom
            above_dist = by - ln.y_bottom
            is_explicit_cap = bool(_CAPTION_RE.match(txt))
            is_short_centered = (
                len(txt) <= 40
                and abs(ln.x_center - fig_center) <= 0.14
                and not txt.endswith(("。", "."))
            )
            if 0.0 <= below_dist <= 0.065 and (is_explicit_cap or is_short_centered):
                if below_dist < best_dist:
                    best_dist = below_dist
                    best_idx = idx
            elif 0.0 <= above_dist <= 0.05 and is_explicit_cap:
                if above_dist < best_dist:
                    best_dist = above_dist
                    best_idx = idx

        caption = ""
        if best_idx is not None:
            caption = lines[best_idx].text.strip()
            used_line_indices.add(best_idx)

        annotated_figs.append(
            {
                "type": "figure",
                "bbox": {"x": round(bx, 4), "y": round(by, 4), "width": round(bw, 4), "height": round(bh, 4)},
                "caption": caption,
                "align": "center",
                "width_pct": int(fig.get("width_pct") or max(35, min(92, round(bw * 100)))),
                "_y": by,
            }
        )

    remaining = [ln for idx, ln in enumerate(lines) if idx not in used_line_indices]
    return remaining, annotated_figs


def _percentile(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    idx = min(len(s) - 1, max(0, int(round((len(s) - 1) * p))))
    return s[idx]


def _lines_to_blocks(lines: list[OcrLine], h_med: float) -> list[dict[str, Any]]:
    if not lines:
        return []
    ordered = sorted(lines, key=lambda ln: (ln.y, ln.x))
    body_left = _percentile([ln.x for ln in ordered], 0.2)
    body_right = _percentile([ln.x_right for ln in ordered], 0.8)
    col_span = max(0.2, body_right - body_left)

    blocks: list[dict[str, Any]] = []
    para_lines: list[OcrLine] = []
    list_mode: bool | None = None  # True=ordered, False=unordered
    list_items: list[str] = []

    def flush_para() -> None:
        nonlocal para_lines
        if not para_lines:
            return
        text = smart_join_fragments([ln.text for ln in para_lines])
        if text:
            first = para_lines[0]
            indented = (first.x - body_left) > max(0.024, h_med * 1.05) and (
                len(para_lines) >= 2 or first.x_right >= body_right - 0.15
            )
            avg_center = statistics.mean(ln.x_center for ln in para_lines)
            align = "left"
            if len(para_lines) <= 2 and all(ln.w < col_span * 0.72 for ln in para_lines) and abs(avg_center - (body_left + body_right) * 0.5) <= 0.06:
                align = "center"
                indented = False
            blocks.append({"type": "paragraph", "text": text, "indent": indented, "align": align})
        para_lines = []

    def flush_list() -> None:
        nonlocal list_mode, list_items
        if list_items and list_mode is not None:
            blocks.append({"type": "list", "ordered": list_mode, "items": list_items})
        list_mode = None
        list_items = []

    for i, ln in enumerate(ordered):
        txt = ln.text.strip()
        if not txt:
            continue

        prev_ln = ordered[i - 1] if i > 0 else None
        next_ln = ordered[i + 1] if i + 1 < len(ordered) else None
        gap_above = (ln.y - prev_ln.y_bottom) if prev_ln else 0.05
        gap_below = (next_ln.y - ln.y_bottom) if next_ln else 0.05

        is_bullet = bool(_BULLET_RE.match(txt))
        is_ordered = bool(_ORDERED_RE.match(txt))

        # Heading detection
        is_large = ln.h >= h_med * 1.32 and len(txt) <= 80
        is_chapter = (
            len(txt) <= 34
            and not txt.endswith(("。", ".", "，", ",", ";", "；"))
            and (bool(_CHAPTER_RE.match(txt)) or (bool(_SECTION_NUM_RE.match(txt)) and not is_ordered))
        )
        is_centered_short = (
            len(txt) <= 32
            and abs(ln.x_center - (body_left + body_right) * 0.5) <= 0.07
            and ln.w < col_span * 0.65
            and not txt.endswith(("。", ".", "，", ","))
            and (gap_above > h_med * 0.75 or gap_below > h_med * 0.75)
            and (prev_ln is None or prev_ln.x_right < body_right - 0.1)
        )

        if (is_large or is_chapter or is_centered_short) and not is_bullet:
            flush_para()
            flush_list()
            if ln.h >= h_med * 1.85:
                level = 1
            elif ln.h >= h_med * 1.48 or _CHAPTER_RE.match(txt):
                level = 2
            else:
                level = 3
            align = (
                "center"
                if (abs(ln.x_center - (body_left + body_right) * 0.5) <= 0.08 and ln.w < col_span * 0.78)
                else "left"
            )
            blocks.append({"type": "heading", "level": level, "text": txt, "align": align})
            continue

        if is_bullet or is_ordered:
            flush_para()
            cur_ordered = is_ordered and not is_bullet
            if list_mode is not None and list_mode != cur_ordered:
                flush_list()
            list_mode = cur_ordered
            cleaned_item = (_ORDERED_RE if cur_ordered else _BULLET_RE).sub("", txt, count=1).strip()
            list_items.append(cleaned_item or txt)
            continue

        # Continuation line of an active list item if close vertically and not outdented
        if list_items and prev_ln is not None:
            gap = ln.y - prev_ln.y_bottom
            if gap <= h_med * 0.75 and ln.x >= body_left + 0.01:
                list_items[-1] = smart_join_fragments([list_items[-1], txt])
                continue
            else:
                flush_list()

        # Body paragraph merging
        if not para_lines:
            para_lines.append(ln)
            continue

        prev_p = para_lines[-1]
        gap = ln.y - prev_p.y_bottom
        cur_indented = (ln.x - body_left) > max(0.024, h_med * 1.05) and ln.x_right > (body_left + col_span * 0.45)
        prev_ended_early = (
            prev_p.x_right < (body_right - max(0.10, col_span * 0.16))
            and prev_p.text.rstrip().endswith(("。", "！", "？", ".", "!", "?", "：", ":", "”", '"', "）", ")"))
        )

        if gap > h_med * 0.85 or cur_indented or prev_ended_early:
            flush_para()
        para_lines.append(ln)

    flush_para()
    flush_list()
    return blocks


def reconstruct_layout_from_ocr_lines(
    lines: list[OcrLine],
    figures: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    merged = _merge_baseline_fragments(lines)
    if not merged and not figures:
        return {"layout_columns": 1, "blocks": []}

    heights = [ln.h for ln in merged if ln.h > 0]
    h_med = statistics.median(heights) if heights else 0.02
    cleaned = _strip_running_headers_footers(merged, h_med)
    body_lines, fig_blocks = _match_figure_captions(cleaned, figures or [])

    # 2-column detection
    left_lines = [ln for ln in body_lines if ln.x_right < 0.52]
    right_lines = [ln for ln in body_lines if ln.x > 0.48]
    crossing = [ln for ln in body_lines if ln.x <= 0.48 and ln.x_right >= 0.52]

    use_two_cols = False
    if len(left_lines) >= 3 and len(right_lines) >= 3:
        col_top = max(min(ln.y for ln in left_lines), min(ln.y for ln in right_lines)) - 0.01
        col_bot = min(max(ln.y_bottom for ln in left_lines), max(ln.y_bottom for ln in right_lines)) + 0.01
        in_band_cross = [ln for ln in crossing if ln.y_bottom >= col_top and ln.y <= col_bot]
        band_total = len(left_lines) + len(right_lines) + len(in_band_cross)
        if band_total > 0 and (len(in_band_cross) / band_total) < 0.12:
            use_two_cols = True

    if use_two_cols:
        col_top = min(min(ln.y for ln in left_lines), min(ln.y for ln in right_lines))
        col_bot = max(max(ln.y_bottom for ln in left_lines), max(ln.y_bottom for ln in right_lines))
        top_span = [ln for ln in crossing if ln.y < col_top]
        bot_span = [ln for ln in crossing if ln.y_bottom > col_bot]
        mid_cross = [ln for ln in crossing if ln not in top_span and ln not in bot_span]

        # Re-assign any slight gutter crossers to nearest column
        for ln in mid_cross:
            if ln.x_center < 0.5:
                left_lines.append(ln)
            else:
                right_lines.append(ln)

        blocks: list[dict[str, Any]] = []
        blocks.extend(_lines_to_blocks(top_span, h_med))

        # Partition figures into top / inside columns / bottom
        left_figs = [f for f in fig_blocks if col_top - 0.02 <= f["_y"] <= col_bot + 0.02 and (f["bbox"]["x"] + f["bbox"]["width"] * 0.5) < 0.5 and f["bbox"]["width"] < 0.55]
        right_figs = [f for f in fig_blocks if col_top - 0.02 <= f["_y"] <= col_bot + 0.02 and (f["bbox"]["x"] + f["bbox"]["width"] * 0.5) >= 0.5 and f["bbox"]["width"] < 0.55]
        span_figs = [f for f in fig_blocks if f not in left_figs and f not in right_figs]

        left_blocks = _interleave_figures(_lines_to_blocks(left_lines, h_med), left_lines, left_figs)
        right_blocks = _interleave_figures(_lines_to_blocks(right_lines, h_med), right_lines, right_figs)
        blocks.append({"type": "columns", "columns": [left_blocks, right_blocks]})
        blocks.extend(_lines_to_blocks(bot_span, h_med))
        blocks = _interleave_figures(blocks, top_span + bot_span, span_figs)
        return {"layout_columns": 2, "blocks": blocks}

    text_blocks = _lines_to_blocks(body_lines, h_med)
    all_blocks = _interleave_figures(text_blocks, body_lines, fig_blocks)
    return {"layout_columns": 1, "blocks": all_blocks}


def _interleave_figures(
    blocks: list[dict[str, Any]],
    source_lines: list[OcrLine],
    fig_blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not fig_blocks:
        return blocks
    clean_figs = []
    for f in sorted(fig_blocks, key=lambda item: item.get("_y", 0.0)):
        item = {k: v for k, v in f.items() if k != "_y"}
        clean_figs.append((float(f.get("_y", 0.0)), item))

    if not blocks:
        return [item for _, item in clean_figs]

    # Estimate monotonic y-positions for blocks from remaining source lines
    ys = sorted(ln.y for ln in source_lines) if source_lines else []
    block_ys: list[float] = []
    for idx in range(len(blocks)):
        if ys:
            pos = min(len(ys) - 1, int(round((idx / max(1, len(blocks))) * (len(ys) - 1))))
            block_ys.append(ys[pos])
        else:
            block_ys.append(idx / max(1, len(blocks)))

    merged_out: list[tuple[float, int, dict[str, Any]]] = []
    for idx, b in enumerate(blocks):
        merged_out.append((block_ys[idx], 0, b))
    for f_y, f_blk in clean_figs:
        merged_out.append((f_y, 1, f_blk))
    merged_out.sort(key=lambda t: (t[0], t[1]))
    return [b for _, _, b in merged_out]


def reconstruct_text_from_ocr_lines(lines: list[OcrLine]) -> str:
    if not lines:
        return ""
    layout = reconstruct_layout_from_ocr_lines(lines)
    parts: list[str] = []

    def visit(blks: list[dict[str, Any]]) -> None:
        for b in blks:
            btype = b.get("type")
            if btype in ("heading", "paragraph", "blockquote"):
                txt = str(b.get("text") or "").strip()
                if txt:
                    parts.append(txt)
            elif btype == "list":
                for it in b.get("items") or []:
                    s = str(it).strip()
                    if s:
                        parts.append(f"- {s}")
            elif btype == "columns":
                for col in b.get("columns") or []:
                    if isinstance(col, list):
                        visit(col)

    visit(layout.get("blocks") or [])
    return "\n\n".join(parts).strip()


def page_needs_ai_refinement(lines: list[OcrLine], layout_data: dict[str, Any] | None = None) -> bool:
    if not lines:
        return False
    if len(lines) >= 3:
        avg_conf = sum(ln.confidence for ln in lines) / len(lines)
        if avg_conf < 0.72:
            return True

    joined = "\n".join(ln.text for ln in lines)
    formula_hits = len(_FORMULA_CHARS_RE.findall(joined))
    if formula_hits >= 4:
        return True

    # Detect multi-column table grids (rows with >= 3 distinct separated horizontal segments)
    sorted_lines = sorted(lines, key=lambda ln: (ln.y_center, ln.x))
    row_groups: list[list[OcrLine]] = []
    for ln in sorted_lines:
        if row_groups and abs(row_groups[-1][0].y_center - ln.y_center) <= max(0.015, ln.h * 0.6):
            row_groups[-1].append(ln)
        else:
            row_groups.append([ln])

    multi_cell_rows = 0
    for rg in row_groups:
        if len(rg) >= 3:
            rg.sort(key=lambda r: r.x)
            cols = 1
            for a, b in zip(rg, rg[1:]):
                if (b.x - a.x_right) > max(0.03, 1.5 * max(a.h, b.h)):
                    cols += 1
            if cols >= 3:
                multi_cell_rows += 1
    if multi_cell_rows >= 4:
        return True

    return False


def detect_figures_on_page(
    doc: fitz.Document,
    page_number: int,
    text_lines: list[OcrLine],
) -> list[dict[str, Any]]:
    page_idx = max(0, min(len(doc) - 1, page_number - 1))
    page = doc[page_idx]
    pr = page.rect
    if pr.width <= 1 or pr.height <= 1:
        return []

    # Fast path for born-digital PDFs with multiple distinct non-full-page image xrefs
    raw_text_len = len("".join((page.get_text("text") or "").split()))
    if raw_text_len >= 60:
        try:
            infos = page.get_image_info(xrefs=True)
        except Exception:
            infos = []
        digital_figs: list[dict[str, Any]] = []
        for info in infos:
            bbox = info.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            x0, y0, x1, y1 = [float(v) for v in bbox]
            nx = max(0.0, min(1.0, (x0 - pr.x0) / pr.width))
            ny = max(0.0, min(1.0, (y0 - pr.y0) / pr.height))
            nw = max(0.0, min(1.0 - nx, (x1 - x0) / pr.width))
            nh = max(0.0, min(1.0 - ny, (y1 - y0) / pr.height))
            area = nw * nh
            if 0.03 <= area <= 0.85 and nw >= 0.12 and nh >= 0.09:
                digital_figs.append(
                    {
                        "bbox": {"x": nx, "y": ny, "width": nw, "height": nh},
                        "width_pct": max(35, min(92, round(nw * 100))),
                    }
                )
        if digital_figs:
            digital_figs.sort(key=lambda f: f["bbox"]["y"])
            return digital_figs

    # Raster / scanned page path: render tiny grayscale preview and subtract OCR text boxes
    zoom = min(0.8, 320.0 / max(pr.width, pr.height, 1.0))
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY, alpha=False)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    h_px, w_px = arr.shape
    if h_px < 24 or w_px < 24:
        return []

    bg = int(np.percentile(arr, 85))
    fg = np.abs(arr.astype(np.int16) - bg) > 26

    # Mask scanner margins
    mx = max(2, int(w_px * 0.035))
    my = max(2, int(h_px * 0.04))
    fg[:, :mx] = False
    fg[:, w_px - mx :] = False
    fg[:my, :] = False
    fg[h_px - my :, :] = False

    # Mask all recognized OCR text regions
    pad_x = int(w_px * 0.015)
    pad_y = int(h_px * 0.012)
    for ln in text_lines:
        x0 = max(0, int(ln.x * w_px) - pad_x)
        y0 = max(0, int(ln.y * h_px) - pad_y)
        x1 = min(w_px, int(ln.x_right * w_px) + pad_x)
        y1 = min(h_px, int(ln.y_bottom * h_px) + pad_y)
        if x1 > x0 and y1 > y0:
            fg[y0:y1, x0:x1] = False

    if int(fg.sum()) < (w_px * h_px * 0.004):
        return []

    gw, gh = 48, 64
    y_edges = np.linspace(0, h_px, gh + 1, dtype=int)
    x_edges = np.linspace(0, w_px, gw + 1, dtype=int)
    grid = np.zeros((gh, gw), dtype=bool)
    for gy in range(gh):
        ys, ye = y_edges[gy], y_edges[gy + 1]
        if ye <= ys:
            continue
        for gx in range(gw):
            xs, xe = x_edges[gx], x_edges[gx + 1]
            if xe <= xs:
                continue
            cell = fg[ys:ye, xs:xe]
            if cell.mean() > 0.045:
                grid[gy, gx] = True

    # Suppress isolated 1-cell-thick horizontal/vertical rules
    cleaned_grid = grid.copy()
    for gy in range(gh):
        for gx in range(gw):
            if not grid[gy, gx]:
                continue
            has_v_nb = (gy > 0 and grid[gy - 1, gx]) or (gy + 1 < gh and grid[gy + 1, gx])
            has_h_nb = (gx > 0 and grid[gy, gx - 1]) or (gx + 1 < gw and grid[gy, gx + 1])
            if not (has_v_nb and has_h_nb):
                # Keep only if part of a thick cluster, drop thin 1-cell rules
                v_span = sum(1 for dy in (-1, 1) if 0 <= gy + dy < gh and grid[gy + dy, gx])
                h_span = sum(1 for dx in (-1, 1) if 0 <= gx + dx < gw and grid[gy, gx + dx])
                if v_span == 0 or h_span == 0:
                    cleaned_grid[gy, gx] = False

    visited = np.zeros((gh, gw), dtype=bool)
    raw_boxes: list[tuple[int, int, int, int]] = []
    for gy in range(gh):
        for gx in range(gw):
            if not cleaned_grid[gy, gx] or visited[gy, gx]:
                continue
            q = deque([(gy, gx)])
            visited[gy, gx] = True
            min_y = max_y = gy
            min_x = max_x = gx
            cells_cnt = 0
            while q:
                cy, cx = q.popleft()
                cells_cnt += 1
                min_y = min(min_y, cy)
                max_y = max(max_y, cy)
                min_x = min(min_x, cx)
                max_x = max(max_x, cx)
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        if dy == 0 and dx == 0:
                            continue
                        ny, nx = cy + dy, cx + dx
                        if 0 <= ny < gh and 0 <= nx < gw and cleaned_grid[ny, nx] and not visited[ny, nx]:
                            visited[ny, nx] = True
                            q.append((ny, nx))
            if cells_cnt >= 6:
                raw_boxes.append((min_x, min_y, max_x + 1, max_y + 1))

    if not raw_boxes:
        return []

    # Merge nearby boxes (gap <= 1 grid cell)
    merged_boxes: list[list[int]] = [list(b) for b in raw_boxes]
    changed = True
    while changed:
        changed = False
        next_boxes: list[list[int]] = []
        while merged_boxes:
            cur = merged_boxes.pop(0)
            i = 0
            while i < len(merged_boxes):
                other = merged_boxes[i]
                if (
                    cur[0] - 1 <= other[2]
                    and cur[2] + 1 >= other[0]
                    and cur[1] - 1 <= other[3]
                    and cur[3] + 1 >= other[1]
                ):
                    cur = [
                        min(cur[0], other[0]),
                        min(cur[1], other[1]),
                        max(cur[2], other[2]),
                        max(cur[3], other[3]),
                    ]
                    merged_boxes.pop(i)
                    changed = True
                else:
                    i += 1
            next_boxes.append(cur)
        merged_boxes = next_boxes

    results: list[dict[str, Any]] = []
    for gx0, gy0, gx1, gy1 in merged_boxes:
        nx = max(0.0, x_edges[gx0] / w_px)
        ny = max(0.0, y_edges[gy0] / h_px)
        nx1 = min(1.0, x_edges[min(gw, gx1)] / w_px)
        ny1 = min(1.0, y_edges[min(gh, gy1)] / h_px)
        nw = nx1 - nx
        nh = ny1 - ny
        if nw < 0.16 or nh < 0.11 or (nw * nh) < 0.035 or (nw * nh) > 0.85:
            continue
        aspect = nw / max(nh, 1e-4)
        if aspect < 0.18 or aspect > 5.5:
            continue
        px0, py0 = int(nx * w_px), int(ny * h_px)
        px1, py1 = int(nx1 * w_px), int(ny1 * h_px)
        density = float(fg[py0:py1, px0:px1].mean())
        if density < 0.035 or density > 0.94:
            continue
        results.append(
            {
                "bbox": {
                    "x": round(nx, 4),
                    "y": round(ny, 4),
                    "width": round(nw, 4),
                    "height": round(nh, 4),
                },
                "width_pct": max(35, min(92, round(nw * 100))),
            }
        )

    results.sort(key=lambda f: f["bbox"]["y"])
    return results[:6]


def _extract_embedded_text_pages(
    pdf_path: Path,
    page_numbers: list[int],
) -> tuple[dict[int, list[OcrLine]], list[int]]:
    embedded: dict[int, list[OcrLine]] = {}
    scanned: list[int] = []
    doc = fitz.open(str(pdf_path))
    try:
        total = len(doc)
        for pno in page_numbers:
            if pno < 1 or pno > total:
                continue
            page = doc[pno - 1]
            raw = (page.get_text("text") or "").strip()
            non_ws = len("".join(raw.split()))
            if non_ws < 60:
                scanned.append(pno)
                continue
            pr = page.rect
            pw = max(1.0, float(pr.width))
            ph = max(1.0, float(pr.height))
            info = page.get_text("dict")
            lines: list[OcrLine] = []
            for blk in info.get("blocks", []):
                if blk.get("type") != 0:
                    continue
                for ln in blk.get("lines", []):
                    spans = ln.get("spans", [])
                    txt = "".join(str(sp.get("text") or "") for sp in spans).strip()
                    if not txt:
                        continue
                    bbox = ln.get("bbox")
                    if not bbox or len(bbox) != 4:
                        continue
                    x0, y0, x1, y1 = [float(v) for v in bbox]
                    nx = max(0.0, min(1.0, (x0 - pr.x0) / pw))
                    ny = max(0.0, min(1.0, (y0 - pr.y0) / ph))
                    nw = max(1e-4, min(1.0 - nx, (x1 - x0) / pw))
                    nh = max(1e-4, min(1.0 - ny, (y1 - y0) / ph))
                    lines.append(OcrLine(text=txt, x=nx, y=ny, w=nw, h=nh, confidence=1.0))
            if lines:
                embedded[pno] = lines
            else:
                scanned.append(pno)
    finally:
        doc.close()
    return embedded, scanned


class LocalVisionOcrService:
    """High-speed native macOS Apple Vision (ANE/GPU) OCR engine with persistent worker & batch PDF streaming."""

    def __init__(self) -> None:
        self._compile_lock = threading.Lock()
        self._server_proc: asyncio.subprocess.Process | None = None
        self._server_bin: Path | None = None
        self._server_loop: asyncio.AbstractEventLoop | None = None
        self._server_lock: asyncio.Lock | None = None

    def _binary_path(self) -> Path:
        return ensure_data_dirs().data_dir / "bin" / "vision_ocr_helper"

    def is_available(self) -> bool:
        if sys.platform != "darwin":
            return False
        if self._binary_path().exists():
            return True
        return _SWIFT_SOURCE.exists() and shutil.which("swiftc") is not None

    def engine_name(self) -> str:
        return "apple-vision" if self.is_available() else "none"

    def _ensure_binary_sync(self) -> Path:
        if sys.platform != "darwin":
            raise RuntimeError("Apple Vision OCR 仅支持 macOS")
        bin_path = self._binary_path()
        with self._compile_lock:
            needs_build = not bin_path.exists()
            if not needs_build and _SWIFT_SOURCE.exists():
                needs_build = _SWIFT_SOURCE.stat().st_mtime > bin_path.stat().st_mtime
            if needs_build:
                if not _SWIFT_SOURCE.exists():
                    raise RuntimeError(f"Missing Swift OCR source: {_SWIFT_SOURCE}")
                swiftc = shutil.which("swiftc")
                if not swiftc:
                    raise RuntimeError("未找到 swiftc 编译器，无法编译本地 Vision OCR 引擎")
                bin_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_out = bin_path.with_suffix(".tmp")
                res = subprocess.run(
                    [swiftc, "-O", str(_SWIFT_SOURCE), "-o", str(tmp_out)],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if res.returncode != 0:
                    tmp_out.unlink(missing_ok=True)
                    raise RuntimeError(f"编译 Vision OCR 失败: {res.stderr.strip()}")
                os.replace(tmp_out, bin_path)
            return bin_path

    async def ensure_binary(self) -> Path:
        return await asyncio.to_thread(self._ensure_binary_sync)

    def _get_server_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._server_lock is None or self._server_loop is not loop:
            self._server_lock = asyncio.Lock()
            self._server_loop = loop
        return self._server_lock

    async def _ensure_server(self) -> asyncio.subprocess.Process:
        bin_path = await self.ensure_binary()
        if (
            self._server_proc is not None
            and self._server_proc.returncode is None
            and self._server_bin == bin_path
            and self._server_proc.stdin is not None
            and self._server_proc.stdout is not None
        ):
            return self._server_proc
        await self._close_server_unlocked()
        proc = await asyncio.create_subprocess_exec(
            str(bin_path),
            "--server",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=4 * 1024 * 1024,
        )
        self._server_proc = proc
        self._server_bin = bin_path
        return proc

    async def _close_server_unlocked(self) -> None:
        proc = self._server_proc
        self._server_proc = None
        self._server_bin = None
        if proc is None:
            return
        if proc.returncode is None:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass

    async def prewarm(self) -> None:
        if not self.is_available():
            return
        try:
            lock = self._get_server_lock()
            async with lock:
                proc = await self._ensure_server()
                assert proc.stdin is not None and proc.stdout is not None
                proc.stdin.write(b'{"cmd":"ping"}\n')
                await proc.stdin.drain()
                await asyncio.wait_for(proc.stdout.readline(), timeout=10.0)
        except Exception:
            pass

    async def shutdown(self) -> None:
        if self._server_proc is not None:
            await self._close_server_unlocked()

    async def ocr_image_lines(self, image_path: Path, accurate: bool = True) -> list[OcrLine]:
        lock = self._get_server_lock()
        async with lock:
            last_err: Exception | None = None
            for attempt in range(2):
                try:
                    proc = await self._ensure_server()
                    assert proc.stdin is not None and proc.stdout is not None
                    cmd = json.dumps(
                        {"cmd": "ocr_image", "path": str(image_path), "accurate": accurate},
                        ensure_ascii=False,
                    ) + "\n"
                    proc.stdin.write(cmd.encode("utf-8"))
                    await proc.stdin.drain()
                    raw = await asyncio.wait_for(proc.stdout.readline(), timeout=30.0)
                    if not raw:
                        raise RuntimeError("Vision OCR worker exited unexpectedly")
                    data = json.loads(raw.decode("utf-8", errors="replace"))
                    if not data.get("ok"):
                        raise RuntimeError(str(data.get("error") or "Vision OCR failed"))
                    return [
                        OcrLine(
                            text=str(item["text"]),
                            x=float(item["x"]),
                            y=float(item["y"]),
                            w=float(item["w"]),
                            h=float(item["h"]),
                            confidence=float(item.get("confidence", 1.0)),
                        )
                        for item in data.get("lines", [])
                        if str(item.get("text") or "").strip()
                    ]
                except Exception as exc:
                    last_err = exc
                    await self._close_server_unlocked()
                    if attempt == 1:
                        break
            raise RuntimeError(f"本地 Vision OCR 识别失败: {last_err}")

    async def ocr_image_text(self, image_path: Path, accurate: bool = True) -> str:
        lines = await self.ocr_image_lines(image_path, accurate=accurate)
        return reconstruct_text_from_ocr_lines(lines)

    async def stream_pdf_pages_ocr(
        self,
        pdf_path: Path,
        page_numbers: list[int],
        *,
        concurrency: int = 4,
        max_dim: int = 1500,
        accurate: bool = True,
    ) -> AsyncIterator[tuple[int, list[OcrLine], str | None]]:
        if not page_numbers:
            return

        embedded_map, scanned_pages = await asyncio.to_thread(_extract_embedded_text_pages, pdf_path, page_numbers)
        for pno in page_numbers:
            if pno in embedded_map:
                yield (pno, embedded_map[pno], None)

        if not scanned_pages:
            return

        bin_path = await self.ensure_binary()
        workers = max(1, min(8, concurrency, len(scanned_pages)))
        pages_spec = _compress_page_ranges(scanned_pages)
        proc = await asyncio.create_subprocess_exec(
            str(bin_path),
            "--pdf-batch",
            str(pdf_path),
            pages_spec,
            str(workers),
            str(max_dim),
            "1" if accurate else "0",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=8 * 1024 * 1024,
        )
        assert proc.stdout is not None and proc.stderr is not None

        seen: set[int] = set()
        try:
            while True:
                raw_line = await proc.stdout.readline()
                if not raw_line:
                    break
                text_line = raw_line.decode("utf-8", errors="replace").strip()
                if not text_line:
                    continue
                try:
                    obj = json.loads(text_line)
                except json.JSONDecodeError:
                    continue
                pno = int(obj.get("page") or 0)
                if pno <= 0:
                    continue
                seen.add(pno)
                if not obj.get("ok"):
                    yield (pno, [], str(obj.get("error") or "Vision OCR failed"))
                    continue
                lines = [
                    OcrLine(
                        text=str(item["text"]),
                        x=float(item["x"]),
                        y=float(item["y"]),
                        w=float(item["w"]),
                        h=float(item["h"]),
                        confidence=float(item.get("confidence", 1.0)),
                    )
                    for item in obj.get("lines", [])
                    if str(item.get("text") or "").strip()
                ]
                yield (pno, lines, None)

            stderr_bytes = await proc.stderr.read()
            await proc.wait()
            stderr_msg = stderr_bytes.decode("utf-8", errors="replace").strip()
            for pno in scanned_pages:
                if pno not in seen:
                    yield (pno, [], stderr_msg or f"Vision OCR 未返回第 {pno} 页结果")
        finally:
            if proc.returncode is None:
                try:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except Exception:
                    try:
                        proc.kill()
                        await proc.wait()
                    except Exception:
                        pass


ocr_service = LocalVisionOcrService()
