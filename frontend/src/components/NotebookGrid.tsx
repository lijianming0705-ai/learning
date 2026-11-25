import { useState } from 'react';
import { BarChart3, BookOpen, Clock, Database, FileText, Highlighter, MessageSquare, Plus, Search, Trash2 } from 'lucide-react';
import type { Notebook, RagMemoryRef } from '../types';
import { api } from '../api/client';
import { formatShortDuration } from '../utils/formatDuration';

const NOTEBOOK_PALETTE = ['#B45309', '#0F766E', '#1D4ED8', '#BE123C', '#6D28D9', '#374151'];

interface NotebookGridProps {
  notebooks: Notebook[];
  onOpenNotebook: (nb: Notebook) => void;
  onCreateNotebook: (payload: { title: string; description: string; color: string }) => Promise<void>;
  onDeleteNotebook: (id: string) => Promise<void>;
  onJumpToRagMemory: (hit: RagMemoryRef) => void;
  onOpenAnalytics: () => void;
}

export function NotebookGrid({
  notebooks,
  onOpenNotebook,
  onCreateNotebook,
  onDeleteNotebook,
  onJumpToRagMemory,
  onOpenAnalytics,
}: NotebookGridProps) {
  const [showCreate, setShowCreate] = useState(false);
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [color, setColor] = useState(NOTEBOOK_PALETTE[0]);
  const [creating, setCreating] = useState(false);

  const [ragQuery, setRagQuery] = useState('');
  const [ragHits, setRagHits] = useState<RagMemoryRef[]>([]);
  const [searchingRag, setSearchingRag] = useState(false);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!title.trim()) return;
    setCreating(true);
    try {
      await onCreateNotebook({ title: title.trim(), description: description.trim(), color });
      setTitle('');
      setDescription('');
      setShowCreate(false);
    } finally {
      setCreating(false);
    }
  };

  const handleRagSearch = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!ragQuery.trim()) return;
    setSearchingRag(true);
    try {
      const hits = await api.searchRag({ q: ragQuery.trim(), docType: 'qa_memory,highlight_note', topK: 8 });
      setRagHits(hits);
    } finally {
      setSearchingRag(false);
    }
  };

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="max-w-6xl mx-auto px-6 py-8 space-y-8">
        {/* Hero + RAG Quick Retrieval */}
        <section className="rounded-2xl bg-gradient-to-br from-white via-[#FAF8F4] to-[#F3EDE0] border border-[#E5E0D5] p-6 shadow-xs">
          <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
            <div>
              <h1 className="text-2xl font-bold tracking-tight text-[#1C1B18]">我的学习笔记本</h1>
              <p className="text-sm text-[#5E5B53] mt-1">
                拖入 PDF / Word / 网页素材，划线记录灵感与 AI 问答；每次提问都会沉淀到 RAG 记忆库，随时跨本调取。
              </p>
            </div>
            <div className="flex items-center gap-2 shrink-0">
              <button
                onClick={onOpenAnalytics}
                className="inline-flex items-center gap-2 px-3.5 py-2.5 rounded-xl border border-[#E5E0D5] bg-white hover:border-[#B45309] text-[#1C1B18] text-sm font-medium shadow-xs transition cursor-pointer"
              >
                <BarChart3 className="w-4 h-4 text-[#B45309]" />
                <span>阅读行为分析</span>
              </button>
              <button
                onClick={() => setShowCreate(true)}
                className="inline-flex items-center gap-2 px-4 py-2.5 rounded-xl bg-[#B45309] hover:bg-[#92400E] text-white text-sm font-medium shadow-xs transition cursor-pointer"
              >
                <Plus className="w-4 h-4" />
                <span>新建笔记本</span>
              </button>
            </div>
          </div>

          <form onSubmit={handleRagSearch} className="mt-5 flex gap-2">
            <div className="relative flex-1">
              <Search className="w-4 h-4 text-[#5E5B53] absolute left-3.5 top-1/2 -translate-y-1/2" />
              <input
                type="text"
                value={ragQuery}
                onChange={(e) => setRagQuery(e.target.value)}
                placeholder="检索 RAG 历史对话与笔记记忆（例如：Transformer 注意力机制、微积分中值定理）..."
                className="w-full pl-10 pr-4 py-2.5 rounded-xl bg-white border border-[#E5E0D5] text-sm focus:outline-none focus:border-[#B45309]"
              />
            </div>
            <button
              type="submit"
              disabled={searchingRag}
              className="px-4 py-2.5 rounded-xl bg-[#1C1B18] hover:bg-[#33312B] text-white text-sm font-medium transition cursor-pointer disabled:opacity-50"
            >
              {searchingRag ? '检索中...' : '调取记忆'}
            </button>
          </form>

          {ragHits.length > 0 && (
            <div className="mt-4 space-y-2">
              <div className="flex items-center justify-between text-xs text-[#5E5B53]">
                <span className="inline-flex items-center gap-1 font-medium text-[#1C1B18]">
                  <Database className="w-3.5 h-3.5 text-[#B45309]" />
                  找到 {ragHits.length} 条历史对话 / 笔记记忆
                </span>
                <button onClick={() => setRagHits([])} className="hover:underline cursor-pointer">
                  收起结果
                </button>
              </div>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-2.5">
                {ragHits.map((hit) => (
                  <button
                    key={hit.doc_id}
                    onClick={() => onJumpToRagMemory(hit)}
                    className="text-left p-3 rounded-xl bg-white border border-[#E5E0D5] hover:border-[#B45309] transition cursor-pointer"
                  >
                    <div className="flex items-center justify-between gap-2 text-xs">
                      <span className="font-medium text-[#B45309] truncate">
                        {hit.notebook_title || '笔记本'} · {hit.source_title || '资料'}
                        {hit.page_number ? ` P.${hit.page_number}` : ''}
                      </span>
                      <span className="text-[11px] text-[#5E5B53] shrink-0">相关度 {(hit.score * 100).toFixed(0)}%</span>
                    </div>
                    <p className="text-xs text-[#1C1B18] mt-1 line-clamp-2">{hit.snippet}</p>
                  </button>
                ))}
              </div>
            </div>
          )}
        </section>

        {/* Notebook Cards */}
        {notebooks.length === 0 ? (
          <div className="rounded-2xl border-2 border-dashed border-[#E5E0D5] bg-white/60 p-12 text-center">
            <BookOpen className="w-10 h-10 text-[#B45309]/70 mx-auto mb-3" />
            <h2 className="text-base font-semibold text-[#1C1B18]">还没有笔记本</h2>
            <p className="text-sm text-[#5E5B53] mt-1">点击右上角“新建笔记本”，开始导入你的第一份 PDF、Word 或网页资料。</p>
            <button
              onClick={() => setShowCreate(true)}
              className="mt-4 inline-flex items-center gap-1.5 px-4 py-2 rounded-xl bg-[#B45309] text-white text-sm font-medium hover:bg-[#92400E] cursor-pointer"
            >
              <Plus className="w-4 h-4" />
              <span>立即创建</span>
            </button>
          </div>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {notebooks.map((nb) => (
              <div
                key={nb.id}
                onClick={() => onOpenNotebook(nb)}
                className="group relative rounded-2xl bg-white border border-[#E5E0D5] hover:border-[#B45309]/70 hover:shadow-md transition p-5 cursor-pointer flex flex-col justify-between min-h-[168px]"
              >
                <div>
                  <div className="flex items-start justify-between gap-2">
                    <div
                      className="w-9 h-9 rounded-xl flex items-center justify-center text-white shrink-0"
                      style={{ backgroundColor: nb.color || '#B45309' }}
                    >
                      <BookOpen className="w-4 h-4" />
                    </div>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        if (window.confirm(`确定删除笔记本《${nb.title}》及其中所有资料和记忆吗？`)) {
                          onDeleteNotebook(nb.id);
                        }
                      }}
                      className="opacity-100 md:opacity-0 md:group-hover:opacity-100 p-1.5 rounded-lg text-[#5E5B53] hover:text-rose-600 hover:bg-rose-50 transition cursor-pointer"
                      title="删除笔记本"
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </div>

                  <h3 className="text-base font-semibold text-[#1C1B18] mt-3 line-clamp-1">{nb.title}</h3>
                  <p className="text-xs text-[#5E5B53] mt-1 line-clamp-2">
                    {nb.description || '点击进入阅读、划线与 AI 问答'}
                  </p>
                </div>

                <div className="mt-4 pt-3 border-t border-[#F0ECE3] space-y-2">
                  <div className="flex flex-wrap items-center gap-3 text-xs text-[#5E5B53]">
                    <span className="inline-flex items-center gap-1">
                      <FileText className="w-3.5 h-3.5" /> {nb.source_count} 份资料
                    </span>
                    <span className="inline-flex items-center gap-1">
                      <Highlighter className="w-3.5 h-3.5" /> {nb.highlight_count} 处划线
                    </span>
                    <span className="inline-flex items-center gap-1">
                      <MessageSquare className="w-3.5 h-3.5" /> {nb.qa_count} 次问答
                    </span>
                    {nb.total_reading_seconds > 0 && (
                      <span className="inline-flex items-center gap-1 text-[#B45309] font-medium">
                        <Clock className="w-3.5 h-3.5" /> {formatShortDuration(nb.total_reading_seconds)}
                      </span>
                    )}
                  </div>
                  {nb.source_count > 0 && (
                    <div className="flex items-center gap-2 text-[10px] text-[#8E897E]">
                      <div className="flex-1 h-1.5 rounded-full bg-[#EFECE5] overflow-hidden">
                        <div
                          style={{ width: `${Math.round((nb.avg_reading_progress || 0) * 100)}%` }}
                          className="h-full rounded-full bg-[#B45309]"
                        />
                      </div>
                      <span className="tabular-nums shrink-0">{Math.round((nb.avg_reading_progress || 0) * 100)}%</span>
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Create Notebook Modal */}
      {showCreate && (
        <div className="fixed inset-0 z-50 bg-black/40 backdrop-blur-xs flex items-center justify-center p-4">
          <form
            onSubmit={handleCreate}
            className="bg-white border border-[#E5E0D5] rounded-2xl shadow-xl w-full max-w-md p-5 space-y-4"
          >
            <h2 className="text-base font-semibold text-[#1C1B18]">新建学习笔记本</h2>

            <div>
              <label className="block text-xs font-medium text-[#5E5B53] mb-1">笔记本名称 *</label>
              <input
                type="text"
                required
                autoFocus
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder="例如：LLM 论文精读 / Rust 系统编程"
                className="w-full rounded-lg border border-[#E5E0D5] px-3 py-2 text-sm focus:outline-none focus:border-[#B45309]"
              />
            </div>

            <div>
              <label className="block text-xs font-medium text-[#5E5B53] mb-1">简介（可选）</label>
              <textarea
                rows={2}
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="记录本笔记本的学习主题或目标"
                className="w-full rounded-lg border border-[#E5E0D5] px-3 py-2 text-sm focus:outline-none focus:border-[#B45309]"
              />
            </div>

            <div>
              <label className="block text-xs font-medium text-[#5E5B53] mb-1.5">封面主题色</label>
              <div className="flex items-center gap-2">
                {NOTEBOOK_PALETTE.map((c) => (
                  <button
                    key={c}
                    type="button"
                    onClick={() => setColor(c)}
                    className={`w-7 h-7 rounded-full cursor-pointer transition ${
                      color === c ? 'ring-2 ring-offset-2 ring-[#1C1B18]' : 'opacity-80 hover:opacity-100'
                    }`}
                    style={{ backgroundColor: c }}
                  />
                ))}
              </div>
            </div>

            <div className="flex justify-end gap-2 pt-2">
              <button
                type="button"
                onClick={() => setShowCreate(false)}
                className="px-4 py-1.5 rounded-lg text-sm text-[#5E5B53] hover:bg-[#EFECE5] cursor-pointer"
              >
                取消
              </button>
              <button
                type="submit"
                disabled={creating || !title.trim()}
                className="px-4 py-1.5 rounded-lg text-sm font-medium bg-[#B45309] text-white hover:bg-[#92400E] cursor-pointer disabled:opacity-50"
              >
                {creating ? '创建中...' : '创建笔记本'}
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}
