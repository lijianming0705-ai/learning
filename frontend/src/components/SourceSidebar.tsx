import { useRef, useState } from 'react';
import { FileText, FileUp, Globe, Link2, Loader2, PanelLeftClose, Trash2, UploadCloud } from 'lucide-react';
import type { SourceSummary } from '../types';

interface SourceSidebarProps {
  sources: SourceSummary[];
  activeSourceId: string | null;
  className?: string;
  onSelectSource: (sourceId: string) => void;
  onUploadFiles: (files: File[]) => Promise<void>;
  onImportUrl: (url: string) => Promise<void>;
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
  onDeleteSource,
  onCollapse,
}: SourceSidebarProps) {
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [urlInput, setUrlInput] = useState('');
  const [importingUrl, setImportingUrl] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

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

  return (
    <aside className={`w-[270px] shrink-0 border-r border-[#E5E0D5] bg-[#FAF8F4] flex flex-col h-full eink-surface ${className}`}>
      <div className="p-3 border-b border-[#E5E0D5] space-y-2.5">
        <div className="flex items-center justify-between">
          <span className="text-xs font-semibold uppercase tracking-wider text-[#5E5B53]">学习素材</span>
          <div className="flex items-center gap-1.5">
            <span className="text-[11px] text-[#5E5B53]">{sources.length} 份</span>
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
      </div>

      {/* Sources List */}
      <div className="flex-1 overflow-y-auto p-2 space-y-1">
        {sources.length === 0 ? (
          <div className="text-center py-10 px-3 text-xs text-[#5E5B53]">
            <FileUp className="w-7 h-7 mx-auto mb-2 text-[#C4BCAE]" />
            拖入文件或粘贴网页链接即可开始精读与划线笔记
          </div>
        ) : (
          sources.map((src) => {
            const active = src.id === activeSourceId;
            const badge = badgeForType(src.type);
            return (
              <div
                key={src.id}
                onClick={() => onSelectSource(src.id)}
                className={`group rounded-xl p-2.5 cursor-pointer transition border ${
                  active
                    ? 'bg-white border-[#B45309]/70 shadow-xs'
                    : 'border-transparent hover:bg-white/70 hover:border-[#E5E0D5]'
                }`}
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="flex items-center gap-1.5 min-w-0">
                    {src.type === 'url' ? (
                      <Globe className="w-3.5 h-3.5 text-emerald-700 shrink-0" />
                    ) : (
                      <FileText className="w-3.5 h-3.5 text-[#5E5B53] shrink-0" />
                    )}
                    <span className="text-xs font-medium text-[#1C1B18] truncate">{src.title}</span>
                  </div>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      if (window.confirm(`删除素材《${src.title}》？其划线和对话也将移除。`)) {
                        onDeleteSource(src.id);
                      }
                    }}
                    className="opacity-100 md:opacity-0 md:group-hover:opacity-100 p-1 rounded text-[#5E5B53] hover:text-rose-600 cursor-pointer shrink-0"
                    title="删除素材"
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                </div>

                <div className="mt-1.5 flex items-center justify-between text-[11px] text-[#5E5B53]">
                  <span className={`px-1.5 py-0.2 rounded border text-[10px] font-medium ${badge.cls}`}>
                    {badge.label}
                  </span>
                  <span>
                    {src.type === 'pdf' ? `${src.page_count} 页` : `${src.word_count} 字`}
                    {src.highlight_count > 0 ? ` · ${src.highlight_count} 划线` : ''}
                  </span>
                </div>
              </div>
            );
          })
        )}
      </div>
    </aside>
  );
}
