import { Highlighter, Languages, MessageSquarePlus, PlayCircle, ScanText, Sparkles, Volume2, X } from 'lucide-react';
import type { HighlightColor, PendingSelection } from '../types';

export const HIGHLIGHT_COLORS: { id: HighlightColor; label: string; dot: string }[] = [
  { id: 'amber', label: '琥珀黄', dot: 'bg-amber-400' },
  { id: 'emerald', label: '青翠绿', dot: 'bg-emerald-400' },
  { id: 'sky', label: '晴空蓝', dot: 'bg-sky-400' },
  { id: 'rose', label: '珊瑚红', dot: 'bg-rose-400' },
  { id: 'violet', label: '丁香紫', dot: 'bg-violet-400' },
];

export type SelectionPopoverAction =
  | 'highlight'
  | 'note'
  | 'ask'
  | 'translate'
  | 'ocr'
  | 'pronounce'
  | 'read_from_here';

interface SelectionPopoverProps {
  selection: PendingSelection;
  selectedColor: HighlightColor;
  hasSpeechScript?: boolean;
  onChangeColor: (c: HighlightColor) => void;
  onAction: (mode: SelectionPopoverAction) => void;
  onClose: () => void;
}

export function SelectionPopover({
  selection,
  selectedColor,
  hasSpeechScript = false,
  onChangeColor,
  onAction,
  onClose,
}: SelectionPopoverProps) {
  const isCompact = typeof window !== 'undefined' && window.innerWidth < 768;
  const hasText = selection.quoteText.trim().length > 0;
  const desktopWidthEstimate = hasText ? 590 : 450;
  const left = Math.max(
    16,
    Math.min(window.innerWidth - desktopWidthEstimate, selection.clientX - Math.round(desktopWidthEstimate / 2)),
  );
  const top = Math.max(64, Math.min(window.innerHeight - 130, selection.clientY - 64));

  return (
    <div
      style={isCompact ? undefined : { left, top }}
      className={
        isCompact
          ? 'fixed bottom-16 left-2 right-2 mx-auto max-w-md z-50 bg-[#1C1B18] text-white rounded-2xl shadow-2xl border border-white/15 px-3 py-2.5 flex flex-wrap items-center justify-between gap-2'
          : 'fixed z-50 max-w-[calc(100vw-32px)] bg-[#1C1B18] text-white rounded-xl shadow-2xl border border-white/10 px-2.5 py-2 flex flex-wrap items-center gap-1.5 animate-in fade-in duration-100'
      }
      onMouseDown={(e) => e.stopPropagation()}
      onTouchStart={(e) => e.stopPropagation()}
    >
      <div className="flex items-center gap-1.5 pr-2 border-r border-white/15">
        {HIGHLIGHT_COLORS.map((c) => (
          <button
            key={c.id}
            type="button"
            title={c.label}
            onClick={() => onChangeColor(c.id)}
            className={`w-5 h-5 sm:w-4 sm:h-4 rounded-full ${c.dot} cursor-pointer transition ${
              selectedColor === c.id ? 'ring-2 ring-white scale-110' : 'opacity-75 hover:opacity-100'
            }`}
          />
        ))}
      </div>

      <button
        type="button"
        onClick={() => onAction('highlight')}
        className="inline-flex items-center gap-1 px-2.5 py-1.5 sm:py-1 rounded-lg hover:bg-white/15 text-xs font-medium cursor-pointer"
      >
        <Highlighter className="w-3.5 h-3.5 text-amber-300" />
        <span>高亮</span>
      </button>

      <button
        type="button"
        onClick={() => onAction('note')}
        className="inline-flex items-center gap-1 px-2.5 py-1.5 sm:py-1 rounded-lg hover:bg-white/15 text-xs font-medium cursor-pointer"
      >
        <MessageSquarePlus className="w-3.5 h-3.5 text-emerald-300" />
        <span>记笔记</span>
      </button>

      {selection.anchorType === 'area' && (
        <button
          type="button"
          onClick={() => onAction('ocr')}
          className="inline-flex items-center gap-1 px-2.5 py-1.5 sm:py-1 rounded-lg hover:bg-white/15 text-xs font-medium cursor-pointer"
          title="将框选截图发送给 AI 提取文本并保存备注"
        >
          <ScanText className="w-3.5 h-3.5 text-cyan-300" />
          <span>OCR 提取</span>
        </button>
      )}

      <button
        type="button"
        onClick={() => onAction('translate')}
        className="inline-flex items-center gap-1 px-2.5 py-1.5 sm:py-1 rounded-lg hover:bg-white/15 text-xs font-medium cursor-pointer"
        title="高亮并立即生成中文翻译、词汇与句式解析"
      >
        <Languages className="w-3.5 h-3.5 text-sky-300" />
        <span>翻译解释</span>
      </button>

      {hasText && (
        <>
          <button
            type="button"
            onClick={() => onAction('pronounce')}
            className="inline-flex items-center gap-1 px-2.5 py-1.5 sm:py-1 rounded-lg hover:bg-white/15 text-xs font-medium cursor-pointer"
            title="使用 agy 生成自然发音文本并朗读选中内容"
          >
            <Volume2 className="w-3.5 h-3.5 text-teal-300" />
            <span>发音</span>
          </button>

          <button
            type="button"
            onClick={() => onAction('read_from_here')}
            className="inline-flex items-center gap-1 px-2.5 py-1.5 sm:py-1 rounded-lg hover:bg-white/15 text-xs font-medium cursor-pointer"
            title={hasSpeechScript ? '快速跳转到全文朗读对应位置并播放' : '生成全文自然朗读文本并从此处开始播放'}
          >
            <PlayCircle className="w-3.5 h-3.5 text-orange-300" />
            <span>从此处朗读</span>
          </button>
        </>
      )}

      <button
        type="button"
        onClick={() => onAction('ask')}
        className="inline-flex items-center gap-1 px-3 py-1.5 sm:py-1 rounded-lg bg-[#B45309] hover:bg-[#D97706] text-xs font-medium cursor-pointer"
      >
        <Sparkles className="w-3.5 h-3.5" />
        <span>问 AI</span>
      </button>

      <button
        type="button"
        onClick={onClose}
        className="p-1.5 sm:p-1 rounded-md text-white/60 hover:text-white hover:bg-white/10 cursor-pointer"
        aria-label="关闭"
      >
        <X className="w-3.5 h-3.5" />
      </button>
    </div>
  );
}
