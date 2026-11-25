import { useEffect, useMemo, useRef, useState } from 'react';
import { ExternalLink, FileText, ZoomIn, ZoomOut } from 'lucide-react';
import { usePinchZoom } from '../hooks/usePinchZoom';
import type { Highlight, PendingSelection, SourceDetail } from '../types';

interface DocumentViewerProps {
  source: SourceDetail;
  highlights: Highlight[];
  activeHighlightId: string | null;
  pulseHighlightId: string | null;
  expandedReading?: boolean;
  fullWidthReading?: boolean;
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
  onSelectHighlight,
  onTextSelected,
}: DocumentViewerProps) {
  const articleRef = useRef<HTMLDivElement | null>(null);
  const scrollContainerRef = useRef<HTMLDivElement | null>(null);
  const [docZoom, setDocZoom] = useState(1);

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

    // Collect all text nodes and their cumulative offsets
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

  // Scroll to active/pulsed highlight
  useEffect(() => {
    const targetId = pulseHighlightId || activeHighlightId;
    if (!targetId || !articleRef.current) return;
    const mark = articleRef.current.querySelector(`mark[data-highlight-id="${targetId}"]`);
    if (mark) {
      mark.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  }, [pulseHighlightId, activeHighlightId]);

  const handleMouseUp = (e: React.MouseEvent<HTMLDivElement>) => {
    const target = e.target as HTMLElement;
    const clickedMark = target.closest('mark[data-highlight-id]') as HTMLElement | null;
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed) {
      if (clickedMark?.dataset.highlightId) {
        onSelectHighlight(clickedMark.dataset.highlightId);
      }
      return;
    }

    const quoteText = sel.toString().trim();
    if (!quoteText || !articleRef.current) return;
    const range = sel.getRangeAt(0);
    if (!articleRef.current.contains(range.commonAncestorContainer)) return;

    // Compute plain-text character offset inside articleRef
    const preRange = document.createRange();
    preRange.selectNodeContents(articleRef.current);
    preRange.setEnd(range.startContainer, range.startOffset);
    const startOffset = preRange.toString().length;
    const endOffset = startOffset + range.toString().length;
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
  };

  const containerWidthClass = fullWidthReading
    ? 'max-w-5xl'
    : expandedReading
      ? 'max-w-4xl'
      : 'max-w-3xl';

  return (
    <div ref={scrollContainerRef} className="flex-1 overflow-y-auto bg-[#F8F6F1] px-6 py-6">
      <div className={`${containerWidthClass} mx-auto bg-white border border-[#E5E0D5] rounded-2xl shadow-xs px-8 py-7 transition-all duration-200`}>
        <div className="pb-4 mb-6 border-b border-[#EFECE5] flex items-start justify-between gap-4">
          <div>
            <div className="inline-flex items-center gap-1.5 text-xs text-[#5E5B53] mb-1">
              <FileText className="w-3.5 h-3.5 text-[#B45309]" />
              <span>{source.type === 'docx' ? 'Word 文档' : source.type === 'url' ? '网页精读' : '文本文档'}</span>
              <span>·</span>
              <span>{source.word_count} 字</span>
            </div>
            <h1 className="text-xl font-bold text-[#1C1B18]">{source.title}</h1>
          </div>

          <div className="flex items-center gap-2 shrink-0 mt-0.5">
            <div className="inline-flex items-center gap-0.5 rounded-lg border border-[#E5E0D5] bg-[#FAF8F4] px-1 py-0.5 text-[11px]">
              <button
                type="button"
                onClick={() => setDocZoom((z) => Math.max(0.7, Number((z - 0.1).toFixed(2))))}
                className="p-1 rounded hover:bg-[#EFECE5] text-[#5E5B53] cursor-pointer"
                title="缩小字号"
              >
                <ZoomOut className="w-3 h-3" />
              </button>
              <button
                type="button"
                onClick={() => setDocZoom(1)}
                className="px-1.5 py-0.5 font-medium text-[#1C1B18] hover:text-[#B45309] cursor-pointer"
                title="支持触控板双指缩放，点击重置为 100%"
              >
                {Math.round(docZoom * 100)}%
              </button>
              <button
                type="button"
                onClick={() => setDocZoom((z) => Math.min(2.2, Number((z + 0.1).toFixed(2))))}
                className="p-1 rounded hover:bg-[#EFECE5] text-[#5E5B53] cursor-pointer"
                title="放大字号"
              >
                <ZoomIn className="w-3 h-3" />
              </button>
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
          onMouseUp={handleMouseUp}
          style={{ fontSize: `${Math.round(16 * docZoom * 10) / 10}px` }}
          className="study-prose select-text"
        />
      </div>
    </div>
  );
}
