from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field


class HighlightRect(BaseModel):
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    width: float = Field(ge=0.0, le=1.0)
    height: float = Field(ge=0.0, le=1.0)


class NotebookCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    color: str = Field(default="#B45309", max_length=32)
    icon: str = Field(default="book", max_length=32)


class NotebookUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    color: str | None = Field(default=None, max_length=32)
    icon: str | None = Field(default=None, max_length=32)


class NotebookOut(BaseModel):
    id: str
    title: str
    description: str
    color: str
    icon: str
    source_count: int = 0
    highlight_count: int = 0
    qa_count: int = 0
    total_reading_seconds: int = 0
    avg_reading_progress: float = 0.0
    created_at: str
    updated_at: str


class UrlImportRequest(BaseModel):
    url: str = Field(min_length=4, max_length=2048)
    title: str | None = Field(default=None, max_length=300)


class SourceSummary(BaseModel):
    id: str
    notebook_id: str
    type: Literal["pdf", "docx", "url", "text"]
    title: str
    category: str = ""
    original_filename: str | None = None
    source_url: str | None = None
    page_count: int = 0
    word_count: int = 0
    highlight_count: int = 0
    is_scanned: bool = False
    last_page: int = 1
    last_scroll_ratio: float = 0.0
    reading_seconds: int = 0
    reading_progress: float = 0.0
    last_read_at: str | None = None
    read_sessions_count: int = 0
    status: str = "ready"
    error_message: str | None = None
    created_at: str
    updated_at: str


class SourceUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    category: str | None = Field(default=None, max_length=60)
    last_page: int | None = Field(default=None, ge=1)
    last_scroll_ratio: float | None = Field(default=None, ge=0.0, le=1.0)
    reading_progress: float | None = Field(default=None, ge=0.0, le=1.0)


class ClassifySourcesRequest(BaseModel):
    model: str | None = None


class ClassifySourcesResponse(BaseModel):
    classified_count: int
    categories: list[str]
    sources: list[SourceSummary]


class SourcePageOut(BaseModel):
    page_number: int
    text_content: str


class SourceDetail(SourceSummary):
    html_content: str = ""
    plain_text: str = ""
    pages: list[SourcePageOut] = Field(default_factory=list)


class RagMemoryRef(BaseModel):
    doc_id: str
    doc_type: Literal["qa_memory", "highlight_note", "source_chunk"]
    score: float
    notebook_id: str
    notebook_title: str = ""
    source_id: str | None = None
    source_title: str = ""
    highlight_id: str | None = None
    conversation_id: str | None = None
    page_number: int | None = None
    title: str = ""
    snippet: str = ""
    content: str = ""
    created_at: str = ""


class MessageOut(BaseModel):
    id: str
    conversation_id: str
    role: Literal["user", "assistant"]
    content: str
    model: str | None = None
    usage: dict[str, Any] | None = None
    retrieved_memories: list[RagMemoryRef] = Field(default_factory=list)
    duration_seconds: float | None = None
    created_at: str


class HighlightCreate(BaseModel):
    color: Literal["amber", "emerald", "sky", "rose", "violet"] = "amber"
    quote_text: str = Field(default="", max_length=20000)
    note_text: str = Field(default="", max_length=20000)
    anchor_type: Literal["text", "area"] = "text"
    page_number: int = Field(default=1, ge=1)
    rects: list[HighlightRect] = Field(default_factory=list)
    start_offset: int | None = Field(default=None, ge=0)
    end_offset: int | None = Field(default=None, ge=0)
    prefix_text: str = Field(default="", max_length=500)
    suffix_text: str = Field(default="", max_length=500)


class HighlightUpdate(BaseModel):
    color: Literal["amber", "emerald", "sky", "rose", "violet"] | None = None
    note_text: str | None = Field(default=None, max_length=20000)
    quote_text: str | None = Field(default=None, max_length=20000)


class HighlightOut(BaseModel):
    id: str
    notebook_id: str
    source_id: str
    source_title: str = ""
    source_type: str = ""
    color: str
    quote_text: str
    note_text: str
    anchor_type: str
    page_number: int
    rects: list[HighlightRect] = Field(default_factory=list)
    start_offset: int | None = None
    end_offset: int | None = None
    prefix_text: str = ""
    suffix_text: str = ""
    image_url: str | None = None
    conversation_id: str | None = None
    messages: list[MessageOut] = Field(default_factory=list)
    created_at: str
    updated_at: str


class OcrHighlightRequest(BaseModel):
    model: str | None = None


class AskHighlightRequest(BaseModel):
    question: str = Field(min_length=1, max_length=12000)
    model: str | None = None
    use_rag: bool = True
    cross_notebook_rag: bool | None = None
    extra_context: str | None = Field(default=None, max_length=8000)


class NotebookChatRequest(BaseModel):
    conversation_id: str | None = None
    source_id: str | None = None
    message: str = Field(min_length=1, max_length=12000)
    model: str | None = None
    use_rag: bool = True
    cross_notebook_rag: bool | None = None
    extra_context: str | None = Field(default=None, max_length=8000)


class ConversationOut(BaseModel):
    id: str
    notebook_id: str
    source_id: str | None = None
    highlight_id: str | None = None
    title: str
    model: str
    message_count: int = 0
    created_at: str
    updated_at: str


class ConversationDetail(ConversationOut):
    messages: list[MessageOut] = Field(default_factory=list)


class AgyModelOut(BaseModel):
    id: str
    name: str


class SettingsOut(BaseModel):
    default_model: str
    rag_top_k: int = 5
    cross_notebook_rag: bool = True
    embedding_provider: str = "hybrid-local"
    ollama_embedding_model: str = ""


class SettingsUpdate(BaseModel):
    default_model: str | None = None
    rag_top_k: int | None = Field(default=None, ge=1, le=20)
    cross_notebook_rag: bool | None = None
    embedding_provider: Literal["hybrid-local", "ollama"] | None = None
    ollama_embedding_model: str | None = None


class RagStatsOut(BaseModel):
    total_documents: int
    counts_by_type: dict[str, int]
    embedding_provider: str
    embedding_dim: int
    db_size_bytes: int


class HealthOut(BaseModel):
    status: str
    agy_available: bool
    agy_path: str | None
    agy_warm_workers: int
    rag_documents: int


class SelectionSpeechRequest(BaseModel):
    text: str = Field(min_length=1, max_length=6000)
    source_id: str | None = None
    page_number: int | None = None
    model: str | None = None
    mode: Literal["natural", "bilingual"] = "natural"


class SelectionSpeechResponse(BaseModel):
    original_text: str
    spoken_text: str
    language: str
    ipa_or_guide: str = ""
    brief_meaning: str = ""
    audio_url: str | None = None


class SpeechSegmentOut(BaseModel):
    id: str
    source_id: str
    segment_index: int
    page_number: int
    heading: str = ""
    original_excerpt: str = ""
    spoken_text: str
    language: str
    audio_url: str


class GenerateSpeechScriptRequest(BaseModel):
    model: str | None = None
    force_regenerate: bool = False


class SpeechScriptResponse(BaseModel):
    source_id: str
    source_title: str = ""
    segments: list[SpeechSegmentOut] = Field(default_factory=list)


class LocateSpeechRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    page_number: int | None = None


class LocateSpeechResponse(BaseModel):
    segment_index: int
    segment_id: str
    page_number: int
    score: float


class ReadingHeartbeatRequest(BaseModel):
    session_id: str = Field(min_length=4, max_length=120)
    source_id: str
    delta_seconds: int = Field(ge=1, le=120)
    current_page: int = Field(default=1, ge=1)
    scroll_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    local_date: str | None = Field(default=None, max_length=16)
    local_hour: int | None = Field(default=None, ge=0, le=23)


class ReadingHeartbeatResponse(BaseModel):
    source_id: str
    session_id: str
    reading_seconds: int
    reading_progress: float
    last_read_at: str
    session_duration_seconds: int


class ReadingSummaryMetrics(BaseModel):
    total_reading_seconds: int
    today_reading_seconds: int
    avg_session_seconds: int
    longest_session_seconds: int
    total_sessions: int
    active_days: int
    current_streak_days: int
    total_sources: int
    completed_sources: int
    in_progress_sources: int
    unread_sources: int
    avg_progress: float
    total_highlights: int
    total_qa_turns: int
    highlights_per_hour: float
    qa_per_hour: float


class DailyReadingPoint(BaseModel):
    date: str
    reading_seconds: int
    sessions_count: int
    highlights_count: int
    qa_count: int


class HourlyReadingPoint(BaseModel):
    hour: int
    reading_seconds: int
    sessions_count: int


class CategoryReadingStat(BaseModel):
    label: str
    reading_seconds: int
    source_count: int
    avg_progress: float
    highlight_count: int


class SourceReadingStat(BaseModel):
    source_id: str
    notebook_id: str
    notebook_title: str
    title: str
    type: str
    category: str
    page_count: int
    word_count: int
    last_page: int
    reading_progress: float
    reading_seconds: int
    read_sessions_count: int
    highlight_count: int
    qa_count: int
    last_read_at: str | None = None
    estimated_remaining_seconds: int | None = None


class ReadingBehaviorProfile(BaseModel):
    reader_archetype: str
    peak_period_label: str
    focus_style: str
    engagement_style: str
    completion_rate: float
    insights: list[str]


class ReadingAnalyticsResponse(BaseModel):
    scope_notebook_id: str | None = None
    days: int
    summary: ReadingSummaryMetrics
    daily_trend: list[DailyReadingPoint]
    hourly_distribution: list[HourlyReadingPoint]
    category_breakdown: list[CategoryReadingStat]
    type_breakdown: list[CategoryReadingStat]
    source_stats: list[SourceReadingStat]
    behavior_profile: ReadingBehaviorProfile


class ReadingAiReportRequest(BaseModel):
    notebook_id: str | None = None
    days: int = Field(default=30, ge=7, le=365)
    model: str | None = None


class ReadingAiReportResponse(BaseModel):
    report_markdown: str
    model: str
    generated_at: str


class OcrConvertSourceRequest(BaseModel):
    model: str | None = None
    start_page: int = Field(default=1, ge=1)
    end_page: int | None = Field(default=None, ge=1)
    title: str | None = Field(default=None, max_length=280)
