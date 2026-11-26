import { useState } from 'react';
import { RefreshCw, X } from 'lucide-react';
import type { AgyModel, AppSettings, RagStats } from '../types';

interface SettingsModalProps {
  settings: AppSettings;
  models: AgyModel[];
  ragStats: RagStats | null;
  onSave: (patch: Partial<AppSettings>) => Promise<void>;
  onReindexRag: () => Promise<void>;
  onClose: () => void;
}

const MLX_VOICE_OPTIONS = [
  { id: 'Vivian', label: 'Vivian（默认 · 自然女声）' },
  { id: 'Serena', label: 'Serena（温柔女声）' },
  { id: 'Ryan', label: 'Ryan（清晰男声）' },
  { id: 'Uncle_Fu', label: 'Uncle_Fu（沉稳男声）' },
  { id: 'Aiden', label: 'Aiden（年轻男声）' },
  { id: 'Eric', label: 'Eric（自然男声）' },
  { id: 'Dylan', label: 'Dylan（朗读男声）' },
  { id: 'Ono_Anna', label: 'Ono_Anna（日系女声）' },
  { id: 'Sohee', label: 'Sohee（韩系女声）' },
];

export function SettingsModal({
  settings,
  models,
  ragStats,
  onSave,
  onReindexRag,
  onClose,
}: SettingsModalProps) {
  const [defaultModel, setDefaultModel] = useState(settings.default_model);
  const [ragTopK, setRagTopK] = useState(settings.rag_top_k);
  const [crossNotebook, setCrossNotebook] = useState(settings.cross_notebook_rag);
  const [embeddingProvider, setEmbeddingProvider] = useState<'hybrid-local' | 'ollama'>(
    settings.embedding_provider,
  );
  const [ollamaModel, setOllamaModel] = useState(settings.ollama_embedding_model);
  const [ttsEngine, setTtsEngine] = useState<'mlx-audio' | 'edge-tts' | 'macos-say'>(
    settings.tts_engine || 'mlx-audio',
  );
  const [mlxVoice, setMlxVoice] = useState(settings.mlx_tts_voice || 'Vivian');
  const [saving, setSaving] = useState(false);
  const [reindexing, setReindexing] = useState(false);

  const handleSave = async () => {
    setSaving(true);
    try {
      await onSave({
        default_model: defaultModel,
        rag_top_k: ragTopK,
        cross_notebook_rag: crossNotebook,
        embedding_provider: embeddingProvider,
        ollama_embedding_model: ollamaModel.trim(),
        tts_engine: ttsEngine,
        mlx_tts_voice: mlxVoice,
      });
      onClose();
    } finally {
      setSaving(false);
    }
  };

  const handleReindex = async () => {
    setReindexing(true);
    try {
      await onReindexRag();
    } finally {
      setReindexing(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 bg-black/40 backdrop-blur-xs flex items-center justify-center p-4">
      <div className="bg-white border border-[#E5E0D5] rounded-2xl shadow-xl w-full max-w-lg overflow-hidden">
        <div className="px-5 py-4 border-b border-[#E5E0D5] flex items-center justify-between">
          <h2 className="text-base font-semibold text-[#1C1B18]">平台与 agy / RAG 设置</h2>
          <button
            onClick={onClose}
            className="p-1.5 rounded-lg text-[#5E5B53] hover:bg-[#EFECE5] cursor-pointer"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="p-5 space-y-4 text-sm">
          <div>
            <label className="block text-xs font-medium text-[#5E5B53] mb-1">默认 agy 模型</label>
            <select
              value={defaultModel}
              onChange={(e) => setDefaultModel(e.target.value)}
              className="w-full rounded-lg border border-[#E5E0D5] px-3 py-2 bg-[#FAF8F4] text-sm"
            >
              {models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name} ({m.id})
                </option>
              ))}
            </select>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-medium text-[#5E5B53] mb-1">RAG 召回条数 (Top-K)</label>
              <input
                type="number"
                min={1}
                max={20}
                value={ragTopK}
                onChange={(e) => setRagTopK(Number(e.target.value) || 5)}
                className="w-full rounded-lg border border-[#E5E0D5] px-3 py-2 bg-[#FAF8F4] text-sm"
              />
            </div>

            <div className="flex items-end pb-1.5">
              <label className="inline-flex items-center gap-2 cursor-pointer select-none">
                <input
                  type="checkbox"
                  checked={crossNotebook}
                  onChange={(e) => setCrossNotebook(e.target.checked)}
                  className="accent-[#B45309] w-4 h-4"
                />
                <span className="text-xs font-medium text-[#1C1B18]">允许跨笔记本召回历史记忆</span>
              </label>
            </div>
          </div>

          <div>
            <label className="block text-xs font-medium text-[#5E5B53] mb-1">RAG 向量引擎</label>
            <select
              value={embeddingProvider}
              onChange={(e) => setEmbeddingProvider(e.target.value as 'hybrid-local' | 'ollama')}
              className="w-full rounded-lg border border-[#E5E0D5] px-3 py-2 bg-[#FAF8F4] text-sm"
            >
              <option value="hybrid-local">本地多语种混合检索 (Vector + CJK FTS5, 零依赖)</option>
              <option value="ollama">Ollama 本地 Embedding 模型 (失败自动回退)</option>
            </select>
          </div>

          {embeddingProvider === 'ollama' && (
            <div>
              <label className="block text-xs font-medium text-[#5E5B53] mb-1">Ollama Embedding 模型名</label>
              <input
                type="text"
                value={ollamaModel}
                onChange={(e) => setOllamaModel(e.target.value)}
                placeholder="例如：nomic-embed-text 或 bge-m3"
                className="w-full rounded-lg border border-[#E5E0D5] px-3 py-2 bg-[#FAF8F4] text-sm"
              />
            </div>
          )}

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-medium text-[#5E5B53] mb-1">语音合成引擎 (TTS)</label>
              <select
                value={ttsEngine}
                onChange={(e) => setTtsEngine(e.target.value as 'mlx-audio' | 'edge-tts' | 'macos-say')}
                className="w-full rounded-lg border border-[#E5E0D5] px-3 py-2 bg-[#FAF8F4] text-sm"
              >
                <option value="mlx-audio">MLX-Audio 本地神经网络（默认）</option>
                <option value="edge-tts">Edge TTS（微软在线）</option>
                <option value="macos-say">macOS 系统自带 say</option>
              </select>
            </div>

            {ttsEngine === 'mlx-audio' && (
              <div>
                <label className="block text-xs font-medium text-[#5E5B53] mb-1">MLX-Audio 预设音色</label>
                <select
                  value={mlxVoice}
                  onChange={(e) => setMlxVoice(e.target.value)}
                  className="w-full rounded-lg border border-[#E5E0D5] px-3 py-2 bg-[#FAF8F4] text-sm"
                >
                  {MLX_VOICE_OPTIONS.map((v) => (
                    <option key={v.id} value={v.id}>
                      {v.label}
                    </option>
                  ))}
                </select>
              </div>
            )}
          </div>

          <div className="rounded-xl bg-[#FAF8F4] border border-[#E5E0D5] p-3.5 flex items-center justify-between">
            <div className="text-xs text-[#5E5B53] space-y-0.5">
              <div className="font-medium text-[#1C1B18]">RAG 数据库索引状态</div>
              <div>
                文档总数：{ragStats?.total_documents ?? 0}（对话记忆 {ragStats?.counts_by_type?.qa_memory ?? 0} ·
                笔记 {ragStats?.counts_by_type?.highlight_note ?? 0} · 片段 {ragStats?.counts_by_type?.source_chunk ?? 0}）
              </div>
            </div>
            <button
              onClick={handleReindex}
              disabled={reindexing}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-[#E5E0D5] bg-white hover:bg-[#EFECE5] text-xs font-medium cursor-pointer disabled:opacity-50"
            >
              <RefreshCw className={`w-3.5 h-3.5 ${reindexing ? 'animate-spin' : ''}`} />
              <span>{reindexing ? '重建中...' : '重建索引'}</span>
            </button>
          </div>
        </div>

        <div className="px-5 py-3.5 bg-[#FAF8F4] border-t border-[#E5E0D5] flex justify-end gap-2">
          <button
            onClick={onClose}
            className="px-4 py-1.5 rounded-lg text-sm text-[#5E5B53] hover:bg-[#EFECE5] cursor-pointer"
          >
            取消
          </button>
          <button
            onClick={handleSave}
            disabled={saving}
            className="px-4 py-1.5 rounded-lg text-sm font-medium bg-[#B45309] text-white hover:bg-[#92400E] cursor-pointer disabled:opacity-50"
          >
            {saving ? '保存中...' : '保存设置'}
          </button>
        </div>
      </div>
    </div>
  );
}
