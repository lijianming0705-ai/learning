from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path
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
