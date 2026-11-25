import { useEffect, useRef, useState } from 'react';
import * as pdfjsLib from 'pdfjs-dist';
import pdfWorkerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url';
import 'pdfjs-dist/web/pdf_viewer.css';
import { BoxSelect, ChevronLeft, ChevronRight, Highlighter, Loader2, ZoomIn, ZoomOut } from 'lucide-react';
import type { Highlight, HighlightColor, HighlightRect, PendingSelection, SourceDetail } from '../types';

pdfjsLib.GlobalWorkerOptions.workerSrc = pdfWorkerUrl;

interface PdfViewerProps {
  source: SourceDetail;
  highlights: Highlight[];
  activeHighlightId: string | null;
  pulseHighlightId: string | null;
  jumpPage: { page: number; seq: number } | null;
  onSelectHighlight: (highlightId: string) => void;
  onSelectionMade: (sel: PendingSelection) => void;
}

function overlayStyleForColor(color: HighlightColor) {
  switch (color) {
    case 'amber':
      return 'bg-amber-400/35 border-amber-500/60';
    case 'emerald':
      return 'bg-emerald-400/30 border-emerald-500/60';
    case 'sky':
      return 'bg-sky-400/30 border-sky-500/60';
    case 'rose':
      return 'bg-rose-400/30 border-rose-500/60';
    case 'violet':
      return 'bg-violet-400/30 border-violet-500/60';
  }
}

function mergeClientRects(clientRects: DOMRect[], containerRect: DOMRect): HighlightRect[] {
  const raw: HighlightRect[] = [];
  for (const r of clientRects) {
    if (r.width < 2 || r.height < 2) continue;
    const x = Math.max(0, Math.min(1, (r.left - containerRect.left) / containerRect.width));
    const y = Math.max(0, Math.min(1, (r.top - containerRect.top) / containerRect.height));
    const width = Math.max(0, Math.min(1 - x, r.width / containerRect.width));
    const height = Math.max(0, Math.min(1 - y, r.height / containerRect.height));
    if (width > 0.002 && height > 0.002) {
      raw.push({ x, y, width, height });
    }
  }
  if (raw.length <= 1) return raw;

  // Merge rects on roughly the same horizontal line
  raw.sort((a, b) => a.y - b.y || a.x - b.x);
  const merged: HighlightRect[] = [raw[0]];
  for (let i = 1; i < raw.length; i++) {
    const prev = merged[merged.length - 1];
    const cur = raw[i];
    const sameLine = Math.abs(cur.y - prev.y) < Math.max(prev.height, cur.height) * 0.55;
    if (sameLine && cur.x <= prev.x + prev.width + 0.015) {
      const minX = Math.min(prev.x, cur.x);
      const minY = Math.min(prev.y, cur.y);
      const maxX = Math.max(prev.x + prev.width, cur.x + cur.width);
      const maxY = Math.max(prev.y + prev.height, cur.y + cur.height);
      merged[merged.length - 1] = {
        x: minX,
        y: minY,
        width: maxX - minX,
        height: maxY - minY,
      };
    } else {
      merged.push(cur);
    }
  }
  return merged;
}

interface PdfPageCanvasProps {
  pdfDoc: pdfjsLib.PDFDocumentProxy;
  pageNumber: number;
  scale: number;
  areaMode: boolean;
  highlights: Highlight[];
  activeHighlightId: string | null;
  pulseHighlightId: string | null;
  onVisible: (pageNumber: number) => void;
  onSelectHighlight: (id: string) => void;
  onSelectionMade: (sel: PendingSelection) => void;
  registerPageRef: (pageNumber: number, el: HTMLDivElement | null) => void;
}

function PdfPageCanvas({
  pdfDoc,
  pageNumber,
  scale,
  areaMode,
  highlights,
  activeHighlightId,
  pulseHighlightId,
  onVisible,
  onSelectHighlight,
  onSelectionMade,
  registerPageRef,
}: PdfPageCanvasProps) {
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const textLayerRef = useRef<HTMLDivElement | null>(null);
  const [inView, setInView] = useState(pageNumber <= 2);
  const [dims, setDims] = useState<{ width: number; height: number }>({ width: 720, height: 960 });
  const [dragBox, setDragBox] = useState<{ startX: number; startY: number; curX: number; curY: number } | null>(null);

  useEffect(() => {
    const el = wrapperRef.current;
    registerPageRef(pageNumber, el);
    if (!el) return;

    const obs = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            setInView(true);
            if (entry.intersectionRatio > 0.35) {
              onVisible(pageNumber);
            }
          }
        }
      },
      { rootMargin: '320px 0px', threshold: [0.05, 0.4] },
    );
    obs.observe(el);
    return () => {
      obs.disconnect();
      registerPageRef(pageNumber, null);
    };
  }, [pageNumber, onVisible, registerPageRef]);

  useEffect(() => {
    if (!inView) return;
    let cancelled = false;
    let renderTask: pdfjsLib.RenderTask | null = null;
    let textLayer: pdfjsLib.TextLayer | null = null;

    const renderPage = async () => {
      const page = await pdfDoc.getPage(pageNumber);
      if (cancelled) return;

      const viewport = page.getViewport({ scale });
      setDims({ width: viewport.width, height: viewport.height });

      const canvas = canvasRef.current;
      const textLayerDiv = textLayerRef.current;
      if (!canvas || !textLayerDiv) return;

      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.floor(viewport.width * dpr);
      canvas.height = Math.floor(viewport.height * dpr);
      canvas.style.width = `${viewport.width}px`;
      canvas.style.height = `${viewport.height}px`;

      const ctx = canvas.getContext('2d');
      if (!ctx) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

      renderTask = page.render({ canvasContext: ctx, canvas, viewport });
      await renderTask.promise;
      if (cancelled) return;

      textLayerDiv.innerHTML = '';
      textLayerDiv.style.width = `${viewport.width}px`;
      textLayerDiv.style.height = `${viewport.height}px`;
      textLayerDiv.style.setProperty('--scale-factor', String(viewport.scale));
      textLayerDiv.style.setProperty('--total-scale-factor', String(viewport.scale));

      textLayer = new pdfjsLib.TextLayer({
        textContentSource: page.streamTextContent(),
        container: textLayerDiv,
        viewport,
      });
      await textLayer.render();
    };

    renderPage().catch(() => {});

    return () => {
      cancelled = true;
      renderTask?.cancel();
      textLayer?.cancel();
    };
  }, [pdfDoc, pageNumber, scale, inView]);

  const handleTextMouseUp = () => {
    if (areaMode) return;
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed) return;
    const quoteText = sel.toString().trim();
    if (!quoteText || !wrapperRef.current) return;

    const range = sel.getRangeAt(0);
    if (!wrapperRef.current.contains(range.commonAncestorContainer)) return;

    const containerRect = wrapperRef.current.getBoundingClientRect();
    const rects = mergeClientRects(Array.from(range.getClientRects()), containerRect);
    if (rects.length === 0) return;
    const box = range.getBoundingClientRect();

    onSelectionMade({
      quoteText,
      anchorType: 'text',
      pageNumber,
      rects,
      clientX: box.left + box.width / 2,
      clientY: box.top,
    });
  };

  const handleAreaMouseDown = (e: React.MouseEvent<HTMLDivElement>) => {
    if (!areaMode || !wrapperRef.current) return;
    e.preventDefault();
    const r = wrapperRef.current.getBoundingClientRect();
    const x = (e.clientX - r.left) / r.width;
    const y = (e.clientY - r.top) / r.height;
    setDragBox({ startX: x, startY: y, curX: x, curY: y });
  };

  const handleAreaMouseMove = (e: React.MouseEvent<HTMLDivElement>) => {
    if (!areaMode || !dragBox || !wrapperRef.current) return;
    const r = wrapperRef.current.getBoundingClientRect();
    const x = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
    const y = Math.max(0, Math.min(1, (e.clientY - r.top) / r.height));
    setDragBox((prev) => (prev ? { ...prev, curX: x, curY: y } : null));
  };

  const handleAreaMouseUp = (e: React.MouseEvent<HTMLDivElement>) => {
    if (!areaMode || !dragBox || !wrapperRef.current) return;
    const minX = Math.min(dragBox.startX, dragBox.curX);
    const minY = Math.min(dragBox.startY, dragBox.curY);
    const width = Math.abs(dragBox.curX - dragBox.startX);
    const height = Math.abs(dragBox.curY - dragBox.startY);
    setDragBox(null);

    if (width < 0.015 || height < 0.015) return;
    onSelectionMade({
      quoteText: '',
      anchorType: 'area',
      pageNumber,
      rects: [{ x: minX, y: minY, width, height }],
      clientX: e.clientX,
      clientY: e.clientY,
    });
  };

  return (
    <div className="flex flex-col items-center">
      <div
        ref={wrapperRef}
        style={{ width: dims.width, height: dims.height }}
        onMouseUp={areaMode ? handleAreaMouseUp : handleTextMouseUp}
        onMouseDown={areaMode ? handleAreaMouseDown : undefined}
        onMouseMove={areaMode ? handleAreaMouseMove : undefined}
        className={`relative bg-white shadow-md border border-[#E5E0D5] rounded-sm overflow-hidden ${
          areaMode ? 'cursor-crosshair select-none' : ''
        }`}
      >
        <canvas ref={canvasRef} className="block" />
        <div
          ref={textLayerRef}
          className={`textLayer ${areaMode ? 'pointer-events-none' : ''}`}
        />

        {/* Saved Highlights Overlay */}
        <div className="absolute inset-0 pointer-events-none z-10">
          {highlights.map((hl) => {
            const active = hl.id === activeHighlightId;
            const pulsed = hl.id === pulseHighlightId;
            const qaCount = Math.ceil(hl.messages.length / 2);
            const firstRect = hl.rects[0];
            return (
              <div key={hl.id}>
                {hl.rects.map((rect, idx) => (
                  <button
                    key={`${hl.id}-${idx}`}
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      onSelectHighlight(hl.id);
                    }}
                    style={{
                      left: `${rect.x * 100}%`,
                      top: `${rect.y * 100}%`,
                      width: `${rect.width * 100}%`,
                      height: `${rect.height * 100}%`,
                    }}
                    className={`absolute pointer-events-auto border rounded-[2px] transition cursor-pointer ${overlayStyleForColor(
                      hl.color,
                    )} ${active ? 'ring-2 ring-[#B45309]' : 'hover:brightness-95'} ${pulsed ? 'hl-pulse' : ''}`}
                    title={hl.note_text || hl.quote_text || '查看划线备注'}
                  />
                ))}
                {firstRect && (hl.note_text || qaCount > 0) && (
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      onSelectHighlight(hl.id);
                    }}
                    style={{
                      top: `${firstRect.y * 100}%`,
                      right: '6px',
                    }}
                    className="absolute pointer-events-auto px-1.5 py-0.5 rounded-full bg-[#1C1B18]/85 text-white text-[10px] font-medium shadow-xs hover:bg-[#B45309] cursor-pointer"
                  >
                    {qaCount > 0 ? `${qaCount} 问答` : '笔记'}
                  </button>
                )}
              </div>
            );
          })}

          {/* Area Drag Preview */}
          {dragBox && (
            <div
              style={{
                left: `${Math.min(dragBox.startX, dragBox.curX) * 100}%`,
                top: `${Math.min(dragBox.startY, dragBox.curY) * 100}%`,
                width: `${Math.abs(dragBox.curX - dragBox.startX) * 100}%`,
                height: `${Math.abs(dragBox.curY - dragBox.startY) * 100}%`,
              }}
              className="absolute border-2 border-dashed border-[#B45309] bg-amber-300/25 rounded-xs"
            />
          )}
        </div>
      </div>
      <div className="text-[11px] text-[#5E5B53] mt-1.5 mb-5">第 {pageNumber} 页</div>
    </div>
  );
}

export function PdfViewer({
  source,
  highlights,
  activeHighlightId,
  pulseHighlightId,
  jumpPage,
  onSelectHighlight,
  onSelectionMade,
}: PdfViewerProps) {
  const [pdfDoc, setPdfDoc] = useState<pdfjsLib.PDFDocumentProxy | null>(null);
  const [loading, setLoading] = useState(true);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [scale, setScale] = useState(1.25);
  const [currentPage, setCurrentPage] = useState(1);
  const [areaMode, setAreaMode] = useState(false);

  const pageRefs = useRef<Record<number, HTMLDivElement | null>>({});

  useEffect(() => {
    let cancelled = false;
    let loadedDoc: pdfjsLib.PDFDocumentProxy | null = null;
    setLoading(true);
    setErrorMsg(null);
    setCurrentPage(1);

    const task = pdfjsLib.getDocument(`/api/sources/${source.id}/file`);
    task.promise
      .then((doc) => {
        if (cancelled) {
          doc.destroy();
          return;
        }
        loadedDoc = doc;
        setPdfDoc(doc);
        setLoading(false);
      })
      .catch((err) => {
        if (cancelled) return;
        setErrorMsg(err instanceof Error ? err.message : 'PDF 加载失败');
        setLoading(false);
      });

    return () => {
      cancelled = true;
      task.destroy();
      loadedDoc?.destroy();
    };
  }, [source.id]);

  const scrollToPage = (pageNum: number) => {
    const el = pageRefs.current[pageNum];
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'start' });
      setCurrentPage(pageNum);
    }
  };

  useEffect(() => {
    if (!jumpPage) return;
    scrollToPage(jumpPage.page);
  }, [jumpPage]);

  const numPages = pdfDoc?.numPages || source.page_count || 1;

  return (
    <div className="flex-1 flex flex-col min-h-0 bg-[#EAE6DC]">
      {/* PDF Toolbar */}
      <div className="h-11 shrink-0 bg-white border-b border-[#E5E0D5] px-4 flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-xs font-semibold text-[#1C1B18] truncate max-w-[260px]">{source.title}</span>
          <div className="flex items-center gap-1 text-xs text-[#5E5B53] border-l border-[#E5E0D5] pl-2">
            <button
              type="button"
              onClick={() => scrollToPage(Math.max(1, currentPage - 1))}
              disabled={currentPage <= 1}
              className="p-1 rounded hover:bg-[#EFECE5] disabled:opacity-35 cursor-pointer"
            >
              <ChevronLeft className="w-3.5 h-3.5" />
            </button>
            <span>
              {currentPage} / {numPages}
            </span>
            <button
              type="button"
              onClick={() => scrollToPage(Math.min(numPages, currentPage + 1))}
              disabled={currentPage >= numPages}
              className="p-1 rounded hover:bg-[#EFECE5] disabled:opacity-35 cursor-pointer"
            >
              <ChevronRight className="w-3.5 h-3.5" />
            </button>
          </div>
        </div>

        <div className="flex items-center gap-2">
          <div className="flex items-center rounded-lg border border-[#E5E0D5] bg-[#FAF8F4] p-0.5 text-[11px]">
            <button
              type="button"
              onClick={() => setAreaMode(false)}
              className={`inline-flex items-center gap-1 px-2 py-1 rounded-md cursor-pointer ${
                !areaMode ? 'bg-white text-[#B45309] font-medium shadow-xs' : 'text-[#5E5B53]'
              }`}
            >
              <Highlighter className="w-3 h-3" />
              <span>划词画线</span>
            </button>
            <button
              type="button"
              onClick={() => setAreaMode(true)}
              className={`inline-flex items-center gap-1 px-2 py-1 rounded-md cursor-pointer ${
                areaMode ? 'bg-white text-[#B45309] font-medium shadow-xs' : 'text-[#5E5B53]'
              }`}
              title="框选公式、图表或扫描页区域"
            >
              <BoxSelect className="w-3 h-3" />
              <span>框选画线</span>
            </button>
          </div>

          <div className="flex items-center gap-1 border-l border-[#E5E0D5] pl-2">
            <button
              type="button"
              onClick={() => setScale((s) => Math.max(0.7, Number((s - 0.15).toFixed(2))))}
              className="p-1.5 rounded-lg hover:bg-[#EFECE5] text-[#5E5B53] cursor-pointer"
              title="缩小"
            >
              <ZoomOut className="w-3.5 h-3.5" />
            </button>
            <span className="text-xs font-medium text-[#1C1B18] w-11 text-center">{Math.round(scale * 100)}%</span>
            <button
              type="button"
              onClick={() => setScale((s) => Math.min(2.4, Number((s + 0.15).toFixed(2))))}
              className="p-1.5 rounded-lg hover:bg-[#EFECE5] text-[#5E5B53] cursor-pointer"
              title="放大"
            >
              <ZoomIn className="w-3.5 h-3.5" />
            </button>
          </div>
        </div>
      </div>

      {/* Pages Scroll Area */}
      <div className="flex-1 overflow-y-auto px-4 py-6">
        {loading ? (
          <div className="flex items-center justify-center h-64 text-sm text-[#5E5B53] gap-2">
            <Loader2 className="w-5 h-5 animate-spin text-[#B45309]" />
            <span>正在渲染 PDF 文档...</span>
          </div>
        ) : errorMsg ? (
          <div className="max-w-md mx-auto mt-12 p-4 rounded-xl bg-rose-50 border border-rose-200 text-xs text-rose-700">
            {errorMsg}
          </div>
        ) : pdfDoc ? (
          Array.from({ length: pdfDoc.numPages }, (_, idx) => idx + 1).map((pageNum) => (
            <PdfPageCanvas
              key={`${source.id}-${pageNum}`}
              pdfDoc={pdfDoc}
              pageNumber={pageNum}
              scale={scale}
              areaMode={areaMode}
              highlights={highlights.filter((h) => h.page_number === pageNum)}
              activeHighlightId={activeHighlightId}
              pulseHighlightId={pulseHighlightId}
              onVisible={setCurrentPage}
              onSelectHighlight={onSelectHighlight}
              onSelectionMade={onSelectionMade}
              registerPageRef={(p, el) => {
                pageRefs.current[p] = el;
              }}
            />
          ))
        ) : null}
      </div>
    </div>
  );
}
