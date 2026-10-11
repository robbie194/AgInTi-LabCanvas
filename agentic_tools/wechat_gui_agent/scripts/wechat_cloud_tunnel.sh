#!/usr/bin/env bash
set -euo pipefail

# Local-only SSH forwarding for the private Cloud personal-group API.
# Run this as the normal workstation user (the user owning the SSH key), not
# as overtree-wechat. The Agent profile only consumes 127.0.0.1:33220.
ACTION="${1:-status}"
LOCAL_BIND="${WECHAT_CLOUD_TUNNEL_BIND:-127.0.0.1}"
LOCAL_PORT="${WECHAT_CLOUD_TUNNEL_PORT:-33220}"
REMOTE_HOST="${WECHAT_CLOUD_TUNNEL_HOST:-177.4.41.190}"
REMOTE_USER="${WECHAT_CLOUD_TUNNEL_USER:-root}"
REMOTE_PORT="${WECHAT_CLOUD_TUNNEL_REMOTE_PORT:-3220}"
SSH_PORT="${WECHAT_CLOUD_TUNNEL_SSH_PORT:-22}"
STATE_DIR="${WECHAT_CLOUD_TUNNEL_STATE_DIR:-$HOME/.local/state/overtree-wechat-cloud-tunnel}"
PID_FILE="$STATE_DIR/tunnel.pid"
LOG_FILE="$STATE_DIR/tunnel.log"

mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"

read_pid() {
  [[ -s "$PID_FILE" ]] || return 1
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  printf '%s\n' "$pid"
}

start_tunnel() {
  if read_pid >/dev/null; then
    echo "already_running pid=$(read_pid) local=${LOCAL_BIND}:${LOCAL_PORT}"
    return 0
  fi
  rm -f "$PID_FILE"
  # Restart the SSH forward after a transient network failure. The listener
  # stays on loopback, and ExitOnForwardFailure prevents false readiness.
  setsid nohup bash -c '
    set -u
    while true; do
      ssh -NT \
        -o BatchMode=yes \
        -o ExitOnForwardFailure=yes \
        -o ServerAliveInterval=20 \
        -o ServerAliveCountMax=3 \
        -o ConnectTimeout=10 \
        -o StrictHostKeyChecking=accept-new \
        -p "$1" \
        -L "$2:$3:127.0.0.1:$4" \
        "$5@$6" >>"$7" 2>&1 || true
      sleep 5
    done
  ' _ "$SSH_PORT" "$LOCAL_BIND" "$LOCAL_PORT" "$REMOTE_PORT" "$REMOTE_USER" "$REMOTE_HOST" "$LOG_FILE" \
    >/dev/null 2>&1 &
  local pid=$!
  printf '%s\n' "$pid" >"$PID_FILE"
  chmod 600 "$PID_FILE" "$LOG_FILE" 2>/dev/null || true
  sleep 1
  if ! read_pid >/dev/null; then
    echo "SSH tunnel failed to start; inspect $LOG_FILE" >&2
    rm -f "$PID_FILE"
    return 1
  fi
  echo "started pid=$pid local=${LOCAL_BIND}:${LOCAL_PORT} -> ${REMOTE_HOST}:127.0.0.1:${REMOTE_PORT}"
}

stop_tunnel() {
  local pid
  if ! pid="$(read_pid)"; then
    rm -f "$PID_FILE"
    echo "not_running"
    return 0
  fi
  kill -- "-$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
  for _ in 1 2 3 4 5; do
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
  rm -f "$PID_FILE"
  echo "stopped pid=$pid"
}

case "$ACTION" in
  start|ensure) start_tunnel ;;
  stop) stop_tunnel ;;
  restart) stop_tunnel; start_tunnel ;;
  status)
    if pid="$(read_pid)"; then
      echo "running pid=$pid local=${LOCAL_BIND}:${LOCAL_PORT} remote=${REMOTE_HOST}:127.0.0.1:${REMOTE_PORT}"
    else
      echo "not_running local=${LOCAL_BIND}:${LOCAL_PORT}"
      exit 1
    fi
    ;;
  *)
    echo "Usage: $0 start|ensure|stop|restart|status" >&2
    exit 2
    ;;
esac
