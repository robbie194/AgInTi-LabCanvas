#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
ACTION="${1:-ensure}"
DISPLAY_ID="${WECHAT_DISPLAY:-:97}"
VNC_PORT="${WECHAT_VNC_PORT:-5917}"
NOVNC_PORT="${WECHAT_NOVNC_PORT:-6107}"
LOG_DIR="${WECHAT_AGENT_LOG_DIR:-$ROOT/output/virtual_desktop/$(date +%F)}"
PRIVATE_DIR="${WECHAT_AGENT_PRIVATE_DIR:-$ROOT/agentic_tools/wechat_gui_agent/.private}"
WECHAT_HOME="${WECHAT_AGENT_HOME:-$HOME}"
CLIENT_LOCK="$PRIVATE_DIR/wechat_client_lifecycle.lock"
KEEP_AWAKE_INTERVAL="${WECHAT_KEEP_AWAKE_INTERVAL:-55}"
MIN_WINDOW_WIDTH="${WECHAT_MIN_WINDOW_WIDTH:-640}"
MIN_WINDOW_HEIGHT="${WECHAT_MIN_WINDOW_HEIGHT:-480}"
START_WAIT_SECONDS="${WECHAT_START_WAIT_SECONDS:-12}"
RESTART_WAIT_SECONDS="${WECHAT_RESTART_WAIT_SECONDS:-12}"
AUTO_RECOVER_UNMAPPED="${WECHAT_AUTO_RECOVER_UNMAPPED:-1}"
X11_PROBE_TIMEOUT_SECONDS="${WECHAT_X11_PROBE_TIMEOUT_SECONDS:-3}"
mkdir -p "$LOG_DIR"
mkdir -p "$PRIVATE_DIR"
LAUNCH_LOG="$LOG_DIR/wechat_virtual_desktop_launch.log"
APP_LOG="$LOG_DIR/wechat_app.log"

case "$ACTION" in
  ensure|restart-client) ;;
  *)
    echo "Usage: $0 [ensure|restart-client]" >&2
    exit 2
    ;;
esac

wechat_main_window() {
  local window_id geometry width height
  while IFS= read -r window_id; do
    [[ -n "$window_id" ]] || continue
    geometry="$(timeout "${X11_PROBE_TIMEOUT_SECONDS}s" env DISPLAY="$DISPLAY_ID" XAUTHORITY= \
      xdotool getwindowgeometry --shell "$window_id" 2>/dev/null || true)"
    width="$(awk -F= '$1 == "WIDTH" {print $2}' <<<"$geometry")"
    height="$(awk -F= '$1 == "HEIGHT" {print $2}' <<<"$geometry")"
    if [[ "$width" =~ ^[0-9]+$ && "$height" =~ ^[0-9]+$ ]] \
      && (( width >= MIN_WINDOW_WIDTH && height >= MIN_WINDOW_HEIGHT )); then
      printf '%s\n' "$window_id"
      return 0
    fi
  done < <(timeout "${X11_PROBE_TIMEOUT_SECONDS}s" env DISPLAY="$DISPLAY_ID" XAUTHORITY= \
    xdotool search --onlyvisible --class wechat 2>/dev/null || true)
  return 1
}

wechat_visible_window() {
  local window_id geometry width height
  while IFS= read -r window_id; do
    [[ -n "$window_id" ]] || continue
    geometry="$(timeout "${X11_PROBE_TIMEOUT_SECONDS}s" env DISPLAY="$DISPLAY_ID" XAUTHORITY= \
      xdotool getwindowgeometry --shell "$window_id" 2>/dev/null || true)"
    width="$(awk -F= '$1 == "WIDTH" {print $2}' <<<"$geometry")"
    height="$(awk -F= '$1 == "HEIGHT" {print $2}' <<<"$geometry")"
    if [[ "$width" =~ ^[0-9]+$ && "$height" =~ ^[0-9]+$ ]] \
      && (( width >= 120 && height >= 120 )); then
      printf '%s\n' "$window_id"
      return 0
    fi
  done < <(timeout "${X11_PROBE_TIMEOUT_SECONDS}s" env DISPLAY="$DISPLAY_ID" XAUTHORITY= \
    xdotool search --onlyvisible --class wechat 2>/dev/null || true)
  return 1
}

wechat_pids_on_display() {
  local pid
  while IFS= read -r pid; do
    [[ -r "/proc/$pid/environ" ]] || continue
    if tr '\0' '\n' <"/proc/$pid/environ" | grep -Fxq "DISPLAY=$DISPLAY_ID"; then
      printf '%s\n' "$pid"
    fi
  done < <(pgrep -f '^/usr/bin/wechat([[:space:]]|$)' 2>/dev/null || true)
}

launch_wechat() {
  env \
    -u WAYLAND_DISPLAY \
    -u QT_PLUGIN_PATH \
    -u QT_QPA_PLATFORM_PLUGIN_PATH \
    -u QT_QPA_FONTDIR \
    -u QT_STYLE_OVERRIDE \
    HOME="$WECHAT_HOME" \
    XDG_CONFIG_HOME="${WECHAT_AGENT_XDG_CONFIG_HOME:-$WECHAT_HOME/.config}" \
    XDG_DATA_HOME="${WECHAT_AGENT_XDG_DATA_HOME:-$WECHAT_HOME/.local/share}" \
    XDG_CACHE_HOME="${WECHAT_AGENT_XDG_CACHE_HOME:-$WECHAT_HOME/.cache}" \
    DISPLAY="$DISPLAY_ID" XAUTHORITY= NO_AT_BRIDGE=1 QT_QPA_PLATFORM=xcb \
    setsid -f /usr/bin/wechat 9>&- >>"$APP_LOG" 2>&1
}

wait_for_main_window() {
  local timeout="$1" deadline window_id
  deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    if window_id="$(wechat_main_window)"; then
      printf '%s\n' "$window_id"
      return 0
    fi
    sleep 1
  done
  return 1
}

stop_stale_wechat() {
  local deadline pid
  local -a pids=()
  mapfile -t pids < <(wechat_pids_on_display)
  ((${#pids[@]} > 0)) || return 0
  for pid in "${pids[@]}"; do
    kill -TERM "$pid" 2>/dev/null || true
  done
  deadline=$((SECONDS + RESTART_WAIT_SECONDS))
  while (( SECONDS < deadline )); do
    mapfile -t pids < <(wechat_pids_on_display)
    ((${#pids[@]} == 0)) && return 0
    sleep 1
  done
  return 1
}

exec 9>"$CLIENT_LOCK"
if ! flock -w "${WECHAT_CLIENT_LIFECYCLE_LOCK_WAIT_SECONDS:-20}" 9; then
  echo "WeChat client lifecycle operation is already active; leaving the persisted client untouched." >&2
  exit 75
fi

if ! timeout "${X11_PROBE_TIMEOUT_SECONDS}s" env DISPLAY="$DISPLAY_ID" XAUTHORITY= xdpyinfo >/dev/null 2>&1; then
  echo "WeChat display $DISPLAY_ID is stale; stopping only its display-bound client before recovery." >>"$APP_LOG"
  if ! stop_stale_wechat; then
    echo "WeChat did not exit before stale-display recovery; refusing to continue." >&2
    exit 1
  fi
fi

"$ROOT/agentic_tools/virtual_desktop/launch_virtual_desktop.sh" \
  --name wechat \
  --display "$DISPLAY_ID" \
  --screen 1920x1080x24 \
  --vnc-port "$VNC_PORT" \
  --novnc-port "$NOVNC_PORT" \
  --recover-stale-display \
  --keep-awake-interval "$KEEP_AWAKE_INTERVAL" \
  --log-dir "$LOG_DIR" \
  -- /bin/true 9>&- >"$LAUNCH_LOG"

main_window=""
visible_window=""
if [[ "$ACTION" == "restart-client" ]]; then
  echo "Gracefully restarting the input-stalled WeChat client on $DISPLAY_ID while preserving its profile." >>"$APP_LOG"
  if ! stop_stale_wechat; then
    echo "WeChat did not exit within ${RESTART_WAIT_SECONDS}s; refusing to launch a duplicate client." >&2
    exit 1
  fi
  launch_wechat
  main_window="$(wait_for_main_window "$START_WAIT_SECONDS" || true)"
  visible_window="$(wechat_visible_window || true)"
else
  main_window="$(wechat_main_window || true)"
  visible_window="$(wechat_visible_window || true)"
  if [[ -z "$main_window" && -z "$visible_window" ]]; then
    echo "No visible WeChat window on $DISPLAY_ID; requesting normal activation." >>"$APP_LOG"
    launch_wechat
    main_window="$(wait_for_main_window "$START_WAIT_SECONDS" || true)"
    visible_window="$(wechat_visible_window || true)"
  fi
fi

if [[ -z "$main_window" && -z "$visible_window" && "$AUTO_RECOVER_UNMAPPED" == "1" ]]; then
  mapfile -t stale_pids < <(wechat_pids_on_display)
  if ((${#stale_pids[@]} > 0)); then
    echo "WeChat stayed background-only on $DISPLAY_ID; gracefully restarting stale PID(s): ${stale_pids[*]}" >>"$APP_LOG"
    if stop_stale_wechat; then
      launch_wechat
      main_window="$(wait_for_main_window "$START_WAIT_SECONDS" || true)"
      visible_window="$(wechat_visible_window || true)"
    else
      echo "Stale WeChat process did not exit within ${RESTART_WAIT_SECONDS}s; leaving it untouched." >>"$APP_LOG"
    fi
  fi
fi

if [[ -n "$main_window" ]]; then
  echo "WeChat main window healthy on $DISPLAY_ID: $main_window" >>"$APP_LOG"
elif [[ -n "$visible_window" ]]; then
  echo "WeChat entry/login window healthy on $DISPLAY_ID: $visible_window" >>"$APP_LOG"
else
  echo "WARNING: no visible WeChat window on $DISPLAY_ID; noVNC will show only the X root background." >>"$APP_LOG"
fi

cat "$LAUNCH_LOG"
echo
echo "WeChat noVNC:"
echo "  http://127.0.0.1:${NOVNC_PORT}/vnc_lite.html?host=127.0.0.1&port=${NOVNC_PORT}&autoconnect=1&resize=remote"
