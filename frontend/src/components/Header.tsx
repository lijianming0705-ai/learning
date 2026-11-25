import {
  ArrowLeft,
  BarChart3,
  BookOpenText,
  Contrast,
  Database,
  Maximize2,
  Minimize2,
  PanelLeftClose,
  PanelLeftOpen,
  PanelRightClose,
  PanelRightOpen,
  Settings,
  Sparkles,
} from 'lucide-react';
import type { AgyModel, HealthStatus, Notebook } from '../types';

interface HeaderProps {
  activeNotebook: Notebook | null;
  onBackToNotebooks: () => void;
  models: AgyModel[];
  selectedModel: string;
  onSelectModel: (modelId: string) => void;
  health: HealthStatus | null;
  leftCollapsed?: boolean;
  rightCollapsed?: boolean;
  einkMode?: boolean;
  onToggleLeft?: () => void;
  onToggleRight?: () => void;
  onToggleFocusMode?: () => void;
  onToggleEinkMode?: () => void;
  onOpenAnalytics: () => void;
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
  leftCollapsed = false,
  rightCollapsed = false,
  einkMode = false,
  onToggleLeft,
  onToggleRight,
  onToggleFocusMode,
  onToggleEinkMode,
  onOpenAnalytics,
  onOpenGlobalRag,
  onOpenSettings,
}: HeaderProps) {
  const isFocusMode = leftCollapsed && rightCollapsed;
  return (
    <header className="h-14 shrink-0 border-b border-[#E5E0D5] bg-white/95 backdrop-blur px-2.5 sm:px-4 flex items-center justify-between gap-2 z-20 eink-surface">
      <div className="flex items-center gap-2 sm:gap-3 min-w-0">
        {activeNotebook ? (
          <button
            onClick={onBackToNotebooks}
            className="inline-flex items-center gap-1 px-2 py-1.5 rounded-lg text-xs sm:text-sm font-medium text-[#5E5B53] hover:text-[#1C1B18] hover:bg-[#EFECE5] transition cursor-pointer shrink-0"
          >
            <ArrowLeft className="w-4 h-4" />
            <span className="hidden sm:inline">笔记本</span>
          </button>
        ) : null}

        <div className="flex items-center gap-2 min-w-0">
          <div className="w-8 h-8 rounded-lg bg-[#B45309] text-white flex items-center justify-center shadow-xs shrink-0">
            <BookOpenText className="w-4 h-4" />
          </div>
          <div className="leading-tight min-w-0">
            <div
              title={activeNotebook ? activeNotebook.title : 'InkMind Study'}
              className="text-sm font-semibold tracking-tight text-[#1C1B18] truncate max-w-[140px] sm:max-w-[260px]"
            >
              {activeNotebook ? activeNotebook.title : 'InkMind Study'}
            </div>
            <div
              title={activeNotebook ? activeNotebook.description || '个人智能学习工作台' : '个人知识库 · 划线精读 · agy + RAG'}
              className="hidden sm:block text-[11px] text-[#5E5B53] truncate"
            >
              {activeNotebook ? activeNotebook.description || '个人智能学习工作台' : '个人知识库 · 划线精读 · agy + RAG'}
            </div>
          </div>
        </div>
      </div>

      <div className="flex items-center gap-1.5 sm:gap-2.5 shrink-0">
        {activeNotebook && (
          <div className="hidden lg:flex items-center rounded-lg border border-[#E5E0D5] bg-[#FAF8F4] p-0.5">
            <button
              type="button"
              onClick={onToggleLeft}
              title={leftCollapsed ? '展开左侧素材栏 (⌘[)' : '收起左侧素材栏 (⌘[)'}
              className={`p-1.5 rounded-md transition cursor-pointer ${
                leftCollapsed
                  ? 'text-[#B45309] bg-white shadow-xs'
                  : 'text-[#5E5B53] hover:text-[#1C1B18] hover:bg-[#EFECE5]'
              }`}
            >
              {leftCollapsed ? <PanelLeftOpen className="w-3.5 h-3.5" /> : <PanelLeftClose className="w-3.5 h-3.5" />}
            </button>

            <button
              type="button"
              onClick={onToggleFocusMode}
              title={isFocusMode ? '恢复左右分栏' : '收起两侧，专注阅读'}
              className={`inline-flex items-center gap-1 px-2 py-1 rounded-md text-xs font-medium transition cursor-pointer ${
                isFocusMode
                  ? 'bg-[#B45309] text-white shadow-xs'
                  : 'text-[#5E5B53] hover:text-[#1C1B18] hover:bg-[#EFECE5]'
              }`}
            >
              {isFocusMode ? <Minimize2 className="w-3.5 h-3.5" /> : <Maximize2 className="w-3.5 h-3.5" />}
              <span className="hidden lg:inline">{isFocusMode ? '恢复分栏' : '专注阅读'}</span>
            </button>

            <button
              type="button"
              onClick={onToggleRight}
              title={rightCollapsed ? '展开右侧笔记栏 (⌘])' : '收起右侧笔记栏 (⌘])'}
              className={`p-1.5 rounded-md transition cursor-pointer ${
                rightCollapsed
                  ? 'text-[#B45309] bg-white shadow-xs'
                  : 'text-[#5E5B53] hover:text-[#1C1B18] hover:bg-[#EFECE5]'
              }`}
            >
              {rightCollapsed ? <PanelRightOpen className="w-3.5 h-3.5" /> : <PanelRightClose className="w-3.5 h-3.5" />}
            </button>
          </div>
        )}

        {onToggleEinkMode && (
          <button
            type="button"
            onClick={onToggleEinkMode}
            title="切换墨水屏高对比度无残影模式"
            className={`inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg border text-xs font-medium transition cursor-pointer ${
              einkMode
                ? 'bg-black text-white border-black'
                : 'border-[#E5E0D5] bg-[#FAF8F4] hover:bg-[#EFECE5] text-[#1C1B18]'
            }`}
          >
            <Contrast className="w-3.5 h-3.5" />
            <span className="hidden sm:inline">墨水屏</span>
          </button>
        )}

        <button
          onClick={onOpenAnalytics}
          className="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border border-[#E5E0D5] bg-[#FAF8F4] hover:bg-[#EFECE5] text-xs font-medium text-[#1C1B18] transition cursor-pointer"
          title="查看阅读进度、阅读时长与阅读行为分析"
        >
          <BarChart3 className="w-3.5 h-3.5 text-[#B45309]" />
          <span className="hidden md:inline">阅读统计</span>
        </button>

        <button
          onClick={onOpenGlobalRag}
          className="inline-flex items-center gap-1.5 px-2.5 sm:px-3 py-1.5 rounded-lg border border-[#E5E0D5] bg-[#FAF8F4] hover:bg-[#EFECE5] text-xs font-medium text-[#1C1B18] transition cursor-pointer"
          title="搜索历史对话与学习记忆 (⌘K)"
        >
          <Database className="w-3.5 h-3.5 text-[#B45309]" />
          <span className="hidden sm:inline">RAG 记忆</span>
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-[#E5E0D5]/70 text-[#5E5B53]">
            {health?.rag_documents ?? 0}
          </span>
        </button>

        <div className="hidden sm:flex items-center gap-1.5 px-2.5 py-1 rounded-lg border border-[#E5E0D5] bg-[#FAF8F4]">
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
                  {m.id === 'gemini-3.8-flash-low' ? `⚡ ${m.name} · 最快` : m.name}
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
