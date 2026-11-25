export function formatDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s <= 0) return '0分钟';
  if (s < 60) return `${s}秒`;
  const mins = Math.floor(s / 60);
  if (mins < 60) return `${mins}分钟`;
  const hrs = Math.floor(mins / 60);
  const remMins = mins % 60;
  return remMins > 0 ? `${hrs}小时${remMins}分` : `${hrs}小时`;
}

export function formatShortDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s <= 0) return '0m';
  if (s < 60) return `${s}s`;
  const mins = Math.floor(s / 60);
  if (mins < 60) return `${mins}m`;
  const hrs = s / 3600;
  return `${hrs >= 10 ? Math.round(hrs) : hrs.toFixed(1)}h`;
}

export function computeReadingProgress(
  type: 'pdf' | 'docx' | 'url' | 'text',
  pageCount: number,
  currentPage: number,
  scrollRatio: number,
  readingSeconds = 0,
): number {
  const safeRatio = Math.max(0, Math.min(1, scrollRatio));
  if (type === 'pdf' && pageCount > 1) {
    const safePage = Math.max(1, Math.min(pageCount, currentPage));
    if (safePage >= pageCount && safeRatio >= 0.72) {
      return 1;
    }
    const base = (safePage - 1 + Math.max(0.15, safeRatio)) / pageCount;
    return Math.max(0, Math.min(1, Number(base.toFixed(4))));
  }
  if (safeRatio >= 0.92) return 1;
  if (safeRatio > 0.02) return Number(safeRatio.toFixed(4));
  if (readingSeconds >= 20) return 0.2;
  return 0;
}

export function getLocalDateAndHour(): { local_date: string; local_hour: number } {
  const now = new Date();
  const y = now.getFullYear();
  const m = String(now.getMonth() + 1).padStart(2, '0');
  const d = String(now.getDate()).padStart(2, '0');
  return {
    local_date: `${y}-${m}-${d}`,
    local_hour: now.getHours(),
  };
}
