import { Highlighter, Languages, MessageSquarePlus, Sparkles, X } from 'lucide-react';
import type { HighlightColor, PendingSelection } from '../types';

export const HIGHLIGHT_COLORS: { id: HighlightColor; label: string; dot: string }[] = [
  { id: 'amber', label: '琥珀黄', dot: 'bg-amber-400' },
  { id: 'emerald', label: '青翠绿', dot: 'bg-emerald-400' },
  { id: 'sky', label: '晴空蓝', dot: 'bg-sky-400' },
  { id: 'rose', label: '珊瑚红', dot: 'bg-rose-400' },
  { id: 'violet', label: '丁香紫', dot: 'bg-violet-400' },
];

interface SelectionPopoverProps {
  selection: PendingSelection;
  selectedColor: HighlightColor;
  onChangeColor: (c: HighlightColor) => void;
  onAction: (mode: 'highlight' | 'note' | 'ask' | 'translate') => void;
  onClose: () => void;
}

export function SelectionPopover({
  selection,
  selectedColor,
  onChangeColor,
  onAction,
  onClose,
}: SelectionPopoverProps) {
  const left = Math.max(16, Math.min(window.innerWidth - 430, selection.clientX - 195));
  const top = Math.max(64, Math.min(window.innerHeight - 110, selection.clientY - 62));

  return (
    <div
      style={{ left, top }}
      className="fixed z-50 bg-[#1C1B18] text-white rounded-xl shadow-2xl border border-white/10 px-2.5 py-2 flex items-center gap-2 animate-in fade-in duration-100"
      onMouseDown={(e) => e.stopPropagation()}
    >
      <div className="flex items-center gap-1 pr-2 border-r border-white/15">
        {HIGHLIGHT_COLORS.map((c) => (
          <button
            key={c.id}
            type="button"
            title={c.label}
            onClick={() => onChangeColor(c.id)}
            className={`w-4 h-4 rounded-full ${c.dot} cursor-pointer transition ${
              selectedColor === c.id ? 'ring-2 ring-white scale-110' : 'opacity-75 hover:opacity-100'
            }`}
          />
        ))}
      </div>

      <button
        type="button"
        onClick={() => onAction('highlight')}
        className="inline-flex items-center gap-1 px-2 py-1 rounded-lg hover:bg-white/15 text-xs font-medium cursor-pointer"
      >
        <Highlighter className="w-3.5 h-3.5 text-amber-300" />
        <span>高亮</span>
      </button>

      <button
        type="button"
        onClick={() => onAction('note')}
        className="inline-flex items-center gap-1 px-2 py-1 rounded-lg hover:bg-white/15 text-xs font-medium cursor-pointer"
      >
        <MessageSquarePlus className="w-3.5 h-3.5 text-emerald-300" />
        <span>记笔记</span>
      </button>

      <button
        type="button"
        onClick={() => onAction('translate')}
        className="inline-flex items-center gap-1 px-2 py-1 rounded-lg hover:bg-white/15 text-xs font-medium cursor-pointer"
        title="高亮并立即生成中文翻译、词汇与句式解析"
      >
        <Languages className="w-3.5 h-3.5 text-sky-300" />
        <span>翻译解释</span>
      </button>

      <button
        type="button"
        onClick={() => onAction('ask')}
        className="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg bg-[#B45309] hover:bg-[#D97706] text-xs font-medium cursor-pointer"
      >
        <Sparkles className="w-3.5 h-3.5" />
        <span>问 AI</span>
      </button>

      <button
        type="button"
        onClick={onClose}
        className="p-1 rounded-md text-white/60 hover:text-white hover:bg-white/10 cursor-pointer"
        aria-label="关闭"
      >
        <X className="w-3.5 h-3.5" />
      </button>
    </div>
  );
}
