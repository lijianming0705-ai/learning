from __future__ import annotations

import zipfile
from pathlib import Path

import fitz

from app.services.source_parser import parse_docx, parse_html_article, parse_pdf, parse_text_file


def _create_minimal_docx(path: Path, paragraph_text: str) -> None:
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>'
        "</Relationships>"
    )
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>"
        f"{paragraph_text}"
        "</w:t></w:r></w:p></w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", document_xml)


def test_parse_pdf_multipage(tmp_path: Path) -> None:
    pdf_path = tmp_path / "sample.pdf"
    doc = fitz.open()
    p1 = doc.new_page()
    p1.insert_text((72, 72), "Page 1: Transformer architecture and self-attention.", fontsize=12)
    p2 = doc.new_page()
    p2.insert_text((72, 72), "Page 2: Layer normalization and residual connections.", fontsize=12)
    doc.save(str(pdf_path))
    doc.close()

    parsed = parse_pdf(pdf_path, "sample")
    assert parsed.type == "pdf"
    assert parsed.page_count == 2
    assert len(parsed.pages) == 2
    assert "self-attention" in parsed.pages[0][1]
    assert "residual connections" in parsed.pages[1][1]
    assert len(parsed.chunks) >= 2


def test_parse_docx_and_text(tmp_path: Path) -> None:
    docx_path = tmp_path / "notes.docx"
    _create_minimal_docx(docx_path, "Word document study notes on gradient descent.")
    parsed_docx = parse_docx(docx_path, "notes")
    assert parsed_docx.type == "docx"
    assert "gradient descent" in parsed_docx.plain_text
    assert "<p>" in parsed_docx.html_content

    txt_path = tmp_path / "readme.md"
    txt_path.write_text("第一段学习内容\n\n第二段总结", encoding="utf-8")
    parsed_txt = parse_text_file(txt_path, "readme")
    assert parsed_txt.type == "text"
    assert "第一段学习内容" in parsed_txt.plain_text


def test_parse_html_article_sanitization() -> None:
    raw = """
    <html>
      <head><title>Web Study Article</title></head>
      <body>
        <script>alert('xss')</script>
        <article>
          <h1>Deep Learning Basics</h1>
          <p onclick="evil()">Backpropagation computes gradients via the chain rule.</p>
          <a href="/next">Next Chapter</a>
        </article>
      </body>
    </html>
    """
    parsed = parse_html_article(raw, "https://example.com/guide/intro")
    assert parsed.type == "url"
    assert "Web Study Article" in parsed.title
    assert "<script" not in parsed.html_content.lower()
    assert "onclick" not in parsed.html_content.lower()
    assert "https://example.com/next" in parsed.html_content
    assert "chain rule" in parsed.plain_text


def test_parse_agy_models_output_formats() -> None:
    from app.services.agy_service import parse_agy_models_output

    raw = (
        "\r⠋ Fetching available models...\r⠙ Fetching available models..."
        "gemini-4-neon-high\tGemini 4 Neon (High)\n"
        "gemini-3.8-flash-medium        Gemini 3.8 Flash (Medium)\n"
        "claude-sonnet-5-5-high\tClaude Sonnet 5.5 (High)\n"
    )
    models = parse_agy_models_output(raw)
    assert [m.id for m in models] == [
        "gemini-3.8-flash-medium",
        "gemini-4-neon-high",
        "claude-sonnet-5-5-high",
    ]

