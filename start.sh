#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOST="${HOST:-0.0.0.0}"
BACKEND_PID=""
FRONTEND_PID=""

cleanup() {
  echo ""
  echo "正在停止 InkMind Study 服务..."
  if [[ -n "$FRONTEND_PID" ]] && kill -0 "$FRONTEND_PID" 2>/dev/null; then
    kill "$FRONTEND_PID" 2>/dev/null || true
  fi
  if [[ -n "$BACKEND_PID" ]] && kill -0 "$BACKEND_PID" 2>/dev/null; then
    kill "$BACKEND_PID" 2>/dev/null || true
  fi
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "启动后端 API (http://$HOST:8000)..."
(
  cd "$ROOT_DIR/backend"
  uv run uvicorn app.main:app --host "$HOST" --port 8000
) &
BACKEND_PID=$!

echo "启动前端界面 (http://$HOST:5173)..."
(
  cd "$ROOT_DIR/frontend"
  npm run dev -- --host "$HOST" --port 5173
) &
FRONTEND_PID=$!

LAN_IP=""
if command -v ipconfig >/dev/null 2>&1; then
  LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
fi

echo ""
echo "InkMind Study 已启动："
echo "  本机访问:     http://127.0.0.1:5173"
if [[ -n "$LAN_IP" ]]; then
  echo "  手机/平板/墨水屏局域网访问: http://$LAN_IP:5173"
fi
echo "  API 文档:     http://127.0.0.1:8000/docs"
echo "按 Ctrl+C 停止服务。"
wait
