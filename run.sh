#!/bin/bash

# run.sh

# Usage:
#   bash run.sh         # 启动所有服务
#   bash run.sh stop    # 停止所有服务
#   bash run.sh status  # 查看服务状态

# conda activate xagent

APP_ROOT=$(cd "$(dirname "$0")"; pwd)
export PYTHONPATH="$APP_ROOT"

API_PID_FILE="$APP_ROOT/logs/api.pid"
MCP_PID_FILE="$APP_ROOT/logs/mcp_server.pid"
FRONTEND_PID_FILE="$APP_ROOT/logs/frontend.pid"

start_services() {
    echo "Starting the application..."

    mkdir -p "$APP_ROOT/logs"

    echo "Starting the API server..."
    nohup uvicorn api.main:app --reload > logs/api.log 2>&1 &
    echo $! > "$API_PID_FILE"

    echo "Starting the MCP server..."
    nohup python tools/mcp_server.py > logs/mcp_server.log 2>&1 &
    echo $! > "$MCP_PID_FILE"

    echo "Starting the frontend..."
    nohup streamlit run frontend/chat_app.py > logs/frontend.log 2>&1 &
    echo $! > "$FRONTEND_PID_FILE"

    echo "All services started."
}

stop_services() {
    echo "Stopping services..."

    for pidfile in "$API_PID_FILE" "$MCP_PID_FILE" "$FRONTEND_PID_FILE"; do
        if [ -f "$pidfile" ]; then
            pid=$(cat "$pidfile")
            if kill -0 "$pid" 2>/dev/null; then
                kill "$pid"
                echo "Stopped process $pid from $pidfile"
            fi
            rm -f "$pidfile"
        fi
    done
    echo "All services stopped."
}

status_services() {
    for pidfile in "$API_PID_FILE" "$MCP_PID_FILE" "$FRONTEND_PID_FILE"; do
        if [ -f "$pidfile" ]; then
            pid=$(cat "$pidfile")
            if kill -0 "$pid" 2>/dev/null; then
                echo "$(basename "$pidfile" .pid) is running (PID $pid)"
            else
                echo "$(basename "$pidfile" .pid) is not running"
            fi
        else
            echo "$(basename "$pidfile" .pid) is not running"
        fi
    done
}

case "$1" in
    stop)
        stop_services
        ;;
    status)
        status_services
        ;;
    *)
        start_services
        ;;
esac