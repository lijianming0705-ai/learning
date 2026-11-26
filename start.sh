#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOST="${HOST:-0.0.0.0}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
RELOAD="${RELOAD:-1}"
LOG_DIR="${LOG_DIR:-/tmp}"

BACKEND_PID=""
FRONTEND_PID=""
CLEANED_UP=0

get_ancestor_pids() {
  local cur="$$"
  local ancestors=""
  while [[ -n "$cur" && "$cur" =~ ^[0-9]+$ && "$cur" -gt 1 ]]; do
    ancestors="$ancestors $cur"
    local parent
    parent="$(ps -o ppid= -p "$cur" 2>/dev/null | tr -d ' ' || true)"
    if [[ -z "$parent" || "$parent" == "$cur" ]]; then
      break
    fi
    cur="$parent"
  done
  echo "$ancestors"
}

collect_residual_pids() {
  local ancestors
  ancestors="$(get_ancestor_pids)"
  local raw_pids=""
  raw_pids+=$'\n'"$(lsof -tiTCP:"$BACKEND_PORT" -sTCP:LISTEN 2>/dev/null || true)"
  raw_pids+=$'\n'"$(lsof -tiTCP:"$FRONTEND_PORT" -sTCP:LISTEN 2>/dev/null || true)"
  raw_pids+=$'\n'"$(pgrep -f "$ROOT_DIR/backend" 2>/dev/null || true)"
  raw_pids+=$'\n'"$(pgrep -f "uvicorn app.main:app" 2>/dev/null || true)"
  raw_pids+=$'\n'"$(pgrep -f "$ROOT_DIR/data/bin/vision_ocr_helper" 2>/dev/null || true)"
  raw_pids+=$'\n'"$(pgrep -f "$ROOT_DIR/frontend/node_modules/.bin/vite" 2>/dev/null || true)"

  local pids=()
  local pid
  while IFS= read -r pid; do
    [[ "$pid" =~ ^[0-9]+$ ]] || continue
    if [[ " $ancestors " == *" $pid "* ]]; then
      continue
    fi
    if kill -0 "$pid" 2>/dev/null; then
      pids+=("$pid")
    fi
  done < <(printf '%s\n' "$raw_pids" | sort -u)

  if [[ ${#pids[@]} -gt 0 ]]; then
    printf '%s\n' "${pids[@]}"
  fi
}

kill_residual_processes() {
  local pids=()
  local line
  while IFS= read -r line; do
    [[ -n "$line" ]] && pids+=("$line")
  done < <(collect_residual_pids)

  if [[ ${#pids[@]} -eq 0 ]]; then
    echo "未发现残留后台进程。"
    return 0
  fi

  echo "发现残留后台进程 (${pids[*]})，正在优雅终止..."
  local pid
  for pid in "${pids[@]}"; do
    pkill -TERM -P "$pid" 2>/dev/null || true
    kill -TERM "$pid" 2>/dev/null || true
  done

  # Wait up to 4s (8 x 0.5s) for graceful exit
  local attempt
  for attempt in 1 2 3 4 5 6 7 8; do
    local alive=()
    for pid in "${pids[@]}"; do
      if kill -0 "$pid" 2>/dev/null; then
        alive+=("$pid")
      fi
    done
    if [[ ${#alive[@]} -eq 0 ]]; then
      break
    fi
    sleep 0.5
  done

  # Force kill any survivors or anything still holding the ports
  local force_pids=()
  while IFS= read -r line; do
    [[ -n "$line" ]] && force_pids+=("$line")
  done < <(collect_residual_pids)

  if [[ ${#force_pids[@]} -gt 0 ]]; then
    echo "强制清理未退出进程 (${force_pids[*]})..."
    for pid in "${force_pids[@]}"; do
      pkill -KILL -P "$pid" 2>/dev/null || true
      kill -KILL "$pid" 2>/dev/null || true
    done
    sleep 0.5
  fi

  echo "残留进程清理完成。"
}

print_urls() {
  local lan_ip=""
  if command -v ipconfig >/dev/null 2>&1; then
    lan_ip="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
  fi
  echo ""
  echo "InkMind Study 已启动："
  echo "  本机访问:     http://127.0.0.1:$FRONTEND_PORT"
  if [[ -n "$lan_ip" ]]; then
    echo "  手机/平板/墨水屏局域网访问: http://$lan_ip:$FRONTEND_PORT"
  fi
  echo "  API 文档:     http://127.0.0.1:$BACKEND_PORT/docs"
}

cleanup() {
  if [[ "$CLEANED_UP" == "1" ]]; then
    return
  fi
  CLEANED_UP=1
  echo ""
  echo "正在停止 InkMind Study 服务..."
  if [[ -n "$FRONTEND_PID" ]] && kill -0 "$FRONTEND_PID" 2>/dev/null; then
    pkill -TERM -P "$FRONTEND_PID" 2>/dev/null || true
    kill -TERM "$FRONTEND_PID" 2>/dev/null || true
  fi
  if [[ -n "$BACKEND_PID" ]] && kill -0 "$BACKEND_PID" 2>/dev/null; then
    pkill -TERM -P "$BACKEND_PID" 2>/dev/null || true
    kill -TERM "$BACKEND_PID" 2>/dev/null || true
  fi
  wait 2>/dev/null || true
  kill_residual_processes
}

print_status() {
  local b_pids f_pids v_pids
  b_pids="$(lsof -tiTCP:"$BACKEND_PORT" -sTCP:LISTEN 2>/dev/null | tr '\n' ' ' || true)"
  f_pids="$(lsof -tiTCP:"$FRONTEND_PORT" -sTCP:LISTEN 2>/dev/null | tr '\n' ' ' || true)"
  v_pids="$(pgrep -f "$ROOT_DIR/data/bin/vision_ocr_helper" 2>/dev/null | tr '\n' ' ' || true)"

  echo "InkMind Study 服务状态："
  echo "  后端 ($BACKEND_PORT):   ${b_pids:-未运行}"
  echo "  前端 ($FRONTEND_PORT):   ${f_pids:-未运行}"
  echo "  Vision OCR 常驻: ${v_pids:-未运行}"
}

usage() {
  cat <<EOF
用法: $(basename "$0") [start|restart|daemon|stop|status]
  start / restart  清理残留进程并在前台启动前后端（默认）
  daemon / -d      清理残留进程并在后台守护运行
  stop             停止并清理所有 InkMind Study 后台与残留进程
  status           查看端口与后台进程状态
EOF
}

start_backend_cmd() {
  cd "$ROOT_DIR/backend"
  if [[ "$RELOAD" == "1" ]]; then
    exec uv run uvicorn app.main:app --host "$HOST" --port "$BACKEND_PORT" --reload --reload-dir "$ROOT_DIR/backend/app"
  else
    exec uv run uvicorn app.main:app --host "$HOST" --port "$BACKEND_PORT"
  fi
}

start_frontend_cmd() {
  cd "$ROOT_DIR/frontend"
  exec npm run dev -- --host "$HOST" --port "$FRONTEND_PORT" --strictPort
}

ACTION="${1:-start}"

case "$ACTION" in
  -h|--help)
    usage
    exit 0
    ;;
  status)
    print_status
    exit 0
    ;;
  stop)
    kill_residual_processes
    exit 0
    ;;
  daemon|-d|--daemon)
    kill_residual_processes
    echo "后台启动后端 API (http://$HOST:$BACKEND_PORT)..."
    nohup bash -c 'cd "$1/backend" && if [[ "$4" == "1" ]]; then exec uv run uvicorn app.main:app --host "$2" --port "$3" --reload --reload-dir "$1/backend/app"; else exec uv run uvicorn app.main:app --host "$2" --port "$3"; fi' _ "$ROOT_DIR" "$HOST" "$BACKEND_PORT" "$RELOAD" > "$LOG_DIR/inkmind_backend.log" 2>&1 < /dev/null &
    BACKEND_PID=$!

    echo "后台启动前端界面 (http://$HOST:$FRONTEND_PORT)..."
    nohup bash -c 'cd "$1/frontend" && exec npm run dev -- --host "$2" --port "$3" --strictPort' _ "$ROOT_DIR" "$HOST" "$FRONTEND_PORT" > "$LOG_DIR/inkmind_frontend.log" 2>&1 < /dev/null &
    FRONTEND_PID=$!

    backend_ready=0
    for _ in $(seq 1 20); do
      if curl -sf "http://127.0.0.1:$BACKEND_PORT/api/health" >/dev/null 2>&1; then
        backend_ready=1
        break
      fi
      sleep 0.5
    done

    if [[ "$backend_ready" != "1" ]]; then
      echo "警告：后端在 10 秒内未就绪，请检查日志：$LOG_DIR/inkmind_backend.log" >&2
      exit 1
    fi

    print_urls
    echo "  后台日志:     $LOG_DIR/inkmind_backend.log | $LOG_DIR/inkmind_frontend.log"
    echo "  停止服务:     ./start.sh stop"
    ;;
  start|restart)
    kill_residual_processes
    trap cleanup EXIT INT TERM

    echo "启动后端 API (http://$HOST:$BACKEND_PORT)..."
    (start_backend_cmd) &
    BACKEND_PID=$!

    echo "启动前端界面 (http://$HOST:$FRONTEND_PORT)..."
    (start_frontend_cmd) &
    FRONTEND_PID=$!

    print_urls
    echo "按 Ctrl+C 停止服务并自动清理残留进程。"
    wait
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
