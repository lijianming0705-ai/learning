import { useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import type { ReadingHeartbeatResponse, SourceDetail } from '../types';
import { computeReadingProgress, getLocalDateAndHour } from '../utils/formatDuration';

function createSessionId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `sess_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
}

interface UseReadingTrackerOptions {
  source: SourceDetail | null;
  currentPage: number;
  scrollRatio: number;
  isAudioPlaying: boolean;
  onStatsUpdated: (res: ReadingHeartbeatResponse) => void;
}

export function useReadingTracker({
  source,
  currentPage,
  scrollRatio,
  isAudioPlaying,
  onStatsUpdated,
}: UseReadingTrackerOptions) {
  const [liveSessionSeconds, setLiveSessionSeconds] = useState(0);

  const sourceRef = useRef<SourceDetail | null>(source);
  sourceRef.current = source;

  const pageRef = useRef(currentPage);
  pageRef.current = currentPage;

  const scrollRef = useRef(scrollRatio);
  scrollRef.current = scrollRatio;

  const audioPlayingRef = useRef(isAudioPlaying);
  audioPlayingRef.current = isAudioPlaying;

  const onStatsUpdatedRef = useRef(onStatsUpdated);
  onStatsUpdatedRef.current = onStatsUpdated;

  const sessionIdRef = useRef<string>(createSessionId());
  const sessionSourceIdRef = useRef<string | null>(null);
  const lastActivityRef = useRef<number>(Date.now());
  const pendingSecondsRef = useRef<number>(0);
  const sessionTotalSecondsRef = useRef<number>(0);
  const lastTickAtRef = useRef<number>(Date.now());

  const flushHeartbeat = (useBeacon = false) => {
    const activeSrc = sourceRef.current;
    const delta = pendingSecondsRef.current;
    if (!activeSrc || delta <= 0) return;

    pendingSecondsRef.current = 0;
    const { local_date, local_hour } = getLocalDateAndHour();
    const progress = computeReadingProgress(
      activeSrc.type,
      activeSrc.page_count,
      pageRef.current,
      scrollRef.current,
      (activeSrc.reading_seconds || 0) + sessionTotalSecondsRef.current,
    );

    const payload = {
      session_id: sessionIdRef.current,
      source_id: activeSrc.id,
      delta_seconds: Math.min(120, Math.max(1, delta)),
      current_page: Math.max(1, pageRef.current),
      scroll_ratio: Math.max(0, Math.min(1, scrollRef.current)),
      progress,
      local_date,
      local_hour,
    };

    if (useBeacon && typeof navigator !== 'undefined' && typeof navigator.sendBeacon === 'function') {
      try {
        const blob = new Blob([JSON.stringify(payload)], { type: 'application/json' });
        navigator.sendBeacon('/api/analytics/reading/heartbeat', blob);
        return;
      } catch {
        // fallback to normal fetch
      }
    }

    api
      .sendReadingHeartbeat(payload)
      .then((res) => {
        onStatsUpdatedRef.current(res);
      })
      .catch(() => {
        // Restore pending seconds silently on transient error
        pendingSecondsRef.current += delta;
      });
  };

  // Reset session when active source switches
  useEffect(() => {
    if (sessionSourceIdRef.current && sessionSourceIdRef.current !== source?.id) {
      flushHeartbeat(false);
    }
    sessionIdRef.current = createSessionId();
    sessionSourceIdRef.current = source?.id || null;
    pendingSecondsRef.current = 0;
    sessionTotalSecondsRef.current = 0;
    lastActivityRef.current = Date.now();
    lastTickAtRef.current = Date.now();
    setLiveSessionSeconds(0);

    return () => {
      flushHeartbeat(true);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [source?.id]);

  // Activity listeners + 1s reading timer + unload flush
  useEffect(() => {
    if (!source) return;

    let lastMouseMark = 0;
    const markActivity = () => {
      const now = Date.now();
      if (now - lastActivityRef.current > 5 * 60 * 1000 && pendingSecondsRef.current === 0) {
        sessionIdRef.current = createSessionId();
        sessionTotalSecondsRef.current = 0;
        setLiveSessionSeconds(0);
      }
      lastActivityRef.current = now;
    };

    const onMouseMove = () => {
      const now = Date.now();
      if (now - lastMouseMark > 2000) {
        lastMouseMark = now;
        markActivity();
      }
    };

    const onVisibilityChange = () => {
      if (document.visibilityState === 'hidden') {
        flushHeartbeat(true);
      } else {
        lastTickAtRef.current = Date.now();
        markActivity();
      }
    };

    const onPageHide = () => {
      flushHeartbeat(true);
    };

    window.addEventListener('pointerdown', markActivity, { passive: true });
    window.addEventListener('touchstart', markActivity, { passive: true });
    window.addEventListener('keydown', markActivity, { passive: true });
    window.addEventListener('wheel', markActivity, { passive: true });
    window.addEventListener('scroll', markActivity, { passive: true, capture: true });
    window.addEventListener('mousemove', onMouseMove, { passive: true });
    document.addEventListener('visibilitychange', onVisibilityChange);
    window.addEventListener('pagehide', onPageHide);

    const timer = window.setInterval(() => {
      const now = Date.now();
      const elapsed = Math.round((now - lastTickAtRef.current) / 1000);
      lastTickAtRef.current = now;
      if (!sourceRef.current) return;
      if (document.visibilityState !== 'visible') return;

      const isActive = now - lastActivityRef.current < 60_000 || audioPlayingRef.current;
      if (!isActive || elapsed <= 0) return;

      const addSec = Math.min(3, elapsed);
      pendingSecondsRef.current += addSec;
      sessionTotalSecondsRef.current += addSec;
      setLiveSessionSeconds(sessionTotalSecondsRef.current);

      if (pendingSecondsRef.current >= 15) {
        flushHeartbeat(false);
      }
    }, 1000);

    return () => {
      window.clearInterval(timer);
      window.removeEventListener('pointerdown', markActivity);
      window.removeEventListener('touchstart', markActivity);
      window.removeEventListener('keydown', markActivity);
      window.removeEventListener('wheel', markActivity);
      window.removeEventListener('scroll', markActivity, true);
      window.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('visibilitychange', onVisibilityChange);
      window.removeEventListener('pagehide', onPageHide);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [source?.id]);

  return {
    liveSessionSeconds,
    flushReadingHeartbeat: () => flushHeartbeat(false),
  };
}
