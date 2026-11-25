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

function touchDistance(t1: Touch, t2: Touch): number {
  const dx = t1.clientX - t2.clientX;
  const dy = t1.clientY - t2.clientY;
  return Math.hypot(dx, dy);
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
  const touchPinchStartDistRef = useRef<number | null>(null);
  const touchPinchBaseScaleRef = useRef<number>(currentScale);

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

    const onTouchStart = (e: TouchEvent) => {
      if (e.touches.length === 2) {
        e.preventDefault();
        touchPinchStartDistRef.current = touchDistance(e.touches[0], e.touches[1]);
        touchPinchBaseScaleRef.current = scaleRef.current;
      }
    };

    const onTouchMove = (e: TouchEvent) => {
      if (e.touches.length === 2 && touchPinchStartDistRef.current) {
        e.preventDefault();
        const dist = touchDistance(e.touches[0], e.touches[1]);
        const ratio = dist / touchPinchStartDistRef.current;
        const next = clamp(touchPinchBaseScaleRef.current * ratio, minScale, maxScale);
        scaleRef.current = next;
        latestPinchRef.current = next;
        const cx = (e.touches[0].clientX + e.touches[1].clientX) / 2;
        const cy = (e.touches[0].clientY + e.touches[1].clientY) / 2;
        onChangeRef.current(next, { clientX: cx, clientY: cy });
      }
    };

    const onTouchEnd = (e: TouchEvent) => {
      if (touchPinchStartDistRef.current !== null && e.touches.length < 2) {
        touchPinchStartDistRef.current = null;
        if (commitTimerRef.current !== null) {
          window.clearTimeout(commitTimerRef.current);
          commitTimerRef.current = null;
        }
        onCommitRef.current?.(latestPinchRef.current);
      }
    };

    el.addEventListener('wheel', onWheel, { passive: false });
    el.addEventListener('gesturestart', onGestureStart, { passive: false });
    el.addEventListener('gesturechange', onGestureChange, { passive: false });
    el.addEventListener('gestureend', onGestureEnd, { passive: false });
    el.addEventListener('touchstart', onTouchStart, { passive: false });
    el.addEventListener('touchmove', onTouchMove, { passive: false });
    el.addEventListener('touchend', onTouchEnd, { passive: true });
    el.addEventListener('touchcancel', onTouchEnd, { passive: true });

    return () => {
      el.removeEventListener('wheel', onWheel);
      el.removeEventListener('gesturestart', onGestureStart);
      el.removeEventListener('gesturechange', onGestureChange);
      el.removeEventListener('gestureend', onGestureEnd);
      el.removeEventListener('touchstart', onTouchStart);
      el.removeEventListener('touchmove', onTouchMove);
      el.removeEventListener('touchend', onTouchEnd);
      el.removeEventListener('touchcancel', onTouchEnd);
      if (commitTimerRef.current !== null) {
        window.clearTimeout(commitTimerRef.current);
      }
    };
  }, [containerRef, minScale, maxScale]);
}
