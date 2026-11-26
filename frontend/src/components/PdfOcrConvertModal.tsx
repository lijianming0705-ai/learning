import { useEffect, useRef, useState } from 'react';
import { BookOpen, CheckCircle2, Clock, Cpu, FileText, Gauge, Image, Loader2, ScanText, Sparkles, X, Zap } from 'lucide-react';
import { streamPdfOcrConvert, type OcrConvertProgressEvent } from '../api/client';
import type { AgyModel, SourceDetail } from '../types';
import { formatDuration } from '../utils/formatDuration';

export interface OcrConvertTarget {
  id: string;
  title: string;
  page_count: number;
  is_scanned: boolean;
}

type OcrEngineMode = 'local' | 'hybrid' | 'agy';

interface PdfOcrConvertModalProps {
  target: OcrConvertTarget;
  models: AgyModel[];
  selectedModel: string;
  localOcrAvailable?: boolean;
  onSelectModel: (modelId: string) => void;
  onConverted: (newSource: SourceDetail) => void;
  onPartialRefresh?: () => Promise<void> | void;
  onClose: () => void;
}

export function PdfOcrConvertModal({
  target,
  models,
  selectedModel,
  localOcrAvailable = true,
  onSelectModel,
  onConverted,
  onPartialRefresh,
  onClose,
}: PdfOcrConvertModalProps) {
  const maxPage = Math.max(1, target.page_count || 1);
  const [title, setTitle] = useState(`${target.title}（OCR 全本文本版）`);
  const [fullBook, setFullBook] = useState(true);
  const [startPage, setStartPage] = useState(1);
  const [endPage, setEndPage] = useState(maxPage);
  const [engine, setEngine] = useState<OcrEngineMode>(localOcrAvailable ? 'local' : 'agy');
  const [concurrency, setConcurrency] = useState<number>(localOcrAvailable ? Math.min(4, maxPage) : maxPage >= 6 ? 2 : 1);

  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState<{
    current: number;
    total: number;
    message: string;
    totalFigures: number;
    pagesPerSec?: number;
  } | null>(null);
  const [elapsedSec, setElapsedSec] = useState(0);
  const [logs, setLogs] = useState<{ page: number; figures: number; failed?: boolean; preview: string }[]>([]);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const startedAtRef = useRef<number>(0);

  useEffect(() => {
    return () => {
      abortRef.current?.abort();
    };
  }, []);

  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => {
      if (startedAtRef.current > 0) {
        setElapsedSec(Math.floor((Date.now() - startedAtRef.current) / 1000));
      }
    }, 1000);
    return () => window.clearInterval(timer);
  }, [running]);

  const handleEngineChange = (next: OcrEngineMode) => {
    setEngine(next);
    if (next === 'local') {
      setConcurrency(Math.min(4, Math.max(1, maxPage)));
    } else if (next === 'hybrid') {
      setConcurrency(Math.min(3, Math.max(1, maxPage)));
    } else {
      setConcurrency(maxPage >= 6 ? 2 : 1);
    }
  };

  const handleStart = async () => {
    if (running) return;
    const safeStart = fullBook ? 1 : Math.max(1, Math.min(maxPage, startPage));
    const safeEnd = fullBook ? maxPage : Math.max(safeStart, Math.min(maxPage, endPage));
    const totalCount = safeEnd - safeStart + 1;

    setRunning(true);
    setErrorMsg(null);
    setLogs([]);
    setElapsedSec(0);
    startedAtRef.current = Date.now();
    const engineLabel =
      engine === 'local'
        ? 'Apple Vision 本地极速'
        : engine === 'hybrid'
          ? '智能混合（本地 + AI 精修）'
          : 'agy AI 多模态';
    setProgress({
      current: 0,
      total: totalCount,
      totalFigures: 0,
      message: `正在启动 ${engineLabel}（${concurrency} 路并发）...`,
    });

    const controller = new AbortController();
    abortRef.current = controller;

    await streamPdfOcrConvert(
      target.id,
      {
        model: selectedModel,
        engine,
        full_book: fullBook,
        start_page: safeStart,
        end_page: safeEnd,
        concurrency,
        title: title.trim() || `${target.title}（OCR 文本版）`,
      },
      {
        onStart: (ev) => {
          setProgress({
            current: 0,
            total: ev.total_pages,
            totalFigures: 0,
            message: `已就绪，开始识别第 ${ev.start_page}–${ev.end_page} 页（共 ${ev.total_pages} 页）...`,
          });
        },
        onProgress: (ev: OcrConvertProgressEvent) => {
          setProgress((prev) => ({
            current: ev.stage === 'done_page' ? ev.current : (prev?.current ?? ev.current),
            total: ev.total,
            totalFigures: ev.total_figures ?? prev?.totalFigures ?? 0,
            pagesPerSec: ev.pages_per_sec ?? prev?.pagesPerSec,
            message: ev.message,
          }));
          if (ev.stage === 'done_page') {
            setLogs((prev) => [
              ...prev,
              {
                page: ev.page_number,
                figures: ev.figures_count ?? 0,
                failed: ev.failed,
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

  const handleCancelOrClose = async () => {
    const hadPartial = running && logs.length > 0;
    if (running) {
      abortRef.current?.abort();
      setRunning(false);
      if (hadPartial && onPartialRefresh) {
        await Promise.resolve(onPartialRefresh()).catch(() => {});
      }
    }
    onClose();
  };

  const pct =
    progress && progress.total > 0 ? Math.min(100, Math.round((progress.current / progress.total) * 100)) : 0;
  const etaSeconds =
    running && progress && progress.current >= 1 && progress.current < progress.total && elapsedSec > 0
      ? Math.round((elapsedSec / progress.current) * (progress.total - progress.current))
      : null;

  const concurrencyOptions =
    engine === 'local'
      ? [
          { value: 2, label: '2x 轻载并发' },
          { value: 4, label: '4x 本地极速（推荐）' },
          { value: 6, label: '6x 高速全本' },
          { value: 8, label: '8x 满血并发' },
        ]
      : [
          { value: 1, label: '1x 单线程稳健' },
          { value: 2, label: '2x 双路并发（推荐）' },
          { value: 3, label: '3x 三路并发' },
          { value: 4, label: '4x 四路并发' },
          { value: 6, label: '6x 六路极速' },
        ];

  return (
    <div className="fixed inset-0 z-50 bg-black/45 backdrop-blur-xs flex items-center justify-center p-3 sm:p-4">
      <div className="bg-white border border-[#E5E0D5] rounded-2xl shadow-2xl w-full max-w-lg overflow-hidden flex flex-col max-h-[92dvh] eink-surface">
        <div className="px-5 py-3.5 border-b border-[#E5E0D5] flex items-center justify-between bg-[#FAF8F4] eink-surface">
          <div className="flex items-center gap-2 min-w-0">
            <div className="w-8 h-8 rounded-lg bg-[#B45309]/10 text-[#B45309] flex items-center justify-center shrink-0">
              <ScanText className="w-4 h-4" />
            </div>
            <div className="min-w-0">
              <h2 className="text-sm font-semibold text-[#1C1B18] truncate">PDF 扫描件全本 OCR 重排文本版</h2>
              <p className="text-[11px] text-[#5E5B53] truncate">
                {target.title} · 共 {maxPage} 页{target.is_scanned ? '（扫描件）' : ''}
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={() => void handleCancelOrClose()}
            className="p-1.5 rounded-lg text-[#5E5B53] hover:text-[#1C1B18] hover:bg-[#EFECE5] cursor-pointer"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="p-5 space-y-4 overflow-y-auto text-xs">
          <div className="rounded-xl bg-amber-50/80 border border-amber-200 px-3 py-2.5 text-amber-950 space-y-1">
            <div className="font-semibold flex items-center gap-1.5">
              <Image className="w-3.5 h-3.5 text-[#B45309]" />
              <span>全本极速转换 · 断点自动保存 · 保留原版排版与配图</span>
            </div>
            <p className="text-[11px] leading-relaxed text-amber-900">
              支持整本书不限页数极速转换：本地 Apple Vision 引擎直调神经网络引擎并发识图，自动重建段落/标题/分栏/列表并无损裁剪原图插图回插原位。
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

          {/* Engine Selector */}
          <div className="space-y-1.5">
            <label className="block font-medium text-[#5E5B53]">OCR 识别引擎</label>
            <div className="grid grid-cols-1 gap-1.5">
              {localOcrAvailable && (
                <button
                  type="button"
                  disabled={running}
                  onClick={() => handleEngineChange('local')}
                  className={`flex items-start gap-2.5 px-3 py-2 rounded-xl border text-left cursor-pointer transition ${
                    engine === 'local'
                      ? 'border-[#B45309] bg-amber-50/90 text-[#1C1B18] ring-1 ring-[#B45309]/30'
                      : 'border-[#E5E0D5] bg-[#FAF8F4] text-[#5E5B53] hover:text-[#1C1B18]'
                  }`}
                >
                  <Zap className="w-4 h-4 text-[#B45309] shrink-0 mt-0.5" />
                  <div className="min-w-0">
                    <div className="font-semibold text-xs text-[#1C1B18]">
                      Apple Vision 本地极速 <span className="text-[10px] font-medium text-emerald-700 bg-emerald-100 px-1.5 py-0.5 rounded-full ml-1">推荐 · 秒级百页</span>
                    </div>
                    <div className="text-[11px] text-[#5E5B53] mt-0.5">
                      调用 macOS 神经网络引擎，零网络延迟与零失败，自动排版并提取配图
                    </div>
                  </div>
                </button>
              )}

              {localOcrAvailable && (
                <button
                  type="button"
                  disabled={running}
                  onClick={() => handleEngineChange('hybrid')}
                  className={`flex items-start gap-2.5 px-3 py-2 rounded-xl border text-left cursor-pointer transition ${
                    engine === 'hybrid'
                      ? 'border-[#B45309] bg-amber-50/90 text-[#1C1B18] ring-1 ring-[#B45309]/30'
                      : 'border-[#E5E0D5] bg-[#FAF8F4] text-[#5E5B53] hover:text-[#1C1B18]'
                  }`}
                >
                  <Cpu className="w-4 h-4 text-[#B45309] shrink-0 mt-0.5" />
                  <div className="min-w-0">
                    <div className="font-semibold text-xs text-[#1C1B18]">智能混合（本地 + AI 精修）</div>
                    <div className="text-[11px] text-[#5E5B53] mt-0.5">
                      常规页本地秒出，检测到复杂表格/公式页自动交由 agy 精排
                    </div>
                  </div>
                </button>
              )}

              <button
                type="button"
                disabled={running}
                onClick={() => handleEngineChange('agy')}
                className={`flex items-start gap-2.5 px-3 py-2 rounded-xl border text-left cursor-pointer transition ${
                  engine === 'agy'
                    ? 'border-[#B45309] bg-amber-50/90 text-[#1C1B18] ring-1 ring-[#B45309]/30'
                    : 'border-[#E5E0D5] bg-[#FAF8F4] text-[#5E5B53] hover:text-[#1C1B18]'
                }`}
              >
                <Sparkles className="w-4 h-4 text-[#B45309] shrink-0 mt-0.5" />
                <div className="min-w-0">
                  <div className="font-semibold text-xs text-[#1C1B18]">AI 多模态全页精排</div>
                  <div className="text-[11px] text-[#5E5B53] mt-0.5">
                    全页使用视觉大模型深度理解（适合复杂公式 LaTeX 与表格，速度较慢）
                  </div>
                </div>
              </button>
            </div>
          </div>

          {/* Scope Toggle: Full Book vs Page Range */}
          <div className="space-y-2">
            <label className="block font-medium text-[#5E5B53]">转换范围</label>
            <div className="grid grid-cols-2 gap-2">
              <button
                type="button"
                disabled={running}
                onClick={() => setFullBook(true)}
                className={`inline-flex items-center justify-center gap-1.5 px-3 py-2 rounded-xl border text-xs font-medium cursor-pointer transition ${
                  fullBook
                    ? 'border-[#B45309] bg-amber-50 text-[#B45309] ring-1 ring-[#B45309]/30'
                    : 'border-[#E5E0D5] bg-[#FAF8F4] text-[#5E5B53] hover:text-[#1C1B18]'
                }`}
              >
                <BookOpen className="w-3.5 h-3.5" />
                <span>全本转化（全部 {maxPage} 页）</span>
              </button>
              <button
                type="button"
                disabled={running}
                onClick={() => setFullBook(false)}
                className={`inline-flex items-center justify-center gap-1.5 px-3 py-2 rounded-xl border text-xs font-medium cursor-pointer transition ${
                  !fullBook
                    ? 'border-[#B45309] bg-amber-50 text-[#B45309] ring-1 ring-[#B45309]/30'
                    : 'border-[#E5E0D5] bg-[#FAF8F4] text-[#5E5B53] hover:text-[#1C1B18]'
                }`}
              >
                <FileText className="w-3.5 h-3.5" />
                <span>指定起止页码</span>
              </button>
            </div>

            {!fullBook && (
              <div className="grid grid-cols-2 gap-3 pt-1">
                <div className="space-y-1">
                  <label className="block text-[11px] text-[#5E5B53]">起始页（1 ~ {maxPage}）</label>
                  <input
                    type="number"
                    min={1}
                    max={maxPage}
                    disabled={running}
                    value={startPage}
                    onChange={(e) => setStartPage(Math.max(1, Math.min(maxPage, Number(e.target.value) || 1)))}
                    className="w-full rounded-lg border border-[#E5E0D5] bg-[#FAF8F4] px-3 py-1.5 text-xs text-[#1C1B18]"
                  />
                </div>
                <div className="space-y-1">
                  <label className="block text-[11px] text-[#5E5B53]">结束页（至第 {maxPage} 页）</label>
                  <input
                    type="number"
                    min={startPage}
                    max={maxPage}
                    disabled={running}
                    value={endPage}
                    onChange={(e) => setEndPage(Math.max(startPage, Math.min(maxPage, Number(e.target.value) || maxPage)))}
                    className="w-full rounded-lg border border-[#E5E0D5] bg-[#FAF8F4] px-3 py-1.5 text-xs text-[#1C1B18]"
                  />
                </div>
              </div>
            )}
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <label className="inline-flex items-center gap-1 font-medium text-[#5E5B53]">
                <Sparkles className="w-3 h-3 text-[#B45309]" />
                <span>{engine === 'local' ? '识别引擎状态' : '视觉识别 agy 模型'}</span>
              </label>
              {engine === 'local' ? (
                <div className="w-full rounded-lg border border-emerald-200 bg-emerald-50/70 px-3 py-2 text-[11px] font-medium text-emerald-800">
                  Apple Vision 神经网络引擎（本地离线，无需调用模型）
                </div>
              ) : (
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
              )}
            </div>

            <div className="space-y-1.5">
              <label className="inline-flex items-center gap-1 font-medium text-[#5E5B53]">
                <Gauge className="w-3 h-3 text-[#B45309]" />
                <span>并发速度</span>
              </label>
              <select
                disabled={running}
                value={concurrency}
                onChange={(e) => setConcurrency(Number(e.target.value) || 4)}
                className="w-full rounded-lg border border-[#E5E0D5] bg-[#FAF8F4] px-3 py-2 text-xs text-[#1C1B18] cursor-pointer"
              >
                {concurrencyOptions.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
            </div>
          </div>

          {progress && (
            <div className="rounded-xl border border-[#E5E0D5] bg-[#FAF8F4] p-3 space-y-2">
              <div className="flex items-center justify-between gap-2 text-[11px]">
                <span className="inline-flex items-center gap-1.5 font-medium text-[#1C1B18] min-w-0">
                  {running ? (
                    <Loader2 className="w-3.5 h-3.5 animate-spin text-[#B45309] shrink-0" />
                  ) : (
                    <CheckCircle2 className="w-3.5 h-3.5 text-emerald-600 shrink-0" />
                  )}
                  <span className="truncate">{progress.message}</span>
                </span>
                <span className="tabular-nums text-[#5E5B53] shrink-0">
                  {progress.current}/{progress.total} ({pct}%)
                </span>
              </div>

              <div className="h-2 rounded-full bg-[#E5E0D5] overflow-hidden">
                <div
                  style={{ width: `${pct}%` }}
                  className="h-full rounded-full bg-[#B45309] transition-all"
                />
              </div>

              <div className="flex flex-wrap items-center justify-between gap-2 text-[10px] text-[#5E5B53] tabular-nums">
                <span className="inline-flex items-center gap-1">
                  <Clock className="w-3 h-3" />
                  <span>已用 {formatDuration(elapsedSec)}</span>
                  {progress.pagesPerSec ? <span>· {progress.pagesPerSec.toFixed(1)} 页/秒</span> : null}
                  {etaSeconds !== null && <span>· 预计剩余 {formatDuration(etaSeconds)}</span>}
                </span>
                <span>已提取配图 {progress.totalFigures} 张</span>
              </div>

              {logs.length > 0 && (
                <div className="max-h-32 overflow-y-auto space-y-1 pt-1 border-t border-[#E5E0D5]">
                  {logs.slice(-40).map((item) => (
                    <div key={item.page} className="text-[11px] text-[#5E5B53] flex items-start gap-1.5">
                      <FileText className={`w-3 h-3 shrink-0 mt-0.5 ${item.failed ? 'text-rose-600' : 'text-[#B45309]'}`} />
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
            onClick={() => void handleCancelOrClose()}
            className="px-3.5 py-2 rounded-xl text-xs font-medium text-[#5E5B53] hover:bg-[#EFECE5] cursor-pointer"
          >
            {running ? (logs.length > 0 ? '停止并保留已转页面' : '取消转换') : '关闭'}
          </button>
          <button
            type="button"
            disabled={running}
            onClick={handleStart}
            className="inline-flex items-center gap-1.5 px-4 py-2 rounded-xl bg-[#B45309] hover:bg-[#92400E] disabled:opacity-50 text-white text-xs font-medium shadow-xs cursor-pointer"
          >
            {running ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <ScanText className="w-3.5 h-3.5" />}
            <span>
              {running
                ? '正在全本转换识别...'
                : fullBook
                  ? `开始全本转换 (${maxPage} 页)`
                  : `开始转换 (${Math.max(1, endPage - startPage + 1)} 页)`}
            </span>
          </button>
        </div>
      </div>
    </div>
  );
}
