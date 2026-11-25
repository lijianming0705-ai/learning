import { useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  ChevronDown,
  ChevronRight,
  Database,
  Highlighter,
  Loader2,
  MapPin,
  Save,
  ScanText,
  Send,
  Sparkles,
  Trash2,
  X,
  ZoomIn,
} from 'lucide-react';
import type { AgyModel, Highlight, HighlightColor, RagMemoryRef } from '../types';
import { HIGHLIGHT_COLORS } from './SelectionPopover';

const TEXT_QUICK_PROMPTS = ['翻译并解释', '通俗解释', '提炼核心要点', '举个实际例子', '结合历史笔记对比'];
const IMAGE_QUICK_PROMPTS = ['OCR 识别并解释', '翻译图片内容', '讲解公式或图表含义', '提炼核心要点'];

interface RemarksPanelProps {
  highlights: Highlight[];
  activeHighlightId: string | null;
  focusIntent: { highlightId: string; mode: 'note' | 'ask' } | null;
  streamingMap: Record<string, { text: string; status: string; memories: RagMemoryRef[] }>;
  ocrLoadingMap: Record<string, boolean>;
  quotedContext: string | null;
  models: AgyModel[];
  selectedModel: string;
  onSelectModel: (modelId: string) => void;
  onClearQuotedContext: () => void;
  onSelectHighlight: (hl: Highlight) => void;
  onUpdateHighlight: (id: string, patch: { color?: HighlightColor; note_text?: string }) => Promise<void>;
  onDeleteHighlight: (id: string) => Promise<void>;
  onAskHighlight: (highlightId: string, question: string, useRag: boolean, crossNotebook: boolean) => Promise<void>;
  onOcrHighlight: (highlightId: string) => Promise<void>;
  onJumpToMemory: (mem: RagMemoryRef) => void;
}

export function RemarksPanel({
  highlights,
  activeHighlightId,
  focusIntent,
  streamingMap,
  ocrLoadingMap,
  quotedContext,
  models,
  selectedModel,
  onSelectModel,
  onClearQuotedContext,
  onSelectHighlight,
  onUpdateHighlight,
  onDeleteHighlight,
  onAskHighlight,
  onOcrHighlight,
  onJumpToMemory,
}: RemarksPanelProps) {
  const [filterColor, setFilterColor] = useState<string>('all');
  const [searchText, setSearchText] = useState('');
  const [noteDrafts, setNoteDrafts] = useState<Record<string, string>>({});
  const [questionDrafts, setQuestionDrafts] = useState<Record<string, string>>({});
  const [useRag, setUseRag] = useState(true);
  const [crossNotebook, setCrossNotebook] = useState(true);
  const [expandedRefs, setExpandedRefs] = useState<Record<string, boolean>>({});
  const [lightboxImageUrl, setLightboxImageUrl] = useState<string | null>(null);

  const cardRefs = useRef<Record<string, HTMLDivElement | null>>({});
  const noteInputRefs = useRef<Record<string, HTMLTextAreaElement | null>>({});
  const askInputRefs = useRef<Record<string, HTMLInputElement | null>>({});

  useEffect(() => {
    const next: Record<string, string> = {};
    for (const h of highlights) {
      next[h.id] = h.note_text;
    }
    setNoteDrafts(next);
  }, [highlights]);

  useEffect(() => {
    if (!activeHighlightId) return;
    const el = cardRefs.current[activeHighlightId];
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }, [activeHighlightId]);

  useEffect(() => {
    if (!focusIntent) return;
    const { highlightId, mode } = focusIntent;
    const timer = setTimeout(() => {
      if (mode === 'note') {
        noteInputRefs.current[highlightId]?.focus();
      } else {
        askInputRefs.current[highlightId]?.focus();
      }
    }, 80);
    return () => clearTimeout(timer);
  }, [focusIntent]);

  const filtered = highlights.filter((h) => {
    if (filterColor !== 'all' && h.color !== filterColor) return false;
    if (searchText.trim()) {
      const q = searchText.trim().toLowerCase();
      const hay = [
        h.quote_text,
        h.note_text,
        ...h.messages.map((m) => m.content),
      ]
        .join('\n')
        .toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  const colorBarClass = (c: HighlightColor) => {
    switch (c) {
      case 'amber':
        return 'bg-amber-400';
      case 'emerald':
        return 'bg-emerald-400';
      case 'sky':
        return 'bg-sky-400';
      case 'rose':
        return 'bg-rose-400';
      case 'violet':
        return 'bg-violet-400';
    }
  };

  return (
    <div className="flex flex-col h-full">
      {/* Filter Bar */}
      <div className="p-3 border-b border-[#E5E0D5] space-y-2 bg-[#FAF8F4]">
        <div className="flex items-center gap-2">
          <input
            type="text"
            value={searchText}
            onChange={(e) => setSearchText(e.target.value)}
            placeholder="筛选划线、笔记或历史问答..."
            className="flex-1 rounded-lg border border-[#E5E0D5] bg-white px-2.5 py-1.5 text-xs focus:outline-none focus:border-[#B45309]"
          />
          <div className="flex items-center gap-1">
            <button
              onClick={() => setFilterColor('all')}
              className={`px-2 py-1 rounded text-[11px] cursor-pointer ${
                filterColor === 'all' ? 'bg-[#1C1B18] text-white' : 'bg-white border border-[#E5E0D5] text-[#5E5B53]'
              }`}
            >
              全部
            </button>
            {HIGHLIGHT_COLORS.map((c) => (
              <button
                key={c.id}
                onClick={() => setFilterColor(filterColor === c.id ? 'all' : c.id)}
                title={c.label}
                className={`w-4 h-4 rounded-full ${c.dot} cursor-pointer ${
                  filterColor === c.id ? 'ring-2 ring-offset-1 ring-[#1C1B18]' : 'opacity-65 hover:opacity-100'
                }`}
              />
            ))}
          </div>
        </div>

        <div className="flex items-center justify-between gap-2 text-[11px] text-[#5E5B53]">
          <div className="flex items-center gap-3">
            <label className="inline-flex items-center gap-1 cursor-pointer">
              <input
                type="checkbox"
                checked={useRag}
                onChange={(e) => setUseRag(e.target.checked)}
                className="accent-[#B45309]"
              />
              <span>启用 RAG 记忆</span>
            </label>
            {useRag && (
              <label className="inline-flex items-center gap-1 cursor-pointer">
                <input
                  type="checkbox"
                  checked={crossNotebook}
                  onChange={(e) => setCrossNotebook(e.target.checked)}
                  className="accent-[#B45309]"
                />
                <span>跨笔记本</span>
              </label>
            )}
          </div>
          <span>共 {filtered.length} 条备注</span>
        </div>

        <div className="flex items-center justify-between gap-2 text-[11px]">
          <span className="inline-flex items-center gap-1 text-[#5E5B53] shrink-0">
            <Sparkles className="w-3 h-3 text-[#B45309]" />
            <span>提问模型</span>
          </span>
          <select
            value={selectedModel}
            onChange={(e) => onSelectModel(e.target.value)}
            aria-label="划线提问 agy 模型"
            className="flex-1 min-w-0 rounded-lg border border-[#E5E0D5] bg-white px-2 py-1 text-[11px] font-medium text-[#1C1B18] focus:outline-none focus:border-[#B45309] cursor-pointer"
          >
            {models.length === 0 ? (
              <option value={selectedModel}>{selectedModel}</option>
            ) : (
              models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.id === 'gemini-3.8-flash-low' ? `⚡ ${m.name} · 最快` : m.name}
                </option>
              ))
            )}
          </select>
        </div>

        {quotedContext && (
          <div className="rounded-lg bg-amber-50 border border-amber-200 px-2.5 py-1.5 text-[11px] text-amber-900 flex items-center justify-between gap-2">
            <span className="truncate">已引用记忆：{quotedContext}</span>
            <button onClick={onClearQuotedContext} className="text-amber-700 hover:underline shrink-0 cursor-pointer">
              清除
            </button>
          </div>
        )}
      </div>

      {/* Remark Cards */}
      <div className="flex-1 overflow-y-auto p-3 space-y-3">
        {filtered.length === 0 ? (
          <div className="text-center py-12 px-4 text-xs text-[#5E5B53]">
            <Highlighter className="w-8 h-8 mx-auto mb-2 text-[#C4BCAE]" />
            <p className="font-medium text-[#1C1B18]">还没有高亮备注</p>
            <p className="mt-1">在左侧文档或 PDF 中划选任意文字（或使用框选模式），即可高亮、写笔记或向 AI 提问。</p>
          </div>
        ) : (
          filtered.map((hl) => {
            const isActive = hl.id === activeHighlightId;
            const stream = streamingMap[hl.id];
            const noteVal = noteDrafts[hl.id] ?? hl.note_text;
            const noteDirty = noteVal !== hl.note_text;
            const qDraft = questionDrafts[hl.id] ?? '';

            return (
              <div
                key={hl.id}
                ref={(el) => {
                  cardRefs.current[hl.id] = el;
                }}
                onClick={() => onSelectHighlight(hl)}
                className={`rounded-xl bg-white border transition overflow-hidden ${
                  isActive ? 'border-[#B45309] shadow-sm' : 'border-[#E5E0D5] hover:border-[#C9C1B2]'
                }`}
              >
                <div className="flex">
                  <div className={`w-1.5 shrink-0 ${colorBarClass(hl.color)}`} />
                  <div className="flex-1 p-3 space-y-2.5 min-w-0">
                    {/* Header row */}
                    <div className="flex items-center justify-between gap-2">
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          onSelectHighlight(hl);
                        }}
                        className="inline-flex items-center gap-1 text-[11px] font-medium text-[#B45309] hover:underline cursor-pointer"
                      >
                        <MapPin className="w-3 h-3" />
                        <span>
                          P.{hl.page_number} · {hl.anchor_type === 'area' ? '框选区域' : '文字划线'}
                        </span>
                      </button>

                      <div className="flex items-center gap-1.5">
                        {HIGHLIGHT_COLORS.map((c) => (
                          <button
                            key={c.id}
                            type="button"
                            onClick={(e) => {
                              e.stopPropagation();
                              onUpdateHighlight(hl.id, { color: c.id });
                            }}
                            className={`w-3 h-3 rounded-full ${c.dot} cursor-pointer ${
                              hl.color === c.id ? 'ring-1 ring-offset-1 ring-[#1C1B18]' : 'opacity-45 hover:opacity-100'
                            }`}
                          />
                        ))}
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation();
                            if (window.confirm('删除这条划线及其备注问答？')) {
                              onDeleteHighlight(hl.id);
                            }
                          }}
                          className="p-1 rounded text-[#5E5B53] hover:text-rose-600 cursor-pointer ml-1"
                          title="删除划线"
                        >
                          <Trash2 className="w-3.5 h-3.5" />
                        </button>
                      </div>
                    </div>

                    {/* Cropped Image Preview for Area / Scanned PDF */}
                    {hl.image_url && (
                      <div className="space-y-1.5">
                        <div className="relative group rounded-lg border border-[#E5E0D5] bg-[#FAF8F4] overflow-hidden">
                          <img
                            src={hl.image_url}
                            alt={`P.${hl.page_number} 框选截图`}
                            onClick={(e) => {
                              e.stopPropagation();
                              setLightboxImageUrl(hl.image_url);
                            }}
                            className="w-full max-h-44 object-contain cursor-zoom-in bg-white"
                          />
                          <button
                            type="button"
                            onClick={(e) => {
                              e.stopPropagation();
                              setLightboxImageUrl(hl.image_url);
                            }}
                            title="放大查看框选原图"
                            className="absolute top-1.5 right-1.5 p-1 rounded-md bg-black/60 text-white opacity-100 md:opacity-0 md:group-hover:opacity-100 transition cursor-pointer"
                          >
                            <ZoomIn className="w-3.5 h-3.5" />
                          </button>
                        </div>

                        <div className="flex items-center justify-between gap-2">
                          <span className="text-[11px] text-[#5E5B53]">
                            {hl.quote_text ? '已提取 OCR 文本（已同步 RAG）' : '截图将随提问自动发送给 AI 识别'}
                          </span>
                          <button
                            type="button"
                            disabled={Boolean(ocrLoadingMap[hl.id])}
                            onClick={(e) => {
                              e.stopPropagation();
                              void onOcrHighlight(hl.id);
                            }}
                            className="inline-flex items-center gap-1 px-2 py-1 rounded-md border border-[#E5E0D5] bg-[#FAF8F4] hover:border-[#B45309] text-[11px] font-medium text-[#1C1B18] cursor-pointer disabled:opacity-50"
                          >
                            {ocrLoadingMap[hl.id] ? (
                              <Loader2 className="w-3 h-3 animate-spin text-[#B45309]" />
                            ) : (
                              <ScanText className="w-3 h-3 text-[#B45309]" />
                            )}
                            <span>{ocrLoadingMap[hl.id] ? 'OCR 识别中...' : hl.quote_text ? '重新 OCR' : 'OCR 识别文字'}</span>
                          </button>
                        </div>
                      </div>
                    )}

                    {/* Quoted snippet / OCR Text */}
                    {(hl.quote_text || !hl.image_url) && (
                      <blockquote className="text-xs text-[#2E2C26] bg-[#FAF8F4] border-l-2 border-[#D6CFC0] px-2.5 py-1.5 rounded-r-md whitespace-pre-wrap break-words">
                        {hl.quote_text || '（PDF 图像 / 公式框选区域）'}
                      </blockquote>
                    )}

                    {/* Editable Note */}
                    <div className="space-y-1">
                      <div className="flex items-center justify-between">
                        <span className="text-[11px] font-medium text-[#5E5B53]">学习笔记</span>
                        {noteDirty && (
                          <button
                            type="button"
                            onClick={(e) => {
                              e.stopPropagation();
                              onUpdateHighlight(hl.id, { note_text: noteVal });
                            }}
                            className="inline-flex items-center gap-1 text-[11px] text-[#B45309] font-medium hover:underline cursor-pointer"
                          >
                            <Save className="w-3 h-3" />
                            保存笔记
                          </button>
                        )}
                      </div>
                      <textarea
                        ref={(el) => {
                          noteInputRefs.current[hl.id] = el;
                        }}
                        rows={noteVal ? Math.min(4, Math.max(2, noteVal.split('\n').length)) : 1}
                        value={noteVal}
                        onChange={(e) => setNoteDrafts((prev) => ({ ...prev, [hl.id]: e.target.value }))}
                        onBlur={() => {
                          if (noteDirty) {
                            onUpdateHighlight(hl.id, { note_text: noteVal });
                          }
                        }}
                        placeholder="写下理解、批注或关键词..."
                        className="w-full rounded-lg border border-[#E5E0D5] bg-[#FCFBF8] px-2.5 py-1.5 text-xs focus:outline-none focus:border-[#B45309] resize-y"
                      />
                    </div>

                    {/* Persistent Q&A History Thread */}
                    {(hl.messages.length > 0 || stream) && (
                      <div className="space-y-2 pt-1 border-t border-[#F0ECE3]">
                        <div className="text-[11px] font-medium text-[#5E5B53] flex items-center gap-1">
                          <Sparkles className="w-3 h-3 text-[#B45309]" />
                          <span>历史问答备注 ({Math.ceil(hl.messages.length / 2)} 轮)</span>
                        </div>

                        {hl.messages.map((msg) => (
                          <div
                            key={msg.id}
                            className={`rounded-lg px-2.5 py-2 text-xs ${
                              msg.role === 'user'
                                ? 'bg-[#F3EFE6] text-[#1C1B18] font-medium'
                                : 'bg-[#FAF8F4] border border-[#EAE4D7] text-[#1C1B18]'
                            }`}
                          >
                            <div className="flex items-center justify-between text-[10px] text-[#5E5B53] mb-1">
                              <span>{msg.role === 'user' ? '我的提问' : `agy AI · ${msg.model || ''}`}</span>
                              <span>{new Date(msg.created_at).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })}</span>
                            </div>
                            {msg.role === 'user' ? (
                              <div className="whitespace-pre-wrap">{msg.content}</div>
                            ) : (
                              <>
                                <div className="ai-markdown">
                                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.content}</ReactMarkdown>
                                </div>
                                {msg.retrieved_memories.length > 0 && (
                                  <div className="mt-2 pt-1.5 border-t border-[#E5E0D5]">
                                    <button
                                      type="button"
                                      onClick={(e) => {
                                        e.stopPropagation();
                                        setExpandedRefs((prev) => ({ ...prev, [msg.id]: !prev[msg.id] }));
                                      }}
                                      className="inline-flex items-center gap-1 text-[11px] text-[#B45309] hover:underline cursor-pointer"
                                    >
                                      {expandedRefs[msg.id] ? (
                                        <ChevronDown className="w-3 h-3" />
                                      ) : (
                                        <ChevronRight className="w-3 h-3" />
                                      )}
                                      <Database className="w-3 h-3" />
                                      <span>引用了 {msg.retrieved_memories.length} 条 RAG 记忆</span>
                                    </button>
                                    {expandedRefs[msg.id] && (
                                      <div className="mt-1.5 space-y-1">
                                        {msg.retrieved_memories.map((mem) => (
                                          <button
                                            key={mem.doc_id}
                                            type="button"
                                            onClick={(e) => {
                                              e.stopPropagation();
                                              onJumpToMemory(mem);
                                            }}
                                            className="w-full text-left p-1.5 rounded bg-white border border-[#E5E0D5] hover:border-[#B45309] text-[11px] cursor-pointer"
                                          >
                                            <div className="font-medium text-[#B45309] truncate">{mem.title}</div>
                                            <div className="text-[#5E5B53] line-clamp-2">{mem.snippet}</div>
                                          </button>
                                        ))}
                                      </div>
                                    )}
                                  </div>
                                )}
                              </>
                            )}
                          </div>
                        ))}

                        {stream && (
                          <div className="rounded-lg px-2.5 py-2 bg-[#FAF8F4] border border-amber-300 text-xs">
                            <div className="flex items-center gap-1.5 text-[11px] text-[#B45309] mb-1">
                              <Loader2 className="w-3 h-3 animate-spin" />
                              <span>
                                {stream.status === 'connecting_agy'
                                  ? '正在唤醒 agy CLI 会话...'
                                  : stream.status === 'analyzing_image'
                                    ? 'AI 正在查看框选截图并 OCR 识别...'
                                    : 'agy 正在思考与生成...'}
                              </span>
                            </div>
                            {stream.text && (
                              <div className="ai-markdown">
                                <ReactMarkdown remarkPlugins={[remarkGfm]}>{stream.text}</ReactMarkdown>
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    )}

                    {/* Quick prompt chips + Ask input */}
                    <div className="space-y-1.5 pt-1">
                      <div className="flex flex-wrap gap-1">
                        {(hl.image_url ? IMAGE_QUICK_PROMPTS : TEXT_QUICK_PROMPTS).map((qp) => (
                          <button
                            key={qp}
                            type="button"
                            disabled={Boolean(stream)}
                            onClick={(e) => {
                              e.stopPropagation();
                              onAskHighlight(hl.id, qp, useRag, crossNotebook);
                            }}
                            className="px-2 py-0.5 rounded-full bg-[#F3EFE6] hover:bg-[#E7E0D1] text-[11px] text-[#3F3C35] cursor-pointer disabled:opacity-50"
                          >
                            {qp}
                          </button>
                        ))}
                      </div>

                      <form
                        onSubmit={(e) => {
                          e.preventDefault();
                          const q = qDraft.trim();
                          if (!q || stream) return;
                          setQuestionDrafts((prev) => ({ ...prev, [hl.id]: '' }));
                          onAskHighlight(hl.id, q, useRag, crossNotebook);
                        }}
                        className="flex gap-1.5"
                      >
                        <input
                          ref={(el) => {
                            askInputRefs.current[hl.id] = el;
                          }}
                          type="text"
                          value={qDraft}
                          onChange={(e) => setQuestionDrafts((prev) => ({ ...prev, [hl.id]: e.target.value }))}
                          placeholder={hl.image_url ? '针对框选截图内容向 AI 提问...' : '针对这段划线向 AI 提问...'}
                          className="flex-1 rounded-lg border border-[#E5E0D5] bg-white px-2.5 py-1.5 text-xs focus:outline-none focus:border-[#B45309]"
                        />
                        <button
                          type="submit"
                          disabled={Boolean(stream) || !qDraft.trim()}
                          className="px-2.5 py-1.5 rounded-lg bg-[#B45309] hover:bg-[#92400E] text-white text-xs font-medium cursor-pointer disabled:opacity-45"
                        >
                          <Send className="w-3.5 h-3.5" />
                        </button>
                      </form>
                    </div>
                  </div>
                </div>
              </div>
            );
          })
        )}
      </div>

      {lightboxImageUrl && (
        <div
          onClick={() => setLightboxImageUrl(null)}
          className="fixed inset-0 z-50 bg-black/75 flex items-center justify-center p-4"
        >
          <div
            onClick={(e) => e.stopPropagation()}
            className="relative max-w-4xl max-h-[88vh] bg-white rounded-2xl p-2 shadow-2xl"
          >
            <button
              type="button"
              onClick={() => setLightboxImageUrl(null)}
              className="absolute -top-3 -right-3 w-8 h-8 rounded-full bg-black text-white flex items-center justify-center shadow-lg cursor-pointer"
              aria-label="关闭大图"
            >
              <X className="w-4 h-4" />
            </button>
            <img
              src={lightboxImageUrl}
              alt="框选截图大图预览"
              className="max-w-full max-h-[82vh] object-contain rounded-xl"
            />
          </div>
        </div>
      )}
    </div>
  );
}
