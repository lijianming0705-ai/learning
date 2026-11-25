export type HighlightColor = 'amber' | 'emerald' | 'sky' | 'rose' | 'violet';

export interface HighlightRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface Notebook {
  id: string;
  title: string;
  description: string;
  color: string;
  icon: string;
  source_count: number;
  highlight_count: number;
  qa_count: number;
  created_at: string;
  updated_at: string;
}

export interface SourceSummary {
  id: string;
  notebook_id: string;
  type: 'pdf' | 'docx' | 'url' | 'text';
  title: string;
  original_filename: string | null;
  source_url: string | null;
  page_count: number;
  word_count: number;
  highlight_count: number;
  status: string;
  error_message: string | null;
  created_at: string;
  updated_at: string;
}

export interface SourcePage {
  page_number: number;
  text_content: string;
}

export interface SourceDetail extends SourceSummary {
  html_content: string;
  plain_text: string;
  pages: SourcePage[];
}

export interface RagMemoryRef {
  doc_id: string;
  doc_type: 'qa_memory' | 'highlight_note' | 'source_chunk';
  score: number;
  notebook_id: string;
  notebook_title: string;
  source_id: string | null;
  source_title: string;
  highlight_id: string | null;
  conversation_id: string | null;
  page_number: number | null;
  title: string;
  snippet: string;
  content: string;
  created_at: string;
}

export interface ChatMessage {
  id: string;
  conversation_id: string;
  role: 'user' | 'assistant';
  content: string;
  model: string | null;
  usage: Record<string, number> | null;
  retrieved_memories: RagMemoryRef[];
  duration_seconds: number | null;
  created_at: string;
}

export interface Highlight {
  id: string;
  notebook_id: string;
  source_id: string;
  source_title: string;
  source_type: string;
  color: HighlightColor;
  quote_text: string;
  note_text: string;
  anchor_type: 'text' | 'area';
  page_number: number;
  rects: HighlightRect[];
  start_offset: number | null;
  end_offset: number | null;
  prefix_text: string;
  suffix_text: string;
  conversation_id: string | null;
  messages: ChatMessage[];
  created_at: string;
  updated_at: string;
}

export interface ConversationSummary {
  id: string;
  notebook_id: string;
  source_id: string | null;
  highlight_id: string | null;
  title: string;
  model: string;
  message_count: number;
  created_at: string;
  updated_at: string;
}

export interface ConversationDetail extends ConversationSummary {
  messages: ChatMessage[];
}

export interface AgyModel {
  id: string;
  name: string;
}

export interface AppSettings {
  default_model: string;
  rag_top_k: number;
  cross_notebook_rag: boolean;
  embedding_provider: 'hybrid-local' | 'ollama';
  ollama_embedding_model: string;
}

export interface RagStats {
  total_documents: number;
  counts_by_type: Record<string, number>;
  embedding_provider: string;
  embedding_dim: number;
  db_size_bytes: number;
}

export interface HealthStatus {
  status: string;
  agy_available: boolean;
  agy_path: string | null;
  agy_warm_workers: number;
  rag_documents: number;
}

export interface PendingSelection {
  quoteText: string;
  anchorType: 'text' | 'area';
  pageNumber: number;
  rects: HighlightRect[];
  startOffset?: number;
  endOffset?: number;
  prefixText?: string;
  suffixText?: string;
  clientX: number;
  clientY: number;
}
