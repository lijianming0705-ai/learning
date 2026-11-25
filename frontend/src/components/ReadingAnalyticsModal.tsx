import { useEffect, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  Activity,
  BarChart3,
  BookOpenCheck,
  Clock,
  Flame,
  Highlighter,
  Loader2,
  Sparkles,
  Target,
  TrendingUp,
  X,
} from 'lucide-react';
import { api } from '../api/client';
import type { AgyModel, Notebook, ReadingAiReport, ReadingAnalytics, SourceReadingStat } from '../types';
import { formatDuration, formatShortDuration } from '../utils/formatDuration';

interface ReadingAnalyticsModalProps {
  activeNotebook: Notebook | null;
  models: AgyModel[];
  selectedModel: string;
  onSelectModel: (modelId: string) => void;
  onJumpToSource: (notebookId: string, sourceId: string) => void;
  onClose: () => void;
}

export function ReadingAnalyticsModal({
  activeNotebook,
  models,
  selectedModel,
  onSelectModel,
  onJumpToSource,
  onClose,
}: ReadingAnalyticsModalProps) {
  const [scope, setScope] = useState<'all' | 'notebook'>(activeNotebook ? 'notebook' : 'all');
  const [days, setDays] = useState<7 | 30 | 90>(30);
  const [loading, setLoading] = useState(true);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [data, setData] = useState<ReadingAnalytics | null>(null);

  const [aiReport, setAiReport] = useState<ReadingAiReport | null>(null);
  const [aiLoading, setAiLoading] = useState(false);

  const targetNotebookId = scope === 'notebook' && activeNotebook ? activeNotebook.id : undefined;

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setErrorMsg(null);
    api
      .getReadingAnalytics({ notebookId: targetNotebookId, days })
      .then((res) => {
        if (!cancelled) setData(res);
      })
      .catch((err) => {
        if (!cancelled) setErrorMsg(err instanceof Error ? err.message : '加载阅读统计失败');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [targetNotebookId, days]);

  const handleGenerateAiReport = async () => {
    if (aiLoading) return;
    setAiLoading(true);
    setErrorMsg(null);
    try {
      const report = await api.generateReadingAiReport({
        notebook_id: targetNotebookId || null,
        days,
        model: selectedModel,
      });
      setAiReport(report);
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : 'AI 阅读行为分析生成失败');
    } finally {
      setAiLoading(false);
    }
  };

  const dailyMax = data ? Math.max(60, ...data.daily_trend.map((d) => d.reading_seconds)) : 60;
  const hourlyMax = data ? Math.max(60, ...data.hourly_distribution.map((h) => h.reading_seconds)) : 60;

  return (
    <div className="fixed inset-0 z-50 bg-black/45 backdrop-blur-xs flex items-center justify-center p-2 sm:p-4">
      <div className="bg-[#FAF8F4] border border-[#E5E0D5] rounded-2xl shadow-2xl w-full max-w-5xl max-h-[92dvh] flex flex-col overflow-hidden eink-surface">
        {/* Header */}
        <div className="shrink-0 bg-white border-b border-[#E5E0D5] px-4 sm:px-5 py-3 flex flex-wrap items-center justify-between gap-2 eink-surface">
          <div className="flex items-center gap-2">
            <div className="w-8 h-8 rounded-lg bg-[#B45309]/10 text-[#B45309] flex items-center justify-center">
              <BarChart3 className="w-4 h-4" />
            </div>
            <div>
              <h2 className="text-sm sm:text-base font-semibold text-[#1C1B18]">阅读进度、时长统计与行为分析</h2>
              <p className="text-[11px] text-[#5E5B53]">
                自动记录活跃阅读会话、完读进度与批注问答密度
              </p>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            {activeNotebook && (
              <div className="inline-flex rounded-lg border border-[#E5E0D5] bg-[#FAF8F4] p-0.5 text-[11px]">
                <button
                  type="button"
                  onClick={() => {
                    setScope('notebook');
                    setAiReport(null);
                  }}
                  className={`px-2.5 py-1 rounded-md cursor-pointer transition ${
                    scope === 'notebook' ? 'bg-white text-[#B45309] font-medium shadow-xs' : 'text-[#5E5B53]'
                  }`}
                >
                  当前笔记本
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setScope('all');
                    setAiReport(null);
                  }}
                  className={`px-2.5 py-1 rounded-md cursor-pointer transition ${
                    scope === 'all' ? 'bg-white text-[#B45309] font-medium shadow-xs' : 'text-[#5E5B53]'
                  }`}
                >
                  全部笔记本
                </button>
              </div>
            )}

            <div className="inline-flex rounded-lg border border-[#E5E0D5] bg-[#FAF8F4] p-0.5 text-[11px]">
              {([7, 30, 90] as const).map((d) => (
                <button
                  key={d}
                  type="button"
                  onClick={() => {
                    setDays(d);
                    setAiReport(null);
                  }}
                  className={`px-2 py-1 rounded-md cursor-pointer transition ${
                    days === d ? 'bg-white text-[#B45309] font-medium shadow-xs' : 'text-[#5E5B53]'
                  }`}
                >
                  {d}天
                </button>
              ))}
            </div>

            <button
              type="button"
              onClick={onClose}
              className="p-1.5 rounded-lg text-[#5E5B53] hover:text-[#1C1B18] hover:bg-[#EFECE5] cursor-pointer"
              aria-label="关闭统计"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto p-4 sm:p-5 space-y-5">
          {errorMsg && (
            <div className="rounded-xl bg-rose-50 border border-rose-200 px-3 py-2 text-xs text-rose-700">
              {errorMsg}
            </div>
          )}

          {loading || !data ? (
            <div className="py-20 flex items-center justify-center gap-2 text-sm text-[#5E5B53]">
              <Loader2 className="w-5 h-5 animate-spin text-[#B45309]" />
              <span>正在汇总阅读统计与行为画像...</span>
            </div>
          ) : (
            <>
              {/* 4 KPI Cards */}
              <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
                <div className="rounded-xl bg-white border border-[#E5E0D5] p-3.5 space-y-1 eink-surface">
                  <div className="flex items-center justify-between text-xs text-[#5E5B53]">
                    <span>累计阅读时长</span>
                    <Clock className="w-4 h-4 text-[#B45309]" />
                  </div>
                  <div className="text-lg sm:text-xl font-bold text-[#1C1B18] tabular-nums">
                    {formatDuration(data.summary.total_reading_seconds)}
                  </div>
                  <div className="text-[11px] text-[#5E5B53]">
                    今日已读 <strong className="text-[#1C1B18]">{formatDuration(data.summary.today_reading_seconds)}</strong>
                  </div>
                </div>

                <div className="rounded-xl bg-white border border-[#E5E0D5] p-3.5 space-y-1 eink-surface">
                  <div className="flex items-center justify-between text-xs text-[#5E5B53]">
                    <span>连续学习与专注</span>
                    <Flame className="w-4 h-4 text-orange-600" />
                  </div>
                  <div className="text-lg sm:text-xl font-bold text-[#1C1B18] tabular-nums">
                    {data.summary.current_streak_days} 天连续
                  </div>
                  <div className="text-[11px] text-[#5E5B53]">
                    活跃 {data.summary.active_days} 天 · 单次均 {formatDuration(data.summary.avg_session_seconds)}
                  </div>
                </div>

                <div className="rounded-xl bg-white border border-[#E5E0D5] p-3.5 space-y-1 eink-surface">
                  <div className="flex items-center justify-between text-xs text-[#5E5B53]">
                    <span>整体阅读进度</span>
                    <BookOpenCheck className="w-4 h-4 text-emerald-700" />
                  </div>
                  <div className="text-lg sm:text-xl font-bold text-[#1C1B18] tabular-nums">
                    {Math.round(data.summary.avg_progress * 100)}%
                  </div>
                  <div className="text-[11px] text-[#5E5B53]">
                    已读完 {data.summary.completed_sources} · 在读 {data.summary.in_progress_sources} · 未读 {data.summary.unread_sources}
                  </div>
                </div>

                <div className="rounded-xl bg-white border border-[#E5E0D5] p-3.5 space-y-1 eink-surface">
                  <div className="flex items-center justify-between text-xs text-[#5E5B53]">
                    <span>批注与问答密度</span>
                    <Highlighter className="w-4 h-4 text-sky-700" />
                  </div>
                  <div className="text-lg sm:text-xl font-bold text-[#1C1B18] tabular-nums">
                    {data.summary.total_highlights} 划线 · {data.summary.total_qa_turns} 问
                  </div>
                  <div className="text-[11px] text-[#5E5B53]">
                    约 {data.summary.highlights_per_hour}/h 划线 · {data.summary.qa_per_hour}/h 问答
                  </div>
                </div>
              </div>

              {/* Behavior Profile + AI Diagnosis */}
              <div className="rounded-2xl bg-white border border-[#E5E0D5] p-4 space-y-3 eink-surface">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="inline-flex items-center gap-1.5 text-sm font-semibold text-[#1C1B18]">
                      <Activity className="w-4 h-4 text-[#B45309]" />
                      <span>阅读行为画像</span>
                    </span>
                    <span className="px-2.5 py-0.5 rounded-full bg-amber-50 border border-amber-200 text-xs font-semibold text-[#B45309]">
                      {data.behavior_profile.reader_archetype}
                    </span>
                  </div>

                  <div className="flex items-center gap-2">
                    <select
                      value={selectedModel}
                      onChange={(e) => onSelectModel(e.target.value)}
                      className="rounded-lg border border-[#E5E0D5] bg-[#FAF8F4] px-2 py-1 text-[11px] text-[#1C1B18] max-w-[170px] cursor-pointer"
                      aria-label="行为诊断 agy 模型"
                    >
                      {models.length === 0 ? (
                        <option value={selectedModel}>{selectedModel}</option>
                      ) : (
                        models.map((m) => (
                          <option key={m.id} value={m.id}>
                            {m.id === 'gemini-3.8-flash-low' ? `⚡ ${m.name}` : m.name}
                          </option>
                        ))
                      )}
                    </select>

                    <button
                      type="button"
                      disabled={aiLoading}
                      onClick={handleGenerateAiReport}
                      className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-[#B45309] hover:bg-[#92400E] disabled:opacity-50 text-white text-xs font-medium cursor-pointer transition"
                    >
                      {aiLoading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Sparkles className="w-3.5 h-3.5" />}
                      <span>{aiLoading ? 'agy 分析中...' : aiReport ? '重新 AI 诊断' : 'AI 深度行为分析'}</span>
                    </button>
                  </div>
                </div>

                <div className="grid grid-cols-1 sm:grid-cols-3 gap-2.5 text-xs">
                  <div className="rounded-xl bg-[#FAF8F4] border border-[#ECE7DD] px-3 py-2">
                    <div className="text-[11px] text-[#8E897E]">活跃黄金时段</div>
                    <div className="font-medium text-[#1C1B18] mt-0.5">{data.behavior_profile.peak_period_label}</div>
                  </div>
                  <div className="rounded-xl bg-[#FAF8F4] border border-[#ECE7DD] px-3 py-2">
                    <div className="text-[11px] text-[#8E897E]">专注节奏</div>
                    <div className="font-medium text-[#1C1B18] mt-0.5">{data.behavior_profile.focus_style}</div>
                  </div>
                  <div className="rounded-xl bg-[#FAF8F4] border border-[#ECE7DD] px-3 py-2">
                    <div className="text-[11px] text-[#8E897E]">互动吸收模式</div>
                    <div className="font-medium text-[#1C1B18] mt-0.5">{data.behavior_profile.engagement_style}</div>
                  </div>
                </div>

                <ul className="space-y-1.5 text-xs text-[#4A473F] pl-1">
                  {data.behavior_profile.insights.map((tip, i) => (
                    <li key={i} className="flex items-start gap-2">
                      <Target className="w-3.5 h-3.5 text-[#B45309] shrink-0 mt-0.5" />
                      <span>{tip}</span>
                    </li>
                  ))}
                </ul>

                {aiReport && (
                  <div className="mt-3 pt-3 border-t border-[#ECE7DD] space-y-1.5">
                    <div className="flex items-center justify-between text-[11px] text-[#5E5B53]">
                      <span className="inline-flex items-center gap-1 font-medium text-[#B45309]">
                        <Sparkles className="w-3 h-3" />
                        <span>agy 个性化阅读行为分析报告（{aiReport.model}）</span>
                      </span>
                    </div>
                    <div className="prose prose-sm max-w-none text-xs text-[#1C1B18] leading-relaxed bg-[#FAF8F4] rounded-xl p-3.5 border border-[#ECE7DD]">
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>{aiReport.report_markdown}</ReactMarkdown>
                    </div>
                  </div>
                )}
              </div>

              {/* Charts Row: Daily Trend & 24h Distribution */}
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                <div className="rounded-2xl bg-white border border-[#E5E0D5] p-4 space-y-3 eink-surface">
                  <div className="flex items-center justify-between">
                    <span className="inline-flex items-center gap-1.5 text-xs font-semibold text-[#1C1B18]">
                      <TrendingUp className="w-3.5 h-3.5 text-[#B45309]" />
                      <span>每日阅读时长趋势（近 {data.days} 天）</span>
                    </span>
                    <span className="text-[11px] text-[#8E897E]">峰值 {formatShortDuration(dailyMax)}</span>
                  </div>

                  <div className="h-32 flex items-end gap-[2px] sm:gap-1 pt-2 border-b border-[#ECE7DD] pb-1">
                    {data.daily_trend.map((pt) => {
                      const pct = pt.reading_seconds > 0 ? Math.max(6, Math.round((pt.reading_seconds / dailyMax) * 100)) : 2;
                      return (
                        <div
                          key={pt.date}
                          title={`${pt.date} · 阅读 ${formatDuration(pt.reading_seconds)} · ${pt.sessions_count} 次会话 · ${pt.highlights_count} 划线`}
                          className="flex-1 h-full flex flex-col justify-end group"
                        >
                          <div
                            style={{ height: `${pct}%` }}
                            className={`w-full rounded-t transition-all ${
                              pt.reading_seconds > 0
                                ? 'bg-[#B45309]/80 group-hover:bg-[#B45309]'
                                : 'bg-[#EFECE5]'
                            }`}
                          />
                        </div>
                      );
                    })}
                  </div>
                  <div className="flex justify-between text-[10px] text-[#8E897E]">
                    <span>{data.daily_trend[0]?.date.slice(5)}</span>
                    <span>{data.daily_trend[Math.floor(data.daily_trend.length / 2)]?.date.slice(5)}</span>
                    <span>{data.daily_trend[data.daily_trend.length - 1]?.date.slice(5)}</span>
                  </div>
                </div>

                <div className="rounded-2xl bg-white border border-[#E5E0D5] p-4 space-y-3 eink-surface">
                  <div className="flex items-center justify-between">
                    <span className="inline-flex items-center gap-1.5 text-xs font-semibold text-[#1C1B18]">
                      <Clock className="w-3.5 h-3.5 text-[#B45309]" />
                      <span>24 小时学习时段分布</span>
                    </span>
                    <span className="text-[11px] text-[#8E897E]">按小时累计时长</span>
                  </div>

                  <div className="h-32 flex items-end gap-1 pt-2 border-b border-[#ECE7DD] pb-1">
                    {data.hourly_distribution.map((hp) => {
                      const pct = hp.reading_seconds > 0 ? Math.max(8, Math.round((hp.reading_seconds / hourlyMax) * 100)) : 2;
                      return (
                        <div
                          key={hp.hour}
                          title={`${String(hp.hour).padStart(2, '0')}:00 · ${formatDuration(hp.reading_seconds)} (${hp.sessions_count} 次)`}
                          className="flex-1 h-full flex flex-col justify-end group"
                        >
                          <div
                            style={{ height: `${pct}%` }}
                            className={`w-full rounded-t transition-all ${
                              hp.reading_seconds > 0
                                ? 'bg-teal-600/80 group-hover:bg-teal-700'
                                : 'bg-[#EFECE5]'
                            }`}
                          />
                        </div>
                      );
                    })}
                  </div>
                  <div className="flex justify-between text-[10px] text-[#8E897E]">
                    <span>00:00</span>
                    <span>06:00</span>
                    <span>12:00</span>
                    <span>18:00</span>
                    <span>23:00</span>
                  </div>
                </div>
              </div>

              {/* Category & Type Breakdown */}
              {(data.category_breakdown.length > 0 || data.type_breakdown.length > 0) && (
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  <div className="rounded-2xl bg-white border border-[#E5E0D5] p-4 space-y-2.5 eink-surface">
                    <div className="text-xs font-semibold text-[#1C1B18]">按主题分类阅读分布</div>
                    <div className="space-y-2">
                      {data.category_breakdown.map((cat) => (
                        <div key={cat.label} className="space-y-1">
                          <div className="flex items-center justify-between text-[11px]">
                            <span className="font-medium text-[#1C1B18] truncate max-w-[170px]">{cat.label}</span>
                            <span className="text-[#5E5B53] tabular-nums">
                              {cat.source_count} 份 · {formatShortDuration(cat.reading_seconds)} · 进度 {Math.round(cat.avg_progress * 100)}%
                            </span>
                          </div>
                          <div className="h-1.5 rounded-full bg-[#EFECE5] overflow-hidden">
                            <div
                              style={{ width: `${Math.round(cat.avg_progress * 100)}%` }}
                              className="h-full rounded-full bg-[#B45309]"
                            />
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>

                  <div className="rounded-2xl bg-white border border-[#E5E0D5] p-4 space-y-2.5 eink-surface">
                    <div className="text-xs font-semibold text-[#1C1B18]">按素材媒介偏好分布</div>
                    <div className="space-y-2">
                      {data.type_breakdown.map((tp) => (
                        <div key={tp.label} className="space-y-1">
                          <div className="flex items-center justify-between text-[11px]">
                            <span className="font-medium text-[#1C1B18]">{tp.label}</span>
                            <span className="text-[#5E5B53] tabular-nums">
                              {tp.source_count} 份 · {formatShortDuration(tp.reading_seconds)} · {tp.highlight_count} 划线
                            </span>
                          </div>
                          <div className="h-1.5 rounded-full bg-[#EFECE5] overflow-hidden">
                            <div
                              style={{ width: `${Math.round(tp.avg_progress * 100)}%` }}
                              className="h-full rounded-full bg-teal-600"
                            />
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              )}

              {/* Source Progress Table */}
              <div className="rounded-2xl bg-white border border-[#E5E0D5] overflow-hidden eink-surface">
                <div className="px-4 py-3 border-b border-[#ECE7DD] flex items-center justify-between">
                  <span className="text-xs font-semibold text-[#1C1B18]">
                    各素材阅读进度与投入统计（共 {data.source_stats.length} 份 · 点击可直达阅读）
                  </span>
                </div>

                {data.source_stats.length === 0 ? (
                  <div className="p-8 text-center text-xs text-[#5E5B53]">暂无阅读素材记录</div>
                ) : (
                  <div className="divide-y divide-[#F2EEE6] max-h-[340px] overflow-y-auto">
                    {data.source_stats.map((src: SourceReadingStat) => {
                      const pct = Math.round(src.reading_progress * 100);
                      const completed = pct >= 95;
                      return (
                        <button
                          key={src.source_id}
                          type="button"
                          onClick={() => {
                            onJumpToSource(src.notebook_id, src.source_id);
                            onClose();
                          }}
                          className="w-full text-left px-4 py-2.5 hover:bg-[#FAF8F4] transition flex flex-col sm:flex-row sm:items-center justify-between gap-2 cursor-pointer"
                        >
                          <div className="min-w-0 flex-1">
                            <div className="flex items-center gap-2 min-w-0">
                              <span className="text-xs font-medium text-[#1C1B18] truncate">{src.title}</span>
                              {src.category && (
                                <span className="px-1.5 py-0.2 rounded bg-[#EFECE5] text-[10px] text-[#5E5B53] shrink-0">
                                  {src.category}
                                </span>
                              )}
                            </div>
                            <div className="text-[11px] text-[#8E897E] mt-0.5">
                              {scope === 'all' ? `${src.notebook_title} · ` : ''}
                              {src.type === 'pdf' ? `P.${src.last_page}/${src.page_count}` : `${src.word_count} 字`}
                              {src.highlight_count > 0 ? ` · ${src.highlight_count} 划线` : ''}
                              {src.qa_count > 0 ? ` · ${src.qa_count} 问答` : ''}
                            </div>
                          </div>

                          <div className="flex items-center gap-3 shrink-0 sm:w-[270px]">
                            <div className="flex-1 min-w-[90px]">
                              <div className="flex justify-between text-[10px] mb-0.5 tabular-nums">
                                <span className={completed ? 'text-emerald-700 font-semibold' : 'text-[#5E5B53]'}>
                                  {completed ? '已读完' : `${pct}%`}
                                </span>
                                <span className="text-[#8E897E]">
                                  {src.estimated_remaining_seconds === 0
                                    ? '已完成'
                                    : src.estimated_remaining_seconds
                                      ? `约剩 ${formatShortDuration(src.estimated_remaining_seconds)}`
                                      : ''}
                                </span>
                              </div>
                              <div className="h-1.5 rounded-full bg-[#EFECE5] overflow-hidden">
                                <div
                                  style={{ width: `${Math.max(pct, pct > 0 ? 4 : 0)}%` }}
                                  className={`h-full rounded-full ${completed ? 'bg-emerald-600' : 'bg-[#B45309]'}`}
                                />
                              </div>
                            </div>

                            <div className="text-right w-16 tabular-nums">
                              <div className="text-xs font-semibold text-[#1C1B18]">
                                {formatShortDuration(src.reading_seconds)}
                              </div>
                              <div className="text-[10px] text-[#8E897E]">{src.read_sessions_count} 次</div>
                            </div>
                          </div>
                        </button>
                      );
                    })}
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
