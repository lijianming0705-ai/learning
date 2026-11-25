import { useEffect, useRef } from 'react';
import type { RefObject } from 'react';

interface SafariGestureEvent extends Event {
  scale: number;
  clientX: number;
  clientY: number;
}

interface UsePinchZoomOptions {
  containerRef: RefObject<HTMLElement | null>;
  minScale: number;
  maxScale: number;
  currentScale: number;
  onScaleChange: (nextScale: number, origin?: { clientX: number; clientY: number }) => void;
  onScaleCommit?: (finalScale: number) => void;
}

function clamp(val: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, val));
}

export function usePinchZoom({
  containerRef,
  minScale,
  maxScale,
  currentScale,
  onScaleChange,
  onScaleCommit,
}: UsePinchZoomOptions): void {
  const scaleRef = useRef(currentScale);
  const onChangeRef = useRef(onScaleChange);
  const onCommitRef = useRef(onScaleCommit);
  const commitTimerRef = useRef<number | null>(null);
  const gestureBaseRef = useRef(currentScale);
  const latestPinchRef = useRef(currentScale);

  scaleRef.current = currentScale;
  onChangeRef.current = onScaleChange;
  onCommitRef.current = onScaleCommit;

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    const scheduleCommit = (next: number) => {
      latestPinchRef.current = next;
      if (commitTimerRef.current !== null) {
        window.clearTimeout(commitTimerRef.current);
      }
      commitTimerRef.current = window.setTimeout(() => {
        commitTimerRef.current = null;
        onCommitRef.current?.(latestPinchRef.current);
      }, 140);
    };

    const onWheel = (e: WheelEvent) => {
      if (!e.ctrlKey) return;
      e.preventDefault();

      const clampedDelta = Math.max(-45, Math.min(45, e.deltaY));
      const factor = Math.exp(-clampedDelta * 0.0085);
      const next = clamp(scaleRef.current * factor, minScale, maxScale);
      scaleRef.current = next;
      onChangeRef.current(next, { clientX: e.clientX, clientY: e.clientY });
      scheduleCommit(next);
    };

    const onGestureStart = (e: Event) => {
      e.preventDefault();
      gestureBaseRef.current = scaleRef.current;
    };

    const onGestureChange = (e: Event) => {
      e.preventDefault();
      const ge = e as SafariGestureEvent;
      const next = clamp(gestureBaseRef.current * (ge.scale || 1), minScale, maxScale);
      scaleRef.current = next;
      latestPinchRef.current = next;
      onChangeRef.current(next, { clientX: ge.clientX, clientY: ge.clientY });
    };

    const onGestureEnd = (e: Event) => {
      e.preventDefault();
      if (commitTimerRef.current !== null) {
        window.clearTimeout(commitTimerRef.current);
        commitTimerRef.current = null;
      }
      onCommitRef.current?.(latestPinchRef.current);
    };

    el.addEventListener('wheel', onWheel, { passive: false });
    el.addEventListener('gesturestart', onGestureStart, { passive: false });
    el.addEventListener('gesturechange', onGestureChange, { passive: false });
    el.addEventListener('gestureend', onGestureEnd, { passive: false });

    return () => {
      el.removeEventListener('wheel', onWheel);
      el.removeEventListener('gesturestart', onGestureStart);
      el.removeEventListener('gesturechange', onGestureChange);
      el.removeEventListener('gestureend', onGestureEnd);
      if (commitTimerRef.current !== null) {
        window.clearTimeout(commitTimerRef.current);
      }
    };
  }, [containerRef, minScale, maxScale]);
}
