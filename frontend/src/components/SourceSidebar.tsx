import { useEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowDownAZ,
  ArrowUpZA,
  Check,
  ChevronDown,
  ChevronRight,
  Clock,
  FileText,
  FileUp,
  Folder,
  FolderOpen,
  Globe,
  Link2,
  List,
  Loader2,
  PanelLeftClose,
  Pencil,
  Search,
  Sparkles,
  Trash2,
  UploadCloud,
  X,
} from 'lucide-react';
import type { SourceSummary } from '../types';

type SourceSortMode = 'recent' | 'name_asc' | 'name_desc';

interface SourceSidebarProps {
  sources: SourceSummary[];
  activeSourceId: string | null;
  className?: string;
  onSelectSource: (sourceId: string) => void;
  onUploadFiles: (files: File[]) => Promise<void>;
  onImportUrl: (url: string) => Promise<void>;
  onUpdateSource: (sourceId: string, patch: { title?: string; category?: string }) => Promise<void>;
  onClassifySources: () => Promise<void>;
  onDeleteSource: (sourceId: string) => Promise<void>;
  onCollapse?: () => void;
}

export function SourceSidebar({
  sources,
  activeSourceId,
  className = '',
  onSelectSource,
  onUploadFiles,
  onImportUrl,
  onUpdateSource,
  onClassifySources,
  onDeleteSource,
  onCollapse,
}: SourceSidebarProps) {
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [urlInput, setUrlInput] = useState('');
  const [importingUrl, setImportingUrl] = useState(false);
  const [classifying, setClassifying] = useState(false);
  const [groupByCategory, setGroupByCategory] = useState(false);
  const [collapsedCategories, setCollapsedCategories] = useState<Record<string, boolean>>({});
  const [editingId, setEditingId] = useState<string | null>(null);
  const [titleDraft, setTitleDraft] = useState('');
  const [categoryDraft, setCategoryDraft] = useState('');
  const [savingEdit, setSavingEdit] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [sortMode, setSortMode] = useState<SourceSortMode>(() => {
    try {
      const saved = localStorage.getItem('inkmind.sourceSort');
      if (saved === 'name_asc' || saved === 'name_desc' || saved === 'recent') return saved;
    } catch {
      // ignore storage errors
    }
    return 'recent';
  });

  useEffect(() => {
    try {
      localStorage.setItem('inkmind.sourceSort', sortMode);
    } catch {
      // ignore storage errors
    }
  }, [sortMode]);

  const hasAnyCategory = useMemo(() => sources.some((s) => Boolean(s.category?.trim())), [sources]);

  useEffect(() => {
    if (hasAnyCategory) {
      setGroupByCategory(true);
    }
  }, [hasAnyCategory]);

  const filteredAndSortedSources = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    const filtered = q
      ? sources.filter((s) =>
          [s.title, s.category, s.original_filename || '', s.source_url || '', s.type]
            .join(' ')
            .toLowerCase()
            .includes(q),
        )
      : [...sources];

    filtered.sort((a, b) => {
      if (sortMode === 'name_asc') {
        return a.title.localeCompare(b.title, 'zh-CN', { numeric: true, sensitivity: 'base' });
      }
      if (sortMode === 'name_desc') {
        return b.title.localeCompare(a.title, 'zh-CN', { numeric: true, sensitivity: 'base' });
      }
      return b.created_at.localeCompare(a.created_at);
    });
    return filtered;
  }, [sources, searchQuery, sortMode]);

  const groupedSources = useMemo(() => {
    const groups = new Map<string, SourceSummary[]>();
    for (const src of filteredAndSortedSources) {
      const cat = src.category?.trim() || '未分类';
      const list = groups.get(cat) || [];
      list.push(src);
      groups.set(cat, list);
    }
    return Array.from(groups.entries()).sort(([a], [b]) => {
      if (a === '未分类') return 1;
      if (b === '未分类') return -1;
      return a.localeCompare(b, 'zh-CN', { numeric: true, sensitivity: 'base' });
    });
  }, [filteredAndSortedSources]);

  const cycleSortMode = () => {
    setSortMode((prev) => (prev === 'recent' ? 'name_asc' : prev === 'name_asc' ? 'name_desc' : 'recent'));
  };

  const handleFiles = async (fileList: FileList | null) => {
    if (!fileList || fileList.length === 0) return;
    setErrorMsg(null);
    setUploading(true);
    try {
      await onUploadFiles(Array.from(fileList));
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : '上传失败');
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };

  const handleUrlSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = urlInput.trim();
    if (!trimmed) return;
    setErrorMsg(null);
    setImportingUrl(true);
    try {
      await onImportUrl(trimmed);
      setUrlInput('');
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : '网址抓取失败');
    } finally {
      setImportingUrl(false);
    }
  };

  const handleClassify = async () => {
    if (sources.length === 0 || classifying) return;
    setErrorMsg(null);
    setClassifying(true);
    try {
      await onClassifySources();
      setGroupByCategory(true);
      setCollapsedCategories({});
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : 'AI 分类失败');
    } finally {
      setClassifying(false);
    }
  };

  const startEditing = (src: SourceSummary) => {
    setEditingId(src.id);
    setTitleDraft(src.title);
    setCategoryDraft(src.category || '');
  };

  const cancelEditing = () => {
    setEditingId(null);
    setTitleDraft('');
    setCategoryDraft('');
  };

  const saveEditing = async (sourceId: string) => {
    const cleanTitle = titleDraft.trim();
    if (!cleanTitle || savingEdit) return;
    setSavingEdit(true);
    setErrorMsg(null);
    try {
      await onUpdateSource(sourceId, {
        title: cleanTitle,
        category: categoryDraft.trim(),
      });
      cancelEditing();
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : '重命名失败');
    } finally {
      setSavingEdit(false);
    }
  };

  const badgeForType = (type: SourceSummary['type']) => {
    switch (type) {
      case 'pdf':
        return { label: 'PDF', cls: 'bg-rose-50 text-rose-700 border-rose-200' };
      case 'docx':
        return { label: 'WORD', cls: 'bg-sky-50 text-sky-700 border-sky-200' };
      case 'url':
        return { label: 'WEB', cls: 'bg-emerald-50 text-emerald-700 border-emerald-200' };
      default:
        return { label: 'TXT', cls: 'bg-stone-100 text-stone-700 border-stone-200' };
    }
  };

  const renderSourceItem = (src: SourceSummary, idxInSection: number) => {
    const active = src.id === activeSourceId;
    const badge = badgeForType(src.type);
    const isEditing = editingId === src.id;

    if (isEditing) {
      return (
        <div
          key={src.id}
          onClick={(e) => e.stopPropagation()}
          className="rounded-xl p-2.5 bg-white border border-[#B45309] space-y-2 shadow-xs"
        >
          <input
            type="text"
            autoFocus
            value={titleDraft}
            onChange={(e) => setTitleDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                void saveEditing(src.id);
              } else if (e.key === 'Escape') {
                cancelEditing();
              }
            }}
            placeholder="输入素材标题..."
            className="w-full rounded-lg border border-[#E5E0D5] bg-[#FAF8F4] px-2 py-1 text-xs text-[#1C1B18] focus:outline-none focus:border-[#B45309]"
          />
          <input
            type="text"
            value={categoryDraft}
            onChange={(e) => setCategoryDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                void saveEditing(src.id);
              } else if (e.key === 'Escape') {
                cancelEditing();
              }
            }}
            placeholder="分类标签（可选）"
            className="w-full rounded-lg border border-[#E5E0D5] bg-[#FAF8F4] px-2 py-1 text-[11px] text-[#5E5B53] focus:outline-none focus:border-[#B45309]"
          />
          <div className="flex items-center justify-end gap-1.5">
            <button
              type="button"
              onClick={cancelEditing}
              className="inline-flex items-center gap-0.5 px-2 py-1 rounded-md text-[11px] text-[#5E5B53] hover:bg-[#EFECE5] cursor-pointer"
            >
              <X className="w-3 h-3" />
              <span>取消</span>
            </button>
            <button
              type="button"
              disabled={savingEdit || !titleDraft.trim()}
              onClick={() => void saveEditing(src.id)}
              className="inline-flex items-center gap-0.5 px-2 py-1 rounded-md text-[11px] font-medium bg-[#B45309] text-white hover:bg-[#92400E] disabled:opacity-50 cursor-pointer"
            >
              <Check className="w-3 h-3" />
              <span>保存</span>
            </button>
          </div>
        </div>
      );
    }

    return (
      <div
        key={src.id}
        onClick={() => onSelectSource(src.id)}
        className={`group relative rounded-xl p-2.5 cursor-pointer transition border ${
          active
            ? 'bg-white border-[#B45309]/70 shadow-xs'
            : 'border-transparent hover:bg-white/80 hover:border-[#E5E0D5]'
        }`}
      >
        <div className="flex items-start justify-between gap-1.5">
          <div className="flex items-center gap-1.5 min-w-0">
            {src.type === 'url' ? (
              <Globe className="w-3.5 h-3.5 text-emerald-700 shrink-0" />
            ) : (
              <FileText className="w-3.5 h-3.5 text-[#5E5B53] shrink-0" />
            )}
            <span
              title={src.title}
              onDoubleClick={(e) => {
                e.stopPropagation();
                startEditing(src);
              }}
              className="text-xs font-medium text-[#1C1B18] truncate"
            >
              {src.title}
            </span>
          </div>
          <div className="flex items-center gap-0.5 shrink-0">
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                startEditing(src);
              }}
              className="opacity-100 md:opacity-0 md:group-hover:opacity-100 p-1 rounded text-[#5E5B53] hover:text-[#B45309] cursor-pointer"
              title="重命名素材"
            >
              <Pencil className="w-3 h-3" />
            </button>
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                if (window.confirm(`删除素材《${src.title}》？其划线和对话也将移除。`)) {
                  onDeleteSource(src.id);
                }
              }}
              className="opacity-100 md:opacity-0 md:group-hover:opacity-100 p-1 rounded text-[#5E5B53] hover:text-rose-600 cursor-pointer"
              title="删除素材"
            >
              <Trash2 className="w-3.5 h-3.5" />
            </button>
          </div>
        </div>

        {/* Hover full-title tooltip when title is truncated */}
        <div
          className={`hidden md:group-hover:block absolute left-2 right-2 ${
            idxInSection === 0 ? 'top-full mt-1' : 'bottom-full mb-1'
          } z-30 pointer-events-none rounded-lg bg-[#1C1B18] text-white px-2.5 py-1.5 text-[11px] leading-snug shadow-lg break-words`}
        >
          <div className="font-medium">{src.title}</div>
          {(src.category || (src.original_filename && src.original_filename !== src.title)) && (
            <div className="text-[10px] text-white/75 mt-0.5 break-all">
              {src.category ? `分类：${src.category}` : ''}
              {src.category && src.original_filename && src.original_filename !== src.title ? ' · ' : ''}
              {src.original_filename && src.original_filename !== src.title ? src.original_filename : ''}
            </div>
          )}
        </div>

        <div className="mt-1.5 flex items-center justify-between text-[11px] text-[#5E5B53] gap-1">
          <div className="flex items-center gap-1 min-w-0">
            <span className={`px-1.5 py-0.2 rounded border text-[10px] font-medium shrink-0 ${badge.cls}`}>
              {badge.label}
            </span>
            {src.is_scanned && (
              <span className="px-1.5 py-0.2 rounded border border-amber-300 bg-amber-50 text-amber-800 text-[10px] font-medium shrink-0">
                扫描件
              </span>
            )}
            {!groupByCategory && src.category && (
              <span className="px-1.5 py-0.2 rounded bg-[#EFECE5] text-[#4A473F] text-[10px] truncate max-w-[84px]">
                {src.category}
              </span>
            )}
          </div>
          <span className="shrink-0">
            {src.type === 'pdf' ? `${src.page_count} 页` : `${src.word_count} 字`}
            {src.highlight_count > 0 ? ` · ${src.highlight_count} 划线` : ''}
          </span>
        </div>
      </div>
    );
  };

  return (
    <aside className={`w-[270px] shrink-0 border-r border-[#E5E0D5] bg-[#FAF8F4] flex flex-col h-full eink-surface ${className}`}>
      <div className="p-3 border-b border-[#E5E0D5] space-y-2.5">
        <div className="flex items-center justify-between">
          <span className="text-xs font-semibold uppercase tracking-wider text-[#5E5B53]">学习素材</span>
          <div className="flex items-center gap-1.5">
            <span className="text-[11px] text-[#5E5B53]">
              {searchQuery.trim() ? `匹配 ${filteredAndSortedSources.length} / ${sources.length}` : `${sources.length} 份`}
            </span>
            {onCollapse && (
              <button
                type="button"
                onClick={onCollapse}
                title="收起左侧素材栏 (⌘[)"
                className="p-1 rounded-md text-[#5E5B53] hover:text-[#1C1B18] hover:bg-[#EFECE5] cursor-pointer"
              >
                <PanelLeftClose className="w-3.5 h-3.5" />
              </button>
            )}
          </div>
        </div>

        {/* AI One-Click Classify + View Toggle */}
        <div className="flex items-center gap-1.5">
          <button
            type="button"
            disabled={sources.length === 0 || classifying}
            onClick={handleClassify}
            className="flex-1 inline-flex items-center justify-center gap-1 px-2.5 py-1.5 rounded-lg bg-[#B45309] hover:bg-[#92400E] text-white text-[11px] font-medium disabled:opacity-45 cursor-pointer transition"
            title="使用 agy AI 自动识别素材主题并归类"
          >
            {classifying ? <Loader2 className="w-3 h-3 animate-spin" /> : <Sparkles className="w-3 h-3" />}
            <span>{classifying ? 'AI 分类中...' : 'AI 一键分类'}</span>
          </button>

          <button
            type="button"
            disabled={sources.length === 0}
            onClick={() => setGroupByCategory((v) => !v)}
            title={groupByCategory ? '当前按分类分组，点击切换平铺列表' : '点击按分类折叠浏览'}
            className={`inline-flex items-center gap-1 px-2 py-1.5 rounded-lg border text-[11px] font-medium cursor-pointer disabled:opacity-45 ${
              groupByCategory
                ? 'border-[#B45309] bg-amber-50 text-[#B45309]'
                : 'border-[#E5E0D5] bg-white text-[#5E5B53] hover:text-[#1C1B18]'
            }`}
          >
            {groupByCategory ? <FolderOpen className="w-3 h-3" /> : <List className="w-3 h-3" />}
            <span>{groupByCategory ? '分组' : '列表'}</span>
          </button>
        </div>

        {/* Drag and Drop Upload Zone */}
        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragging(false);
            handleFiles(e.dataTransfer.files);
          }}
          onClick={() => fileInputRef.current?.click()}
          className={`rounded-xl border-2 border-dashed p-3 text-center cursor-pointer transition ${
            dragging
              ? 'border-[#B45309] bg-amber-50/70'
              : 'border-[#DCD5C7] bg-white hover:border-[#B45309]/60'
          }`}
        >
          <input
            ref={fileInputRef}
            type="file"
            multiple
            accept=".pdf,.docx,.md,.markdown,.txt"
            className="hidden"
            onChange={(e) => handleFiles(e.target.files)}
          />
          {uploading ? (
            <div className="flex items-center justify-center gap-1.5 text-xs text-[#B45309] py-1">
              <Loader2 className="w-4 h-4 animate-spin" />
              <span>正在解析并建立 RAG 索引...</span>
            </div>
          ) : (
            <div className="space-y-1">
              <UploadCloud className="w-5 h-5 text-[#B45309] mx-auto" />
              <div className="text-xs font-medium text-[#1C1B18]">拖入 PDF / Word / Markdown</div>
              <div className="text-[11px] text-[#5E5B53]">或点击选择本地文件</div>
            </div>
          )}
        </div>

        {/* URL Import */}
        <form onSubmit={handleUrlSubmit} className="flex gap-1.5">
          <div className="relative flex-1">
            <Link2 className="w-3.5 h-3.5 text-[#5E5B53] absolute left-2.5 top-1/2 -translate-y-1/2" />
            <input
              type="url"
              value={urlInput}
              onChange={(e) => setUrlInput(e.target.value)}
              placeholder="输入文章网址 https://..."
              className="w-full pl-8 pr-2 py-1.5 rounded-lg bg-white border border-[#E5E0D5] text-xs focus:outline-none focus:border-[#B45309]"
            />
          </div>
          <button
            type="submit"
            disabled={importingUrl || !urlInput.trim()}
            className="px-2.5 py-1.5 rounded-lg bg-[#1C1B18] text-white text-xs font-medium hover:bg-[#33312B] disabled:opacity-45 cursor-pointer shrink-0"
          >
            {importingUrl ? '...' : '导入'}
          </button>
        </form>

        {errorMsg && (
          <div className="text-[11px] text-rose-700 bg-rose-50 border border-rose-200 rounded-lg px-2.5 py-1.5">
            {errorMsg}
          </div>
        )}

        {/* Quick Search & Name Sort */}
        {sources.length > 0 && (
          <div className="flex items-center gap-1.5">
            <div className="relative flex-1 min-w-0">
              <Search className="w-3.5 h-3.5 text-[#5E5B53] absolute left-2 top-1/2 -translate-y-1/2 pointer-events-none" />
              <input
                type="text"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="检索素材名称或分类..."
                className="w-full pl-7 pr-6 py-1 rounded-lg bg-white border border-[#E5E0D5] text-xs focus:outline-none focus:border-[#B45309]"
              />
              {searchQuery && (
                <button
                  type="button"
                  onClick={() => setSearchQuery('')}
                  title="清空检索"
                  className="absolute right-1.5 top-1/2 -translate-y-1/2 p-0.5 text-[#5E5B53] hover:text-[#1C1B18] cursor-pointer"
                >
                  <X className="w-3 h-3" />
                </button>
              )}
            </div>

            <button
              type="button"
              onClick={cycleSortMode}
              title={
                sortMode === 'recent'
                  ? '当前按时间排序，点击按名称 A→Z 排序'
                  : sortMode === 'name_asc'
                    ? '当前按名称 A→Z 排序，点击切换 Z→A'
                    : '当前按名称 Z→A 排序，点击切换最近导入'
              }
              className={`inline-flex items-center gap-1 px-2 py-1 rounded-lg border text-[11px] font-medium cursor-pointer shrink-0 ${
                sortMode !== 'recent'
                  ? 'border-[#B45309] bg-amber-50 text-[#B45309]'
                  : 'border-[#E5E0D5] bg-white text-[#5E5B53] hover:text-[#1C1B18]'
              }`}
            >
              {sortMode === 'name_asc' ? (
                <ArrowDownAZ className="w-3.5 h-3.5" />
              ) : sortMode === 'name_desc' ? (
                <ArrowUpZA className="w-3.5 h-3.5" />
              ) : (
                <Clock className="w-3.5 h-3.5" />
              )}
              <span>{sortMode === 'name_asc' ? 'A-Z' : sortMode === 'name_desc' ? 'Z-A' : '最近'}</span>
            </button>
          </div>
        )}
      </div>

      {/* Sources List / Grouped Categories */}
      <div className="flex-1 overflow-y-auto p-2 space-y-1.5">
        {sources.length === 0 ? (
          <div className="text-center py-10 px-3 text-xs text-[#5E5B53]">
            <FileUp className="w-7 h-7 mx-auto mb-2 text-[#C4BCAE]" />
            拖入文件或粘贴网页链接即可开始精读与划线笔记
          </div>
        ) : filteredAndSortedSources.length === 0 ? (
          <div className="text-center py-10 px-3 text-xs text-[#5E5B53] space-y-2">
            <div>未找到匹配 “{searchQuery.trim()}” 的素材</div>
            <button
              type="button"
              onClick={() => setSearchQuery('')}
              className="px-2.5 py-1 rounded-lg border border-[#E5E0D5] bg-white text-[11px] font-medium text-[#1C1B18] hover:border-[#B45309] cursor-pointer"
            >
              清空检索
            </button>
          </div>
        ) : groupByCategory ? (
          groupedSources.map(([cat, items]) => {
            const isCollapsed = Boolean(collapsedCategories[cat]);
            return (
              <div key={cat} className="space-y-1">
                <button
                  type="button"
                  onClick={() =>
                    setCollapsedCategories((prev) => ({ ...prev, [cat]: !prev[cat] }))
                  }
                  className="w-full flex items-center justify-between px-2 py-1 rounded-lg text-[11px] font-semibold text-[#5E5B53] hover:bg-white/70 hover:text-[#1C1B18] cursor-pointer"
                >
                  <span className="inline-flex items-center gap-1.5 min-w-0">
                    {isCollapsed ? (
                      <ChevronRight className="w-3.5 h-3.5 shrink-0" />
                    ) : (
                      <ChevronDown className="w-3.5 h-3.5 shrink-0" />
                    )}
                    {isCollapsed ? (
                      <Folder className="w-3.5 h-3.5 text-[#B45309] shrink-0" />
                    ) : (
                      <FolderOpen className="w-3.5 h-3.5 text-[#B45309] shrink-0" />
                    )}
                    <span className="truncate">{cat}</span>
                  </span>
                  <span className="text-[10px] px-1.5 py-0.2 rounded-full bg-[#EFECE5] text-[#5E5B53]">
                    {items.length}
                  </span>
                </button>
                {!isCollapsed && (
                  <div className="pl-2 space-y-1">
                    {items.map((src, idx) => renderSourceItem(src, idx))}
                  </div>
                )}
              </div>
            );
          })
        ) : (
          filteredAndSortedSources.map((src, idx) => renderSourceItem(src, idx))
        )}
      </div>
    </aside>
  );
}
