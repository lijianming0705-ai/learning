import type {
  AgyModel,
  AppSettings,
  ChatMessage,
  ClassifySourcesResponse,
  ConversationDetail,
  ConversationSummary,
  HealthStatus,
  Highlight,
  HighlightColor,
  HighlightRect,
  LocateSpeechResponse,
  Notebook,
  RagMemoryRef,
  RagStats,
  ReadingAiReport,
  ReadingAnalytics,
  ReadingHeartbeatPayload,
  ReadingHeartbeatResponse,
  SelectionSpeechResult,
  SourceDetail,
  SourceSummary,
  SpeechScriptResponse,
} from '../types';

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(url, init);
  if (!resp.ok) {
    let msg = `请求失败 (${resp.status})`;
    try {
      const data = await resp.json();
      if (data?.detail) msg = String(data.detail);
    } catch {
      // ignore parse failure
    }
    throw new Error(msg);
  }
  return resp.json() as Promise<T>;
}

export const api = {
  health: () => requestJson<HealthStatus>('/api/health'),
  listModels: () => requestJson<AgyModel[]>('/api/agy/models'),
  getSettings: () => requestJson<AppSettings>('/api/settings'),
  updateSettings: (payload: Partial<AppSettings>) =>
    requestJson<AppSettings>('/api/settings', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),

  listNotebooks: () => requestJson<Notebook[]>('/api/notebooks'),
  createNotebook: (payload: { title: string; description: string; color: string; icon?: string }) =>
    requestJson<Notebook>('/api/notebooks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  updateNotebook: (id: string, payload: Partial<{ title: string; description: string; color: string }>) =>
    requestJson<Notebook>(`/api/notebooks/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  deleteNotebook: (id: string) => requestJson<{ deleted: boolean }>(`/api/notebooks/${id}`, { method: 'DELETE' }),

  listSources: (notebookId: string) => requestJson<SourceSummary[]>(`/api/notebooks/${notebookId}/sources`),
  getSource: (sourceId: string) => requestJson<SourceDetail>(`/api/sources/${sourceId}`),
  uploadSources: async (notebookId: string, files: File[]): Promise<SourceDetail[]> => {
    const formData = new FormData();
    files.forEach((f) => formData.append('files', f));
    return requestJson<SourceDetail[]>(`/api/notebooks/${notebookId}/sources/upload`, {
      method: 'POST',
      body: formData,
    });
  },
  importUrl: (notebookId: string, url: string, title?: string) =>
    requestJson<SourceDetail>(`/api/notebooks/${notebookId}/sources/url`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, title: title || null }),
    }),
  updateSource: (
    sourceId: string,
    payload: {
      title?: string;
      category?: string;
      last_page?: number;
      last_scroll_ratio?: number;
      reading_progress?: number;
    },
  ) =>
    requestJson<SourceDetail>(`/api/sources/${sourceId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  classifySources: (notebookId: string, model?: string) =>
    requestJson<ClassifySourcesResponse>(`/api/notebooks/${notebookId}/sources/classify`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: model || null }),
    }),
  deleteSource: (sourceId: string) => requestJson<{ deleted: boolean }>(`/api/sources/${sourceId}`, { method: 'DELETE' }),

  listSourceHighlights: (sourceId: string) => requestJson<Highlight[]>(`/api/sources/${sourceId}/highlights`),
  listNotebookHighlights: (notebookId: string) => requestJson<Highlight[]>(`/api/notebooks/${notebookId}/highlights`),
  createHighlight: (
    sourceId: string,
    payload: {
      color: HighlightColor;
      quote_text: string;
      note_text: string;
      anchor_type: 'text' | 'area';
      page_number: number;
      rects: HighlightRect[];
      start_offset?: number;
      end_offset?: number;
      prefix_text?: string;
      suffix_text?: string;
    },
  ) =>
    requestJson<Highlight>(`/api/sources/${sourceId}/highlights`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  updateHighlight: (
    highlightId: string,
    payload: Partial<{ color: HighlightColor; note_text: string; quote_text: string }>,
  ) =>
    requestJson<Highlight>(`/api/highlights/${highlightId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  deleteHighlight: (highlightId: string) =>
    requestJson<{ deleted: boolean }>(`/api/highlights/${highlightId}`, { method: 'DELETE' }),
  ocrHighlight: (highlightId: string, model?: string) =>
    requestJson<Highlight>(`/api/highlights/${highlightId}/ocr`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: model || null }),
    }),

  listConversations: (notebookId: string) =>
    requestJson<ConversationSummary[]>(`/api/notebooks/${notebookId}/conversations`),
  getConversation: (conversationId: string) => requestJson<ConversationDetail>(`/api/conversations/${conversationId}`),
  deleteConversation: (conversationId: string) =>
    requestJson<{ deleted: boolean }>(`/api/conversations/${conversationId}`, { method: 'DELETE' }),

  searchRag: (params: {
    q: string;
    notebookId?: string;
    sourceId?: string;
    docType?: string;
    topK?: number;
  }) => {
    const qs = new URLSearchParams({ q: params.q });
    if (params.notebookId) qs.set('notebook_id', params.notebookId);
    if (params.sourceId) qs.set('source_id', params.sourceId);
    if (params.docType) qs.set('doc_type', params.docType);
    if (params.topK) qs.set('top_k', String(params.topK));
    return requestJson<RagMemoryRef[]>(`/api/rag/search?${qs.toString()}`);
  },
  ragStats: () => requestJson<RagStats>('/api/rag/stats'),
  ragReindex: () => requestJson<RagStats>('/api/rag/reindex', { method: 'POST' }),

  pronounceSelection: (payload: {
    text: string;
    source_id?: string;
    page_number?: number;
    model?: string;
    mode?: 'natural' | 'bilingual';
  }) =>
    requestJson<SelectionSpeechResult>('/api/speech/selection', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  getSpeechScript: (sourceId: string) =>
    requestJson<SpeechScriptResponse>(`/api/sources/${sourceId}/speech-script`),
  generateSpeechScript: (sourceId: string, model?: string, forceRegenerate?: boolean) =>
    requestJson<SpeechScriptResponse>(`/api/sources/${sourceId}/speech-script`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: model || null, force_regenerate: Boolean(forceRegenerate) }),
    }),
  locateSpeechSegment: (sourceId: string, payload: { text: string; page_number?: number }) =>
    requestJson<LocateSpeechResponse>(`/api/sources/${sourceId}/speech-script/locate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),

  sendReadingHeartbeat: (payload: ReadingHeartbeatPayload) =>
    requestJson<ReadingHeartbeatResponse>('/api/analytics/reading/heartbeat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  getReadingAnalytics: (params?: { notebookId?: string; days?: number }) => {
    const qs = new URLSearchParams();
    if (params?.notebookId) qs.set('notebook_id', params.notebookId);
    if (params?.days) qs.set('days', String(params.days));
    const q = qs.toString();
    return requestJson<ReadingAnalytics>(`/api/analytics/reading${q ? `?${q}` : ''}`);
  },
  generateReadingAiReport: (payload: { notebook_id?: string | null; days?: number; model?: string }) =>
    requestJson<ReadingAiReport>('/api/analytics/reading/ai-report', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
};

export interface StreamCallbacks {
  onRagContext?: (payload: {
    conversation_id: string;
    user_message: ChatMessage;
    memories: RagMemoryRef[];
  }) => void;
  onStatus?: (stage: string) => void;
  onDelta?: (text: string) => void;
  onDone?: (payload: { conversation_id: string; message: ChatMessage }) => void;
  onError?: (message: string) => void;
}

async function consumeSse(url: string, body: unknown, cb: StreamCallbacks, signal?: AbortSignal): Promise<void> {
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  });

  if (!resp.ok) {
    let errText = `请求失败 (${resp.status})`;
    try {
      const j = await resp.json();
      if (j?.detail) errText = String(j.detail);
    } catch {
      // ignore
    }
    cb.onError?.(errText);
    return;
  }

  const reader = resp.body?.getReader();
  if (!reader) {
    cb.onError?.('浏览器不支持流式读取响应');
    return;
  }

  const decoder = new TextDecoder('utf-8');
  let buffer = '';

  const processBlock = (block: string) => {
    const trimmed = block.trim();
    if (!trimmed) return;
    let eventName = 'message';
    const dataLines: string[] = [];
    for (const line of trimmed.split('\n')) {
      if (line.startsWith('event:')) {
        eventName = line.slice(6).trim();
      } else if (line.startsWith('data:')) {
        dataLines.push(line.slice(5).trim());
      }
    }
    if (dataLines.length === 0) return;
    let parsed: Record<string, unknown> = {};
    try {
      parsed = JSON.parse(dataLines.join('\n')) as Record<string, unknown>;
    } catch {
      return;
    }

    if (eventName === 'rag_context') {
      cb.onRagContext?.(parsed as unknown as Parameters<NonNullable<StreamCallbacks['onRagContext']>>[0]);
    } else if (eventName === 'status') {
      cb.onStatus?.(String(parsed.stage || ''));
    } else if (eventName === 'delta') {
      cb.onDelta?.(String(parsed.text || ''));
    } else if (eventName === 'done') {
      cb.onDone?.(parsed as unknown as Parameters<NonNullable<StreamCallbacks['onDone']>>[0]);
    } else if (eventName === 'error') {
      cb.onError?.(String(parsed.message || '生成失败'));
    }
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let sepIdx = buffer.indexOf('\n\n');
    while (sepIdx !== -1) {
      const block = buffer.slice(0, sepIdx);
      buffer = buffer.slice(sepIdx + 2);
      processBlock(block);
      sepIdx = buffer.indexOf('\n\n');
    }
  }
  if (buffer.trim()) {
    processBlock(buffer);
  }
}

export function streamHighlightAsk(
  highlightId: string,
  payload: {
    question: string;
    model?: string;
    use_rag?: boolean;
    cross_notebook_rag?: boolean;
    extra_context?: string;
  },
  cb: StreamCallbacks,
  signal?: AbortSignal,
) {
  return consumeSse(`/api/highlights/${highlightId}/ask`, payload, cb, signal);
}

export function streamNotebookChat(
  notebookId: string,
  payload: {
    conversation_id?: string | null;
    source_id?: string | null;
    message: string;
    model?: string;
    use_rag?: boolean;
    cross_notebook_rag?: boolean;
    extra_context?: string;
  },
  cb: StreamCallbacks,
  signal?: AbortSignal,
) {
  return consumeSse(`/api/notebooks/${notebookId}/chat`, payload, cb, signal);
}

export interface OcrConvertProgressEvent {
  current: number;
  total: number;
  page_number: number;
  stage: 'rendering' | 'ocr' | 'done_page';
  message: string;
  figures_count?: number;
  total_figures?: number;
  failed?: boolean;
  preview?: string;
}

export interface OcrConvertCallbacks {
  onStart?: (payload: {
    job_id: string;
    source_id: string;
    source_title: string;
    output_title: string;
    start_page: number;
    end_page: number;
    total_pages: number;
    concurrency?: number;
    model: string;
  }) => void;
  onProgress?: (payload: OcrConvertProgressEvent) => void;
  onDone?: (payload: { source: SourceDetail; total_figures: number; failed_pages?: number }) => void;
  onError?: (message: string) => void;
}

export async function streamPdfOcrConvert(
  sourceId: string,
  payload: {
    model?: string;
    full_book?: boolean;
    start_page?: number;
    end_page?: number;
    concurrency?: number;
    title?: string;
  },
  cb: OcrConvertCallbacks,
  signal?: AbortSignal,
): Promise<void> {
  const resp = await fetch(`/api/sources/${sourceId}/ocr-convert`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal,
  });

  if (!resp.ok) {
    let errText =
      resp.status === 405
        ? 'OCR 转换接口返回 405 (Method Not Allowed)：当前运行的后端进程尚未加载最新路由，请重启 ./start.sh 后重试'
        : `OCR 转换请求失败 (${resp.status})`;
    if (resp.status !== 405) {
      try {
        const j = await resp.json();
        if (j?.detail) errText = String(j.detail);
      } catch {
        // ignore parse failure
      }
    }
    cb.onError?.(errText);
    return;
  }

  const reader = resp.body?.getReader();
  if (!reader) {
    cb.onError?.('浏览器不支持流式读取响应');
    return;
  }

  const decoder = new TextDecoder('utf-8');
  let buffer = '';

  const processBlock = (block: string) => {
    const trimmed = block.trim();
    if (!trimmed) return;
    let eventName = 'message';
    const dataLines: string[] = [];
    for (const line of trimmed.split('\n')) {
      if (line.startsWith('event:')) {
        eventName = line.slice(6).trim();
      } else if (line.startsWith('data:')) {
        dataLines.push(line.slice(5).trim());
      }
    }
    if (dataLines.length === 0) return;
    let parsed: Record<string, unknown> = {};
    try {
      parsed = JSON.parse(dataLines.join('\n')) as Record<string, unknown>;
    } catch {
      return;
    }
    if (eventName === 'start') {
      cb.onStart?.(parsed as unknown as Parameters<NonNullable<OcrConvertCallbacks['onStart']>>[0]);
    } else if (eventName === 'progress') {
      cb.onProgress?.(parsed as unknown as OcrConvertProgressEvent);
    } else if (eventName === 'done') {
      cb.onDone?.(parsed as unknown as Parameters<NonNullable<OcrConvertCallbacks['onDone']>>[0]);
    } else if (eventName === 'error') {
      cb.onError?.(String(parsed.message || 'OCR 转换失败'));
    }
  };

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let sepIdx = buffer.indexOf('\n\n');
      while (sepIdx !== -1) {
        const block = buffer.slice(0, sepIdx);
        buffer = buffer.slice(sepIdx + 2);
        processBlock(block);
        sepIdx = buffer.indexOf('\n\n');
      }
    }
    if (buffer.trim()) {
      processBlock(buffer);
    }
  } catch (err) {
    if (signal?.aborted) return;
    cb.onError?.(err instanceof Error ? err.message : 'OCR 转换中断');
  }
}
