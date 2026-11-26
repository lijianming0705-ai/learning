from __future__ import annotations

import zipfile
from pathlib import Path

import fitz

from app.services.source_parser import crop_pdf_region, parse_docx, parse_html_article, parse_pdf, parse_text_file


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
        "gemini-4-hydrogen-medium\tGemini 4 Hydrogen (Medium)\n"
        "claude-sonnet-5-5-high\tClaude Sonnet 5.5 (High)\n"
    )
    models = parse_agy_models_output(raw)
    assert [m.id for m in models] == [
        "gemini-4-hydrogen-medium",
        "gemini-3.8-flash-medium",
        "gemini-4-neon-high",
        "claude-sonnet-5-5-high",
    ]


def test_scanned_pdf_detection_and_region_crop(tmp_path: Path) -> None:
    # Render a text page to a raster PNG and embed into a blank PDF (0 text layer)
    src_doc = fitz.open()
    sp = src_doc.new_page(width=400, height=200)
    sp.insert_text((30, 80), "Scanned Raster Page Formula: E = mc^2", fontsize=14)
    img_bytes = src_doc[0].get_pixmap(matrix=fitz.Matrix(2, 2)).tobytes("png")
    src_doc.close()

    scanned_pdf = tmp_path / "scanned.pdf"
    out_doc = fitz.open()
    page = out_doc.new_page(width=400, height=200)
    page.insert_image(page.rect, stream=img_bytes)
    out_doc.save(str(scanned_pdf))
    out_doc.close()

    parsed = parse_pdf(scanned_pdf, "scanned")
    assert parsed.is_scanned is True
    assert parsed.page_count == 1
    assert parsed.plain_text == ""

    crop_out = tmp_path / "crop.png"
    crop_pdf_region(
        scanned_pdf,
        1,
        [{"x": 0.05, "y": 0.2, "width": 0.8, "height": 0.4}],
        crop_out,
    )
    assert crop_out.exists()
    assert crop_out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_reconstruct_layout_from_ocr_lines() -> None:
    from app.services.ocr_service import OcrLine, reconstruct_layout_from_ocr_lines

    lines = [
        # Running header at very top
        OcrLine(text="12 | 启示录：打造用户喜爱的产品", x=0.10, y=0.03, w=0.45, h=0.016),
        # Large centered chapter title
        OcrLine(text="第1章 关键角色及其职责", x=0.22, y=0.12, w=0.56, h=0.036),
        # Indented paragraph across two lines
        OcrLine(text="在现代软件团队中，产品经", x=0.16, y=0.20, w=0.72, h=0.020),
        OcrLine(text="理需要与设计师紧密协作。", x=0.10, y=0.23, w=0.42, h=0.020),
        # Unordered list
        OcrLine(text="• 定义产品愿景与核心价值", x=0.10, y=0.30, w=0.50, h=0.020),
        OcrLine(text="• 持续验证用户真实需求", x=0.10, y=0.34, w=0.48, h=0.020),
        # Figure caption right below figure
        OcrLine(text="图 1-1 产品探索流程示意图", x=0.28, y=0.69, w=0.44, h=0.018),
        # Bottom running page number
        OcrLine(text="12", x=0.48, y=0.94, w=0.04, h=0.015),
    ]
    figures = [{"bbox": {"x": 0.15, "y": 0.42, "width": 0.70, "height": 0.25}, "width_pct": 70}]

    layout = reconstruct_layout_from_ocr_lines(lines, figures=figures)
    assert layout["layout_columns"] == 1
    blocks = layout["blocks"]
    types = [b["type"] for b in blocks]
    assert "heading" in types
    assert "paragraph" in types
    assert "list" in types
    assert "figure" in types

    para = [b for b in blocks if b["type"] == "paragraph"][0]
    assert para["text"] == "在现代软件团队中，产品经理需要与设计师紧密协作。"
    assert para["indent"] is True

    fig = [b for b in blocks if b["type"] == "figure"][0]
    assert fig["caption"] == "图 1-1 产品探索流程示意图"

    lst = [b for b in blocks if b["type"] == "list"][0]
    assert lst["ordered"] is False
    assert lst["items"] == ["定义产品愿景与核心价值", "持续验证用户真实需求"]

    # Verify running header/footer were stripped
    all_text = str(blocks)
    assert "12 | 启示录" not in all_text

    # 2-column detection
    col_lines = [
        OcrLine(text="Left Col Line 1", x=0.08, y=0.15, w=0.36, h=0.02),
        OcrLine(text="Left Col Line 2", x=0.08, y=0.19, w=0.36, h=0.02),
        OcrLine(text="Left Col Line 3", x=0.08, y=0.23, w=0.36, h=0.02),
        OcrLine(text="Right Col Line 1", x=0.54, y=0.15, w=0.36, h=0.02),
        OcrLine(text="Right Col Line 2", x=0.54, y=0.19, w=0.36, h=0.02),
        OcrLine(text="Right Col Line 3", x=0.54, y=0.23, w=0.36, h=0.02),
    ]
    col_layout = reconstruct_layout_from_ocr_lines(col_lines)
    assert col_layout["layout_columns"] == 2
    assert any(b["type"] == "columns" for b in col_layout["blocks"])

