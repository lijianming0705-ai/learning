import { useCallback, useEffect, useRef, useState } from 'react';
import * as pdfjsLib from 'pdfjs-dist';
import pdfWorkerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url';
import 'pdfjs-dist/web/pdf_viewer.css';
import {
  BookOpenText,
  BoxSelect,
  ChevronLeft,
  ChevronRight,
  Highlighter,
  Loader2,
  MoveHorizontal,
  ScrollText,
  ZoomIn,
  ZoomOut,
} from 'lucide-react';
import { usePinchZoom } from '../hooks/usePinchZoom';
import type { Highlight, HighlightColor, HighlightRect, PendingSelection, SourceDetail } from '../types';

pdfjsLib.GlobalWorkerOptions.workerSrc = pdfWorkerUrl;

interface PdfViewerProps {
  source: SourceDetail;
  highlights: Highlight[];
  activeHighlightId: string | null;
  pulseHighlightId: string | null;
  jumpPage: { page: number; seq: number } | null;
  einkMode?: boolean;
  onSelectHighlight: (highlightId: string) => void;
  onSelectionMade: (sel: PendingSelection) => void;
}

function overlayStyleForColor(color: HighlightColor, einkMode: boolean) {
  if (einkMode) {
    switch (color) {
      case 'amber':
        return 'bg-black/10 border-black border-b-2';
      case 'emerald':
        return 'bg-black/8 border-black border-dashed border-b-2';
      case 'sky':
        return 'bg-black/8 border-black border-double border-b-4';
      case 'rose':
        return 'bg-black/15 border-black border-dotted border-b-2';
      case 'violet':
        return 'bg-black/12 border-black border-2';
    }
  }
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
  paginatedMode: boolean;
  einkMode: boolean;
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
  paginatedMode,
  einkMode,
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
  const [inView, setInView] = useState(paginatedMode || pageNumber <= 2);
  const [dims, setDims] = useState<{ width: number; height: number }>({ width: 720, height: 960 });
  const [dragBox, setDragBox] = useState<{ startX: number; startY: number; curX: number; curY: number } | null>(null);
  const activePointerRef = useRef<number | null>(null);
  const lastSelKeyRef = useRef('');

  useEffect(() => {
    const el = wrapperRef.current;
    registerPageRef(pageNumber, el);
    if (paginatedMode) {
      setInView(true);
      return () => registerPageRef(pageNumber, null);
    }
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
  }, [pageNumber, paginatedMode, onVisible, registerPageRef]);

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

  const emitTextSelection = useCallback(() => {
    if (areaMode) return;
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !wrapperRef.current) {
      lastSelKeyRef.current = '';
      return;
    }
    const quoteText = sel.toString().trim();
    if (!quoteText) return;

    const range = sel.getRangeAt(0);
    if (!wrapperRef.current.contains(range.commonAncestorContainer)) return;

    const key = `${pageNumber}:${quoteText}`;
    if (key === lastSelKeyRef.current) return;

    const containerRect = wrapperRef.current.getBoundingClientRect();
    const rects = mergeClientRects(Array.from(range.getClientRects()), containerRect);
    if (rects.length === 0) return;

    lastSelKeyRef.current = key;
    const box = range.getBoundingClientRect();
    onSelectionMade({
      quoteText,
      anchorType: 'text',
      pageNumber,
      rects,
      clientX: box.left + box.width / 2,
      clientY: box.top,
    });
  }, [areaMode, onSelectionMade, pageNumber]);

  // Debounced selectionchange handles touchscreen / E-Ink drag handles reliably
  useEffect(() => {
    if (areaMode) return;
    let timer: number | null = null;
    const onSelChange = () => {
      if (timer !== null) window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        emitTextSelection();
      }, 240);
    };
    document.addEventListener('selectionchange', onSelChange);
    return () => {
      document.removeEventListener('selectionchange', onSelChange);
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [areaMode, emitTextSelection]);

  // Pointer Events work uniformly for mouse, touch finger, and E-Ink stylus
  const handleAreaPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!areaMode || !wrapperRef.current) return;
    if ((e.target as HTMLElement).closest('button')) return;
    e.preventDefault();
    activePointerRef.current = e.pointerId;
    e.currentTarget.setPointerCapture(e.pointerId);
    const r = wrapperRef.current.getBoundingClientRect();
    const x = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
    const y = Math.max(0, Math.min(1, (e.clientY - r.top) / r.height));
    setDragBox({ startX: x, startY: y, curX: x, curY: y });
  };

  const handleAreaPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!areaMode || !dragBox || activePointerRef.current !== e.pointerId || !wrapperRef.current) return;
    const r = wrapperRef.current.getBoundingClientRect();
    const x = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
    const y = Math.max(0, Math.min(1, (e.clientY - r.top) / r.height));
    setDragBox((prev) => (prev ? { ...prev, curX: x, curY: y } : null));
  };

  const handleAreaPointerUp = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!areaMode || !dragBox || activePointerRef.current !== e.pointerId) return;
    activePointerRef.current = null;
    if (e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId);
    }
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
        onMouseUp={!areaMode ? emitTextSelection : undefined}
        onTouchEnd={!areaMode ? emitTextSelection : undefined}
        onPointerDown={areaMode ? handleAreaPointerDown : undefined}
        onPointerMove={areaMode ? handleAreaPointerMove : undefined}
        onPointerUp={areaMode ? handleAreaPointerUp : undefined}
        onPointerCancel={areaMode ? handleAreaPointerUp : undefined}
        className={`relative bg-white shadow-md border border-[#E5E0D5] rounded-sm overflow-hidden eink-surface ${
          areaMode ? 'cursor-crosshair select-none touch-none' : ''
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
                      einkMode,
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
      {!paginatedMode && <div className="text-[11px] text-[#5E5B53] mt-1.5 mb-5">第 {pageNumber} 页</div>}
    </div>
  );
}

export function PdfViewer({
  source,
  highlights,
  activeHighlightId,
  pulseHighlightId,
  jumpPage,
  einkMode = false,
  onSelectHighlight,
  onSelectionMade,
}: PdfViewerProps) {
  const [pdfDoc, setPdfDoc] = useState<pdfjsLib.PDFDocumentProxy | null>(null);
  const [loading, setLoading] = useState(true);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [renderScale, setRenderScale] = useState(1.25);
  const [displayScale, setDisplayScale] = useState(1.25);
  const [basePageWidth, setBasePageWidth] = useState<number | null>(null);
  const [currentPage, setCurrentPage] = useState(1);
  const [areaMode, setAreaMode] = useState(false);
  const [paginatedMode, setPaginatedMode] = useState(einkMode);

  const pageRefs = useRef<Record<number, HTMLDivElement | null>>({});
  const scrollContainerRef = useRef<HTMLDivElement | null>(null);
  const displayScaleRef = useRef(displayScale);
  displayScaleRef.current = displayScale;
  const swipeStartRef = useRef<{ x: number; y: number } | null>(null);

  useEffect(() => {
    setPaginatedMode(einkMode);
  }, [einkMode]);

  const setZoomImmediate = (next: number) => {
    const clamped = Math.max(0.6, Math.min(3.0, Number(next.toFixed(2))));
    setDisplayScale(clamped);
    setRenderScale(clamped);
  };

  usePinchZoom({
    containerRef: scrollContainerRef,
    minScale: 0.6,
    maxScale: 3.0,
    currentScale: displayScale,
    onScaleChange: (next) => {
      const container = scrollContainerRef.current;
      const prev = displayScaleRef.current;
      if (container && prev > 0) {
        const ratio = next / prev;
        container.scrollTop = container.scrollTop * ratio;
      }
      setDisplayScale(next);
    },
    onScaleCommit: (finalScale) => {
      const rounded = Math.max(0.6, Math.min(3.0, Number(finalScale.toFixed(2))));
      setDisplayScale(rounded);
      setRenderScale(rounded);
    },
  });

  useEffect(() => {
    let cancelled = false;
    let loadedDoc: pdfjsLib.PDFDocumentProxy | null = null;
    setLoading(true);
    setErrorMsg(null);
    setCurrentPage(1);

    const task = pdfjsLib.getDocument(`/api/sources/${source.id}/file`);
    task.promise
      .then(async (doc) => {
        if (cancelled) {
          doc.destroy();
          return;
        }
        loadedDoc = doc;
        try {
          const p1 = await doc.getPage(1);
          if (!cancelled) {
            const pageW = p1.getViewport({ scale: 1 }).width;
            setBasePageWidth(pageW);
            // Auto-fit narrow screens / tablets on first load
            if (scrollContainerRef.current && window.innerWidth < 1024) {
              const avail = Math.max(280, scrollContainerRef.current.clientWidth - 24);
              const fitted = Math.max(0.6, Math.min(1.8, Number((avail / pageW).toFixed(2))));
              setDisplayScale(fitted);
              setRenderScale(fitted);
            }
          }
        } catch {
          // ignore
        }
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

  const numPages = pdfDoc?.numPages || source.page_count || 1;

  const goToPage = useCallback(
    (pageNum: number) => {
      const clamped = Math.max(1, Math.min(numPages, pageNum));
      setCurrentPage(clamped);
      if (paginatedMode) {
        scrollContainerRef.current?.scrollTo({ top: 0, behavior: 'instant' });
        return;
      }
      const el = pageRefs.current[clamped];
      if (el) {
        el.scrollIntoView({ behavior: einkMode ? 'auto' : 'smooth', block: 'start' });
      }
    },
    [numPages, paginatedMode, einkMode],
  );

  useEffect(() => {
    if (!jumpPage) return;
    goToPage(jumpPage.page);
  }, [jumpPage, goToPage]);

  useEffect(() => {
    if (!paginatedMode) return;
    const onKeyDown = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      if (target?.tagName === 'INPUT' || target?.tagName === 'TEXTAREA' || target?.isContentEditable) return;
      if (e.key === 'ArrowRight' || e.key === 'PageDown') {
        e.preventDefault();
        goToPage(currentPage + 1);
      } else if (e.key === 'ArrowLeft' || e.key === 'PageUp') {
        e.preventDefault();
        goToPage(currentPage - 1);
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [paginatedMode, currentPage, goToPage]);

  const handleFitWidth = () => {
    if (!scrollContainerRef.current || !basePageWidth) return;
    const avail = Math.max(280, scrollContainerRef.current.clientWidth - 32);
    const next = Math.max(0.6, Math.min(3.0, Number((avail / basePageWidth).toFixed(2))));
    setZoomImmediate(next);
  };

  const previewScaleRatio = renderScale > 0 ? displayScale / renderScale : 1;

  const handleSwipeTouchStart = (e: React.TouchEvent<HTMLDivElement>) => {
    if (!paginatedMode || areaMode || e.touches.length !== 1) return;
    swipeStartRef.current = { x: e.touches[0].clientX, y: e.touches[0].clientY };
  };

  const handleSwipeTouchEnd = (e: React.TouchEvent<HTMLDivElement>) => {
    if (!paginatedMode || areaMode || !swipeStartRef.current || e.changedTouches.length !== 1) return;
    const sel = window.getSelection();
    if (sel && !sel.isCollapsed) {
      swipeStartRef.current = null;
      return;
    }
    const dx = e.changedTouches[0].clientX - swipeStartRef.current.x;
    const dy = e.changedTouches[0].clientY - swipeStartRef.current.y;
    swipeStartRef.current = null;
    if (Math.abs(dx) > 65 && Math.abs(dx) > Math.abs(dy) * 1.6) {
      if (dx < 0) goToPage(currentPage + 1);
      else goToPage(currentPage - 1);
    }
  };

  return (
    <div className="flex-1 flex flex-col min-h-0 bg-[#EAE6DC]">
      {/* PDF Toolbar */}
      <div className="min-h-11 shrink-0 bg-white border-b border-[#E5E0D5] px-2.5 sm:px-4 py-1.5 flex flex-wrap items-center justify-between gap-2 eink-surface">
        <div className="flex items-center gap-2 min-w-0">
          <span className="hidden md:inline text-xs font-semibold text-[#1C1B18] truncate max-w-[220px]">
            {source.title}
          </span>
          <div className="flex items-center gap-1 text-xs text-[#5E5B53] md:border-l md:border-[#E5E0D5] md:pl-2">
            <button
              type="button"
              onClick={() => goToPage(currentPage - 1)}
              disabled={currentPage <= 1}
              className="p-1.5 sm:p-1 rounded hover:bg-[#EFECE5] disabled:opacity-35 cursor-pointer"
            >
              <ChevronLeft className="w-4 h-4 sm:w-3.5 sm:h-3.5" />
            </button>
            <span className="tabular-nums font-medium">
              {currentPage} / {numPages}
            </span>
            <button
              type="button"
              onClick={() => goToPage(currentPage + 1)}
              disabled={currentPage >= numPages}
              className="p-1.5 sm:p-1 rounded hover:bg-[#EFECE5] disabled:opacity-35 cursor-pointer"
            >
              <ChevronRight className="w-4 h-4 sm:w-3.5 sm:h-3.5" />
            </button>
          </div>
        </div>

        <div className="flex items-center gap-1.5 sm:gap-2">
          <div className="flex items-center rounded-lg border border-[#E5E0D5] bg-[#FAF8F4] p-0.5 text-[11px]">
            <button
              type="button"
              onClick={() => setAreaMode(false)}
              className={`inline-flex items-center gap-1 px-2 py-1 rounded-md cursor-pointer ${
                !areaMode ? 'bg-white text-[#B45309] font-medium shadow-xs' : 'text-[#5E5B53]'
              }`}
            >
              <Highlighter className="w-3 h-3" />
              <span>划词</span>
            </button>
            <button
              type="button"
              onClick={() => setAreaMode(true)}
              className={`inline-flex items-center gap-1 px-2 py-1 rounded-md cursor-pointer ${
                areaMode ? 'bg-white text-[#B45309] font-medium shadow-xs' : 'text-[#5E5B53]'
              }`}
              title="手指/触控笔/鼠标框选公式、图表或扫描页区域"
            >
              <BoxSelect className="w-3 h-3" />
              <span>框选</span>
            </button>
          </div>

          <button
            type="button"
            onClick={() => setPaginatedMode((v) => !v)}
            title={paginatedMode ? '当前为单页翻页模式，点击切换连续滚动' : '当前为连续滚动，点击切换墨水屏单页翻页模式'}
            className={`inline-flex items-center gap-1 px-2 py-1 rounded-lg border text-[11px] font-medium cursor-pointer ${
              paginatedMode
                ? 'border-[#B45309] bg-amber-50 text-[#B45309]'
                : 'border-[#E5E0D5] bg-[#FAF8F4] text-[#5E5B53] hover:text-[#1C1B18]'
            }`}
          >
            {paginatedMode ? <BookOpenText className="w-3 h-3" /> : <ScrollText className="w-3 h-3" />}
            <span className="hidden sm:inline">{paginatedMode ? '翻页' : '滚动'}</span>
          </button>

          <div className="flex items-center gap-0.5 sm:gap-1 border-l border-[#E5E0D5] pl-1.5 sm:pl-2">
            <button
              type="button"
              onClick={() => setZoomImmediate(displayScale - 0.15)}
              className="p-1.5 rounded-lg hover:bg-[#EFECE5] text-[#5E5B53] cursor-pointer"
              title="缩小"
            >
              <ZoomOut className="w-3.5 h-3.5" />
            </button>
            <span
              className="text-xs font-medium text-[#1C1B18] w-10 text-center"
              title="支持触控板与触屏双指捏合缩放"
            >
              {Math.round(displayScale * 100)}%
            </span>
            <button
              type="button"
              onClick={() => setZoomImmediate(displayScale + 0.15)}
              className="p-1.5 rounded-lg hover:bg-[#EFECE5] text-[#5E5B53] cursor-pointer"
              title="放大"
            >
              <ZoomIn className="w-3.5 h-3.5" />
            </button>
            <button
              type="button"
              onClick={handleFitWidth}
              disabled={!basePageWidth}
              className="inline-flex items-center gap-1 px-2 py-1 rounded-lg border border-[#E5E0D5] bg-[#FAF8F4] hover:bg-[#EFECE5] text-[11px] font-medium text-[#1C1B18] cursor-pointer disabled:opacity-40 ml-0.5"
              title="自适应当前阅读区宽度"
            >
              <MoveHorizontal className="w-3 h-3 text-[#B45309]" />
              <span className="hidden sm:inline">适宽</span>
            </button>
          </div>
        </div>
      </div>

      {/* Pages Scroll Area */}
      <div
        ref={scrollContainerRef}
        onTouchStart={handleSwipeTouchStart}
        onTouchEnd={handleSwipeTouchEnd}
        className="flex-1 overflow-auto px-2 sm:px-4 py-4 sm:py-6"
      >
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
          <div
            style={
              Math.abs(previewScaleRatio - 1) > 0.002
                ? { transform: `scale(${previewScaleRatio})`, transformOrigin: 'top center' }
                : undefined
            }
          >
            {(paginatedMode
              ? [currentPage]
              : Array.from({ length: pdfDoc.numPages }, (_, idx) => idx + 1)
            ).map((pageNum) => (
              <PdfPageCanvas
                key={`${source.id}-${pageNum}`}
                pdfDoc={pdfDoc}
                pageNumber={pageNum}
                scale={renderScale}
                areaMode={areaMode}
                paginatedMode={paginatedMode}
                einkMode={einkMode}
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
            ))}
          </div>
        ) : null}
      </div>

      {/* Paginated / E-Ink Page Turn Bar */}
      {paginatedMode && pdfDoc && (
        <div className="h-12 shrink-0 border-t border-[#E5E0D5] bg-white px-3 flex items-center justify-between gap-3 eink-surface">
          <button
            type="button"
            disabled={currentPage <= 1}
            onClick={() => goToPage(currentPage - 1)}
            className="flex-1 h-9 rounded-lg border border-[#E5E0D5] bg-[#FAF8F4] text-xs font-semibold inline-flex items-center justify-center gap-1 disabled:opacity-35 cursor-pointer eink-surface"
          >
            <ChevronLeft className="w-4 h-4" />
            <span>上一页</span>
          </button>
          <span className="text-xs font-semibold tabular-nums px-2">
            {currentPage} / {numPages}
          </span>
          <button
            type="button"
            disabled={currentPage >= numPages}
            onClick={() => goToPage(currentPage + 1)}
            className="flex-1 h-9 rounded-lg border border-[#E5E0D5] bg-[#1C1B18] text-white text-xs font-semibold inline-flex items-center justify-center gap-1 disabled:opacity-35 cursor-pointer"
          >
            <span>下一页</span>
            <ChevronRight className="w-4 h-4" />
          </button>
        </div>
      )}
    </div>
  );
}
