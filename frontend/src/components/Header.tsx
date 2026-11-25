import { ArrowLeft, BookOpenText, Database, Settings, Sparkles } from 'lucide-react';
import type { AgyModel, HealthStatus, Notebook } from '../types';

interface HeaderProps {
  activeNotebook: Notebook | null;
  onBackToNotebooks: () => void;
  models: AgyModel[];
  selectedModel: string;
  onSelectModel: (modelId: string) => void;
  health: HealthStatus | null;
  onOpenGlobalRag: () => void;
  onOpenSettings: () => void;
}

export function Header({
  activeNotebook,
  onBackToNotebooks,
  models,
  selectedModel,
  onSelectModel,
  health,
  onOpenGlobalRag,
  onOpenSettings,
}: HeaderProps) {
  return (
    <header className="h-14 shrink-0 border-b border-[#E5E0D5] bg-white/90 backdrop-blur px-4 flex items-center justify-between gap-3 z-20">
      <div className="flex items-center gap-3 min-w-0">
        {activeNotebook ? (
          <button
            onClick={onBackToNotebooks}
            className="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-sm font-medium text-[#5E5B53] hover:text-[#1C1B18] hover:bg-[#EFECE5] transition cursor-pointer"
          >
            <ArrowLeft className="w-4 h-4" />
            <span>笔记本</span>
          </button>
        ) : null}

        <div className="flex items-center gap-2">
          <div className="w-8 h-8 rounded-lg bg-[#B45309] text-white flex items-center justify-center shadow-xs">
            <BookOpenText className="w-4 h-4" />
          </div>
          <div className="leading-tight">
            <div className="text-sm font-semibold tracking-tight text-[#1C1B18]">
              {activeNotebook ? activeNotebook.title : 'InkMind Study'}
            </div>
            <div className="text-[11px] text-[#5E5B53]">
              {activeNotebook ? activeNotebook.description || '个人智能学习工作台' : '个人知识库 · 划线精读 · agy + RAG'}
            </div>
          </div>
        </div>
      </div>

      <div className="flex items-center gap-2.5">
        <button
          onClick={onOpenGlobalRag}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-[#E5E0D5] bg-[#FAF8F4] hover:bg-[#EFECE5] text-xs font-medium text-[#1C1B18] transition cursor-pointer"
          title="搜索历史对话与学习记忆 (⌘K)"
        >
          <Database className="w-3.5 h-3.5 text-[#B45309]" />
          <span>RAG 记忆检索</span>
          <span className="hidden sm:inline text-[10px] px-1.5 py-0.5 rounded bg-[#E5E0D5]/70 text-[#5E5B53]">
            {health?.rag_documents ?? 0}
          </span>
        </button>

        <div className="hidden md:flex items-center gap-1.5 px-2.5 py-1 rounded-lg border border-[#E5E0D5] bg-[#FAF8F4]">
          <span
            className={`w-2 h-2 rounded-full ${health?.agy_available ? 'bg-emerald-500' : 'bg-rose-500'}`}
            title={health?.agy_available ? `agy CLI 已连接 (${health.agy_path})` : '未检测到 agy CLI'}
          />
          <Sparkles className="w-3.5 h-3.5 text-[#B45309]" />
          <select
            value={selectedModel}
            onChange={(e) => onSelectModel(e.target.value)}
            className="bg-transparent text-xs font-medium text-[#1C1B18] focus:outline-none cursor-pointer max-w-[190px]"
            aria-label="选择 agy 模型"
          >
            {models.length === 0 ? (
              <option value={selectedModel}>{selectedModel}</option>
            ) : (
              models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name}
                </option>
              ))
            )}
          </select>
        </div>

        <button
          onClick={onOpenSettings}
          className="p-2 rounded-lg text-[#5E5B53] hover:text-[#1C1B18] hover:bg-[#EFECE5] transition cursor-pointer"
          title="系统设置"
        >
          <Settings className="w-4 h-4" />
        </button>
      </div>
    </header>
  );
}
