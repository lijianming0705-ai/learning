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
  total_reading_seconds: number;
  avg_reading_progress: number;
  created_at: string;
  updated_at: string;
}

export interface SourceSummary {
  id: string;
  notebook_id: string;
  type: 'pdf' | 'docx' | 'url' | 'text';
  title: string;
  category: string;
  original_filename: string | null;
  source_url: string | null;
  page_count: number;
  word_count: number;
  highlight_count: number;
  is_scanned: boolean;
  last_page: number;
  last_scroll_ratio: number;
  reading_seconds: number;
  reading_progress: number;
  last_read_at: string | null;
  read_sessions_count: number;
  status: string;
  error_message: string | null;
  created_at: string;
  updated_at: string;
}

export interface ClassifySourcesResponse {
  classified_count: number;
  categories: string[];
  sources: SourceSummary[];
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
  image_url: string | null;
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

export interface SelectionSpeechResult {
  original_text: string;
  spoken_text: string;
  language: string;
  ipa_or_guide: string;
  brief_meaning: string;
  audio_url: string | null;
}

export interface SpeechSegment {
  id: string;
  source_id: string;
  segment_index: number;
  page_number: number;
  heading: string;
  original_excerpt: string;
  spoken_text: string;
  language: string;
  audio_url: string;
}

export interface SpeechScriptResponse {
  source_id: string;
  source_title: string;
  segments: SpeechSegment[];
}

export interface LocateSpeechResponse {
  segment_index: number;
  segment_id: string;
  page_number: number;
  score: number;
}

export interface ReadingHeartbeatPayload {
  session_id: string;
  source_id: string;
  delta_seconds: number;
  current_page: number;
  scroll_ratio: number;
  progress: number;
  local_date?: string;
  local_hour?: number;
}

export interface ReadingHeartbeatResponse {
  source_id: string;
  session_id: string;
  reading_seconds: number;
  reading_progress: number;
  last_read_at: string;
  session_duration_seconds: number;
}

export interface ReadingSummaryMetrics {
  total_reading_seconds: number;
  today_reading_seconds: number;
  avg_session_seconds: number;
  longest_session_seconds: number;
  total_sessions: number;
  active_days: number;
  current_streak_days: number;
  total_sources: number;
  completed_sources: number;
  in_progress_sources: number;
  unread_sources: number;
  avg_progress: number;
  total_highlights: number;
  total_qa_turns: number;
  highlights_per_hour: number;
  qa_per_hour: number;
}

export interface DailyReadingPoint {
  date: string;
  reading_seconds: number;
  sessions_count: number;
  highlights_count: number;
  qa_count: number;
}

export interface HourlyReadingPoint {
  hour: number;
  reading_seconds: number;
  sessions_count: number;
}

export interface CategoryReadingStat {
  label: string;
  reading_seconds: number;
  source_count: number;
  avg_progress: number;
  highlight_count: number;
}

export interface SourceReadingStat {
  source_id: string;
  notebook_id: string;
  notebook_title: string;
  title: string;
  type: string;
  category: string;
  page_count: number;
  word_count: number;
  last_page: number;
  reading_progress: number;
  reading_seconds: number;
  read_sessions_count: number;
  highlight_count: number;
  qa_count: number;
  last_read_at: string | null;
  estimated_remaining_seconds: number | null;
}

export interface ReadingBehaviorProfile {
  reader_archetype: string;
  peak_period_label: string;
  focus_style: string;
  engagement_style: string;
  completion_rate: number;
  insights: string[];
}

export interface ReadingAnalytics {
  scope_notebook_id: string | null;
  days: number;
  summary: ReadingSummaryMetrics;
  daily_trend: DailyReadingPoint[];
  hourly_distribution: HourlyReadingPoint[];
  category_breakdown: CategoryReadingStat[];
  type_breakdown: CategoryReadingStat[];
  source_stats: SourceReadingStat[];
  behavior_profile: ReadingBehaviorProfile;
}

export interface ReadingAiReport {
  report_markdown: string;
  model: string;
  generated_at: string;
}
