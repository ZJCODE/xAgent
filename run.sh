#!/bin/bash

# run.sh

# Usage:
#   bash run.sh            # 启动所有服务
#   bash run.sh start      # 启动所有服务
#   bash run.sh stop       # 停止所有服务
#   bash run.sh restart    # 重启所有服务
#   bash run.sh status     # 查看服务状态
#   bash run.sh start-mcp  # 只启动 MCP 服务器
#   bash run.sh start-agent # 只启动 Agent 服务器
#   bash run.sh start-frontend # 只启动前端
#   bash run.sh logs       # 查看所有服务日志

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
MCP_PORT="${MCP_PORT:-8001}"

DEFAULT_CONFIG_PATH="$APP_ROOT/config/agent.yaml"
DEFAULT_TOOLKIT_PATH="$APP_ROOT/toolkit"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

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
      echo -e "${YELLOW}$name already running (PID $pid)${NC}"
      return 0
    fi
  fi

  echo -e "${GREEN}Starting $name...${NC}"
  nohup bash -lc "$cmd" > "$logfile" 2>&1 &
  echo $! > "$pidfile"
  echo -e "${GREEN}Started $name (PID $(cat "$pidfile"))${NC}"
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
        echo -e "${YELLOW}Process $pid did not stop gracefully, force killing...${NC}"
        kill -9 "$pid" || true
      fi
      echo -e "${GREEN}Stopped process $pid from $pidfile${NC}"
    fi
    rm -f "$pidfile"
  fi
}

start_services() {
  echo -e "${GREEN}Starting the application...${NC}"
  ensure_logs_dir

  # MCP Server
  start_process \
    "MCP server" \
    "python toolkit/mcp_server.py" \
    "$MCP_PID_FILE" \
    "$APP_ROOT/logs/mcp_server.log"

  # Wait a bit for MCP server to start
  sleep 2

  # Agent HTTP Server (xagent/core/server.py)
  start_process \
    "Agent HTTP server" \
    "python xagent/core/server.py --config $DEFAULT_CONFIG_PATH --toolkit_path $DEFAULT_TOOLKIT_PATH --host $AGENT_HOST --port $AGENT_PORT" \
    "$AGENT_PID_FILE" \
    "$APP_ROOT/logs/agent.log"

  # Wait a bit for Agent server to start
  sleep 2

  # Frontend (Streamlit)
  start_process \
    "Frontend" \
    "streamlit run frontend/chat_app.py --server.port $FRONTEND_PORT" \
    "$FRONTEND_PID_FILE" \
    "$APP_ROOT/logs/frontend.log"

  echo -e "${GREEN}All services started.${NC}"
  echo -e "${YELLOW}Access the application at: http://localhost:$FRONTEND_PORT${NC}"
  echo -e "${YELLOW}Agent API available at: http://localhost:$AGENT_PORT${NC}"
  echo -e "${YELLOW}MCP server running on port: $MCP_PORT${NC}"
}

stop_services() {
  echo -e "${RED}Stopping services...${NC}"
  for pidfile in "$MCP_PID_FILE" "$FRONTEND_PID_FILE" "$AGENT_PID_FILE"; do
    stop_pidfile "$pidfile"
  done
  echo -e "${GREEN}All services stopped.${NC}"
}

status_services() {
  echo -e "${YELLOW}Service Status:${NC}"
  for pidfile in "$MCP_PID_FILE" "$AGENT_PID_FILE" "$FRONTEND_PID_FILE"; do
    svc=$(basename "$pidfile" .pid)
    if [ -f "$pidfile" ]; then
      pid=$(cat "$pidfile") || true
      if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
        echo -e "${GREEN}✓ $svc is running (PID $pid)${NC}"
      else
        echo -e "${RED}✗ $svc is not running${NC}"
      fi
    else
      echo -e "${RED}✗ $svc is not running${NC}"
    fi
  done
}

# Individual service start functions
start_mcp() {
  ensure_logs_dir
  start_process \
    "MCP server" \
    "python xagent/tools/mcp_server.py" \
    "$MCP_PID_FILE" \
    "$APP_ROOT/logs/mcp_server.log"
}

start_agent() {
  ensure_logs_dir
  start_process \
    "Agent HTTP server" \
    "python xagent/core/server.py --config config/agent.yaml --host $AGENT_HOST --port $AGENT_PORT" \
    "$AGENT_PID_FILE" \
    "$APP_ROOT/logs/agent.log"
}

start_frontend() {
  ensure_logs_dir
  start_process \
    "Frontend" \
    "streamlit run frontend/chat_app.py --server.port $FRONTEND_PORT" \
    "$FRONTEND_PID_FILE" \
    "$APP_ROOT/logs/frontend.log"
}

show_logs() {
  echo -e "${YELLOW}Showing logs for all services (Ctrl+C to exit):${NC}"
  tail -f "$APP_ROOT/logs/"*.log 2>/dev/null || echo -e "${RED}No log files found${NC}"
}

check_dependencies() {
  echo -e "${YELLOW}Checking dependencies...${NC}"
  
  # Check Python
  if ! command -v python &> /dev/null; then
    echo -e "${RED}✗ Python not found${NC}"
    return 1
  else
    echo -e "${GREEN}✓ Python found: $(python --version)${NC}"
  fi
  
  # Check if requirements are installed
  if ! python -c "import streamlit, fastapi, httpx" &> /dev/null; then
    echo -e "${YELLOW}⚠ Some dependencies may be missing. Run: pip install -r requirements.txt${NC}"
  else
    echo -e "${GREEN}✓ Main dependencies found${NC}"
  fi
  
  # Check config file
  if [ -f "$APP_ROOT/config/agent.yaml" ]; then
    echo -e "${GREEN}✓ Configuration file found${NC}"
  else
    echo -e "${RED}✗ Configuration file not found: config/agent.yaml${NC}"
  fi
}

case "${1:-start}" in
  start)
    check_dependencies
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
  start-mcp)
    start_mcp
    ;;
  start-agent)
    start_agent
    ;;
  start-frontend)
    start_frontend
    ;;
  logs)
    show_logs
    ;;
  check)
    check_dependencies
    ;;
  *)
    echo -e "${YELLOW}xAgent Service Manager${NC}"
    echo ""
    echo "Usage: $0 [command]"
    echo ""
    echo "Commands:"
    echo "  start          Start all services (default)"
    echo "  stop           Stop all services"
    echo "  restart        Restart all services"
    echo "  status         Show service status"
    echo "  start-mcp      Start only MCP server"
    echo "  start-agent    Start only Agent HTTP server"
    echo "  start-frontend Start only Frontend (Streamlit)"
    echo "  logs           Show logs for all services"
    echo "  check          Check dependencies and configuration"
    echo ""
    echo "Environment variables:"
    echo "  FRONTEND_PORT  Frontend port (default: 8501)"
    echo "  AGENT_HOST     Agent server host (default: 0.0.0.0)"
    echo "  AGENT_PORT     Agent server port (default: 8010)"
    echo "  MCP_PORT       MCP server port (default: 8001)"
    exit 1
    ;;
esac