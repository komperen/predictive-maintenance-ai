#!/bin/bash
# Starts the Wave server (waved) and the Python app in one container.
set -u
PORT="${PORT:-8080}"
export H2O_WAVE_LISTEN=":${PORT}"
export H2O_WAVE_ADDRESS="http://127.0.0.1:${PORT}"
export H2O_WAVE_APP_ADDRESS="http://127.0.0.1:8000"
WAVE_HOME="${WAVE_HOME:-/wave}"
export H2O_WAVE_WEB_DIR="${WAVE_HOME}/www"
export H2O_WAVE_DATA_DIR=/tmp/wave-data
mkdir -p "$H2O_WAVE_DATA_DIR"

"${WAVE_HOME}/waved" &
WAVED_PID=$!
sleep 1
python -m uvicorn app:main --host 127.0.0.1 --port 8000 --no-access-log &
APP_PID=$!

trap 'kill $WAVED_PID $APP_PID 2>/dev/null' TERM INT
# If either process dies, stop the container so Kubernetes restarts it.
while kill -0 $WAVED_PID 2>/dev/null && kill -0 $APP_PID 2>/dev/null; do sleep 2; done
kill $WAVED_PID $APP_PID 2>/dev/null
exit 1
