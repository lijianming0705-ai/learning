# InkMind Study · 个人智能学习平台

基于 `antigravity-cli` (`agy`) + FastAPI + React + SQLite Hybrid RAG 构建的本地优先个人 Web 学习平台。

## 核心功能

1. **笔记本管理**：创建多个主题学习笔记本，集中管理资料、划线与问答沉淀。
2. **多源素材导入**：支持拖入本地 **PDF**、**Word (`.docx`)**、**Markdown / TXT**，以及直接输入网页 URL 自动抽取正文。
3. **PDF & 文档划线精读 + 备注式 AI 问答**：
   - PDF 支持 **文字划词高亮** 与 **区域框选高亮**（公式/图表/扫描页同样适用），提供 5 种高亮颜色。
   - 每条划线绑定独立的个人笔记与持久化 **AI 问答备注线程**，所有历史提问与回答永久保留在右侧备注栏中，随时回顾与追问。
4. **`agy` CLI 后端模型驱动**：
   - 自动读取 `agy models` 列表，通过 `agy --input-format stream-json --output-format stream-json` 连接模型并流式返回解答，池化复用暖会话以大幅降低多轮延迟。
5. **独立 Hybrid RAG 历史记忆库 (`data/rag.sqlite`)**：
   - 每次 AI 回答、划线笔记与文档切片均实时写入独立的混合 RAG 数据库（L2 归一化多语种向量检索 + CJK 分词 FTS5 BM25 + RRF 融合排序）。
   - 支持在问答中自动召回过往跨笔记本记忆，也支持随时在 RAG 记忆库面板或 `⌘K` 全局弹窗中检索并一键跳转回原文档与原划线位置。

## 环境要求

- `agy` (`antigravity-cli`) 已安装并完成登录（可在终端运行 `agy` 验证）
- Python >= 3.12 & `uv`
- Node.js >= 20 & `npm`

## 快速启动

```bash
# 1. 安装后端与前端依赖（首次）
cd backend && uv sync
cd ../frontend && npm install

# 2. 一键启动前后端
cd ..
./start.sh
```

启动后访问：`http://127.0.0.1:5173`

## 运行测试

```bash
cd backend && uv run pytest -v
cd frontend && npm run build
```
