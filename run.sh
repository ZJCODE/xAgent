#!/bin/bash

# run.sh

# Usage:
#   bash run.sh            # 启动所有服务
#   bash run.sh start      # 启动所有服务
#   bash run.sh stop       # 停止所有服务
#   bash run.sh restart    # 重启所有服务
#   bash run.sh status     # 查看服务状态

set -euo pipefail

# conda activate xagent

# export PYTHONPATH="${pwd}"

APP_ROOT=$(cd "$(dirname "$0")"; pwd)
export PYTHONPATH="$APP_ROOT"

# Load .env if present
if [ -f "$APP_ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$APP_ROOT/.env"
  set +a
fi

# PID files
MCP_PID_FILE="$APP_ROOT/logs/mcp_server.pid"
FRONTEND_PID_FILE="$APP_ROOT/logs/frontend.pid"
AGENT_PID_FILE="$APP_ROOT/logs/agent.pid"

# Default ports (can be overridden by env)
FRONTEND_PORT="${FRONTEND_PORT:-8501}"
AGENT_HOST="${AGENT_HOST:-0.0.0.0}"
AGENT_PORT="${AGENT_PORT:-8010}"

ensure_logs_dir() {
  mkdir -p "$APP_ROOT/logs"
}

is_running() {
  local pid="$1"
  if kill -0 "$pid" 2>/dev/null; then
    return 0
  else
    return 1
  fi
}

start_process() {
  local name="$1"
  local cmd="$2"
  local pidfile="$3"
  local logfile="$4"

  if [ -f "$pidfile" ]; then
    local pid
    pid=$(cat "$pidfile") || true
    if [ -n "${pid:-}" ] && is_running "$pid"; then
      echo "$name already running (PID $pid)"
      return 0
    fi
  fi

  echo "Starting $name..."
  nohup bash -lc "$cmd" > "$logfile" 2>&1 &
  echo $! > "$pidfile"
  echo "Started $name (PID $(cat "$pidfile"))"
}

stop_pidfile() {
  local pidfile="$1"
  if [ -f "$pidfile" ]; then
    local pid
    pid=$(cat "$pidfile") || true
    if [ -n "${pid:-}" ] && is_running "$pid"; then
      kill "$pid" || true
      sleep 1
      if is_running "$pid"; then
        echo "Process $pid did not stop gracefully, force killing..."
        kill -9 "$pid" || true
      fi
      echo "Stopped process $pid from $pidfile"
    fi
    rm -f "$pidfile"
  fi
}

start_services() {
  echo "Starting the application..."
  ensure_logs_dir

  # MCP Server
  start_process \
    "MCP server" \
    "python xagent/tools/mcp_server.py" \
    "$MCP_PID_FILE" \
    "$APP_ROOT/logs/mcp_server.log"

  # Frontend (Streamlit)
  start_process \
    "Frontend" \
    "streamlit run frontend/chat_app.py --server.port $FRONTEND_PORT" \
    "$FRONTEND_PID_FILE" \
    "$APP_ROOT/logs/frontend.log"

  # Agent HTTP Server (xagent/core/server.py)
  start_process \
    "Agent HTTP server" \
    "python xagent/core/server.py --config config/agent.yaml --host $AGENT_HOST --port $AGENT_PORT" \
    "$AGENT_PID_FILE" \
    "$APP_ROOT/logs/agent.log"

  echo "All services started."
}

stop_services() {
  echo "Stopping services..."
  for pidfile in "$MCP_PID_FILE" "$FRONTEND_PID_FILE" "$AGENT_PID_FILE"; do
    stop_pidfile "$pidfile"
  done
  echo "All services stopped."
}

status_services() {
  for pidfile in "$MCP_PID_FILE" "$FRONTEND_PID_FILE" "$AGENT_PID_FILE"; do
    svc=$(basename "$pidfile" .pid)
    if [ -f "$pidfile" ]; then
      pid=$(cat "$pidfile") || true
      if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
        echo "$svc is running (PID $pid)"
      else
        echo "$svc is not running"
      fi
    else
      echo "$svc is not running"
    fi
  done
}

case "${1:-start}" in
  start)
    start_services
    ;;
  stop)
    stop_services
    ;;
  restart)
    stop_services
    sleep 1
    start_services
    ;;
  status)
    status_services
    ;;
  *)
    echo "Unknown command: $1"
    echo "Usage: $0 [start|stop|restart|status]"
    exit 1
    ;;
 esac