from __future__ import annotations

import base64
import html
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import fitz  # PyMuPDF
import httpx
import mammoth
from bs4 import BeautifulSoup
from readability import Document as ReadabilityDocument


@dataclass
class SourceChunk:
    page_number: int | None
    content: str


@dataclass
class ParsedSource:
    type: str  # "pdf" | "docx" | "url" | "text"
    title: str
    html_content: str
    plain_text: str
    page_count: int
    word_count: int
    pages: list[tuple[int, str]]
    chunks: list[SourceChunk]
    is_scanned: bool = False


_DANGEROUS_TAGS = {"script", "style", "iframe", "object", "embed", "form", "noscript", "svg"}


def chunk_text(text: str, page_number: int | None = None, max_chars: int = 800, overlap: int = 120) -> list[SourceChunk]:
    cleaned = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if not cleaned:
        return []
    if len(cleaned) <= max_chars:
        return [SourceChunk(page_number=page_number, content=cleaned)]

    chunks: list[SourceChunk] = []
    start = 0
    step = max(200, max_chars - overlap)
    while start < len(cleaned):
        end = min(len(cleaned), start + max_chars)
        # Prefer breaking on paragraph or sentence boundary if not at end
        if end < len(cleaned):
            for sep in ("\n", "。", ". ", "！", "？", "；", " "):
                idx = cleaned.rfind(sep, start + step // 2, end)
                if idx != -1:
                    end = idx + len(sep)
                    break
        piece = cleaned[start:end].strip()
        if piece:
            chunks.append(SourceChunk(page_number=page_number, content=piece))
        if end >= len(cleaned):
            break
        start = max(start + 1, end - overlap)
    return chunks


def count_words(text: str) -> int:
    # Count both CJK characters and space-delimited Latin words
    cjk_count = len(re.findall(r"[\u4e00-\u9fff\u3400-\u4dbf]", text))
    latin_words = len(re.findall(r"[A-Za-z0-9_]+", text))
    return cjk_count + latin_words


def sanitize_html(raw_html: str, base_url: str | None = None) -> tuple[str, str]:
    soup = BeautifulSoup(raw_html, "lxml")
    for tag_name in _DANGEROUS_TAGS:
        for el in soup.find_all(tag_name):
            el.decompose()

    for tag in soup.find_all(True):
        attrs_to_remove = []
        for attr, val in list(tag.attrs.items()):
            low = attr.lower()
            if low.startswith("on"):
                attrs_to_remove.append(attr)
                continue
            if low in ("href", "src") and isinstance(val, str):
                stripped = val.strip()
                if stripped.lower().startswith("javascript:"):
                    attrs_to_remove.append(attr)
                elif base_url and not stripped.startswith(("data:", "http://", "https://", "#")):
                    tag.attrs[attr] = urljoin(base_url, stripped)
        for attr in attrs_to_remove:
            del tag.attrs[attr]
        if tag.name == "a" and tag.get("href"):
            tag["target"] = "_blank"
            tag["rel"] = "noopener noreferrer"

    body = soup.body if soup.body else soup
    cleaned_html = "".join(str(child) for child in body.contents).strip()
    if not cleaned_html:
        cleaned_html = str(soup).strip()
    plain_text = "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())
    return cleaned_html, plain_text


def parse_pdf(file_path: Path, fallback_title: str) -> ParsedSource:
    doc = fitz.open(str(file_path))
    try:
        meta_title = (doc.metadata or {}).get("title") or ""
        title = meta_title.strip() or fallback_title
        pages: list[tuple[int, str]] = []
        chunks: list[SourceChunk] = []
        all_text_parts: list[str] = []
        total_non_ws = 0
        scanned_like_pages = 0
        any_images = False

        for idx, page in enumerate(doc, start=1):
            text = (page.get_text("text") or "").strip()
            pages.append((idx, text))
            non_ws = len("".join(text.split()))
            total_non_ws += non_ws
            has_images = len(page.get_images(full=False)) > 0
            if has_images:
                any_images = True
            if non_ws < 20 and has_images:
                scanned_like_pages += 1
            if text:
                all_text_parts.append(text)
                chunks.extend(chunk_text(text, page_number=idx))

        page_count = len(doc)
        avg_chars = (total_non_ws / page_count) if page_count > 0 else 0.0
        is_scanned = page_count > 0 and any_images and (
            avg_chars < 35 or (scanned_like_pages / page_count) >= 0.6
        )

        plain_text = "\n\n".join(all_text_parts)
        return ParsedSource(
            type="pdf",
            title=title,
            html_content="",
            plain_text=plain_text,
            page_count=page_count,
            word_count=count_words(plain_text),
            pages=pages,
            chunks=chunks,
            is_scanned=is_scanned,
        )
    finally:
        doc.close()


def crop_pdf_region(
    pdf_path: Path,
    page_number: int,
    rects: list[dict[str, float]],
    output_path: Path,
) -> Path:
    if not rects:
        raise ValueError("No bounding rects supplied for PDF crop")
    doc = fitz.open(str(pdf_path))
    try:
        page_idx = max(0, min(len(doc) - 1, page_number - 1))
        page = doc[page_idx]
        pr = page.rect

        min_x = min(max(0.0, float(r["x"])) for r in rects)
        min_y = min(max(0.0, float(r["y"])) for r in rects)
        max_x = max(min(1.0, float(r["x"]) + float(r["width"])) for r in rects)
        max_y = max(min(1.0, float(r["y"]) + float(r["height"])) for r in rects)

        pad = 3.0
        clip = fitz.Rect(
            max(pr.x0, pr.x0 + min_x * pr.width - pad),
            max(pr.y0, pr.y0 + min_y * pr.height - pad),
            min(pr.x1, pr.x0 + max_x * pr.width + pad),
            min(pr.y1, pr.y0 + max_y * pr.height + pad),
        )
        if clip.width < 2 or clip.height < 2:
            clip = pr

        max_dim = max(clip.width, clip.height)
        zoom = min(2.5, 2200.0 / max(1.0, max_dim))
        zoom = max(1.25, zoom)
        pix = page.get_pixmap(clip=clip, matrix=fitz.Matrix(zoom, zoom), alpha=False)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        pix.save(str(output_path))
        return output_path
    finally:
        doc.close()


def parse_docx(file_path: Path, fallback_title: str) -> ParsedSource:
    with open(file_path, "rb") as f:
        result = mammoth.convert_to_html(f)
    cleaned_html, plain_text = sanitize_html(result.value)
    if not cleaned_html and plain_text:
        cleaned_html = _plain_text_to_html(plain_text)

    chunks = chunk_text(plain_text, page_number=1)
    return ParsedSource(
        type="docx",
        title=fallback_title,
        html_content=cleaned_html,
        plain_text=plain_text,
        page_count=1,
        word_count=count_words(plain_text),
        pages=[(1, plain_text)],
        chunks=chunks,
    )


def parse_text_file(file_path: Path, fallback_title: str) -> ParsedSource:
    raw = file_path.read_text(encoding="utf-8", errors="replace")
    cleaned_html = _plain_text_to_html(raw)
    plain_text = "\n".join(line.rstrip() for line in raw.splitlines()).strip()
    chunks = chunk_text(plain_text, page_number=1)
    return ParsedSource(
        type="text",
        title=fallback_title,
        html_content=cleaned_html,
        plain_text=plain_text,
        page_count=1,
        word_count=count_words(plain_text),
        pages=[(1, plain_text)],
        chunks=chunks,
    )


def parse_html_article(raw_html: str, final_url: str, title_override: str | None = None) -> ParsedSource:
    soup_raw = BeautifulSoup(raw_html, "lxml")
    raw_title = (soup_raw.title.string or "").strip() if soup_raw.title and soup_raw.title.string else ""

    article_html = ""
    readable_title = ""
    try:
        rdoc = ReadabilityDocument(raw_html)
        readable_title = (rdoc.short_title() or "").strip()
        article_html = rdoc.summary(html_partial=True)
    except Exception:
        article_html = ""

    if not article_html or len(BeautifulSoup(article_html, "lxml").get_text(strip=True)) < 40:
        fallback_node = soup_raw.find("article") or soup_raw.find("main") or soup_raw.body or soup_raw
        article_html = str(fallback_node)

    cleaned_html, plain_text = sanitize_html(article_html, base_url=final_url)
    title = (title_override or readable_title or raw_title or urlparse(final_url).netloc or final_url).strip()
    chunks = chunk_text(plain_text, page_number=1)

    return ParsedSource(
        type="url",
        title=title[:280],
        html_content=cleaned_html,
        plain_text=plain_text,
        page_count=1,
        word_count=count_words(plain_text),
        pages=[(1, plain_text)],
        chunks=chunks,
    )


async def fetch_and_parse_url(url: str, title_override: str | None = None) -> ParsedSource:
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("请输入有效的 http:// 或 https:// 网址")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    async with httpx.AsyncClient(follow_redirects=True, timeout=20.0, headers=headers) as client:
        resp = await client.get(url.strip())
        resp.raise_for_status()
        final_url = str(resp.url)
        content_type = (resp.headers.get("content-type") or "").lower()
        if "text/plain" in content_type or "text/markdown" in content_type:
            raw_text = resp.text
            cleaned_html = _plain_text_to_html(raw_text)
            title = (title_override or urlparse(final_url).path.rsplit("/", 1)[-1] or final_url).strip()
            return ParsedSource(
                type="url",
                title=title[:280],
                html_content=cleaned_html,
                plain_text=raw_text.strip(),
                page_count=1,
                word_count=count_words(raw_text),
                pages=[(1, raw_text.strip())],
                chunks=chunk_text(raw_text, page_number=1),
            )
        return parse_html_article(resp.text, final_url, title_override=title_override)


def _plain_text_to_html(text: str) -> str:
    blocks = re.split(r"\n\s*\n", text.strip())
    paragraphs: list[str] = []
    for block in blocks:
        lines = "<br />".join(html.escape(line) for line in block.splitlines())
        if lines:
            paragraphs.append(f"<p>{lines}</p>")
    return "\n".join(paragraphs)


def render_pdf_page_to_png(
    pdf_path: Path,
    page_number: int,
    output_path: Path,
    max_dim: float = 2000.0,
) -> Path:
    doc = fitz.open(str(pdf_path))
    try:
        page_idx = max(0, min(len(doc) - 1, page_number - 1))
        page = doc[page_idx]
        pr = page.rect
        largest = max(pr.width, pr.height, 1.0)
        zoom = max(1.3, min(2.2, max_dim / largest))
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        pix.save(str(output_path))
        return output_path
    finally:
        doc.close()


def _safe_align_class(align: Any) -> str:
    val = str(align or "left").strip().lower()
    if val in ("center", "right", "justify"):
        return f"ocr-align-{val}"
    return "ocr-align-left"


def _escape_multiline(text: str) -> str:
    lines = [html.escape(line) for line in (text or "").splitlines()]
    return "<br />".join(lines)


def render_ocr_blocks_to_html(
    blocks: list[dict[str, Any]],
    *,
    pdf_path: Path,
    page_number: int,
    new_source_id: str,
    assets_dir: Path,
    fig_counter: list[int],
    plain_parts: list[str],
) -> str:
    html_parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = str(block.get("type") or "paragraph").strip().lower()

        if btype == "heading":
            txt = str(block.get("text") or "").strip()
            if not txt:
                continue
            try:
                level = int(block.get("level") or 2)
            except (TypeError, ValueError):
                level = 2
            level = max(1, min(4, level))
            align_cls = _safe_align_class(block.get("align"))
            html_parts.append(f'<h{level} class="{align_cls}">{html.escape(txt)}</h{level}>')
            plain_parts.append(txt)

        elif btype == "paragraph":
            txt = str(block.get("text") or "").strip()
            if not txt:
                continue
            classes = [_safe_align_class(block.get("align"))]
            if block.get("indent"):
                classes.append("ocr-indent")
            html_parts.append(f'<p class="{" ".join(classes)}">{_escape_multiline(txt)}</p>')
            plain_parts.append(txt)

        elif btype == "list":
            raw_items = block.get("items")
            if not isinstance(raw_items, list):
                continue
            items = [str(it).strip() for it in raw_items if str(it).strip()]
            if not items:
                continue
            tag = "ol" if block.get("ordered") else "ul"
            lis = "".join(f"<li>{_escape_multiline(it)}</li>" for it in items)
            html_parts.append(f"<{tag}>{lis}</{tag}>")
            plain_parts.extend(items)

        elif btype == "blockquote":
            txt = str(block.get("text") or "").strip()
            if not txt:
                continue
            html_parts.append(f"<blockquote><p>{_escape_multiline(txt)}</p></blockquote>")
            plain_parts.append(txt)

        elif btype == "formula":
            txt = str(block.get("latex") or block.get("text") or "").strip()
            if not txt:
                continue
            html_parts.append(f'<div class="ocr-formula">{_escape_multiline(txt)}</div>')
            plain_parts.append(txt)

        elif btype == "code":
            txt = str(block.get("text") or "").rstrip()
            if not txt:
                continue
            html_parts.append(f"<pre><code>{html.escape(txt)}</code></pre>")
            plain_parts.append(txt)

        elif btype == "table":
            headers = block.get("headers")
            rows = block.get("rows")
            has_headers = isinstance(headers, list) and any(str(h).strip() for h in headers)
            has_rows = isinstance(rows, list) and len(rows) > 0
            if not has_headers and not has_rows:
                continue
            thead = ""
            if has_headers:
                ths = "".join(f"<th>{html.escape(str(h).strip())}</th>" for h in headers)
                thead = f"<thead><tr>{ths}</tr></thead>"
                plain_parts.append(" | ".join(str(h).strip() for h in headers))
            tbody_rows: list[str] = []
            if has_rows:
                for row in rows:
                    if not isinstance(row, list):
                        continue
                    tds = "".join(f"<td>{_escape_multiline(str(c).strip())}</td>" for c in row)
                    tbody_rows.append(f"<tr>{tds}</tr>")
                    row_txt = " | ".join(str(c).strip() for c in row if str(c).strip())
                    if row_txt:
                        plain_parts.append(row_txt)
            tbody = f"<tbody>{''.join(tbody_rows)}</tbody>" if tbody_rows else ""
            html_parts.append(f"<table>{thead}{tbody}</table>")

        elif btype == "columns":
            cols = block.get("columns")
            if not isinstance(cols, list) or not cols:
                continue
            col_htmls: list[str] = []
            for col_blocks in cols[:3]:
                if not isinstance(col_blocks, list):
                    continue
                inner = render_ocr_blocks_to_html(
                    col_blocks,
                    pdf_path=pdf_path,
                    page_number=page_number,
                    new_source_id=new_source_id,
                    assets_dir=assets_dir,
                    fig_counter=fig_counter,
                    plain_parts=plain_parts,
                )
                if inner:
                    col_htmls.append(f'<div class="ocr-column">{inner}</div>')
            if col_htmls:
                col_cls = "ocr-columns-2" if len(col_htmls) >= 2 else ""
                if col_cls:
                    html_parts.append(f'<div class="{col_cls}">{"".join(col_htmls)}</div>')
                else:
                    html_parts.append("".join(col_htmls))

        elif btype == "figure":
            bbox = block.get("bbox")
            if not isinstance(bbox, dict):
                continue
            try:
                bx = max(0.0, min(1.0, float(bbox.get("x", 0.0))))
                by = max(0.0, min(1.0, float(bbox.get("y", 0.0))))
                bw = max(0.0, min(1.0 - bx, float(bbox.get("width", 0.0))))
                bh = max(0.0, min(1.0 - by, float(bbox.get("height", 0.0))))
            except (TypeError, ValueError):
                continue
            if bw < 0.03 or bh < 0.03:
                continue

            fig_counter[0] += 1
            fig_name = f"p{page_number}_fig{fig_counter[0]}.png"
            out_img = assets_dir / fig_name
            try:
                crop_pdf_region(
                    pdf_path,
                    page_number,
                    [{"x": bx, "y": by, "width": bw, "height": bh}],
                    out_img,
                )
            except Exception:
                continue

            caption = str(block.get("caption") or "").strip()
            align_cls = _safe_align_class(block.get("align") or "center")
            try:
                width_pct = int(block.get("width_pct") or 88)
            except (TypeError, ValueError):
                width_pct = 88
            width_pct = max(25, min(100, width_pct))
            img_url = f"/api/sources/{new_source_id}/assets/{fig_name}"
            alt_text = html.escape(caption or f"第 {page_number} 页配图 {fig_counter[0]}")
            cap_html = f"<figcaption>{html.escape(caption)}</figcaption>" if caption else ""
            html_parts.append(
                f'<figure class="ocr-figure {align_cls}" style="max-width: {width_pct}%">'
                f'<img src="{img_url}" alt="{alt_text}" loading="lazy" />'
                f"{cap_html}</figure>"
            )
            if caption:
                plain_parts.append(caption)

    return "\n".join(html_parts)


def build_ocr_layout_page_html(
    layout_data: dict[str, Any],
    *,
    pdf_path: Path,
    page_number: int,
    new_source_id: str,
    assets_dir: Path,
) -> tuple[str, str, int]:
    blocks = layout_data.get("blocks")
    if not isinstance(blocks, list):
        blocks = []
    fig_counter = [0]
    plain_parts: list[str] = []
    inner_html = render_ocr_blocks_to_html(
        blocks,
        pdf_path=pdf_path,
        page_number=page_number,
        new_source_id=new_source_id,
        assets_dir=assets_dir,
        fig_counter=fig_counter,
        plain_parts=plain_parts,
    )
    try:
        cols_hint = int(layout_data.get("layout_columns") or 1)
    except (TypeError, ValueError):
        cols_hint = 1
    body_cls = "ocr-page-body ocr-page-cols-2" if cols_hint >= 2 else "ocr-page-body"
    page_html = (
        f'<section class="ocr-page" data-page="{page_number}">'
        f'<div class="ocr-page-badge">第 {page_number} 页</div>'
        f'<div class="{body_cls}">{inner_html}</div>'
        f"</section>"
    )
    page_plain = "\n\n".join(p for p in plain_parts if p).strip()
    return page_html, page_plain, fig_counter[0]


def build_standalone_export_html(
    *,
    title: str,
    html_content: str,
    source_id: str,
    assets_dir: Path,
) -> str:
    soup = BeautifulSoup(html_content or "", "lxml")
    prefix = f"/api/sources/{source_id}/assets/"
    for img in soup.find_all("img"):
        src = str(img.get("src") or "").strip()
        if src.startswith(prefix):
            fname = Path(src[len(prefix) :]).name
            asset_file = assets_dir / fname
            if asset_file.exists() and asset_file.is_file():
                b64 = base64.b64encode(asset_file.read_bytes()).decode("ascii")
                img["src"] = f"data:image/png;base64,{b64}"

    body = soup.body if soup.body else soup
    inlined = "".join(str(c) for c in body.contents).strip()
    safe_title = html.escape(title or "OCR 文本版")
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{safe_title}</title>
  <style>
    body {{ margin: 0; padding: 32px 16px; background: #F8F6F1; color: #1C1B18; font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif; line-height: 1.8; }}
    main {{ max-width: 860px; margin: 0 auto; background: #FFFFFF; border: 1px solid #E5E0D5; border-radius: 16px; padding: 32px; }}
    h1, h2, h3, h4 {{ color: #1C1B18; margin-top: 1.4em; margin-bottom: 0.6em; line-height: 1.35; }}
    p {{ margin: 0.8em 0; }}
    blockquote {{ border-left: 3px solid #D97706; background: #FAF7F0; padding: 0.6em 1em; margin: 1em 0; border-radius: 0 6px 6px 0; }}
    table {{ width: 100%; border-collapse: collapse; margin: 1em 0; font-size: 0.92em; }}
    th, td {{ border: 1px solid #E5E0D5; padding: 0.5em 0.75em; text-align: left; }}
    th {{ background: #F4F1EA; }}
    .ocr-page {{ padding-bottom: 1.5rem; margin-bottom: 1.75rem; border-bottom: 1px dashed #E5E0D5; }}
    .ocr-page:last-child {{ border-bottom: none; margin-bottom: 0; }}
    .ocr-page-badge {{ display: inline-block; font-size: 11px; font-weight: 600; color: #8E897E; background: #F4F1EA; padding: 2px 8px; border-radius: 999px; margin-bottom: 0.75rem; }}
    .ocr-columns-2 {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 1.25rem; margin: 0.85em 0; }}
    @media (max-width: 640px) {{ .ocr-columns-2 {{ grid-template-columns: 1fr; }} }}
    .ocr-figure {{ margin: 1.1em auto; padding: 0.6em; background: #FAF8F4; border: 1px solid #E5E0D5; border-radius: 10px; text-align: center; }}
    .ocr-figure img {{ max-width: 100%; height: auto; border-radius: 6px; display: block; margin: 0 auto; }}
    .ocr-figure figcaption {{ margin-top: 0.5em; font-size: 0.85em; color: #5E5B53; }}
    .ocr-align-center {{ text-align: center; }}
    .ocr-align-right {{ text-align: right; }}
    .ocr-align-justify {{ text-align: justify; }}
    .ocr-indent {{ text-indent: 2em; }}
    .ocr-formula {{ margin: 1em 0; padding: 0.75em 1em; background: #FAF7F0; border: 1px solid #ECE7DD; border-radius: 8px; text-align: center; font-family: ui-monospace, Menlo, monospace; overflow-x: auto; }}
  </style>
</head>
<body>
  <main>
    <h1>{safe_title}</h1>
    {inlined}
  </main>
</body>
</html>"""


def html_to_markdown_export(*, title: str, html_content: str) -> str:
    if not (html_content or "").strip():
        return f"# {title}\n"

    soup = BeautifulSoup(html_content, "lxml")
    lines: list[str] = [f"# {title}", ""]

    def handle_node(node: Any) -> None:
        name = getattr(node, "name", None)
        if not name:
            return
        if name in ("h1", "h2", "h3", "h4"):
            lvl = int(name[1])
            txt = node.get_text(" ", strip=True)
            if txt:
                lines.append(f"{'#' * lvl} {txt}")
                lines.append("")
        elif name == "p":
            txt = node.get_text("\n", strip=True)
            if txt:
                lines.append(txt)
                lines.append("")
        elif name in ("ul", "ol"):
            ordered = name == "ol"
            for idx, li in enumerate(node.find_all("li", recursive=False), start=1):
                txt = li.get_text(" ", strip=True)
                if txt:
                    prefix = f"{idx}. " if ordered else "- "
                    lines.append(f"{prefix}{txt}")
            lines.append("")
        elif name == "blockquote":
            txt = node.get_text("\n", strip=True)
            if txt:
                for ln in txt.splitlines():
                    lines.append(f"> {ln}")
                lines.append("")
        elif name == "pre":
            txt = node.get_text()
            lines.append("```")
            lines.append(txt.rstrip())
            lines.append("```")
            lines.append("")
        elif name == "table":
            rows = node.find_all("tr")
            if rows:
                for r_idx, tr in enumerate(rows):
                    cells = [c.get_text(" ", strip=True).replace("|", "\\|") for c in tr.find_all(["th", "td"])]
                    if not cells:
                        continue
                    lines.append("| " + " | ".join(cells) + " |")
                    if r_idx == 0:
                        lines.append("| " + " | ".join(["---"] * len(cells)) + " |")
                lines.append("")
        elif name == "figure":
            img = node.find("img")
            cap = node.find("figcaption")
            alt = (cap.get_text(" ", strip=True) if cap else (img.get("alt") if img else "")) or "配图"
            src = str(img.get("src") or "") if img else ""
            if src:
                lines.append(f"![{alt}]({src})")
            if cap:
                lines.append(f"*{cap.get_text(' ', strip=True)}*")
            lines.append("")
        elif name == "div" and "ocr-formula" in (node.get("class") or []):
            txt = node.get_text("\n", strip=True)
            if txt:
                lines.append("$$")
                lines.append(txt)
                lines.append("$$")
                lines.append("")
        elif name == "div" and "ocr-page-badge" in (node.get("class") or []):
            txt = node.get_text(" ", strip=True)
            if txt:
                lines.append(f"--- **{txt}** ---")
                lines.append("")
        else:
            for child in getattr(node, "children", []):
                if getattr(child, "name", None):
                    handle_node(child)

    root = soup.body if soup.body else soup
    for child in root.children:
        if getattr(child, "name", None):
            handle_node(child)
    return "\n".join(lines).strip() + "\n"
