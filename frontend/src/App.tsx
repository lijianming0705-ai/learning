import { useEffect, useState } from 'react';
import {
  Database,
  MessageSquare,
  NotebookPen,
  PanelLeftOpen,
  PanelRightClose,
  PanelRightOpen,
  X,
} from 'lucide-react';
import { api, streamHighlightAsk, streamNotebookChat } from './api/client';
import { DocumentViewer } from './components/DocumentViewer';
import { Header } from './components/Header';
import { NotebookChatPanel } from './components/NotebookChatPanel';
import { NotebookGrid } from './components/NotebookGrid';
import { PdfViewer } from './components/PdfViewer';
import { RagExplorerPanel } from './components/RagExplorerPanel';
import { RemarksPanel } from './components/RemarksPanel';
import { SelectionPopover } from './components/SelectionPopover';
import { SettingsModal } from './components/SettingsModal';
import { SourceSidebar } from './components/SourceSidebar';
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
  SourceDetail,
  SourceSummary,
} from './types';

type RightTab = 'remarks' | 'chat' | 'rag';

export default function App() {
  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [models, setModels] = useState<AgyModel[]>([]);
  const [settings, setSettings] = useState<AppSettings>({
    default_model: 'gemini-3.8-flash-low',
    rag_top_k: 5,
    cross_notebook_rag: true,
    embedding_provider: 'hybrid-local',
    ollama_embedding_model: '',
  });
  const [selectedModel, setSelectedModel] = useState('gemini-3.8-flash-low');
  const [ragStats, setRagStats] = useState<RagStats | null>(null);

  const [notebooks, setNotebooks] = useState<Notebook[]>([]);
  const [activeNotebook, setActiveNotebook] = useState<Notebook | null>(null);

  const [sources, setSources] = useState<SourceSummary[]>([]);
  const [activeSource, setActiveSource] = useState<SourceDetail | null>(null);
  const [highlights, setHighlights] = useState<Highlight[]>([]);
  const [activeHighlightId, setActiveHighlightId] = useState<string | null>(null);
  const [pulseHighlightId, setPulseHighlightId] = useState<string | null>(null);
  const [jumpPage, setJumpPage] = useState<{ page: number; seq: number } | null>(null);

  const [pendingSelection, setPendingSelection] = useState<PendingSelection | null>(null);
  const [selectionColor, setSelectionColor] = useState<HighlightColor>('amber');
  const [focusIntent, setFocusIntent] = useState<{ highlightId: string; mode: 'note' | 'ask' } | null>(null);

  const [rightTab, setRightTab] = useState<RightTab>('remarks');
  const [highlightStreams, setHighlightStreams] = useState<
    Record<string, { text: string; status: string; memories: RagMemoryRef[] }>
  >({});

  // Notebook chat state
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [activeConvId, setActiveConvId] = useState<string | null>(null);
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
  const [chatStreamingText, setChatStreamingText] = useState('');
  const [chatStreamingStatus, setChatStreamingStatus] = useState('');
  const [chatStreaming, setChatStreaming] = useState(false);

  // Quoted memory + modals + layout collapse
  const [quotedContext, setQuotedContext] = useState<string | null>(null);
  const [showSettings, setShowSettings] = useState(false);
  const [showGlobalRag, setShowGlobalRag] = useState(false);
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

  const refreshSystem = async () => {
    const [h, m, s, st] = await Promise.all([
      api.health(),
      api.listModels().catch(() => []),
      api.getSettings(),
      api.ragStats(),
    ]);
    setHealth(h);
    setModels(m);
    setSettings(s);
    setSelectedModel(s.default_model);
    setRagStats(st);
  };

  const refreshNotebooks = async () => {
    const nbs = await api.listNotebooks();
    setNotebooks(nbs);
    return nbs;
  };

  useEffect(() => {
    refreshSystem().catch(() => {});
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

  const openNotebook = async (nb: Notebook, targetSourceId?: string | null, targetHighlightId?: string | null, targetPage?: number | null) => {
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

    const chosenSourceId = targetSourceId || srcList[0]?.id || null;
    if (chosenSourceId) {
      await loadSource(chosenSourceId, targetHighlightId || undefined, targetPage || undefined);
    } else {
      setActiveSource(null);
      setHighlights([]);
    }
  };

  const loadSource = async (sourceId: string, targetHighlightId?: string, targetPage?: number) => {
    const [detail, hls] = await Promise.all([
      api.getSource(sourceId),
      api.listSourceHighlights(sourceId),
    ]);
    setActiveSource(detail);
    setHighlights(hls);
    setPendingSelection(null);
    if (targetHighlightId) {
      setActiveHighlightId(targetHighlightId);
      setPulseHighlightId(targetHighlightId);
      setRightTab('remarks');
      setTimeout(() => setPulseHighlightId((cur) => (cur === targetHighlightId ? null : cur)), 2200);
    }
    if (targetPage) {
      setJumpPage({ page: targetPage, seq: Date.now() });
    }
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

  const QUICK_TRANSLATE_PROMPT =
    '请将这段划线英文翻译成自然流畅的中文，并拆解解释其中的核心词汇、专业术语、固定搭配与句式结构。';

  const handleSelectionAction = async (mode: 'highlight' | 'note' | 'ask' | 'translate') => {
    if (!pendingSelection || !activeSource) return;
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
      if (mode === 'translate') {
        setRightCollapsed(false);
        void handleAskHighlight(created.id, QUICK_TRANSLATE_PROMPT, true, settings.cross_notebook_rag);
      } else if (mode !== 'highlight') {
        setRightCollapsed(false);
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
        setTimeout(() => setPulseHighlightId((cur) => (cur === mem.highlight_id ? null : cur)), 2200);
      } else if (mem.conversation_id) {
        setRightTab('chat');
        setRightCollapsed(false);
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
      setRightTab(activeHighlightId ? 'remarks' : 'chat');
    }
  };

  return (
    <div
      className="h-screen flex flex-col overflow-hidden bg-[#F8F6F1] text-[#1C1B18]"
      onMouseDown={() => {
        if (pendingSelection) setPendingSelection(null);
      }}
    >
      <Header
        activeNotebook={activeNotebook}
        onBackToNotebooks={() => {
          setActiveNotebook(null);
          setActiveSource(null);
          setPendingSelection(null);
          refreshNotebooks().catch(() => {});
        }}
        models={models}
        selectedModel={selectedModel}
        onSelectModel={setSelectedModel}
        health={health}
        leftCollapsed={leftCollapsed}
        rightCollapsed={rightCollapsed}
        onToggleLeft={() => setLeftCollapsed((v) => !v)}
        onToggleRight={() => setRightCollapsed((v) => !v)}
        onToggleFocusMode={toggleFocusMode}
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
        />
      ) : (
        <main className="flex-1 flex min-h-0 overflow-hidden">
          {!leftCollapsed && (
            <SourceSidebar
              sources={sources}
              activeSourceId={activeSource?.id || null}
              onSelectSource={(sid) => loadSource(sid)}
              onUploadFiles={handleUploadFiles}
              onImportUrl={handleImportUrl}
              onDeleteSource={handleDeleteSource}
              onCollapse={() => setLeftCollapsed(true)}
            />
          )}

          {/* Center Document / PDF Reader */}
          <section className="flex-1 flex flex-col min-w-0 min-h-0 relative">
            {leftCollapsed && (
              <button
                type="button"
                onClick={() => setLeftCollapsed(false)}
                title="展开素材栏 (⌘[)"
                className="absolute left-2.5 top-13 z-20 inline-flex items-center gap-1 px-2 py-1.5 rounded-lg bg-white/95 backdrop-blur border border-[#E5E0D5] shadow-sm text-xs font-medium text-[#5E5B53] hover:text-[#1C1B18] hover:border-[#B45309] transition cursor-pointer"
              >
                <PanelLeftOpen className="w-3.5 h-3.5 text-[#B45309]" />
                <span className="hidden sm:inline">素材</span>
              </button>
            )}

            {rightCollapsed && (
              <button
                type="button"
                onClick={() => setRightCollapsed(false)}
                title="展开笔记与 AI 栏 (⌘])"
                className="absolute right-2.5 top-13 z-20 inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg bg-white/95 backdrop-blur border border-[#E5E0D5] shadow-sm text-xs font-medium text-[#5E5B53] hover:text-[#1C1B18] hover:border-[#B45309] transition cursor-pointer"
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
                <div>
                  <p className="font-medium text-[#1C1B18]">请先在左侧拖入 PDF / Word 或导入文章网址</p>
                  <p className="text-xs mt-1">支持 PDF 划词与框选高亮、实时 AI 回答与历史对话 RAG 记忆检索。</p>
                </div>
              </div>
            ) : activeSource.type === 'pdf' ? (
              <PdfViewer
                source={activeSource}
                highlights={highlights}
                activeHighlightId={activeHighlightId}
                pulseHighlightId={pulseHighlightId}
                jumpPage={jumpPage}
                onSelectHighlight={(hlId) => {
                  setActiveHighlightId(hlId);
                  setRightTab('remarks');
                  setRightCollapsed(false);
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
                onSelectHighlight={(hlId) => {
                  setActiveHighlightId(hlId);
                  setRightTab('remarks');
                  setRightCollapsed(false);
                }}
                onTextSelected={setPendingSelection}
              />
            )}
          </section>

          {/* Right Panel: Remarks / Chat / RAG */}
          {!rightCollapsed && (
            <aside className="w-[380px] xl:w-[420px] shrink-0 border-l border-[#E5E0D5] bg-white flex flex-col min-h-0">
              <div className="h-11 shrink-0 border-b border-[#E5E0D5] px-2 flex items-center gap-1 bg-[#FAF8F4]">
                <button
                  onClick={() => setRightTab('remarks')}
                  className={`flex-1 inline-flex items-center justify-center gap-1.5 py-1.5 rounded-lg text-xs font-medium cursor-pointer transition ${
                    rightTab === 'remarks'
                      ? 'bg-white text-[#B45309] shadow-xs border border-[#E5E0D5]'
                      : 'text-[#5E5B53] hover:text-[#1C1B18]'
                  }`}
                >
                  <NotebookPen className="w-3.5 h-3.5" />
                  <span>划线备注 ({highlights.length})</span>
                </button>

                <button
                  onClick={() => setRightTab('chat')}
                  className={`flex-1 inline-flex items-center justify-center gap-1.5 py-1.5 rounded-lg text-xs font-medium cursor-pointer transition ${
                    rightTab === 'chat'
                      ? 'bg-white text-[#B45309] shadow-xs border border-[#E5E0D5]'
                      : 'text-[#5E5B53] hover:text-[#1C1B18]'
                  }`}
                >
                  <MessageSquare className="w-3.5 h-3.5" />
                  <span>AI 对话</span>
                </button>

                <button
                  onClick={() => setRightTab('rag')}
                  className={`flex-1 inline-flex items-center justify-center gap-1.5 py-1.5 rounded-lg text-xs font-medium cursor-pointer transition ${
                    rightTab === 'rag'
                      ? 'bg-white text-[#B45309] shadow-xs border border-[#E5E0D5]'
                      : 'text-[#5E5B53] hover:text-[#1C1B18]'
                  }`}
                >
                  <Database className="w-3.5 h-3.5" />
                  <span>RAG 记忆</span>
                </button>

                <button
                  type="button"
                  onClick={() => setRightCollapsed(true)}
                  title="收起右侧笔记栏 (⌘])"
                  className="p-1.5 rounded-lg text-[#5E5B53] hover:text-[#1C1B18] hover:bg-[#EFECE5] cursor-pointer shrink-0"
                >
                  <PanelRightClose className="w-3.5 h-3.5" />
                </button>
              </div>

            <div className="flex-1 min-h-0">
              {rightTab === 'remarks' && (
                <RemarksPanel
                  highlights={highlights}
                  activeHighlightId={activeHighlightId}
                  focusIntent={focusIntent}
                  streamingMap={highlightStreams}
                  quotedContext={quotedContext}
                  models={models}
                  selectedModel={selectedModel}
                  onSelectModel={setSelectedModel}
                  onClearQuotedContext={() => setQuotedContext(null)}
                  onSelectHighlight={(hl) => {
                    setActiveHighlightId(hl.id);
                    setPulseHighlightId(hl.id);
                    setJumpPage({ page: hl.page_number, seq: Date.now() });
                    setTimeout(() => setPulseHighlightId((cur) => (cur === hl.id ? null : cur)), 1800);
                  }}
                  onUpdateHighlight={handleUpdateHighlight}
                  onDeleteHighlight={handleDeleteHighlight}
                  onAskHighlight={handleAskHighlight}
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
                  onSelectModel={setSelectedModel}
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
      )}

      {pendingSelection && (
        <SelectionPopover
          selection={pendingSelection}
          selectedColor={selectionColor}
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
            await refreshSystem();
          }}
          onReindexRag={async () => {
            const st = await api.ragReindex();
            setRagStats(st);
          }}
          onClose={() => setShowSettings(false)}
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
