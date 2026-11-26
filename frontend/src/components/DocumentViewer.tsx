import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ChevronLeft, ChevronRight, Clock, Download, ExternalLink, FileText, ZoomIn, ZoomOut } from 'lucide-react';
import { usePinchZoom } from '../hooks/usePinchZoom';
import type { Highlight, PendingSelection, SourceDetail } from '../types';
import { formatShortDuration } from '../utils/formatDuration';

interface DocumentViewerProps {
  source: SourceDetail;
  highlights: Highlight[];
  activeHighlightId: string | null;
  pulseHighlightId: string | null;
  expandedReading?: boolean;
  fullWidthReading?: boolean;
  einkMode?: boolean;
  suppressAutoRestore?: boolean;
  liveSessionSeconds?: number;
  onSaveReadingPosition?: (sourceId: string, page: number, scrollRatio: number) => void;
  onSelectHighlight: (highlightId: string) => void;
  onTextSelected: (sel: PendingSelection) => void;
}

export function DocumentViewer({
  source,
  highlights,
  activeHighlightId,
  pulseHighlightId,
  expandedReading = false,
  fullWidthReading = false,
  einkMode = false,
  suppressAutoRestore = false,
  liveSessionSeconds = 0,
  onSaveReadingPosition,
  onSelectHighlight,
  onTextSelected,
}: DocumentViewerProps) {
  const articleRef = useRef<HTMLDivElement | null>(null);
  const scrollContainerRef = useRef<HTMLDivElement | null>(null);
  const [docZoom, setDocZoom] = useState(1);
  const [restoredNotice, setRestoredNotice] = useState<string | null>(null);
  const lastSelKeyRef = useRef('');
  const restoredSourceRef = useRef<string | null>(null);
  const saveTimerRef = useRef<number | null>(null);
  const onSavePosRef = useRef(onSaveReadingPosition);
  onSavePosRef.current = onSaveReadingPosition;

  usePinchZoom({
    containerRef: scrollContainerRef,
    minScale: 0.7,
    maxScale: 2.2,
    currentScale: docZoom,
    onScaleChange: (next) => {
      setDocZoom(next);
    },
    onScaleCommit: (finalScale) => {
      setDocZoom(Number(finalScale.toFixed(2)));
    },
  });

  // Build sanitized DOM + inject <mark> anchors whenever content or highlights change
  const renderedHtml = useMemo(() => source.html_content || '', [source.html_content]);

  useEffect(() => {
    const root = articleRef.current;
    if (!root) return;
    root.innerHTML = renderedHtml;

    if (highlights.length === 0) return;

    const collectNodes = () => {
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      const segments: { node: Text; start: number; end: number }[] = [];
      let cursor = 0;
      let current = walker.nextNode() as Text | null;
      while (current) {
        const len = current.nodeValue?.length || 0;
        if (len > 0) {
          segments.push({ node: current, start: cursor, end: cursor + len });
          cursor += len;
        }
        current = walker.nextNode() as Text | null;
      }
      return { segments, fullText: segments.map((s) => s.node.nodeValue || '').join('') };
    };

    const sorted = [...highlights].sort((a, b) => (b.start_offset ?? 0) - (a.start_offset ?? 0));

    for (const hl of sorted) {
      const { segments, fullText } = collectNodes();
      let start = hl.start_offset ?? -1;
      let end = hl.end_offset ?? -1;

      const sliceMatches =
        start >= 0 &&
        end > start &&
        end <= fullText.length &&
        fullText.slice(start, end).trim() === hl.quote_text.trim();

      if (!sliceMatches && hl.quote_text.trim()) {
        const idx = fullText.indexOf(hl.quote_text.trim());
        if (idx !== -1) {
          start = idx;
          end = idx + hl.quote_text.trim().length;
        } else {
          continue;
        }
      }
      if (start < 0 || end <= start) continue;

      for (let i = segments.length - 1; i >= 0; i--) {
        const seg = segments[i];
        if (seg.end <= start || seg.start >= end) continue;
        const localStart = Math.max(0, start - seg.start);
        const localEnd = Math.min(seg.node.nodeValue?.length || 0, end - seg.start);
        if (localEnd <= localStart) continue;

        const range = document.createRange();
        range.setStart(seg.node, localStart);
        range.setEnd(seg.node, localEnd);

        const mark = document.createElement('mark');
        mark.dataset.highlightId = hl.id;
        mark.className = `hl-mark hl-${hl.color} ${
          hl.id === activeHighlightId ? 'hl-mark-active' : ''
        } ${hl.id === pulseHighlightId ? 'hl-pulse' : ''}`;
        mark.title = hl.note_text || '点击查看划线笔记与 AI 对话';
        try {
          range.surroundContents(mark);
        } catch {
          // Skip split across complex inline boundaries
        }
      }
    }
  }, [renderedHtml, highlights, activeHighlightId, pulseHighlightId]);

  useEffect(() => {
    const targetId = pulseHighlightId || activeHighlightId;
    if (!targetId || !articleRef.current) return;
    const mark = articleRef.current.querySelector(`mark[data-highlight-id="${targetId}"]`);
    if (mark) {
      mark.scrollIntoView({ behavior: einkMode ? 'auto' : 'smooth', block: 'center' });
    }
  }, [pulseHighlightId, activeHighlightId, einkMode]);

  // Auto-restore last scroll position when opening a document
  useEffect(() => {
    restoredSourceRef.current = null;
    setRestoredNotice(null);
    if (suppressAutoRestore || activeHighlightId || pulseHighlightId) {
      restoredSourceRef.current = source.id;
      return;
    }

    let savedRatio = source.last_scroll_ratio || 0;
    try {
      const raw = localStorage.getItem(`inkmind.readPos.${source.id}`);
      if (raw) {
        const parsed = JSON.parse(raw) as { scrollRatio?: number };
        if (typeof parsed.scrollRatio === 'number') {
          savedRatio = parsed.scrollRatio;
        }
      }
    } catch {
      // ignore storage errors
    }

    const ratio = Math.max(0, Math.min(1, savedRatio));
    if (ratio >= 0.02) {
      setRestoredNotice(`已恢复上次阅读进度 · ${Math.round(ratio * 100)}%`);
      const hideTimer = window.setTimeout(() => setRestoredNotice(null), 2600);
      requestAnimationFrame(() => {
        window.setTimeout(() => {
          const container = scrollContainerRef.current;
          if (container) {
            const maxScroll = Math.max(0, container.scrollHeight - container.clientHeight);
            container.scrollTo({ top: ratio * maxScroll, behavior: 'instant' });
          }
          restoredSourceRef.current = source.id;
        }, 60);
      });
      return () => window.clearTimeout(hideTimer);
    }

    if (scrollContainerRef.current) {
      scrollContainerRef.current.scrollTo({ top: 0, behavior: 'instant' });
    }
    restoredSourceRef.current = source.id;
  }, [source.id, source.last_scroll_ratio, renderedHtml, suppressAutoRestore, activeHighlightId, pulseHighlightId]);

  const handleDocumentScroll = () => {
    if (restoredSourceRef.current !== source.id) return;
    if (saveTimerRef.current !== null) window.clearTimeout(saveTimerRef.current);
    saveTimerRef.current = window.setTimeout(() => {
      const container = scrollContainerRef.current;
      if (!container) return;
      const maxScroll = Math.max(1, container.scrollHeight - container.clientHeight);
      const ratio = Math.max(0, Math.min(1, container.scrollTop / maxScroll));
      onSavePosRef.current?.(source.id, 1, ratio);
    }, 500);
  };

  useEffect(() => {
    return () => {
      if (saveTimerRef.current !== null) window.clearTimeout(saveTimerRef.current);
    };
  }, []);

  const emitSelectionFromDom = useCallback(() => {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !articleRef.current) {
      lastSelKeyRef.current = '';
      return;
    }
    const quoteText = sel.toString().trim();
    if (!quoteText) return;

    const range = sel.getRangeAt(0);
    if (!articleRef.current.contains(range.commonAncestorContainer)) return;

    const preRange = document.createRange();
    preRange.selectNodeContents(articleRef.current);
    preRange.setEnd(range.startContainer, range.startOffset);
    const startOffset = preRange.toString().length;
    const endOffset = startOffset + range.toString().length;

    const key = `${source.id}:${startOffset}:${endOffset}:${quoteText}`;
    if (key === lastSelKeyRef.current) return;
    lastSelKeyRef.current = key;

    const rect = range.getBoundingClientRect();
    onTextSelected({
      quoteText,
      anchorType: 'text',
      pageNumber: 1,
      rects: [],
      startOffset,
      endOffset,
      clientX: rect.left + rect.width / 2,
      clientY: rect.top,
    });
  }, [onTextSelected, source.id]);

  // Debounced selectionchange handles touchscreen drag handles on iOS/Android/E-Ink
  useEffect(() => {
    let timer: number | null = null;
    const onSelChange = () => {
      if (timer !== null) window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        emitSelectionFromDom();
      }, 240);
    };
    document.addEventListener('selectionchange', onSelChange);
    return () => {
      document.removeEventListener('selectionchange', onSelChange);
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [emitSelectionFromDom]);

  const handleMouseOrTouchUp = (e: React.SyntheticEvent<HTMLDivElement>) => {
    const target = e.target as HTMLElement;
    const clickedMark = target.closest('mark[data-highlight-id]') as HTMLElement | null;
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed) {
      if (clickedMark?.dataset.highlightId) {
        onSelectHighlight(clickedMark.dataset.highlightId);
      }
      return;
    }
    emitSelectionFromDom();
  };

  const turnScreen = useCallback(
    (direction: 1 | -1) => {
      const container = scrollContainerRef.current;
      if (!container) return;
      const step = Math.max(200, Math.floor(container.clientHeight * 0.86));
      container.scrollBy({
        top: direction * step,
        behavior: einkMode ? 'instant' : 'smooth',
      });
    },
    [einkMode],
  );

  useEffect(() => {
    if (!einkMode) return;
    const onKeyDown = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      if (target?.tagName === 'INPUT' || target?.tagName === 'TEXTAREA' || target?.isContentEditable) return;
      if (e.key === 'PageDown' || e.key === 'ArrowRight') {
        e.preventDefault();
        turnScreen(1);
      } else if (e.key === 'PageUp' || e.key === 'ArrowLeft') {
        e.preventDefault();
        turnScreen(-1);
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [einkMode, turnScreen]);

  const cardPaddingClass = fullWidthReading
    ? 'px-4 sm:px-10'
    : expandedReading
      ? 'px-4 sm:px-9'
      : 'px-4 sm:px-7';

  return (
    <div className="flex-1 flex flex-col min-h-0 bg-[#F8F6F1]">
      {restoredNotice && (
        <div className="shrink-0 bg-[#1C1B18] text-white px-3 py-1 text-[11px] flex items-center justify-center">
          <span>{restoredNotice}</span>
        </div>
      )}
      <div
        ref={scrollContainerRef}
        onScroll={handleDocumentScroll}
        className="flex-1 overflow-y-auto px-2.5 sm:px-4 py-3 sm:py-5"
      >
        <div
          className={`w-full mx-auto bg-white border border-[#E5E0D5] rounded-2xl shadow-xs ${cardPaddingClass} py-5 sm:py-7 eink-surface`}
        >
          <div className="pb-4 mb-5 border-b border-[#EFECE5] flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="inline-flex items-center gap-1.5 text-xs text-[#5E5B53] mb-1">
                <FileText className="w-3.5 h-3.5 text-[#B45309]" />
                <span>{source.type === 'docx' ? 'Word 文档' : source.type === 'url' ? '网页精读' : '文本文档'}</span>
                <span>·</span>
                <span>{source.word_count} 字</span>
                <span>·</span>
                <span className="inline-flex items-center gap-1 tabular-nums text-[#B45309] font-medium">
                  <Clock className="w-3 h-3" />
                  {Math.round((source.reading_progress || 0) * 100)}% ·{' '}
                  {formatShortDuration((source.reading_seconds || 0) + liveSessionSeconds)}
                </span>
              </div>
              <h1 className="text-lg sm:text-xl font-bold text-[#1C1B18] break-words">{source.title}</h1>
            </div>

            <div className="flex items-center gap-2 shrink-0 mt-0.5">
              <div className="inline-flex items-center gap-0.5 rounded-lg border border-[#E5E0D5] bg-[#FAF8F4] px-1 py-0.5 text-[11px]">
                <button
                  type="button"
                  onClick={() => setDocZoom((z) => Math.max(0.7, Number((z - 0.1).toFixed(2))))}
                  className="p-1.5 sm:p-1 rounded hover:bg-[#EFECE5] text-[#5E5B53] cursor-pointer"
                  title="缩小字号"
                >
                  <ZoomOut className="w-3.5 h-3.5 sm:w-3 sm:h-3" />
                </button>
                <button
                  type="button"
                  onClick={() => setDocZoom(1)}
                  className="px-1.5 py-0.5 font-medium text-[#1C1B18] hover:text-[#B45309] cursor-pointer"
                  title="支持双指捏合缩放，点击重置为 100%"
                >
                  {Math.round(docZoom * 100)}%
                </button>
                <button
                  type="button"
                  onClick={() => setDocZoom((z) => Math.min(2.2, Number((z + 0.1).toFixed(2))))}
                  className="p-1.5 sm:p-1 rounded hover:bg-[#EFECE5] text-[#5E5B53] cursor-pointer"
                  title="放大字号"
                >
                  <ZoomIn className="w-3.5 h-3.5 sm:w-3 sm:h-3" />
                </button>
              </div>

              <div className="inline-flex items-center gap-1 rounded-lg border border-[#E5E0D5] bg-[#FAF8F4] px-1.5 py-0.5 text-[11px]">
                <a
                  href={`/api/sources/${source.id}/export?format=html`}
                  download
                  className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded hover:bg-[#EFECE5] text-[#1C1B18]"
                  title="导出包含内嵌配图与原版排版的独立 HTML 文件"
                >
                  <Download className="w-3 h-3 text-[#B45309]" />
                  <span>HTML</span>
                </a>
                <span className="text-[#DCD5C7]">|</span>
                <a
                  href={`/api/sources/${source.id}/export?format=md`}
                  download
                  className="px-1.5 py-0.5 rounded hover:bg-[#EFECE5] text-[#1C1B18]"
                  title="导出 Markdown 文本版"
                >
                  MD
                </a>
              </div>

              {source.source_url && (
                <a
                  href={source.source_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 text-xs text-[#B45309] hover:underline"
                >
                  <span>原文链接</span>
                  <ExternalLink className="w-3.5 h-3.5" />
                </a>
              )}
            </div>
          </div>

          <div
            ref={articleRef}
            onMouseUp={handleMouseOrTouchUp}
            onTouchEnd={handleMouseOrTouchUp}
            style={{ fontSize: `${Math.round(16 * docZoom * 10) / 10}px` }}
            className="study-prose select-text"
          />
        </div>
      </div>

      {einkMode && (
        <div className="h-12 shrink-0 border-t-2 border-black bg-white px-4 flex items-center justify-between gap-4 eink-surface">
          <button
            type="button"
            onClick={() => turnScreen(-1)}
            className="flex-1 h-9 rounded-lg border-2 border-black bg-white text-black text-xs font-semibold inline-flex items-center justify-center gap-1 cursor-pointer"
          >
            <ChevronLeft className="w-4 h-4" />
            <span>上一屏</span>
          </button>
          <button
            type="button"
            onClick={() => turnScreen(1)}
            className="flex-1 h-9 rounded-lg border-2 border-black bg-black text-white text-xs font-semibold inline-flex items-center justify-center gap-1 cursor-pointer"
          >
            <span>下一屏</span>
            <ChevronRight className="w-4 h-4" />
          </button>
        </div>
      )}
    </div>
  );
}
