import { useEffect, useRef } from 'react';
import {
  AudioLines,
  Loader2,
  Pause,
  Play,
  RefreshCw,
  SkipBack,
  SkipForward,
  Sparkles,
  Square,
  Volume2,
  X,
} from 'lucide-react';
import type { AgyModel, SelectionSpeechResult, SpeechSegment } from '../types';

const SPEED_OPTIONS = [0.8, 1.0, 1.25, 1.5, 2.0];

interface SpeechPanelProps {
  hasSource: boolean;
  sourceTitle: string;
  segments: SpeechSegment[];
  generating: boolean;
  activeSegmentIndex: number | null;
  isPlaying: boolean;
  speechRate: number;
  autoSyncPage: boolean;
  models: AgyModel[];
  selectedModel: string;
  selectionSpeech: SelectionSpeechResult | null;
  selectionSpeechLoading: boolean;
  selectionPlaying: boolean;
  onSelectModel: (modelId: string) => void;
  onGenerateScript: (forceRegenerate?: boolean) => void;
  onPlaySegment: (index: number) => void;
  onTogglePlayPause: () => void;
  onPrevSegment: () => void;
  onNextSegment: () => void;
  onStopPlayback: () => void;
  onChangeRate: (rate: number) => void;
  onToggleAutoSyncPage: (enabled: boolean) => void;
  onReplaySelectionSpeech: () => void;
  onStopSelectionSpeech: () => void;
  onClearSelectionSpeech: () => void;
}

export function SpeechPanel({
  hasSource,
  sourceTitle,
  segments,
  generating,
  activeSegmentIndex,
  isPlaying,
  speechRate,
  autoSyncPage,
  models,
  selectedModel,
  selectionSpeech,
  selectionSpeechLoading,
  selectionPlaying,
  onSelectModel,
  onGenerateScript,
  onPlaySegment,
  onTogglePlayPause,
  onPrevSegment,
  onNextSegment,
  onStopPlayback,
  onChangeRate,
  onToggleAutoSyncPage,
  onReplaySelectionSpeech,
  onStopSelectionSpeech,
  onClearSelectionSpeech,
}: SpeechPanelProps) {
  const segRefs = useRef<Record<number, HTMLButtonElement | null>>({});

  useEffect(() => {
    if (activeSegmentIndex === null) return;
    const el = segRefs.current[activeSegmentIndex];
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  }, [activeSegmentIndex]);

  const activeSeg = activeSegmentIndex !== null ? segments[activeSegmentIndex] || null : null;

  return (
    <div className="h-full flex flex-col bg-[#FAF8F4]/50">
      {/* Header Controls */}
      <div className="p-3 border-b border-[#E5E0D5] space-y-2.5 bg-white eink-surface">
        <div className="flex items-center justify-between gap-2 text-[11px]">
          <span className="inline-flex items-center gap-1 text-[#5E5B53] shrink-0">
            <Sparkles className="w-3 h-3 text-[#B45309]" />
            <span>文本生成模型</span>
          </span>
          <select
            value={selectedModel}
            onChange={(e) => onSelectModel(e.target.value)}
            aria-label="语音文本生成 agy 模型"
            className="flex-1 min-w-0 rounded-lg border border-[#E5E0D5] bg-white px-2 py-1 text-[11px] font-medium text-[#1C1B18] focus:outline-none focus:border-[#B45309] cursor-pointer"
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

        <div className="flex items-center gap-2">
          <button
            type="button"
            disabled={!hasSource || generating}
            onClick={() => onGenerateScript(segments.length > 0)}
            className="flex-1 inline-flex items-center justify-center gap-1.5 px-3 py-2 rounded-xl bg-[#B45309] hover:bg-[#92400E] disabled:opacity-50 text-white text-xs font-medium shadow-xs cursor-pointer transition"
          >
            {generating ? (
              <>
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
                <span>agy 正在整理全文自然朗读...</span>
              </>
            ) : segments.length > 0 ? (
              <>
                <RefreshCw className="w-3.5 h-3.5" />
                <span>重新生成全文自然朗读</span>
              </>
            ) : (
              <>
                <AudioLines className="w-3.5 h-3.5" />
                <span>一键生成全文自然朗读</span>
              </>
            )}
          </button>
        </div>

        <div className="flex items-center justify-between gap-2 text-[11px] text-[#5E5B53]">
          <label className="inline-flex items-center gap-1.5 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={autoSyncPage}
              onChange={(e) => onToggleAutoSyncPage(e.target.checked)}
              className="accent-[#B45309]"
            />
            <span>朗读联动自动翻页</span>
          </label>
          <span className="truncate max-w-[160px]" title={sourceTitle}>
            {segments.length > 0 ? `共 ${segments.length} 段朗读文本` : '划选文本可即点发音'}
          </span>
        </div>
      </div>

      {/* Scrollable Body */}
      <div className="flex-1 overflow-y-auto p-3 space-y-3">
        {/* Selection Pronunciation Result Card */}
        {(selectionSpeechLoading || selectionSpeech) && (
          <div className="rounded-xl border border-teal-200 bg-white p-3 shadow-xs space-y-2 eink-surface">
            <div className="flex items-center justify-between gap-2">
              <div className="inline-flex items-center gap-1.5 text-xs font-semibold text-teal-800">
                <Volume2 className="w-3.5 h-3.5 text-teal-600" />
                <span>选中内容发音</span>
                {selectionSpeech?.language && (
                  <span className="px-1.5 py-0.5 rounded bg-teal-50 border border-teal-200 text-[10px] font-medium text-teal-700">
                    {selectionSpeech.language}
                  </span>
                )}
              </div>
              {!selectionSpeechLoading && (
                <button
                  type="button"
                  onClick={onClearSelectionSpeech}
                  className="p-1 rounded text-[#8E897E] hover:text-[#1C1B18] cursor-pointer"
                  aria-label="关闭发音卡片"
                >
                  <X className="w-3.5 h-3.5" />
                </button>
              )}
            </div>

            {selectionSpeechLoading ? (
              <div className="flex items-center gap-2 py-2 text-xs text-[#5E5B53]">
                <Loader2 className="w-4 h-4 animate-spin text-teal-600" />
                <span>正在调用 agy 生成自然发音文本并合成语音...</span>
              </div>
            ) : (
              selectionSpeech && (
                <>
                  <p className="text-xs leading-relaxed text-[#1C1B18] font-medium bg-[#FAF8F4] rounded-lg px-2.5 py-2 border border-[#ECE7DD]">
                    {selectionSpeech.spoken_text}
                  </p>
                  {selectionSpeech.ipa_or_guide && (
                    <div className="text-[11px] text-teal-800 bg-teal-50/70 rounded-lg px-2.5 py-1 border border-teal-100">
                      <span className="font-semibold">音标 / 读法：</span>
                      <span className="font-mono">{selectionSpeech.ipa_or_guide}</span>
                    </div>
                  )}
                  {selectionSpeech.brief_meaning && (
                    <p className="text-[11px] text-[#5E5B53] leading-relaxed">
                      <span className="font-semibold text-[#1C1B18]">释义：</span>
                      {selectionSpeech.brief_meaning}
                    </p>
                  )}
                  <div className="flex items-center gap-2 pt-0.5">
                    {selectionPlaying ? (
                      <button
                        type="button"
                        onClick={onStopSelectionSpeech}
                        className="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg bg-teal-700 text-white text-[11px] font-medium cursor-pointer"
                      >
                        <Square className="w-3 h-3 fill-current" />
                        <span>停止播放</span>
                      </button>
                    ) : (
                      <button
                        type="button"
                        onClick={onReplaySelectionSpeech}
                        className="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg bg-teal-600 hover:bg-teal-700 text-white text-[11px] font-medium cursor-pointer"
                      >
                        <Play className="w-3 h-3 fill-current" />
                        <span>重新朗读</span>
                      </button>
                    )}
                  </div>
                </>
              )
            )}
          </div>
        )}

        {/* Full-Text Player Bar */}
        {segments.length > 0 && (
          <div className="rounded-xl border border-[#E5E0D5] bg-white p-3 shadow-xs space-y-2.5 eink-surface">
            <div className="flex items-center justify-between gap-2 text-[11px] text-[#5E5B53]">
              <span className="font-medium text-[#1C1B18] truncate">
                {activeSeg
                  ? `第 ${activeSeg.segment_index + 1} / ${segments.length} 段 · P.${activeSeg.page_number}`
                  : `已就绪 · 共 ${segments.length} 段`}
              </span>
              <div className="flex items-center gap-1 shrink-0">
                {SPEED_OPTIONS.map((rate) => (
                  <button
                    key={rate}
                    type="button"
                    onClick={() => onChangeRate(rate)}
                    className={`px-1.5 py-0.5 rounded text-[10px] font-medium cursor-pointer transition ${
                      Math.abs(speechRate - rate) < 0.01
                        ? 'bg-[#B45309] text-white'
                        : 'bg-[#F3EFE6] text-[#5E5B53] hover:text-[#1C1B18]'
                    }`}
                  >
                    {rate}x
                  </button>
                ))}
              </div>
            </div>

            <div className="flex items-center justify-center gap-2">
              <button
                type="button"
                onClick={onPrevSegment}
                disabled={activeSegmentIndex === null || activeSegmentIndex <= 0}
                className="p-2 rounded-lg border border-[#E5E0D5] text-[#5E5B53] hover:text-[#1C1B18] disabled:opacity-40 cursor-pointer"
                title="上一段"
              >
                <SkipBack className="w-4 h-4" />
              </button>

              <button
                type="button"
                onClick={onTogglePlayPause}
                className="inline-flex items-center gap-1.5 px-4 py-2 rounded-xl bg-[#B45309] hover:bg-[#92400E] text-white text-xs font-medium cursor-pointer shadow-xs"
              >
                {isPlaying ? (
                  <>
                    <Pause className="w-4 h-4 fill-current" />
                    <span>暂停</span>
                  </>
                ) : (
                  <>
                    <Play className="w-4 h-4 fill-current" />
                    <span>{activeSegmentIndex !== null ? '继续朗读' : '从头朗读'}</span>
                  </>
                )}
              </button>

              <button
                type="button"
                onClick={onNextSegment}
                disabled={activeSegmentIndex === null || activeSegmentIndex >= segments.length - 1}
                className="p-2 rounded-lg border border-[#E5E0D5] text-[#5E5B53] hover:text-[#1C1B18] disabled:opacity-40 cursor-pointer"
                title="下一段"
              >
                <SkipForward className="w-4 h-4" />
              </button>

              {(isPlaying || activeSegmentIndex !== null) && (
                <button
                  type="button"
                  onClick={onStopPlayback}
                  className="p-2 rounded-lg border border-[#E5E0D5] text-[#5E5B53] hover:text-rose-700 cursor-pointer"
                  title="停止朗读"
                >
                  <Square className="w-3.5 h-3.5 fill-current" />
                </button>
              )}
            </div>
          </div>
        )}

        {/* Segment List */}
        {segments.length === 0 ? (
          <div className="text-center py-10 px-4 text-xs text-[#5E5B53] space-y-2">
            <AudioLines className="w-8 h-8 mx-auto text-[#C4BCAE]" />
            <p className="font-medium text-[#1C1B18]">还没有生成全文自然朗读脚本</p>
            <p className="leading-relaxed">
              点击上方 <span className="font-semibold text-[#B45309]">「一键生成全文自然朗读」</span>，agy 会把全文整理为自然口语播客式分段文本；你也可以直接在文档中划选任意句子点击
              <span className="font-semibold text-[#1C1B18]">「发音」</span> 或
              <span className="font-semibold text-[#1C1B18]">「从此处朗读」</span>。
            </p>
          </div>
        ) : (
          <div className="space-y-2">
            {segments.map((seg, idx) => {
              const active = idx === activeSegmentIndex;
              return (
                <button
                  key={seg.id}
                  ref={(el) => {
                    segRefs.current[idx] = el;
                  }}
                  type="button"
                  onClick={() => onPlaySegment(idx)}
                  className={`w-full text-left rounded-xl border p-3 transition cursor-pointer eink-surface ${
                    active
                      ? 'border-[#B45309] bg-amber-50/70 ring-1 ring-[#B45309]/40 shadow-xs'
                      : 'border-[#E5E0D5] bg-white hover:border-[#C4BCAE]'
                  }`}
                >
                  <div className="flex items-center justify-between gap-2 mb-1">
                    <div className="flex items-center gap-1.5 min-w-0">
                      <span
                        className={`inline-flex items-center justify-center px-1.5 py-0.5 rounded text-[10px] font-semibold ${
                          active ? 'bg-[#B45309] text-white' : 'bg-[#F3EFE6] text-[#5E5B53]'
                        }`}
                      >
                        #{idx + 1}
                      </span>
                      <span className="text-[10px] font-medium text-[#8E897E]">P.{seg.page_number}</span>
                      {seg.heading && (
                        <span className="text-[11px] font-semibold text-[#1C1B18] truncate">{seg.heading}</span>
                      )}
                    </div>
                    <span className="text-[10px] text-[#B45309] shrink-0 inline-flex items-center gap-0.5">
                      {active && isPlaying ? (
                        <>
                          <Volume2 className="w-3 h-3 animate-pulse" />
                          <span>朗读中</span>
                        </>
                      ) : (
                        <>
                          <Play className="w-3 h-3" />
                          <span>点此播放</span>
                        </>
                      )}
                    </span>
                  </div>

                  <p className="text-xs leading-relaxed text-[#1C1B18]">{seg.spoken_text}</p>

                  {seg.original_excerpt && (
                    <p className="mt-1.5 text-[10px] text-[#8E897E] truncate border-t border-[#F1EDE4] pt-1">
                      原文定位：{seg.original_excerpt}
                    </p>
                  )}
                </button>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
