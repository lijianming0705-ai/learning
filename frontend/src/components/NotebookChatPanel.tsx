import { useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Database, Loader2, MessageSquarePlus, Send, Trash2 } from 'lucide-react';
import type { ChatMessage, ConversationSummary, RagMemoryRef } from '../types';

interface NotebookChatPanelProps {
  conversations: ConversationSummary[];
  activeConversationId: string | null;
  messages: ChatMessage[];
  streamingText: string;
  streamingStatus: string;
  streamingActive: boolean;
  quotedContext: string | null;
  onClearQuotedContext: () => void;
  onSelectConversation: (convId: string) => void;
  onNewConversation: () => void;
  onDeleteConversation: (convId: string) => void;
  onSendMessage: (message: string, useRag: boolean, crossNotebook: boolean) => Promise<void>;
  onJumpToMemory: (mem: RagMemoryRef) => void;
}

export function NotebookChatPanel({
  conversations,
  activeConversationId,
  messages,
  streamingText,
  streamingStatus,
  streamingActive,
  quotedContext,
  onClearQuotedContext,
  onSelectConversation,
  onNewConversation,
  onDeleteConversation,
  onSendMessage,
  onJumpToMemory,
}: NotebookChatPanelProps) {
  const [input, setInput] = useState('');
  const [useRag, setUseRag] = useState(true);
  const [crossNotebook, setCrossNotebook] = useState(true);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const text = input.trim();
    if (!text || streamingActive) return;
    setInput('');
    await onSendMessage(text, useRag, crossNotebook);
  };

  return (
    <div className="flex flex-col h-full">
      {/* Conversation Switcher */}
      <div className="p-2.5 border-b border-[#E5E0D5] bg-[#FAF8F4] space-y-2">
        <div className="flex items-center gap-1.5">
          <select
            value={activeConversationId || ''}
            onChange={(e) => {
              if (e.target.value) onSelectConversation(e.target.value);
              else onNewConversation();
            }}
            className="flex-1 rounded-lg border border-[#E5E0D5] bg-white px-2.5 py-1.5 text-xs focus:outline-none"
          >
            <option value="">新对话（基于笔记本资料 + RAG）</option>
            {conversations.map((c) => (
              <option key={c.id} value={c.id}>
                {c.title} ({c.message_count} 条)
              </option>
            ))}
          </select>

          <button
            type="button"
            onClick={onNewConversation}
            title="开启新对话"
            className="p-1.5 rounded-lg border border-[#E5E0D5] bg-white hover:bg-[#EFECE5] cursor-pointer"
          >
            <MessageSquarePlus className="w-4 h-4 text-[#B45309]" />
          </button>

          {activeConversationId && (
            <button
              type="button"
              onClick={() => onDeleteConversation(activeConversationId)}
              title="删除当前对话"
              className="p-1.5 rounded-lg border border-[#E5E0D5] bg-white hover:bg-rose-50 text-[#5E5B53] hover:text-rose-600 cursor-pointer"
            >
              <Trash2 className="w-4 h-4" />
            </button>
          )}
        </div>

        <div className="flex items-center gap-3 text-[11px] text-[#5E5B53]">
          <label className="inline-flex items-center gap-1 cursor-pointer">
            <input
              type="checkbox"
              checked={useRag}
              onChange={(e) => setUseRag(e.target.checked)}
              className="accent-[#B45309]"
            />
            <span>自动调取 RAG 历史记忆</span>
          </label>
          {useRag && (
            <label className="inline-flex items-center gap-1 cursor-pointer">
              <input
                type="checkbox"
                checked={crossNotebook}
                onChange={(e) => setCrossNotebook(e.target.checked)}
                className="accent-[#B45309]"
              />
              <span>跨笔记本记忆</span>
            </label>
          )}
        </div>

        {quotedContext && (
          <div className="rounded-lg bg-amber-50 border border-amber-200 px-2.5 py-1.5 text-[11px] text-amber-900 flex items-center justify-between gap-2">
            <span className="truncate">已引用记忆：{quotedContext}</span>
            <button onClick={onClearQuotedContext} className="text-amber-700 hover:underline shrink-0 cursor-pointer">
              清除
            </button>
          </div>
        )}
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto p-3 space-y-3">
        {messages.length === 0 && !streamingActive ? (
          <div className="text-center py-12 px-4 text-xs text-[#5E5B53]">
            <p className="font-medium text-[#1C1B18]">与当前笔记本进行多轮探讨</p>
            <p className="mt-1">所有对话将自动保存并写入 RAG 数据库，方便日后随时调取。</p>
          </div>
        ) : (
          messages.map((m) => (
            <div
              key={m.id}
              className={`rounded-xl p-3 text-xs ${
                m.role === 'user'
                  ? 'bg-[#EFECE5] text-[#1C1B18] ml-6'
                  : 'bg-white border border-[#E5E0D5] text-[#1C1B18] mr-2'
              }`}
            >
              <div className="text-[10px] text-[#5E5B53] mb-1">
                {m.role === 'user' ? '我' : `agy · ${m.model || ''}`}
              </div>
              {m.role === 'user' ? (
                <div className="whitespace-pre-wrap">{m.content}</div>
              ) : (
                <>
                  <div className="ai-markdown">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{m.content}</ReactMarkdown>
                  </div>
                  {m.retrieved_memories.length > 0 && (
                    <div className="mt-2 pt-1.5 border-t border-[#EFECE5] flex flex-wrap gap-1">
                      {m.retrieved_memories.map((mem) => (
                        <button
                          key={mem.doc_id}
                          onClick={() => onJumpToMemory(mem)}
                          className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-[#FAF8F4] border border-[#E5E0D5] hover:border-[#B45309] text-[10px] text-[#5E5B53] cursor-pointer"
                        >
                          <Database className="w-2.5 h-2.5 text-[#B45309]" />
                          <span className="max-w-[160px] truncate">{mem.title}</span>
                        </button>
                      ))}
                    </div>
                  )}
                </>
              )}
            </div>
          ))
        )}

        {streamingActive && (
          <div className="rounded-xl p-3 bg-white border border-amber-300 text-xs mr-2">
            <div className="flex items-center gap-1.5 text-[11px] text-[#B45309] mb-1">
              <Loader2 className="w-3 h-3 animate-spin" />
              <span>{streamingStatus === 'connecting_agy' ? '正在唤醒 agy CLI...' : 'agy 正在回答...'}</span>
            </div>
            {streamingText && (
              <div className="ai-markdown">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{streamingText}</ReactMarkdown>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Input */}
      <form onSubmit={handleSubmit} className="p-3 border-t border-[#E5E0D5] bg-white flex gap-2">
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="结合资料与历史记忆提问..."
          className="flex-1 rounded-xl border border-[#E5E0D5] bg-[#FAF8F4] px-3 py-2 text-xs focus:outline-none focus:border-[#B45309]"
        />
        <button
          type="submit"
          disabled={streamingActive || !input.trim()}
          className="px-3 py-2 rounded-xl bg-[#B45309] hover:bg-[#92400E] text-white text-xs font-medium disabled:opacity-45 cursor-pointer"
        >
          <Send className="w-4 h-4" />
        </button>
      </form>
    </div>
  );
}
