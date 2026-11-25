import { useState } from 'react';
import { ArrowUpRight, Database, Quote, RefreshCw, Search } from 'lucide-react';
import { api } from '../api/client';
import type { RagMemoryRef, RagStats } from '../types';

interface RagExplorerPanelProps {
  notebookId?: string;
  ragStats: RagStats | null;
  onJumpToMemory: (mem: RagMemoryRef) => void;
  onQuoteMemory: (mem: RagMemoryRef) => void;
  onReindex: () => Promise<void>;
}

export function RagExplorerPanel({
  notebookId,
  ragStats,
  onJumpToMemory,
  onQuoteMemory,
  onReindex,
}: RagExplorerPanelProps) {
  const [query, setQuery] = useState('');
  const [docType, setDocType] = useState('qa_memory,highlight_note');
  const [scopeCurrentNb, setScopeCurrentNb] = useState(false);
  const [results, setResults] = useState<RagMemoryRef[]>([]);
  const [searching, setSearching] = useState(false);
  const [reindexing, setReindexing] = useState(false);

  const handleSearch = async (e?: React.FormEvent) => {
    e?.preventDefault();
    if (!query.trim()) return;
    setSearching(true);
    try {
      const hits = await api.searchRag({
        q: query.trim(),
        notebookId: scopeCurrentNb ? notebookId : undefined,
        docType,
        topK: 15,
      });
      setResults(hits);
    } finally {
      setSearching(false);
    }
  };

  const labelForType = (t: RagMemoryRef['doc_type']) => {
    if (t === 'qa_memory') return { text: '历史对话', cls: 'bg-amber-50 text-amber-800 border-amber-200' };
    if (t === 'highlight_note') return { text: '划线笔记', cls: 'bg-emerald-50 text-emerald-800 border-emerald-200' };
    return { text: '原文片段', cls: 'bg-sky-50 text-sky-800 border-sky-200' };
  };

  return (
    <div className="flex flex-col h-full">
      <div className="p-3 border-b border-[#E5E0D5] bg-[#FAF8F4] space-y-2.5">
        <form onSubmit={handleSearch} className="flex gap-1.5">
          <div className="relative flex-1">
            <Search className="w-3.5 h-3.5 text-[#5E5B53] absolute left-2.5 top-1/2 -translate-y-1/2" />
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="检索历史对话、笔记或资料内容..."
              className="w-full pl-8 pr-2.5 py-1.5 rounded-lg bg-white border border-[#E5E0D5] text-xs focus:outline-none focus:border-[#B45309]"
            />
          </div>
          <button
            type="submit"
            disabled={searching || !query.trim()}
            className="px-3 py-1.5 rounded-lg bg-[#1C1B18] text-white text-xs font-medium hover:bg-[#33312B] disabled:opacity-45 cursor-pointer"
          >
            {searching ? '...' : '检索'}
          </button>
        </form>

        <div className="flex flex-wrap items-center justify-between gap-2 text-[11px]">
          <div className="flex items-center gap-1">
            {[
              { id: 'qa_memory,highlight_note', label: '对话与笔记' },
              { id: 'qa_memory', label: '仅历史对话' },
              { id: 'all', label: '全部索引' },
            ].map((tab) => (
              <button
                key={tab.id}
                type="button"
                onClick={() => setDocType(tab.id)}
                className={`px-2 py-0.5 rounded border cursor-pointer ${
                  docType === tab.id
                    ? 'bg-[#B45309] text-white border-[#B45309]'
                    : 'bg-white text-[#5E5B53] border-[#E5E0D5]'
                }`}
              >
                {tab.label}
              </button>
            ))}
          </div>

          {notebookId && (
            <label className="inline-flex items-center gap-1 text-[#5E5B53] cursor-pointer">
              <input
                type="checkbox"
                checked={scopeCurrentNb}
                onChange={(e) => setScopeCurrentNb(e.target.checked)}
                className="accent-[#B45309]"
              />
              <span>仅当前笔记本</span>
            </label>
          )}
        </div>

        <div className="flex items-center justify-between text-[11px] text-[#5E5B53]">
          <span className="inline-flex items-center gap-1">
            <Database className="w-3 h-3 text-[#B45309]" />
            索引总量 {ragStats?.total_documents ?? 0}（对话 {ragStats?.counts_by_type?.qa_memory ?? 0}）
          </span>
          <button
            type="button"
            disabled={reindexing}
            onClick={async () => {
              setReindexing(true);
              try {
                await onReindex();
              } finally {
                setReindexing(false);
              }
            }}
            className="inline-flex items-center gap-1 text-[#B45309] hover:underline cursor-pointer disabled:opacity-50"
          >
            <RefreshCw className={`w-3 h-3 ${reindexing ? 'animate-spin' : ''}`} />
            <span>重建索引</span>
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto p-3 space-y-2.5">
        {results.length === 0 ? (
          <div className="text-center py-12 px-4 text-xs text-[#5E5B53]">
            <Database className="w-8 h-8 mx-auto mb-2 text-[#C4BCAE]" />
            <p className="font-medium text-[#1C1B18]">随时调取过往学习记忆</p>
            <p className="mt-1">输入概念、问题关键词或原文术语，即可从 RAG 数据库检索历次 AI 对话并跳转回原处。</p>
          </div>
        ) : (
          results.map((hit) => {
            const badge = labelForType(hit.doc_type);
            return (
              <div
                key={hit.doc_id}
                className="rounded-xl bg-white border border-[#E5E0D5] p-3 space-y-2 hover:border-[#B45309]/60 transition"
              >
                <div className="flex items-center justify-between gap-2 text-[11px]">
                  <span className={`px-1.5 py-0.5 rounded border font-medium ${badge.cls}`}>{badge.text}</span>
                  <span className="text-[#5E5B53]">匹配度 {(hit.score * 100).toFixed(0)}%</span>
                </div>

                <div className="text-xs font-medium text-[#1C1B18] line-clamp-1">
                  {hit.notebook_title ? `${hit.notebook_title} · ` : ''}
                  {hit.source_title || hit.title}
                  {hit.page_number ? ` P.${hit.page_number}` : ''}
                </div>

                <p className="text-xs text-[#4A473F] whitespace-pre-wrap line-clamp-4 bg-[#FAF8F4] p-2 rounded-lg">
                  {hit.content}
                </p>

                <div className="flex items-center justify-end gap-2 pt-0.5">
                  <button
                    type="button"
                    onClick={() => onQuoteMemory(hit)}
                    className="inline-flex items-center gap-1 px-2 py-1 rounded-md bg-[#F3EFE6] hover:bg-[#E7E0D1] text-[11px] font-medium text-[#1C1B18] cursor-pointer"
                  >
                    <Quote className="w-3 h-3" />
                    <span>引用提问</span>
                  </button>
                  <button
                    type="button"
                    onClick={() => onJumpToMemory(hit)}
                    className="inline-flex items-center gap-1 px-2 py-1 rounded-md bg-[#B45309] hover:bg-[#92400E] text-[11px] font-medium text-white cursor-pointer"
                  >
                    <span>定位原文</span>
                    <ArrowUpRight className="w-3 h-3" />
                  </button>
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
