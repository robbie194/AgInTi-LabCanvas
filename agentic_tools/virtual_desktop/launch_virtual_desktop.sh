#!/usr/bin/env bash
set -euo pipefail

NAME="virtual-gui"
DISPLAY_ID=":98"
SCREEN="1920x1080x24"
VNC_PORT="5908"
NOVNC_PORT="6099"
LOG_DIR="output/virtual_desktop/$(date +%F)"
KEEP_AWAKE="${VIRTUAL_DESKTOP_KEEP_AWAKE:-1}"
KEEP_AWAKE_INTERVAL="${VIRTUAL_DESKTOP_KEEP_AWAKE_INTERVAL:-55}"
OPEN_BROWSER="0"
APP_MATCH=""
APP_COMMAND=()
RECOVER_STALE_DISPLAY="0"
DISABLE_XDAMAGE="0"
PROBE_TIMEOUT_SECONDS="${VIRTUAL_DESKTOP_PROBE_TIMEOUT_SECONDS:-3}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
USER_NAME="${USER:-$(id -un)}"

usage() {
  cat <<'EOF'
Usage:
  launch_virtual_desktop.sh [options] -- [app command...]

Options:
  --name NAME          Label for log files. Default: virtual-gui
  --display :N         X display. Default: :98
  --screen WxHxD       Xvfb screen. Default: 1920x1080x24
  --vnc-port PORT      localhost x11vnc port. Default: 5908
  --novnc-port PORT    localhost noVNC/websockify port. Default: 6099
  --log-dir DIR        Log directory. Default: output/virtual_desktop/YYYY-MM-DD
  --app-match TEXT     Do not relaunch app if a matching process is already running.
  --recover-stale-display
                       Restart only the selected Xvfb/x11vnc pair when its
                       display exists but does not answer bounded X11 probes.
  --no-xdamage        Poll pixels instead of relying on XDamage notifications.
  --no-keep-awake      Do not disable X11 blanking/DPMS.
  --keep-awake-interval SECONDS
                       Refresh X11 keep-awake state on this interval. Default: 55
  --open-browser       Open Chromium/Chrome app window pointed at noVNC.

Example:
  launch_virtual_desktop.sh --name labview --display :98 --vnc-port 5908 --novnc-port 6099 -- \
    /usr/local/natinst/LabVIEW-2026-64/labview
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --display) DISPLAY_ID="$2"; shift 2 ;;
    --screen) SCREEN="$2"; shift 2 ;;
    --vnc-port) VNC_PORT="$2"; shift 2 ;;
    --novnc-port) NOVNC_PORT="$2"; shift 2 ;;
    --log-dir) LOG_DIR="$2"; shift 2 ;;
    --app-match) APP_MATCH="$2"; shift 2 ;;
    --recover-stale-display) RECOVER_STALE_DISPLAY="1"; shift ;;
    --no-xdamage) DISABLE_XDAMAGE="1"; shift ;;
    --no-keep-awake) KEEP_AWAKE="0"; shift ;;
    --keep-awake-interval) KEEP_AWAKE_INTERVAL="$2"; shift 2 ;;
    --open-browser) OPEN_BROWSER="1"; shift ;;
    --help|-h) usage; exit 0 ;;
    --) shift; APP_COMMAND=("$@"); break ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

need() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 3
  fi
}

need Xvfb
need x11vnc
need websockify
need xdpyinfo
need xwininfo
need timeout

mkdir -p "$LOG_DIR"

display_number="${DISPLAY_ID#:}"
display_number="${display_number%%.*}"
socket_path="/tmp/.X11-unix/X$display_number"
lock_path="/tmp/.X$display_number-lock"
escaped_display="${DISPLAY_ID//./\\.}"

display_ready() {
  timeout "${PROBE_TIMEOUT_SECONDS}s" env DISPLAY="$DISPLAY_ID" XAUTHORITY= xdpyinfo >/dev/null 2>&1
}

matching_pids() {
  local pattern="$1"
  pgrep -u "$USER_NAME" -f "$pattern" 2>/dev/null || true
}

stop_exact_processes() {
  local label="$1"
  shift
  local deadline pid
  local -a pids=("$@")
  ((${#pids[@]} > 0)) || return 0
  echo "Stopping stale $label process(es): ${pids[*]}"
  for pid in "${pids[@]}"; do
    kill -TERM "$pid" 2>/dev/null || true
  done
  deadline=$((SECONDS + 8))
  while (( SECONDS < deadline )); do
    local alive="0"
    for pid in "${pids[@]}"; do
      if kill -0 "$pid" 2>/dev/null; then
        alive="1"
        break
      fi
    done
    [[ "$alive" == "0" ]] && return 0
    sleep 1
  done
  echo "Refusing to replace $label because its old process did not exit." >&2
  return 1
}

recover_stale_display() {
  local xvfb_pattern x11vnc_pattern
  local -a xvfb_pids=() x11vnc_pids=()
  xvfb_pattern="^([^[:space:]]*/)?Xvfb[[:space:]]+${escaped_display}([[:space:]]|$)"
  mapfile -t xvfb_pids < <(matching_pids "$xvfb_pattern")
  ((${#xvfb_pids[@]} > 0)) || return 0
  x11vnc_pattern="^([^[:space:]]*/)?x11vnc[[:space:]].*-display[[:space:]]+${escaped_display}([[:space:]].*)?-rfbport[[:space:]]+${VNC_PORT}([[:space:]]|$)"
  mapfile -t x11vnc_pids < <(matching_pids "$x11vnc_pattern")
  stop_exact_processes "x11vnc relay for $DISPLAY_ID" "${x11vnc_pids[@]}"
  stop_exact_processes "Xvfb display $DISPLAY_ID" "${xvfb_pids[@]}"
}

if ! display_ready; then
  if [[ "$RECOVER_STALE_DISPLAY" == "1" ]]; then
    recover_stale_display
  fi
  if [[ -S "$socket_path" ]] && ! pgrep -u "$USER_NAME" -f "Xvfb $DISPLAY_ID( |$)" >/dev/null 2>&1; then
    rm -f "$socket_path"
  fi
  if [[ -f "$lock_path" ]] && ! pgrep -u "$USER_NAME" -f "Xvfb $DISPLAY_ID( |$)" >/dev/null 2>&1; then
    rm -f "$lock_path"
  fi
  echo "Starting Xvfb $DISPLAY_ID with $SCREEN"
  env XAUTHORITY= setsid Xvfb "$DISPLAY_ID" -screen 0 "$SCREEN" -ac >"$LOG_DIR/${NAME}_xvfb.log" 2>&1 < /dev/null &
  sleep 2
fi

if ! display_ready; then
  echo "Display $DISPLAY_ID is not reachable." >&2
  tail -n 80 "$LOG_DIR/${NAME}_xvfb.log" 2>/dev/null || true
  exit 4
fi

if [[ "$KEEP_AWAKE" == "1" ]]; then
  state_dir="$(dirname "$LOG_DIR")"
  mkdir -p "$state_dir"
  keep_awake_id="${DISPLAY_ID#:}"
  keep_awake_id="${keep_awake_id//[^A-Za-z0-9_.-]/_}"
  "$SCRIPT_DIR/start_keep_awake.sh" \
    --display "$DISPLAY_ID" \
    --interval "$KEEP_AWAKE_INTERVAL" \
    --pid-file "$state_dir/${NAME}_${keep_awake_id}_keep_awake.pid" \
    --log-file "$LOG_DIR/${NAME}_keep_awake.log" || true
fi

if ! ss -ltn | awk '{print $4}' | grep -Eq "(^|:)${VNC_PORT}$"; then
  echo "Starting x11vnc on 127.0.0.1:$VNC_PORT for $DISPLAY_ID"
  vnc_extra_args=()
  [[ "$DISABLE_XDAMAGE" == "1" ]] && vnc_extra_args+=(-noxdamage)
  env -u WAYLAND_DISPLAY x11vnc "${vnc_extra_args[@]}" -display "$DISPLAY_ID" -localhost -nopw -forever -shared -rfbport "$VNC_PORT" \
    -bg -o "$LOG_DIR/${NAME}_x11vnc.log" >/dev/null
fi

# Apply the selected polling mode to a reused relay as well as a new one.
if [[ "$DISABLE_XDAMAGE" == "1" ]]; then
  timeout "${PROBE_TIMEOUT_SECONDS}s" env DISPLAY="$DISPLAY_ID" XAUTHORITY= \
    x11vnc -display "$DISPLAY_ID" -R noxdamage -sync >/dev/null 2>&1 || true
fi

if ! ss -ltn | awk '{print $4}' | grep -Eq "(^|:)${NOVNC_PORT}$"; then
  echo "Starting noVNC on http://127.0.0.1:$NOVNC_PORT/"
  websockify -D --web=/usr/share/novnc --log-file="$LOG_DIR/${NAME}_novnc.log" \
    "127.0.0.1:$NOVNC_PORT" "127.0.0.1:$VNC_PORT"
fi

if [[ ${#APP_COMMAND[@]} -gt 0 ]]; then
  MATCHING_APP_PIDS=""
  if [[ -n "$APP_MATCH" ]]; then
    MATCHING_APP_PIDS="$(pgrep -u "$USER_NAME" -f "$APP_MATCH" | grep -v -E "^($$|$PPID)$" || true)"
  fi
  if [[ -n "$MATCHING_APP_PIDS" ]]; then
    echo "App already running: $APP_MATCH"
  else
    echo "Starting app on $DISPLAY_ID: ${APP_COMMAND[*]}"
    env DISPLAY="$DISPLAY_ID" XAUTHORITY= setsid "${APP_COMMAND[@]}" >"$LOG_DIR/${NAME}_app.log" 2>&1 < /dev/null &
  fi
fi

NOVNC_URL="http://127.0.0.1:$NOVNC_PORT/vnc.html?host=127.0.0.1&port=$NOVNC_PORT&autoconnect=1&resize=scale&reconnect=1&reconnect_delay=1000"

if [[ "$OPEN_BROWSER" == "1" ]]; then
  if command -v google-chrome >/dev/null 2>&1; then
    google-chrome --user-data-dir="$HOME/.cache/${NAME}-novnc-${NOVNC_PORT}" --no-first-run --disable-gpu \
      --app="$NOVNC_URL" >"$LOG_DIR/${NAME}_chrome.log" 2>&1 &
  elif command -v chromium >/dev/null 2>&1; then
    chromium --user-data-dir="$HOME/.cache/${NAME}-novnc-${NOVNC_PORT}" --no-first-run --disable-gpu \
      --app="$NOVNC_URL" >"$LOG_DIR/${NAME}_chromium.log" 2>&1 &
  fi
fi

echo
echo "Virtual desktop ready"
echo "  display: $DISPLAY_ID"
screen_summary="$(timeout "${PROBE_TIMEOUT_SECONDS}s" env DISPLAY="$DISPLAY_ID" XAUTHORITY= xdpyinfo 2>/dev/null \
  | awk -F: '/dimensions|depth of root window/ {gsub(/^ +/, "", $2); print $1 ":" $2}' \
  | paste -sd ', ' - || true)"
echo "  screen:  ${screen_summary:-unavailable}"
echo "  vnc:     127.0.0.1:$VNC_PORT"
echo "  noVNC:   $NOVNC_URL"
echo "  awake:   $KEEP_AWAKE"
echo "  logs:    $LOG_DIR"
echo
echo "Windows:"
timeout "${PROBE_TIMEOUT_SECONDS}s" env DISPLAY="$DISPLAY_ID" XAUTHORITY= xwininfo -root -tree 2>/dev/null \
  | sed -n '1,40p' || echo "  window probe unavailable"
