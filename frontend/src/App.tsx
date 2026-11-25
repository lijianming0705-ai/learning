import { useEffect, useRef, useState } from 'react';
import {
  AudioLines,
  BookOpen,
  Database,
  FileText,
  Loader2,
  MessageSquare,
  NotebookPen,
  PanelLeftOpen,
  PanelRightClose,
  PanelRightOpen,
  Pause,
  Play,
  SkipBack,
  SkipForward,
  Square,
  Volume2,
  X,
} from 'lucide-react';
import { api, streamHighlightAsk, streamNotebookChat } from './api/client';
import { DocumentViewer } from './components/DocumentViewer';
import { Header } from './components/Header';
import { NotebookChatPanel } from './components/NotebookChatPanel';
import { NotebookGrid } from './components/NotebookGrid';
import { PdfViewer } from './components/PdfViewer';
import { RagExplorerPanel } from './components/RagExplorerPanel';
import { ReadingAnalyticsModal } from './components/ReadingAnalyticsModal';
import { RemarksPanel } from './components/RemarksPanel';
import { SelectionPopover, type SelectionPopoverAction } from './components/SelectionPopover';
import { SettingsModal } from './components/SettingsModal';
import { SourceSidebar } from './components/SourceSidebar';
import { SpeechPanel } from './components/SpeechPanel';
import { useReadingTracker } from './hooks/useReadingTracker';
import { computeReadingProgress } from './utils/formatDuration';
import type {
  AgyModel,
  AppSettings,
  ChatMessage,
  ConversationSummary,
  HealthStatus,
  Highlight,
  HighlightColor,
  Notebook,
  PendingSelection,
  RagMemoryRef,
  RagStats,
  SelectionSpeechResult,
  SourceDetail,
  SourceSummary,
  SpeechSegment,
} from './types';

type RightTab = 'remarks' | 'chat' | 'speech' | 'rag';

const DEFAULT_AGY_MODEL = 'gemini-4-hydrogen-medium';
const LAST_MODEL_STORAGE_KEY = 'inkmind.lastSelectedModel';

export default function App() {
  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [models, setModels] = useState<AgyModel[]>([]);
  const [settings, setSettings] = useState<AppSettings>({
    default_model: DEFAULT_AGY_MODEL,
    rag_top_k: 5,
    cross_notebook_rag: true,
    embedding_provider: 'hybrid-local',
    ollama_embedding_model: '',
  });
  const [selectedModel, setSelectedModel] = useState<string>(() => {
    try {
      return localStorage.getItem(LAST_MODEL_STORAGE_KEY) || DEFAULT_AGY_MODEL;
    } catch {
      return DEFAULT_AGY_MODEL;
    }
  });
  const [ragStats, setRagStats] = useState<RagStats | null>(null);

  const [notebooks, setNotebooks] = useState<Notebook[]>([]);
  const [activeNotebook, setActiveNotebook] = useState<Notebook | null>(null);

  const [sources, setSources] = useState<SourceSummary[]>([]);
  const [activeSource, setActiveSource] = useState<SourceDetail | null>(null);
  const [highlights, setHighlights] = useState<Highlight[]>([]);
  const [activeHighlightId, setActiveHighlightId] = useState<string | null>(null);
  const [pulseHighlightId, setPulseHighlightId] = useState<string | null>(null);
  const [jumpPage, setJumpPage] = useState<{ page: number; seq: number } | null>(null);
  const [suppressAutoRestore, setSuppressAutoRestore] = useState(false);

  const [pendingSelection, setPendingSelection] = useState<PendingSelection | null>(null);
  const [selectionColor, setSelectionColor] = useState<HighlightColor>('amber');
  const [focusIntent, setFocusIntent] = useState<{ highlightId: string; mode: 'note' | 'ask' } | null>(null);

  const [rightTab, setRightTab] = useState<RightTab>('remarks');
  const [highlightStreams, setHighlightStreams] = useState<
    Record<string, { text: string; status: string; memories: RagMemoryRef[] }>
  >({});
  const [ocrLoadingMap, setOcrLoadingMap] = useState<Record<string, boolean>>({});

  // Notebook chat state
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [activeConvId, setActiveConvId] = useState<string | null>(null);
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
  const [chatStreamingText, setChatStreamingText] = useState('');
  const [chatStreamingStatus, setChatStreamingStatus] = useState('');
  const [chatStreaming, setChatStreaming] = useState(false);

  // Quoted memory + modals + layout collapse
  const [quotedContext, setQuotedContext] = useState<string | null>(null);

  // Speech & natural reading state
  const [speechSegments, setSpeechSegments] = useState<SpeechSegment[]>([]);
  const [speechGenerating, setSpeechGenerating] = useState(false);
  const [activeSegmentIndex, setActiveSegmentIndex] = useState<number | null>(null);
  const [isSpeaking, setIsSpeaking] = useState(false);
  const [speechRate, setSpeechRate] = useState<number>(1.0);
  const [autoSyncPage, setAutoSyncPage] = useState<boolean>(true);
  const [selectionSpeech, setSelectionSpeech] = useState<SelectionSpeechResult | null>(null);
  const [selectionSpeechLoading, setSelectionSpeechLoading] = useState(false);
  const [selectionAudioPlaying, setSelectionAudioPlaying] = useState(false);

  const audioRef = useRef<HTMLAudioElement | null>(null);
  const playModeRef = useRef<'none' | 'selection' | 'segment'>('none');
  const onSegmentEndedRef = useRef<() => void>(() => {});
  const onSelectionEndedRef = useRef<() => void>(() => {});

  const [showSettings, setShowSettings] = useState(false);
  const [showGlobalRag, setShowGlobalRag] = useState(false);
  const [showAnalytics, setShowAnalytics] = useState(false);
  const [activeReadingPos, setActiveReadingPos] = useState<{ page: number; scrollRatio: number }>({
    page: 1,
    scrollRatio: 0,
  });
  const [toastError, setToastError] = useState<string | null>(null);
  const [leftCollapsed, setLeftCollapsed] = useState<boolean>(() => {
    try {
      return localStorage.getItem('inkmind.leftCollapsed') === 'true';
    } catch {
      return false;
    }
  });
  const [rightCollapsed, setRightCollapsed] = useState<boolean>(() => {
    try {
      return localStorage.getItem('inkmind.rightCollapsed') === 'true';
    } catch {
      return false;
    }
  });
  const [einkMode, setEinkMode] = useState<boolean>(() => {
    try {
      return localStorage.getItem('inkmind.einkMode') === 'true';
    } catch {
      return false;
    }
  });
  const [isCompactViewport, setIsCompactViewport] = useState<boolean>(() =>
    typeof window !== 'undefined' ? window.matchMedia('(max-width: 1023px)').matches : false,
  );
  const [mobileDrawer, setMobileDrawer] = useState<'none' | 'sources' | 'right'>('none');

  useEffect(() => {
    document.documentElement.dataset.theme = einkMode ? 'eink' : 'paper';
    try {
      localStorage.setItem('inkmind.einkMode', String(einkMode));
    } catch {
      // ignore storage errors
    }
  }, [einkMode]);

  useEffect(() => {
    const mq = window.matchMedia('(max-width: 1023px)');
    const onChange = () => setIsCompactViewport(mq.matches);
    onChange();
    mq.addEventListener('change', onChange);
    return () => mq.removeEventListener('change', onChange);
  }, []);

  useEffect(() => {
    try {
      localStorage.setItem('inkmind.leftCollapsed', String(leftCollapsed));
    } catch {
      // ignore storage errors
    }
  }, [leftCollapsed]);

  useEffect(() => {
    try {
      localStorage.setItem('inkmind.rightCollapsed', String(rightCollapsed));
    } catch {
      // ignore storage errors
    }
  }, [rightCollapsed]);

  const isFocusMode = leftCollapsed && rightCollapsed;
  const toggleFocusMode = () => {
    if (isFocusMode) {
      setLeftCollapsed(false);
      setRightCollapsed(false);
    } else {
      setLeftCollapsed(true);
      setRightCollapsed(true);
    }
  };

  const handleSelectModel = (modelId: string) => {
    const cleanId = modelId.trim();
    if (!cleanId) return;
    setSelectedModel(cleanId);
    setSettings((prev) => ({ ...prev, default_model: cleanId }));
    try {
      localStorage.setItem(LAST_MODEL_STORAGE_KEY, cleanId);
    } catch {
      // ignore storage errors
    }
    api.updateSettings({ default_model: cleanId }).catch(() => {});
  };

  const refreshSystem = async (syncSelectedModel = false) => {
    const [h, m, s, st] = await Promise.all([
      api.health(),
      api.listModels().catch(() => []),
      api.getSettings(),
      api.ragStats(),
    ]);
    setHealth(h);
    setModels(m);
    setSettings(s);
    if (syncSelectedModel) {
      const resolvedModel = s.default_model || DEFAULT_AGY_MODEL;
      setSelectedModel(resolvedModel);
      try {
        localStorage.setItem(LAST_MODEL_STORAGE_KEY, resolvedModel);
      } catch {
        // ignore storage errors
      }
    }
    setRagStats(st);
  };

  const refreshNotebooks = async () => {
    const nbs = await api.listNotebooks();
    setNotebooks(nbs);
    return nbs;
  };

  useEffect(() => {
    refreshSystem(true).catch(() => {});
    refreshNotebooks().catch(() => {});
  }, []);

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setShowGlobalRag((v) => !v);
        return;
      }
      if (!activeNotebook) return;
      const target = e.target as HTMLElement | null;
      const inInput =
        target?.tagName === 'INPUT' || target?.tagName === 'TEXTAREA' || target?.isContentEditable;
      if (inInput) return;

      if ((e.metaKey || e.ctrlKey) && e.key === '[') {
        e.preventDefault();
        setLeftCollapsed((v) => !v);
      } else if ((e.metaKey || e.ctrlKey) && e.key === ']') {
        e.preventDefault();
        setRightCollapsed((v) => !v);
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [activeNotebook]);

  const showError = (msg: string) => {
    setToastError(msg);
    setTimeout(() => setToastError((cur) => (cur === msg ? null : cur)), 5000);
  };

  const stopAllAudio = () => {
    playModeRef.current = 'none';
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current.removeAttribute('src');
      audioRef.current.load();
    }
    if (typeof window !== 'undefined' && 'speechSynthesis' in window) {
      try {
        window.speechSynthesis.cancel();
      } catch {
        // ignore synthesis cancel errors
      }
    }
    setIsSpeaking(false);
    setSelectionAudioPlaying(false);
  };

  useEffect(() => {
    const audio = new Audio();
    audioRef.current = audio;

    const handleEnded = () => {
      if (playModeRef.current === 'segment') {
        onSegmentEndedRef.current();
      } else if (playModeRef.current === 'selection') {
        onSelectionEndedRef.current();
      }
    };
    audio.addEventListener('ended', handleEnded);
    return () => {
      audio.removeEventListener('ended', handleEnded);
      audio.pause();
      if (typeof window !== 'undefined' && 'speechSynthesis' in window) {
        try {
          window.speechSynthesis.cancel();
        } catch {
          // ignore
        }
      }
    };
  }, []);

  useEffect(() => {
    if (audioRef.current) {
      audioRef.current.playbackRate = speechRate;
    }
  }, [speechRate]);

  const speakViaBrowserTts = (text: string, language: string, mode: 'selection' | 'segment') => {
    if (typeof window === 'undefined' || !('speechSynthesis' in window)) {
      showError('当前浏览器不支持语音播放');
      stopAllAudio();
      return;
    }
    try {
      window.speechSynthesis.cancel();
      const utter = new SpeechSynthesisUtterance(text);
      utter.lang = language || 'zh-CN';
      utter.rate = speechRate;
      playModeRef.current = mode;
      if (mode === 'segment') setIsSpeaking(true);
      else setSelectionAudioPlaying(true);
      utter.onend = () => {
        if (playModeRef.current !== mode) return;
        if (mode === 'segment') onSegmentEndedRef.current();
        else onSelectionEndedRef.current();
      };
      utter.onerror = () => {
        if (mode === 'segment') setIsSpeaking(false);
        else setSelectionAudioPlaying(false);
      };
      window.speechSynthesis.speak(utter);
    } catch {
      if (mode === 'segment') setIsSpeaking(false);
      else setSelectionAudioPlaying(false);
    }
  };

  const playAudioUrlOrFallback = async (
    audioUrl: string | null,
    fallbackText: string,
    fallbackLang: string,
    mode: 'selection' | 'segment',
  ) => {
    stopAllAudio();
    playModeRef.current = mode;
    if (mode === 'segment') setIsSpeaking(true);
    else setSelectionAudioPlaying(true);

    if (audioUrl && audioRef.current) {
      try {
        audioRef.current.src = audioUrl;
        audioRef.current.playbackRate = speechRate;
        audioRef.current.onerror = () => {
          if (playModeRef.current === mode) {
            speakViaBrowserTts(fallbackText, fallbackLang, mode);
          }
        };
        await audioRef.current.play();
        return;
      } catch {
        // fallback to Web Speech API
      }
    }
    speakViaBrowserTts(fallbackText, fallbackLang, mode);
  };

  const playSegmentAt = async (
    index: number,
    segmentsList: SpeechSegment[] = speechSegments,
    syncPageOverride?: boolean,
  ) => {
    const seg = segmentsList[index];
    if (!seg) return;
    setActiveSegmentIndex(index);
    const shouldSync = syncPageOverride ?? autoSyncPage;
    if (shouldSync && seg.page_number) {
      setJumpPage({ page: seg.page_number, seq: Date.now() });
    }
    const nextSeg = segmentsList[index + 1];
    if (nextSeg?.audio_url) {
      fetch(nextSeg.audio_url).catch(() => {});
    }
    await playAudioUrlOrFallback(seg.audio_url, seg.spoken_text, seg.language, 'segment');
  };

  onSegmentEndedRef.current = () => {
    if (activeSegmentIndex !== null && activeSegmentIndex + 1 < speechSegments.length) {
      void playSegmentAt(activeSegmentIndex + 1, speechSegments);
    } else {
      playModeRef.current = 'none';
      setIsSpeaking(false);
    }
  };

  onSelectionEndedRef.current = () => {
    playModeRef.current = 'none';
    setSelectionAudioPlaying(false);
  };

  const handleTogglePlayPause = () => {
    if (speechSegments.length === 0) return;
    if (isSpeaking) {
      if (audioRef.current && audioRef.current.src && !audioRef.current.paused) {
        audioRef.current.pause();
      } else if (typeof window !== 'undefined' && 'speechSynthesis' in window) {
        window.speechSynthesis.cancel();
      }
      setIsSpeaking(false);
      return;
    }
    if (
      activeSegmentIndex !== null &&
      playModeRef.current === 'segment' &&
      audioRef.current &&
      audioRef.current.src &&
      audioRef.current.paused
    ) {
      audioRef.current.playbackRate = speechRate;
      audioRef.current
        .play()
        .then(() => setIsSpeaking(true))
        .catch(() => void playSegmentAt(activeSegmentIndex));
      return;
    }
    void playSegmentAt(activeSegmentIndex ?? 0);
  };

  const handleStopSpeechPlayback = () => {
    stopAllAudio();
    setActiveSegmentIndex(null);
  };

  const handleGenerateSpeechScript = async (forceRegenerate = false): Promise<SpeechSegment[]> => {
    if (!activeSource) return [];
    setSpeechGenerating(true);
    try {
      const res = await api.generateSpeechScript(activeSource.id, selectedModel, forceRegenerate);
      setSpeechSegments(res.segments);
      if (forceRegenerate) {
        handleStopSpeechPlayback();
      }
      return res.segments;
    } catch (err) {
      showError(err instanceof Error ? err.message : '生成全文自然朗读脚本失败');
      return [];
    } finally {
      setSpeechGenerating(false);
    }
  };

  const { liveSessionSeconds } = useReadingTracker({
    source: activeSource,
    currentPage: activeReadingPos.page,
    scrollRatio: activeReadingPos.scrollRatio,
    isAudioPlaying: isSpeaking || selectionAudioPlaying,
    onStatsUpdated: (res) => {
      setActiveSource((prev) =>
        prev && prev.id === res.source_id
          ? {
              ...prev,
              reading_seconds: res.reading_seconds,
              reading_progress: res.reading_progress,
              last_read_at: res.last_read_at,
            }
          : prev,
      );
      setSources((prev) =>
        prev.map((s) =>
          s.id === res.source_id
            ? {
                ...s,
                reading_seconds: res.reading_seconds,
                reading_progress: res.reading_progress,
                last_read_at: res.last_read_at,
              }
            : s,
        ),
      );
    },
  });

  const openNotebook = async (nb: Notebook, targetSourceId?: string | null, targetHighlightId?: string | null, targetPage?: number | null) => {
    stopAllAudio();
    setSpeechSegments([]);
    setActiveSegmentIndex(null);
    setSelectionSpeech(null);
    setActiveNotebook(nb);
    setPendingSelection(null);
    setActiveHighlightId(null);
    const [srcList, convList] = await Promise.all([
      api.listSources(nb.id),
      api.listConversations(nb.id),
    ]);
    setSources(srcList);
    setConversations(convList);
    setActiveConvId(null);
    setChatMessages([]);

    let rememberedSourceId: string | null = null;
    try {
      const savedSid = localStorage.getItem(`inkmind.lastSource.${nb.id}`);
      if (savedSid && srcList.some((s) => s.id === savedSid)) {
        rememberedSourceId = savedSid;
      }
    } catch {
      // ignore storage errors
    }

    const chosenSourceId = targetSourceId || rememberedSourceId || srcList[0]?.id || null;
    if (chosenSourceId) {
      await loadSource(chosenSourceId, targetHighlightId || undefined, targetPage || undefined);
    } else {
      setActiveSource(null);
      setHighlights([]);
    }
  };

  const loadSource = async (sourceId: string, targetHighlightId?: string, targetPage?: number) => {
    stopAllAudio();
    setActiveSegmentIndex(null);
    setSelectionSpeech(null);
    setSuppressAutoRestore(Boolean(targetHighlightId || targetPage));
    const [detail, hls, speechRes] = await Promise.all([
      api.getSource(sourceId),
      api.listSourceHighlights(sourceId),
      api.getSpeechScript(sourceId).catch(() => ({ source_id: sourceId, source_title: '', segments: [] })),
    ]);
    try {
      localStorage.setItem(`inkmind.lastSource.${detail.notebook_id}`, detail.id);
    } catch {
      // ignore storage errors
    }
    setActiveSource(detail);
    setActiveReadingPos({
      page: targetPage || detail.last_page || 1,
      scrollRatio: detail.last_scroll_ratio || 0,
    });
    setHighlights(hls);
    setSpeechSegments(speechRes.segments || []);
    setPendingSelection(null);
    if (targetHighlightId) {
      setActiveHighlightId(targetHighlightId);
      setPulseHighlightId(targetHighlightId);
      setRightTab('remarks');
      setTimeout(() => setPulseHighlightId((cur) => (cur === targetHighlightId ? null : cur)), 2200);
    }
    if (targetPage) {
      setJumpPage({ page: targetPage, seq: Date.now() });
    } else {
      setJumpPage(null);
    }
  };

  const handleSaveReadingPosition = (sourceId: string, lastPage: number, lastScrollRatio: number) => {
    const cleanPage = Math.max(1, Math.round(lastPage));
    const cleanRatio = Math.max(0, Math.min(1, Number(lastScrollRatio.toFixed(4))));
    setActiveReadingPos({ page: cleanPage, scrollRatio: cleanRatio });
    try {
      localStorage.setItem(
        `inkmind.readPos.${sourceId}`,
        JSON.stringify({ page: cleanPage, scrollRatio: cleanRatio, ts: Date.now() }),
      );
    } catch {
      // ignore storage errors
    }
    const targetSrc = (activeSource?.id === sourceId ? activeSource : null) || sources.find((s) => s.id === sourceId);
    const computedProgress = targetSrc
      ? Math.max(
          targetSrc.reading_progress || 0,
          computeReadingProgress(
            targetSrc.type,
            targetSrc.page_count,
            cleanPage,
            cleanRatio,
            targetSrc.reading_seconds || 0,
          ),
        )
      : 0;
    setSources((prev) =>
      prev.map((s) =>
        s.id === sourceId
          ? {
              ...s,
              last_page: cleanPage,
              last_scroll_ratio: cleanRatio,
              reading_progress: Math.max(s.reading_progress || 0, computedProgress),
            }
          : s,
      ),
    );
    setActiveSource((prev) =>
      prev && prev.id === sourceId
        ? {
            ...prev,
            last_page: cleanPage,
            last_scroll_ratio: cleanRatio,
            reading_progress: Math.max(prev.reading_progress || 0, computedProgress),
          }
        : prev,
    );
    api
      .updateSource(sourceId, {
        last_page: cleanPage,
        last_scroll_ratio: cleanRatio,
        reading_progress: computedProgress,
      })
      .catch(() => {});
  };

  const handleCreateNotebook = async (payload: { title: string; description: string; color: string }) => {
    const created = await api.createNotebook(payload);
    await refreshNotebooks();
    await openNotebook(created);
  };

  const handleDeleteNotebook = async (id: string) => {
    await api.deleteNotebook(id);
    if (activeNotebook?.id === id) {
      setActiveNotebook(null);
      setActiveSource(null);
    }
    await Promise.all([refreshNotebooks(), refreshSystem()]);
  };

  const handleUploadFiles = async (files: File[]) => {
    if (!activeNotebook) return;
    const created = await api.uploadSources(activeNotebook.id, files);
    const [srcList] = await Promise.all([api.listSources(activeNotebook.id), refreshNotebooks(), refreshSystem()]);
    setSources(srcList);
    if (created[0]) {
      await loadSource(created[0].id);
    }
  };

  const handleImportUrl = async (url: string) => {
    if (!activeNotebook) return;
    const created = await api.importUrl(activeNotebook.id, url);
    const [srcList] = await Promise.all([api.listSources(activeNotebook.id), refreshNotebooks(), refreshSystem()]);
    setSources(srcList);
    await loadSource(created.id);
  };

  const handleDeleteSource = async (sourceId: string) => {
    if (!activeNotebook) return;
    await api.deleteSource(sourceId);
    const [srcList] = await Promise.all([api.listSources(activeNotebook.id), refreshNotebooks(), refreshSystem()]);
    setSources(srcList);
    if (activeSource?.id === sourceId) {
      if (srcList[0]) await loadSource(srcList[0].id);
      else {
        setActiveSource(null);
        setHighlights([]);
      }
    }
  };

  const handleUpdateSource = async (sourceId: string, patch: { title?: string; category?: string }) => {
    if (!activeNotebook) return;
    const updated = await api.updateSource(sourceId, patch);
    setSources((prev) =>
      prev.map((s) =>
        s.id === sourceId ? { ...s, title: updated.title, category: updated.category, updated_at: updated.updated_at } : s,
      ),
    );
    if (activeSource?.id === sourceId) {
      setActiveSource(updated);
    }
    refreshSystem().catch(() => {});
  };

  const handleClassifySources = async () => {
    if (!activeNotebook) return;
    const res = await api.classifySources(activeNotebook.id, selectedModel);
    setSources(res.sources);
    if (activeSource) {
      const matched = res.sources.find((s) => s.id === activeSource.id);
      if (matched) {
        setActiveSource((prev) => (prev ? { ...prev, category: matched.category } : prev));
      }
    }
  };

  const QUICK_TRANSLATE_PROMPT =
    '请将这段内容翻译成自然流畅的中文，并拆解解释其中的核心词汇、专业术语、固定搭配与关键语义。';

  const handleOcrHighlight = async (highlightId: string) => {
    setOcrLoadingMap((prev) => ({ ...prev, [highlightId]: true }));
    try {
      const updated = await api.ocrHighlight(highlightId, selectedModel);
      setHighlights((prev) => prev.map((h) => (h.id === highlightId ? updated : h)));
      refreshSystem().catch(() => {});
    } catch (err) {
      showError(err instanceof Error ? err.message : 'OCR 识别失败');
    } finally {
      setOcrLoadingMap((prev) => {
        const copy = { ...prev };
        delete copy[highlightId];
        return copy;
      });
    }
  };

  const handlePronounceSelection = async (sel: PendingSelection) => {
    if (!activeSource) return;
    const text = sel.quoteText.trim();
    if (!text) return;
    setRightTab('speech');
    setRightCollapsed(false);
    if (isCompactViewport) setMobileDrawer('right');
    setSelectionSpeechLoading(true);
    setSelectionSpeech(null);
    stopAllAudio();
    try {
      const res = await api.pronounceSelection({
        text,
        source_id: activeSource.id,
        page_number: sel.pageNumber,
        model: selectedModel,
        mode: 'natural',
      });
      setSelectionSpeech(res);
      setSelectionSpeechLoading(false);
      await playAudioUrlOrFallback(res.audio_url, res.spoken_text, res.language, 'selection');
    } catch (err) {
      setSelectionSpeechLoading(false);
      showError(err instanceof Error ? err.message : '选取发音生成失败');
    }
  };

  const handleReadFromSelection = async (sel: PendingSelection) => {
    if (!activeSource) return;
    const text = sel.quoteText.trim();
    if (!text) return;
    setRightTab('speech');
    setRightCollapsed(false);
    if (isCompactViewport) setMobileDrawer('right');

    let segs = speechSegments;
    if (segs.length === 0) {
      segs = await handleGenerateSpeechScript(false);
      if (segs.length === 0) return;
    }

    try {
      const hit = await api.locateSpeechSegment(activeSource.id, {
        text,
        page_number: sel.pageNumber,
      });
      await playSegmentAt(hit.segment_index, segs, true);
    } catch (err) {
      showError(err instanceof Error ? err.message : '定位朗读位置失败');
    }
  };

  const handleSelectionAction = async (mode: SelectionPopoverAction) => {
    if (!pendingSelection || !activeSource) return;
    if (mode === 'pronounce') {
      const sel = pendingSelection;
      setPendingSelection(null);
      window.getSelection()?.removeAllRanges();
      void handlePronounceSelection(sel);
      return;
    }
    if (mode === 'read_from_here') {
      const sel = pendingSelection;
      setPendingSelection(null);
      window.getSelection()?.removeAllRanges();
      void handleReadFromSelection(sel);
      return;
    }
    try {
      const created = await api.createHighlight(activeSource.id, {
        color: selectionColor,
        quote_text: pendingSelection.quoteText,
        note_text: '',
        anchor_type: pendingSelection.anchorType,
        page_number: pendingSelection.pageNumber,
        rects: pendingSelection.rects,
        start_offset: pendingSelection.startOffset,
        end_offset: pendingSelection.endOffset,
        prefix_text: pendingSelection.prefixText,
        suffix_text: pendingSelection.suffixText,
      });
      setHighlights((prev) => [...prev, created]);
      setActiveHighlightId(created.id);
      setRightTab('remarks');
      setPendingSelection(null);
      window.getSelection()?.removeAllRanges();
      if (mode === 'ocr') {
        setRightCollapsed(false);
        if (isCompactViewport) setMobileDrawer('right');
        void handleOcrHighlight(created.id);
      } else if (mode === 'translate') {
        setRightCollapsed(false);
        if (isCompactViewport) setMobileDrawer('right');
        void handleAskHighlight(created.id, QUICK_TRANSLATE_PROMPT, true, settings.cross_notebook_rag);
      } else if (mode !== 'highlight') {
        setRightCollapsed(false);
        if (isCompactViewport) setMobileDrawer('right');
        setFocusIntent({ highlightId: created.id, mode });
      }
      if (activeNotebook) {
        api.listSources(activeNotebook.id).then(setSources).catch(() => {});
      }
      refreshSystem().catch(() => {});
    } catch (err) {
      showError(err instanceof Error ? err.message : '创建高亮失败');
    }
  };

  const handleUpdateHighlight = async (id: string, patch: { color?: HighlightColor; note_text?: string }) => {
    const updated = await api.updateHighlight(id, patch);
    setHighlights((prev) => prev.map((h) => (h.id === id ? updated : h)));
    refreshSystem().catch(() => {});
  };

  const handleDeleteHighlight = async (id: string) => {
    await api.deleteHighlight(id);
    setHighlights((prev) => prev.filter((h) => h.id !== id));
    if (activeHighlightId === id) setActiveHighlightId(null);
    if (activeNotebook) {
      api.listSources(activeNotebook.id).then(setSources).catch(() => {});
    }
    refreshSystem().catch(() => {});
  };

  const handleAskHighlight = async (
    highlightId: string,
    question: string,
    useRag: boolean,
    crossNotebook: boolean,
  ) => {
    setHighlightStreams((prev) => ({
      ...prev,
      [highlightId]: { text: '', status: 'connecting_agy', memories: [] },
    }));

    const extra = quotedContext || undefined;
    if (quotedContext) setQuotedContext(null);

    await streamHighlightAsk(
      highlightId,
      {
        question,
        model: selectedModel,
        use_rag: useRag,
        cross_notebook_rag: crossNotebook,
        extra_context: extra,
      },
      {
        onRagContext: ({ user_message, memories }) => {
          setHighlights((prev) =>
            prev.map((h) => (h.id === highlightId ? { ...h, messages: [...h.messages, user_message] } : h)),
          );
          setHighlightStreams((prev) => ({
            ...prev,
            [highlightId]: { ...(prev[highlightId] || { text: '', status: 'generating' }), memories },
          }));
        },
        onStatus: (stage) => {
          setHighlightStreams((prev) => ({
            ...prev,
            [highlightId]: { ...(prev[highlightId] || { text: '', memories: [] }), status: stage },
          }));
        },
        onDelta: (delta) => {
          setHighlightStreams((prev) => {
            const cur = prev[highlightId] || { text: '', status: 'generating', memories: [] };
            return { ...prev, [highlightId]: { ...cur, status: 'generating', text: cur.text + delta } };
          });
        },
        onDone: ({ conversation_id, message }) => {
          setHighlights((prev) =>
            prev.map((h) =>
              h.id === highlightId
                ? { ...h, conversation_id, messages: [...h.messages, message] }
                : h,
            ),
          );
          setHighlightStreams((prev) => {
            const copy = { ...prev };
            delete copy[highlightId];
            return copy;
          });
          refreshSystem().catch(() => {});
          refreshNotebooks().catch(() => {});
        },
        onError: (msg) => {
          showError(msg);
          setHighlightStreams((prev) => {
            const copy = { ...prev };
            delete copy[highlightId];
            return copy;
          });
        },
      },
    );
  };

  const handleSelectConversation = async (convId: string) => {
    const detail = await api.getConversation(convId);
    setActiveConvId(detail.id);
    setChatMessages(detail.messages);
  };

  const handleDeleteConversation = async (convId: string) => {
    await api.deleteConversation(convId);
    if (activeConvId === convId) {
      setActiveConvId(null);
      setChatMessages([]);
    }
    if (activeNotebook) {
      api.listConversations(activeNotebook.id).then(setConversations).catch(() => {});
    }
    refreshSystem().catch(() => {});
  };

  const handleSendNotebookChat = async (message: string, useRag: boolean, crossNotebook: boolean) => {
    if (!activeNotebook) return;
    setChatStreaming(true);
    setChatStreamingText('');
    setChatStreamingStatus('connecting_agy');
    const extra = quotedContext || undefined;
    if (quotedContext) setQuotedContext(null);

    await streamNotebookChat(
      activeNotebook.id,
      {
        conversation_id: activeConvId,
        source_id: activeSource?.id || null,
        message,
        model: selectedModel,
        use_rag: useRag,
        cross_notebook_rag: crossNotebook,
        extra_context: extra,
      },
      {
        onRagContext: ({ conversation_id, user_message }) => {
          setActiveConvId(conversation_id);
          setChatMessages((prev) => [...prev, user_message]);
        },
        onStatus: (stage) => setChatStreamingStatus(stage),
        onDelta: (delta) => setChatStreamingText((prev) => prev + delta),
        onDone: ({ conversation_id, message: aiMsg }) => {
          setActiveConvId(conversation_id);
          setChatMessages((prev) => [...prev, aiMsg]);
          setChatStreaming(false);
          setChatStreamingText('');
          api.listConversations(activeNotebook.id).then(setConversations).catch(() => {});
          refreshSystem().catch(() => {});
          refreshNotebooks().catch(() => {});
        },
        onError: (msg) => {
          showError(msg);
          setChatStreaming(false);
          setChatStreamingText('');
        },
      },
    );
  };

  const handleJumpToMemory = async (mem: RagMemoryRef) => {
    setShowGlobalRag(false);
    let targetNb = notebooks.find((n) => n.id === mem.notebook_id) || null;
    if (!targetNb) {
      const fresh = await refreshNotebooks();
      targetNb = fresh.find((n) => n.id === mem.notebook_id) || null;
    }
    if (!targetNb) return;

    if (activeNotebook?.id !== targetNb.id) {
      await openNotebook(targetNb, mem.source_id, mem.highlight_id, mem.page_number);
    } else if (mem.source_id && activeSource?.id !== mem.source_id) {
      await loadSource(mem.source_id, mem.highlight_id || undefined, mem.page_number || undefined);
    } else {
      if (mem.highlight_id) {
        setActiveHighlightId(mem.highlight_id);
        setPulseHighlightId(mem.highlight_id);
        setRightTab('remarks');
        setRightCollapsed(false);
        if (isCompactViewport) setMobileDrawer('right');
        setTimeout(() => setPulseHighlightId((cur) => (cur === mem.highlight_id ? null : cur)), 2200);
      } else if (mem.conversation_id) {
        setRightTab('chat');
        setRightCollapsed(false);
        if (isCompactViewport) setMobileDrawer('right');
        await handleSelectConversation(mem.conversation_id);
      }
      if (mem.page_number) {
        setJumpPage({ page: mem.page_number, seq: Date.now() });
      }
    }
  };

  const handleQuoteMemory = (mem: RagMemoryRef) => {
    setQuotedContext(`[${mem.title}] ${mem.content.slice(0, 450)}`);
    setShowGlobalRag(false);
    if (activeNotebook) {
      setRightCollapsed(false);
      if (isCompactViewport) setMobileDrawer('right');
      setRightTab(activeHighlightId ? 'remarks' : 'chat');
    }
  };

  return (
    <div
      className="h-[100dvh] flex flex-col overflow-hidden bg-[#F8F6F1] text-[#1C1B18]"
      onMouseDown={() => {
        if (pendingSelection) setPendingSelection(null);
      }}
    >
      <Header
        activeNotebook={activeNotebook}
        onBackToNotebooks={() => {
          stopAllAudio();
          setSpeechSegments([]);
          setActiveSegmentIndex(null);
          setSelectionSpeech(null);
          setActiveNotebook(null);
          setActiveSource(null);
          setPendingSelection(null);
          setMobileDrawer('none');
          refreshNotebooks().catch(() => {});
        }}
        models={models}
        selectedModel={selectedModel}
        onSelectModel={handleSelectModel}
        health={health}
        leftCollapsed={leftCollapsed}
        rightCollapsed={rightCollapsed}
        einkMode={einkMode}
        onToggleLeft={() => setLeftCollapsed((v) => !v)}
        onToggleRight={() => setRightCollapsed((v) => !v)}
        onToggleFocusMode={toggleFocusMode}
        onToggleEinkMode={() => setEinkMode((v) => !v)}
        onOpenAnalytics={() => setShowAnalytics(true)}
        onOpenGlobalRag={() => setShowGlobalRag(true)}
        onOpenSettings={() => setShowSettings(true)}
      />

      {toastError && (
        <div className="fixed bottom-4 right-4 z-50 max-w-md bg-rose-700 text-white px-4 py-2.5 rounded-xl shadow-lg text-xs flex items-center gap-2">
          <span className="flex-1">{toastError}</span>
          <button onClick={() => setToastError(null)} className="opacity-80 hover:opacity-100 cursor-pointer">
            <X className="w-4 h-4" />
          </button>
        </div>
      )}

      {!activeNotebook ? (
        <NotebookGrid
          notebooks={notebooks}
          onOpenNotebook={(nb) => openNotebook(nb)}
          onCreateNotebook={handleCreateNotebook}
          onDeleteNotebook={handleDeleteNotebook}
          onJumpToRagMemory={handleJumpToMemory}
          onOpenAnalytics={() => setShowAnalytics(true)}
        />
      ) : (
        <div className="flex-1 flex flex-col min-h-0 overflow-hidden">
          <main className="flex-1 flex min-h-0 overflow-hidden relative">
            {/* Desktop Left Sidebar */}
            {!isCompactViewport && !leftCollapsed && (
              <SourceSidebar
                sources={sources}
                activeSourceId={activeSource?.id || null}
                onSelectSource={(sid) => loadSource(sid)}
                onUploadFiles={handleUploadFiles}
                onImportUrl={handleImportUrl}
                onUpdateSource={handleUpdateSource}
                onClassifySources={handleClassifySources}
                onDeleteSource={handleDeleteSource}
                onOpenAnalytics={() => setShowAnalytics(true)}
                onCollapse={() => setLeftCollapsed(true)}
              />
            )}

            {/* Mobile/Tablet Left Drawer */}
            {isCompactViewport && mobileDrawer === 'sources' && (
              <div className="fixed inset-0 z-40 flex">
                <div
                  className="fixed inset-0 bg-black/40"
                  onClick={() => setMobileDrawer('none')}
                />
                <div className="relative z-10 h-full">
                  <SourceSidebar
                    className="w-[85vw] max-w-[320px] shadow-2xl"
                    sources={sources}
                    activeSourceId={activeSource?.id || null}
                    onSelectSource={(sid) => {
                      loadSource(sid);
                      setMobileDrawer('none');
                    }}
                    onUploadFiles={handleUploadFiles}
                    onImportUrl={handleImportUrl}
                    onUpdateSource={handleUpdateSource}
                    onClassifySources={handleClassifySources}
                    onDeleteSource={handleDeleteSource}
                    onOpenAnalytics={() => {
                      setMobileDrawer('none');
                      setShowAnalytics(true);
                    }}
                    onCollapse={() => setMobileDrawer('none')}
                  />
                </div>
              </div>
            )}

            {/* Center Document / PDF Reader */}
            <section className="flex-1 flex flex-col min-w-0 min-h-0 relative">
              {!isCompactViewport && leftCollapsed && (
                <button
                  type="button"
                  onClick={() => setLeftCollapsed(false)}
                  title="展开素材栏 (⌘[)"
                  className="absolute left-2.5 top-13 z-20 inline-flex items-center gap-1 px-2 py-1.5 rounded-lg bg-white/95 backdrop-blur border border-[#E5E0D5] shadow-sm text-xs font-medium text-[#5E5B53] hover:text-[#1C1B18] hover:border-[#B45309] transition cursor-pointer eink-surface"
                >
                  <PanelLeftOpen className="w-3.5 h-3.5 text-[#B45309]" />
                  <span className="hidden sm:inline">素材</span>
                </button>
              )}

              {!isCompactViewport && rightCollapsed && (
                <button
                  type="button"
                  onClick={() => setRightCollapsed(false)}
                  title="展开笔记与 AI 栏 (⌘])"
                  className="absolute right-2.5 top-13 z-20 inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg bg-white/95 backdrop-blur border border-[#E5E0D5] shadow-sm text-xs font-medium text-[#5E5B53] hover:text-[#1C1B18] hover:border-[#B45309] transition cursor-pointer eink-surface"
                >
                  <PanelRightOpen className="w-3.5 h-3.5 text-[#B45309]" />
                  <span className="hidden sm:inline">笔记 / AI</span>
                  {highlights.length > 0 && (
                    <span className="px-1.5 py-0.2 rounded-full bg-[#B45309]/10 text-[#B45309] text-[10px] font-semibold">
                      {highlights.length}
                    </span>
                  )}
                </button>
              )}

              {!activeSource ? (
                <div className="flex-1 flex items-center justify-center p-8 text-center text-sm text-[#5E5B53]">
                  <div className="space-y-3">
                    <p className="font-medium text-[#1C1B18]">请先在素材栏拖入 PDF / Word 或导入文章网址</p>
                    <p className="text-xs">支持 PDF 划词与框选高亮、翻译解释、AI 问答与历史对话 RAG 记忆检索。</p>
                    {isCompactViewport && (
                      <button
                        type="button"
                        onClick={() => setMobileDrawer('sources')}
                        className="inline-flex items-center gap-1.5 px-4 py-2 rounded-xl bg-[#B45309] text-white text-xs font-medium cursor-pointer"
                      >
                        <FileText className="w-3.5 h-3.5" />
                        <span>打开素材栏导入</span>
                      </button>
                    )}
                  </div>
                </div>
              ) : activeSource.type === 'pdf' ? (
                <PdfViewer
                  source={activeSource}
                  highlights={highlights}
                  activeHighlightId={activeHighlightId}
                  pulseHighlightId={pulseHighlightId}
                  jumpPage={jumpPage}
                  einkMode={einkMode}
                  suppressAutoRestore={suppressAutoRestore}
                  liveSessionSeconds={liveSessionSeconds}
                  onSaveReadingPosition={handleSaveReadingPosition}
                  onSelectHighlight={(hlId) => {
                    setActiveHighlightId(hlId);
                    setRightTab('remarks');
                    setRightCollapsed(false);
                    if (isCompactViewport) setMobileDrawer('right');
                  }}
                  onSelectionMade={setPendingSelection}
                />
              ) : (
                <DocumentViewer
                  source={activeSource}
                  highlights={highlights}
                  activeHighlightId={activeHighlightId}
                  pulseHighlightId={pulseHighlightId}
                  expandedReading={leftCollapsed || rightCollapsed}
                  fullWidthReading={leftCollapsed && rightCollapsed}
                  einkMode={einkMode}
                  suppressAutoRestore={suppressAutoRestore}
                  liveSessionSeconds={liveSessionSeconds}
                  onSaveReadingPosition={handleSaveReadingPosition}
                  onSelectHighlight={(hlId) => {
                    setActiveHighlightId(hlId);
                    setRightTab('remarks');
                    setRightCollapsed(false);
                    if (isCompactViewport) setMobileDrawer('right');
                  }}
                  onTextSelected={setPendingSelection}
                />
              )}

              {/* Compact Sticky Mini Audio Player Bar */}
              {activeSource && (isSpeaking || activeSegmentIndex !== null || selectionSpeechLoading || selectionAudioPlaying) && (
                <div className="shrink-0 border-t border-[#E5E0D5] bg-white/95 backdrop-blur px-3 py-2 flex items-center justify-between gap-2 z-20 eink-surface">
                  <div className="flex items-center gap-2 min-w-0 flex-1">
                    {selectionSpeechLoading ? (
                      <>
                        <Loader2 className="w-4 h-4 animate-spin text-teal-600 shrink-0" />
                        <span className="text-xs text-[#5E5B53] truncate">正在生成选中发音文本与音频...</span>
                      </>
                    ) : selectionAudioPlaying && selectionSpeech ? (
                      <>
                        <Volume2 className="w-4 h-4 text-teal-600 animate-pulse shrink-0" />
                        <span className="text-xs text-[#1C1B18] truncate font-medium">
                          发音：{selectionSpeech.spoken_text}
                        </span>
                      </>
                    ) : (
                      <>
                        <AudioLines className={`w-4 h-4 shrink-0 ${isSpeaking ? 'text-[#B45309] animate-pulse' : 'text-[#8E897E]'}`} />
                        <div className="min-w-0 flex-1">
                          <div className="text-xs font-medium text-[#1C1B18] truncate">
                            {activeSegmentIndex !== null && speechSegments[activeSegmentIndex]
                              ? speechSegments[activeSegmentIndex].spoken_text
                              : '全文自然朗读'}
                          </div>
                          {activeSegmentIndex !== null && speechSegments[activeSegmentIndex] && (
                            <div className="text-[10px] text-[#8E897E]">
                              第 {activeSegmentIndex + 1}/{speechSegments.length} 段 · P.{speechSegments[activeSegmentIndex].page_number} · {speechRate}x
                            </div>
                          )}
                        </div>
                      </>
                    )}
                  </div>

                  <div className="flex items-center gap-1 shrink-0">
                    {selectionAudioPlaying ? (
                      <button
                        type="button"
                        onClick={stopAllAudio}
                        className="p-1.5 rounded-lg bg-teal-600 text-white cursor-pointer"
                        title="停止发音"
                      >
                        <Square className="w-3.5 h-3.5 fill-current" />
                      </button>
                    ) : activeSegmentIndex !== null && speechSegments.length > 0 ? (
                      <>
                        <button
                          type="button"
                          onClick={() => {
                            if (activeSegmentIndex > 0) void playSegmentAt(activeSegmentIndex - 1);
                          }}
                          disabled={activeSegmentIndex <= 0}
                          className="p-1.5 rounded-lg text-[#5E5B53] hover:bg-[#EFECE5] disabled:opacity-40 cursor-pointer"
                          title="上一段"
                        >
                          <SkipBack className="w-3.5 h-3.5" />
                        </button>
                        <button
                          type="button"
                          onClick={handleTogglePlayPause}
                          className="p-1.5 rounded-lg bg-[#B45309] text-white cursor-pointer"
                          title={isSpeaking ? '暂停' : '继续'}
                        >
                          {isSpeaking ? <Pause className="w-3.5 h-3.5 fill-current" /> : <Play className="w-3.5 h-3.5 fill-current" />}
                        </button>
                        <button
                          type="button"
                          onClick={() => {
                            if (activeSegmentIndex + 1 < speechSegments.length) void playSegmentAt(activeSegmentIndex + 1);
                          }}
                          disabled={activeSegmentIndex >= speechSegments.length - 1}
                          className="p-1.5 rounded-lg text-[#5E5B53] hover:bg-[#EFECE5] disabled:opacity-40 cursor-pointer"
                          title="下一段"
                        >
                          <SkipForward className="w-3.5 h-3.5" />
                        </button>
                        <button
                          type="button"
                          onClick={handleStopSpeechPlayback}
                          className="p-1.5 rounded-lg text-[#5E5B53] hover:text-rose-700 cursor-pointer"
                          title="停止朗读"
                        >
                          <X className="w-3.5 h-3.5" />
                        </button>
                      </>
                    ) : null}

                    <button
                      type="button"
                      onClick={() => {
                        setRightTab('speech');
                        setRightCollapsed(false);
                        if (isCompactViewport) setMobileDrawer('right');
                      }}
                      className="px-2 py-1 rounded-lg border border-[#E5E0D5] text-[11px] font-medium text-[#5E5B53] hover:text-[#1C1B18] cursor-pointer"
                    >
                      朗读文本
                    </button>
                  </div>
                </div>
              )}
            </section>

            {/* Right Panel Backdrop for Mobile/Tablet */}
            {isCompactViewport && mobileDrawer === 'right' && (
              <div
                className="fixed inset-0 z-30 bg-black/40"
                onClick={() => setMobileDrawer('none')}
              />
            )}

            {/* Right Panel: Remarks / Chat / RAG */}
            {((!isCompactViewport && !rightCollapsed) || (isCompactViewport && mobileDrawer === 'right')) && (
              <aside
                className={
                  isCompactViewport
                    ? 'fixed inset-x-0 bottom-0 h-[82dvh] rounded-t-2xl sm:inset-y-0 sm:left-auto sm:right-0 sm:h-full sm:w-[88vw] sm:max-w-[430px] sm:rounded-none z-40 border-t sm:border-t-0 sm:border-l border-[#E5E0D5] bg-white flex flex-col min-h-0 shadow-2xl eink-surface'
                    : 'w-[380px] xl:w-[420px] shrink-0 border-l border-[#E5E0D5] bg-white flex flex-col min-h-0 eink-surface'
                }
              >
                <div className="h-11 shrink-0 border-b border-[#E5E0D5] px-1.5 flex items-center gap-1 bg-[#FAF8F4] eink-surface">
                  <button
                    onClick={() => setRightTab('remarks')}
                    className={`flex-1 inline-flex items-center justify-center gap-1 py-1.5 px-1 rounded-lg text-xs font-medium cursor-pointer transition ${
                      rightTab === 'remarks'
                        ? 'bg-white text-[#B45309] shadow-xs border border-[#E5E0D5]'
                        : 'text-[#5E5B53] hover:text-[#1C1B18]'
                    }`}
                  >
                    <NotebookPen className="w-3.5 h-3.5 shrink-0" />
                    <span className="truncate">备注 ({highlights.length})</span>
                  </button>

                  <button
                    onClick={() => setRightTab('chat')}
                    className={`flex-1 inline-flex items-center justify-center gap-1 py-1.5 px-1 rounded-lg text-xs font-medium cursor-pointer transition ${
                      rightTab === 'chat'
                        ? 'bg-white text-[#B45309] shadow-xs border border-[#E5E0D5]'
                        : 'text-[#5E5B53] hover:text-[#1C1B18]'
                    }`}
                  >
                    <MessageSquare className="w-3.5 h-3.5 shrink-0" />
                    <span className="truncate">对话</span>
                  </button>

                  <button
                    onClick={() => setRightTab('speech')}
                    className={`flex-1 inline-flex items-center justify-center gap-1 py-1.5 px-1 rounded-lg text-xs font-medium cursor-pointer transition ${
                      rightTab === 'speech'
                        ? 'bg-white text-[#B45309] shadow-xs border border-[#E5E0D5]'
                        : 'text-[#5E5B53] hover:text-[#1C1B18]'
                    }`}
                  >
                    <AudioLines className="w-3.5 h-3.5 shrink-0" />
                    <span className="truncate">语音{speechSegments.length > 0 ? ` (${speechSegments.length})` : ''}</span>
                  </button>

                  <button
                    onClick={() => setRightTab('rag')}
                    className={`flex-1 inline-flex items-center justify-center gap-1 py-1.5 px-1 rounded-lg text-xs font-medium cursor-pointer transition ${
                      rightTab === 'rag'
                        ? 'bg-white text-[#B45309] shadow-xs border border-[#E5E0D5]'
                        : 'text-[#5E5B53] hover:text-[#1C1B18]'
                    }`}
                  >
                    <Database className="w-3.5 h-3.5 shrink-0" />
                    <span className="truncate">RAG</span>
                  </button>

                  <button
                    type="button"
                    onClick={() => {
                      if (isCompactViewport) setMobileDrawer('none');
                      else setRightCollapsed(true);
                    }}
                    title={isCompactViewport ? '关闭面板' : '收起右侧笔记栏 (⌘])'}
                    className="p-1.5 rounded-lg text-[#5E5B53] hover:text-[#1C1B18] hover:bg-[#EFECE5] cursor-pointer shrink-0"
                  >
                    {isCompactViewport ? <X className="w-4 h-4" /> : <PanelRightClose className="w-3.5 h-3.5" />}
                  </button>
                </div>

                <div className="flex-1 min-h-0">
                  {rightTab === 'remarks' && (
                    <RemarksPanel
                      highlights={highlights}
                      activeHighlightId={activeHighlightId}
                      focusIntent={focusIntent}
                      streamingMap={highlightStreams}
                      ocrLoadingMap={ocrLoadingMap}
                      quotedContext={quotedContext}
                      models={models}
                      selectedModel={selectedModel}
                      onSelectModel={handleSelectModel}
                      onClearQuotedContext={() => setQuotedContext(null)}
                      onSelectHighlight={(hl) => {
                        setActiveHighlightId(hl.id);
                        setPulseHighlightId(hl.id);
                        setJumpPage({ page: hl.page_number, seq: Date.now() });
                        if (isCompactViewport) setMobileDrawer('none');
                        setTimeout(() => setPulseHighlightId((cur) => (cur === hl.id ? null : cur)), 1800);
                      }}
                      onUpdateHighlight={handleUpdateHighlight}
                      onDeleteHighlight={handleDeleteHighlight}
                      onAskHighlight={handleAskHighlight}
                      onOcrHighlight={handleOcrHighlight}
                      onJumpToMemory={handleJumpToMemory}
                    />
                  )}

                  {rightTab === 'chat' && (
                    <NotebookChatPanel
                      conversations={conversations}
                      activeConversationId={activeConvId}
                      messages={chatMessages}
                      streamingText={chatStreamingText}
                      streamingStatus={chatStreamingStatus}
                      streamingActive={chatStreaming}
                      quotedContext={quotedContext}
                      models={models}
                      selectedModel={selectedModel}
                      onSelectModel={handleSelectModel}
                      onClearQuotedContext={() => setQuotedContext(null)}
                      onSelectConversation={handleSelectConversation}
                      onNewConversation={() => {
                        setActiveConvId(null);
                        setChatMessages([]);
                      }}
                      onDeleteConversation={handleDeleteConversation}
                      onSendMessage={handleSendNotebookChat}
                      onJumpToMemory={handleJumpToMemory}
                    />
                  )}

                  {rightTab === 'speech' && (
                    <SpeechPanel
                      hasSource={Boolean(activeSource)}
                      sourceTitle={activeSource?.title || ''}
                      segments={speechSegments}
                      generating={speechGenerating}
                      activeSegmentIndex={activeSegmentIndex}
                      isPlaying={isSpeaking}
                      speechRate={speechRate}
                      autoSyncPage={autoSyncPage}
                      models={models}
                      selectedModel={selectedModel}
                      selectionSpeech={selectionSpeech}
                      selectionSpeechLoading={selectionSpeechLoading}
                      selectionPlaying={selectionAudioPlaying}
                      onSelectModel={handleSelectModel}
                      onGenerateScript={(force) => {
                        void handleGenerateSpeechScript(Boolean(force));
                      }}
                      onPlaySegment={(idx) => {
                        void playSegmentAt(idx);
                      }}
                      onTogglePlayPause={handleTogglePlayPause}
                      onPrevSegment={() => {
                        if (activeSegmentIndex !== null && activeSegmentIndex > 0) {
                          void playSegmentAt(activeSegmentIndex - 1);
                        }
                      }}
                      onNextSegment={() => {
                        if (activeSegmentIndex !== null && activeSegmentIndex + 1 < speechSegments.length) {
                          void playSegmentAt(activeSegmentIndex + 1);
                        }
                      }}
                      onStopPlayback={handleStopSpeechPlayback}
                      onChangeRate={setSpeechRate}
                      onToggleAutoSyncPage={setAutoSyncPage}
                      onReplaySelectionSpeech={() => {
                        if (selectionSpeech) {
                          void playAudioUrlOrFallback(
                            selectionSpeech.audio_url,
                            selectionSpeech.spoken_text,
                            selectionSpeech.language,
                            'selection',
                          );
                        }
                      }}
                      onStopSelectionSpeech={stopAllAudio}
                      onClearSelectionSpeech={() => {
                        if (selectionAudioPlaying) stopAllAudio();
                        setSelectionSpeech(null);
                      }}
                    />
                  )}

                  {rightTab === 'rag' && (
                    <RagExplorerPanel
                      notebookId={activeNotebook.id}
                      ragStats={ragStats}
                      onJumpToMemory={handleJumpToMemory}
                      onQuoteMemory={handleQuoteMemory}
                      onReindex={async () => {
                        const st = await api.ragReindex();
                        setRagStats(st);
                      }}
                    />
                  )}
                </div>
              </aside>
            )}
          </main>

          {/* Mobile / Tablet Bottom Navigation Bar */}
          {isCompactViewport && (
            <nav className="lg:hidden shrink-0 border-t border-[#E5E0D5] bg-white safe-pb grid grid-cols-5 z-20 eink-surface">
              <button
                type="button"
                onClick={() => setMobileDrawer((d) => (d === 'sources' ? 'none' : 'sources'))}
                className={`min-h-[50px] flex flex-col items-center justify-center gap-0.5 text-[11px] font-medium cursor-pointer ${
                  mobileDrawer === 'sources' ? 'text-[#B45309]' : 'text-[#5E5B53]'
                }`}
              >
                <FileText className="w-4 h-4" />
                <span>素材 ({sources.length})</span>
              </button>

              <button
                type="button"
                onClick={() => setMobileDrawer('none')}
                className={`min-h-[50px] flex flex-col items-center justify-center gap-0.5 text-[11px] font-medium cursor-pointer ${
                  mobileDrawer === 'none' ? 'text-[#B45309]' : 'text-[#5E5B53]'
                }`}
              >
                <BookOpen className="w-4 h-4" />
                <span>阅读</span>
              </button>

              <button
                type="button"
                onClick={() => {
                  if (mobileDrawer === 'right' && (rightTab === 'remarks' || rightTab === 'chat')) {
                    setMobileDrawer('none');
                  } else {
                    if (rightTab === 'rag' || rightTab === 'speech') setRightTab('remarks');
                    setMobileDrawer('right');
                  }
                }}
                className={`min-h-[50px] flex flex-col items-center justify-center gap-0.5 text-[11px] font-medium cursor-pointer ${
                  mobileDrawer === 'right' && (rightTab === 'remarks' || rightTab === 'chat')
                    ? 'text-[#B45309]'
                    : 'text-[#5E5B53]'
                }`}
              >
                <NotebookPen className="w-4 h-4" />
                <span>笔记/AI{highlights.length > 0 ? ` (${highlights.length})` : ''}</span>
              </button>

              <button
                type="button"
                onClick={() => {
                  if (mobileDrawer === 'right' && rightTab === 'speech') {
                    setMobileDrawer('none');
                  } else {
                    setRightTab('speech');
                    setMobileDrawer('right');
                  }
                }}
                className={`min-h-[50px] flex flex-col items-center justify-center gap-0.5 text-[11px] font-medium cursor-pointer ${
                  mobileDrawer === 'right' && rightTab === 'speech' ? 'text-[#B45309]' : 'text-[#5E5B53]'
                }`}
              >
                <AudioLines className="w-4 h-4" />
                <span>语音</span>
              </button>

              <button
                type="button"
                onClick={() => {
                  if (mobileDrawer === 'right' && rightTab === 'rag') {
                    setMobileDrawer('none');
                  } else {
                    setRightTab('rag');
                    setMobileDrawer('right');
                  }
                }}
                className={`min-h-[50px] flex flex-col items-center justify-center gap-0.5 text-[11px] font-medium cursor-pointer ${
                  mobileDrawer === 'right' && rightTab === 'rag' ? 'text-[#B45309]' : 'text-[#5E5B53]'
                }`}
              >
                <Database className="w-4 h-4" />
                <span>RAG</span>
              </button>
            </nav>
          )}
        </div>
      )}

      {pendingSelection && (
        <SelectionPopover
          selection={pendingSelection}
          selectedColor={selectionColor}
          hasSpeechScript={speechSegments.length > 0}
          onChangeColor={setSelectionColor}
          onAction={handleSelectionAction}
          onClose={() => setPendingSelection(null)}
        />
      )}

      {showSettings && (
        <SettingsModal
          settings={settings}
          models={models}
          ragStats={ragStats}
          onSave={async (patch) => {
            const updated = await api.updateSettings(patch);
            setSettings(updated);
            setSelectedModel(updated.default_model);
            try {
              localStorage.setItem(LAST_MODEL_STORAGE_KEY, updated.default_model);
            } catch {
              // ignore storage errors
            }
            await refreshSystem(false);
          }}
          onReindexRag={async () => {
            const st = await api.ragReindex();
            setRagStats(st);
          }}
          onClose={() => setShowSettings(false)}
        />
      )}

      {showAnalytics && (
        <ReadingAnalyticsModal
          activeNotebook={activeNotebook}
          models={models}
          selectedModel={selectedModel}
          onSelectModel={handleSelectModel}
          onJumpToSource={async (notebookId, sourceId) => {
            let targetNb = notebooks.find((n) => n.id === notebookId) || null;
            if (!targetNb) {
              const fresh = await refreshNotebooks();
              targetNb = fresh.find((n) => n.id === notebookId) || null;
            }
            if (!targetNb) return;
            if (activeNotebook?.id !== targetNb.id) {
              await openNotebook(targetNb, sourceId);
            } else if (activeSource?.id !== sourceId) {
              await loadSource(sourceId);
            }
          }}
          onClose={() => setShowAnalytics(false)}
        />
      )}

      {showGlobalRag && (
        <div className="fixed inset-0 z-50 bg-black/40 backdrop-blur-xs flex items-center justify-center p-4">
          <div className="bg-white border border-[#E5E0D5] rounded-2xl shadow-2xl w-full max-w-2xl h-[75vh] flex flex-col overflow-hidden">
            <div className="px-4 py-3 border-b border-[#E5E0D5] flex items-center justify-between">
              <div className="flex items-center gap-2 text-sm font-semibold text-[#1C1B18]">
                <Database className="w-4 h-4 text-[#B45309]" />
                <span>全局 RAG 历史对话与笔记记忆库</span>
              </div>
              <button
                onClick={() => setShowGlobalRag(false)}
                className="p-1.5 rounded-lg text-[#5E5B53] hover:bg-[#EFECE5] cursor-pointer"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
            <div className="flex-1 min-h-0">
              <RagExplorerPanel
                notebookId={activeNotebook?.id}
                ragStats={ragStats}
                onJumpToMemory={handleJumpToMemory}
                onQuoteMemory={handleQuoteMemory}
                onReindex={async () => {
                  const st = await api.ragReindex();
                  setRagStats(st);
                }}
              />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
