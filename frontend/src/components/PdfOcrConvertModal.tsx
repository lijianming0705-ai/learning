import { useEffect, useRef, useState } from 'react';
import { CheckCircle2, FileText, Image, Loader2, ScanText, Sparkles, X } from 'lucide-react';
import { streamPdfOcrConvert, type OcrConvertProgressEvent } from '../api/client';
import type { AgyModel, SourceDetail } from '../types';

export interface OcrConvertTarget {
  id: string;
  title: string;
  page_count: number;
  is_scanned: boolean;
}

interface PdfOcrConvertModalProps {
  target: OcrConvertTarget;
  models: AgyModel[];
  selectedModel: string;
  onSelectModel: (modelId: string) => void;
  onConverted: (newSource: SourceDetail) => void;
  onClose: () => void;
}

export function PdfOcrConvertModal({
  target,
  models,
  selectedModel,
  onSelectModel,
  onConverted,
  onClose,
}: PdfOcrConvertModalProps) {
  const maxPage = Math.max(1, target.page_count || 1);
  const [title, setTitle] = useState(`${target.title}（OCR 文本版）`);
  const [startPage, setStartPage] = useState(1);
  const [endPage, setEndPage] = useState(Math.min(maxPage, 20));

  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState<{ current: number; total: number; message: string } | null>(null);
  const [logs, setLogs] = useState<{ page: number; figures: number; preview: string }[]>([]);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    return () => {
      abortRef.current?.abort();
    };
  }, []);

  const handleStart = async () => {
    if (running) return;
    const safeStart = Math.max(1, Math.min(maxPage, startPage));
    const safeEnd = Math.max(safeStart, Math.min(maxPage, endPage));
    if (safeEnd - safeStart + 1 > 50) {
      setErrorMsg('单次转换最多支持 50 页，请分批指定起止页码。');
      return;
    }

    setRunning(true);
    setErrorMsg(null);
    setLogs([]);
    setProgress({
      current: 0,
      total: safeEnd - safeStart + 1,
      message: '正在启动 agy 多模态版面 OCR...',
    });

    const controller = new AbortController();
    abortRef.current = controller;

    await streamPdfOcrConvert(
      target.id,
      {
        model: selectedModel,
        start_page: safeStart,
        end_page: safeEnd,
        title: title.trim() || `${target.title}（OCR 文本版）`,
      },
      {
        onStart: (ev) => {
          setProgress({
            current: 0,
            total: ev.total_pages,
            message: `已就绪，准备识别第 ${ev.start_page}–${ev.end_page} 页...`,
          });
        },
        onProgress: (ev: OcrConvertProgressEvent) => {
          setProgress({
            current: ev.stage === 'done_page' ? ev.current : Math.max(0, ev.current - 1),
            total: ev.total,
            message: ev.message,
          });
          if (ev.stage === 'done_page') {
            setLogs((prev) => [
              ...prev,
              {
                page: ev.page_number,
                figures: ev.figures_count ?? 0,
                preview: ev.preview || '本页识别完成',
              },
            ]);
          }
        },
        onDone: ({ source }) => {
          setRunning(false);
          onConverted(source);
          onClose();
        },
        onError: (msg) => {
          setRunning(false);
          setErrorMsg(msg);
        },
      },
      controller.signal,
    );
  };

  const handleCancelOrClose = () => {
    if (running) {
      abortRef.current?.abort();
      setRunning(false);
    }
    onClose();
  };

  const pct =
    progress && progress.total > 0 ? Math.min(100, Math.round((progress.current / progress.total) * 100)) : 0;

  return (
    <div className="fixed inset-0 z-50 bg-black/45 backdrop-blur-xs flex items-center justify-center p-3 sm:p-4">
      <div className="bg-white border border-[#E5E0D5] rounded-2xl shadow-2xl w-full max-w-lg overflow-hidden flex flex-col max-h-[90dvh] eink-surface">
        <div className="px-5 py-3.5 border-b border-[#E5E0D5] flex items-center justify-between bg-[#FAF8F4] eink-surface">
          <div className="flex items-center gap-2 min-w-0">
            <div className="w-8 h-8 rounded-lg bg-[#B45309]/10 text-[#B45309] flex items-center justify-center shrink-0">
              <ScanText className="w-4 h-4" />
            </div>
            <div className="min-w-0">
              <h2 className="text-sm font-semibold text-[#1C1B18] truncate">PDF 扫描件一键 OCR 重排文本版</h2>
              <p className="text-[11px] text-[#5E5B53] truncate">
                {target.title} · 共 {maxPage} 页{target.is_scanned ? '（扫描件）' : ''}
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={handleCancelOrClose}
            className="p-1.5 rounded-lg text-[#5E5B53] hover:text-[#1C1B18] hover:bg-[#EFECE5] cursor-pointer"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="p-5 space-y-4 overflow-y-auto text-xs">
          <div className="rounded-xl bg-amber-50/80 border border-amber-200 px-3 py-2.5 text-amber-950 space-y-1">
            <div className="font-semibold flex items-center gap-1.5">
              <Image className="w-3.5 h-3.5 text-[#B45309]" />
              <span>保留原版排版的双引擎还原</span>
            </div>
            <p className="text-[11px] leading-relaxed text-amber-900">
              系统将渲染每页高清图像并交由 <code>agy</code> 识别标题层级、段落缩进、双栏混排、表格与公式，同时<strong>自动无损裁剪原页插图/图表</strong>插回对应位置，最终输出可划词高亮、语音朗读、RAG 检索且支持 HTML/Markdown 导出的文本版素材。
            </p>
          </div>

          <div className="space-y-1.5">
            <label className="block font-medium text-[#5E5B53]">输出文本版名称</label>
            <input
              type="text"
              disabled={running}
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              className="w-full rounded-lg border border-[#E5E0D5] bg-[#FAF8F4] px-3 py-2 text-xs text-[#1C1B18] focus:outline-none focus:border-[#B45309]"
            />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <label className="block font-medium text-[#5E5B53]">起始页（1 ~ {maxPage}）</label>
              <input
                type="number"
                min={1}
                max={maxPage}
                disabled={running}
                value={startPage}
                onChange={(e) => setStartPage(Math.max(1, Math.min(maxPage, Number(e.target.value) || 1)))}
                className="w-full rounded-lg border border-[#E5E0D5] bg-[#FAF8F4] px-3 py-2 text-xs text-[#1C1B18]"
              />
            </div>
            <div className="space-y-1.5">
              <label className="block font-medium text-[#5E5B53]">结束页（单次最多 50 页）</label>
              <input
                type="number"
                min={startPage}
                max={maxPage}
                disabled={running}
                value={endPage}
                onChange={(e) => setEndPage(Math.max(startPage, Math.min(maxPage, Number(e.target.value) || maxPage)))}
                className="w-full rounded-lg border border-[#E5E0D5] bg-[#FAF8F4] px-3 py-2 text-xs text-[#1C1B18]"
              />
            </div>
          </div>

          <div className="space-y-1.5">
            <label className="inline-flex items-center gap-1 font-medium text-[#5E5B53]">
              <Sparkles className="w-3 h-3 text-[#B45309]" />
              <span>视觉识别 agy 模型</span>
            </label>
            <select
              disabled={running}
              value={selectedModel}
              onChange={(e) => onSelectModel(e.target.value)}
              className="w-full rounded-lg border border-[#E5E0D5] bg-[#FAF8F4] px-3 py-2 text-xs text-[#1C1B18] cursor-pointer"
            >
              {models.length === 0 ? (
                <option value={selectedModel}>{selectedModel}</option>
              ) : (
                models.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.id === 'gemini-4-hydrogen-medium'
                      ? `★ ${m.name} · 默认`
                      : m.id === 'gemini-3.8-flash-low'
                        ? `⚡ ${m.name} · 最快`
                        : m.name}
                  </option>
                ))
              )}
            </select>
          </div>

          {progress && (
            <div className="rounded-xl border border-[#E5E0D5] bg-[#FAF8F4] p-3 space-y-2">
              <div className="flex items-center justify-between text-[11px]">
                <span className="inline-flex items-center gap-1.5 font-medium text-[#1C1B18]">
                  {running ? (
                    <Loader2 className="w-3.5 h-3.5 animate-spin text-[#B45309]" />
                  ) : (
                    <CheckCircle2 className="w-3.5 h-3.5 text-emerald-600" />
                  )}
                  <span>{progress.message}</span>
                </span>
                <span className="tabular-nums text-[#5E5B53]">
                  {progress.current}/{progress.total} ({pct}%)
                </span>
              </div>
              <div className="h-2 rounded-full bg-[#E5E0D5] overflow-hidden">
                <div
                  style={{ width: `${pct}%` }}
                  className="h-full rounded-full bg-[#B45309] transition-all"
                />
              </div>
              {logs.length > 0 && (
                <div className="max-h-32 overflow-y-auto space-y-1 pt-1 border-t border-[#E5E0D5]">
                  {logs.map((item) => (
                    <div key={item.page} className="text-[11px] text-[#5E5B53] flex items-start gap-1.5">
                      <FileText className="w-3 h-3 text-[#B45309] shrink-0 mt-0.5" />
                      <span className="truncate">
                        <strong>P.{item.page}</strong>
                        {item.figures > 0 ? `（含 ${item.figures} 张配图）` : ''}：{item.preview}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {errorMsg && (
            <div className="rounded-xl bg-rose-50 border border-rose-200 px-3 py-2 text-xs text-rose-700">
              {errorMsg}
            </div>
          )}
        </div>

        <div className="px-5 py-3.5 border-t border-[#E5E0D5] bg-[#FAF8F4] flex items-center justify-end gap-2 eink-surface">
          <button
            type="button"
            onClick={handleCancelOrClose}
            className="px-3.5 py-2 rounded-xl text-xs font-medium text-[#5E5B53] hover:bg-[#EFECE5] cursor-pointer"
          >
            {running ? '取消转换' : '关闭'}
          </button>
          <button
            type="button"
            disabled={running}
            onClick={handleStart}
            className="inline-flex items-center gap-1.5 px-4 py-2 rounded-xl bg-[#B45309] hover:bg-[#92400E] disabled:opacity-50 text-white text-xs font-medium shadow-xs cursor-pointer"
          >
            {running ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <ScanText className="w-3.5 h-3.5" />}
            <span>{running ? '正在转换识别...' : '开始一键 OCR 转换'}</span>
          </button>
        </div>
      </div>
    </div>
  );
}
