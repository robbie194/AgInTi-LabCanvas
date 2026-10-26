#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd -P)"
source "$ROOT/scripts/labcanvas_storage_gate.sh"
labcanvas_storage_ready || exit 75
PRIVATE_DIR="${WECHAT_AGENT_PRIVATE_DIR:-$ROOT/agentic_tools/wechat_gui_agent/.private}"
PRIVATE_ENV="${WECHAT_AGENT_ENV_FILE:-$PRIVATE_DIR/wechat_supervisor.local.env}"
if [[ -f "$PRIVATE_ENV" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$PRIVATE_ENV"
  set +a
fi
# The profile launcher supplies a separate HOME/XDG root. Keep these exports
# explicit for every tmux child, including the GUI sender and decrypt loop.
export HOME="${WECHAT_AGENT_HOME:-$HOME}"
export XDG_CONFIG_HOME="${WECHAT_AGENT_XDG_CONFIG_HOME:-$HOME/.config}"
export XDG_DATA_HOME="${WECHAT_AGENT_XDG_DATA_HOME:-$HOME/.local/share}"
export XDG_CACHE_HOME="${WECHAT_AGENT_XDG_CACHE_HOME:-$HOME/.cache}"
if [[ "${WECHAT_AGENT_PROFILE_REQUIRED:-0}" == "1" \
  && "$(id -un)" != "${WECHAT_AGENT_OS_USER:-overtree-wechat}" ]]; then
  echo "Agent-WeChat profile requires Linux user ${WECHAT_AGENT_OS_USER:-overtree-wechat}." >&2
  exit 2
fi
SESSION="${WECHAT_SUPERVISOR_SESSION:-labcanvas-wechat}"
CONFIG="${WECHAT_DIRECT_CONFIG:-$PRIVATE_DIR/lazy-research-direct-chatops.local.json}"
CONFIGS="${WECHAT_DIRECT_CONFIGS:-$CONFIG}"
DAILY_BRIEFS_CONFIG="${WECHAT_DAILY_BRIEFS_CONFIG:-$PRIVATE_DIR/daily-briefs.local.json}"
QUEUE="${WECHAT_WORKER_QUEUE:-$PRIVATE_DIR/wechat_task_queue.jsonl}"
LOG_DIR="${WECHAT_AGENT_LOG_DIR:-$ROOT/output/wechat_gui_agent/$(date +%F)}"
PY="${WECHAT_AGENT_PYTHON:-$PRIVATE_DIR/wechat_decrypt/.venv/bin/python}"
DIRECT_POLL_SECONDS="${WECHAT_DIRECT_POLL_SECONDS:-0.8}"
DIRECT_CATCHUP_POLL_SECONDS="${WECHAT_DIRECT_CATCHUP_POLL_SECONDS:-0.1}"
WECHAT_DISPLAY="${WECHAT_DISPLAY:-:97}"
UNLOCK_WATCHDOG="${WECHAT_UNLOCK_WATCHDOG:-1}"
UNLOCK_INTERVAL="${WECHAT_UNLOCK_INTERVAL:-20}"
UNLOCK_ADB_SERIAL="${WECHAT_UNLOCK_ADB_SERIAL:-${ANDROID_SERIAL:-}}"
UNLOCK_DRY_RUN="${WECHAT_UNLOCK_DRY_RUN:-0}"
UNLOCK_FLUSH_DEFERRED="${WECHAT_UNLOCK_FLUSH_DEFERRED:-1}"
CHAT_SYNC_WATCHDOG="${WECHAT_CHAT_SYNC_WATCHDOG:-1}"
CHAT_SYNC_INTERVAL="${WECHAT_CHAT_SYNC_INTERVAL:-45}"
CHAT_SYNC_PAUSE="${WECHAT_CHAT_SYNC_PAUSE:-0.8}"
CHAT_SYNC_TIMEOUT="${WECHAT_CHAT_SYNC_TIMEOUT:-60}"
CHAT_SYNC_PRIORITY="${WECHAT_CHAT_SYNC_PRIORITY:-}"
CHAT_SYNC_MAX_TARGETS_PER_CYCLE="${WECHAT_CHAT_SYNC_MAX_TARGETS_PER_CYCLE:-0}"
ANDROID_INGRESS="${WECHAT_ANDROID_INGRESS:-1}"
ANDROID_INGRESS_INTERVAL="${WECHAT_ANDROID_INGRESS_INTERVAL:-1}"
ANDROID_INGRESS_SERIAL="${WECHAT_ANDROID_INGRESS_SERIAL:-$UNLOCK_ADB_SERIAL}"
ANDROID_SCREEN_INGRESS="${WECHAT_ANDROID_SCREEN_INGRESS:-1}"
ANDROID_SCREEN_INGRESS_INTERVAL="${WECHAT_ANDROID_SCREEN_INGRESS_INTERVAL:-4}"
OCR_GROUP_ENABLED="${WECHAT_OCR_GROUP_ENABLED:-0}"
OCR_GROUP_CONFIGS="${WECHAT_OCR_GROUP_CONFIGS:-}"
OCR_GROUP_REMOTE_SYNC="${WECHAT_OCR_GROUP_REMOTE_SYNC:-0}"
OCR_GROUP_INTERVAL="${WECHAT_OCR_GROUP_INTERVAL:-3}"
OCR_GROUP_REQUIRE_BADGE="${WECHAT_OCR_GROUP_REQUIRE_BADGE:-0}"
OCR_GROUP_ACTIVE_RESCAN_SECONDS="${WECHAT_OCR_GROUP_ACTIVE_RESCAN_SECONDS:-180}"
TINY11=0
if PYTHONPATH="$ROOT/agentic_tools/wechat_gui_agent/scripts" python3 -c \
  'from wechat_transport_selection import tiny11_enabled; raise SystemExit(0 if tiny11_enabled() else 1)'; then
  TINY11=1
  UNLOCK_WATCHDOG=0
  CHAT_SYNC_WATCHDOG=0
  ANDROID_INGRESS=0
  ANDROID_SCREEN_INGRESS=0
fi
WORKER_COUNT="${WECHAT_WORKER_COUNT:-2}"
LOG_MAINTENANCE="${WECHAT_LOG_MAINTENANCE:-1}"
LOG_MAINTENANCE_INTERVAL="${WECHAT_LOG_MAINTENANCE_INTERVAL:-300}"
LOG_MAX_MIB="${WECHAT_LOG_MAX_MIB:-4}"
LOG_KEEP_MIB="${WECHAT_LOG_KEEP_MIB:-1}"
LOG_RETENTION_DAYS="${WECHAT_LOG_RETENTION_DAYS:-3}"
export WECHAT_DECRYPT_REFRESH_INTERVAL="${WECHAT_DECRYPT_REFRESH_INTERVAL:-1}"
export WECHAT_RESTART_DELAY="${WECHAT_RESTART_DELAY:-2}"
export WECHAT_WORKER_EXPIRE_LEGACY_QUEUE="${WECHAT_WORKER_EXPIRE_LEGACY_QUEUE:-1}"
mkdir -p "$LOG_DIR"

if [[ ! -x "$PY" ]]; then
  PY="$(command -v python3)"
fi

if [[ -z "${WECHAT_WORKER_QUEUE:-}" && -f "$CONFIG" ]]; then
  QUEUE="$("$PY" - <<PY
import json
from pathlib import Path
path = Path("$CONFIG")
try:
    print(json.loads(path.read_text(encoding="utf-8")).get("worker_queue") or "$QUEUE")
except Exception:
    print("$QUEUE")
PY
)"
fi

CHAT_NAME="${WECHAT_CHAT_NAME:-wechat-chat}"
if [[ -f "$CONFIG" ]]; then
  CHAT_NAME="$("$PY" - <<PY
import json
from pathlib import Path
path = Path("$CONFIG")
try:
    print(json.loads(path.read_text(encoding="utf-8")).get("chat_name") or "$CHAT_NAME")
except Exception:
    print("$CHAT_NAME")
PY
)"
fi
MEDIA_CHATS="${WECHAT_MEDIA_CHATS:-}"
if [[ -z "$MEDIA_CHATS" ]]; then
  MEDIA_CHATS="$(WECHAT_DIRECT_CONFIGS_VALUE="$CONFIGS" WECHAT_FALLBACK_CHAT="$CHAT_NAME" "$PY" - <<'PY'
import json
import os
from pathlib import Path

names = []
for item in os.environ.get("WECHAT_DIRECT_CONFIGS_VALUE", "").split(","):
    item = item.strip()
    if not item:
        continue
    try:
        name = json.loads(Path(item).read_text(encoding="utf-8")).get("chat_name")
    except Exception:
        name = ""
    if name and name not in names:
        names.append(name)
print(",".join(names) or os.environ.get("WECHAT_FALLBACK_CHAT", "wechat-chat"))
PY
)"
fi

usage() {
  cat <<'EOF'
Usage:
  wechat_supervisor_tmux.sh start|ensure|stop|restart|reload-workers|reload-monitors|reload-unlock|restart-all|status

Notes:
  restart/reload-workers keeps the WeChat GUI desktop alive and only reloads
  monitor, worker, and media-sync windows. Use restart-all, or stop then start,
  only when you intentionally want to restart the official WeChat client.

Environment:
  WECHAT_SUPERVISOR_SESSION   tmux session name, default labcanvas-wechat
  WECHAT_DIRECT_CONFIG        private direct-chatops JSON config
  WECHAT_DIRECT_CONFIGS       comma-separated private direct-chatops configs
  WECHAT_WORKER_QUEUE         private JSONL worker queue
  WECHAT_MEDIA_SOURCES        optional colon-separated folders to sync
  WECHAT_MEDIA_CHATS          optional comma-separated chat names to mirror
  WECHAT_CHAT_NAME            fallback chat name for media-sync events
  WECHAT_UNLOCK_WATCHDOG      1 to keep desktop WeChat unlocked by phone UI, default 1
  WECHAT_UNLOCK_DRY_RUN       1 to probe desktop state without controlling Android, default 0
  WECHAT_UNLOCK_ADB_SERIAL    optional Android serial for phone-side unlock
  WECHAT_UNLOCK_INTERVAL      watchdog poll interval, default 20 seconds
  WECHAT_CHAT_SYNC_WATCHDOG   1 to dry-open configured chats so DB rows stay fresh, default 1
  WECHAT_CHAT_SYNC_INTERVAL   chat sync loop interval, default 45 seconds
  WECHAT_CHAT_SYNC_TIMEOUT    per-chat dry-open timeout, default 60 seconds
  WECHAT_CHAT_SYNC_PRIORITY   optional comma-separated chat names to dry-open first
  WECHAT_CHAT_SYNC_MAX_TARGETS_PER_CYCLE  max chats to dry-open per pass, 0 for all
  WECHAT_ANDROID_INGRESS      1 to consume private Android WeChat notifications, default 1
  WECHAT_ANDROID_INGRESS_INTERVAL  Android notification poll interval, default 1 second
  WECHAT_ANDROID_INGRESS_SERIAL    optional device serial; defaults to unlock-watchdog serial
  WECHAT_ANDROID_SCREEN_INGRESS    1 to recover self-authored text from physical display 0
  WECHAT_ANDROID_SCREEN_INGRESS_INTERVAL  passive screen poll interval, default 4 seconds
  WECHAT_OCR_GROUP_ENABLED  1 to enable the opt-in GUI/OCR personal-group ingress, default 0
  WECHAT_OCR_GROUP_CONFIGS  comma-separated private OCR group configs
  WECHAT_OCR_GROUP_REMOTE_SYNC  1 to sync registered group names from OverTree Cloud
  WECHAT_OCR_GROUP_INTERVAL  OCR group scan interval, default 3 seconds
  WECHAT_OCR_GROUP_REQUIRE_BADGE  1 to require a visible unread red marker before opening a group
  WECHAT_OCR_GROUP_ACTIVE_RESCAN_SECONDS  OCR safety rescan interval when the screen is unchanged, default 180 seconds
  WECHAT_OCR_GROUP_NATIVE_COPY  false to disable native bubble Copy and keep the OCR fallback
  WECHAT_WORKER_COUNT         parallel queue workers, default 2
  WECHAT_LOG_MAINTENANCE      1 to cap generated logs/evidence, default 1
  WECHAT_LOG_MAINTENANCE_INTERVAL  retention pass interval, default 300 seconds
  WECHAT_LOG_MAX_MIB          maximum generated log size, default 4 MiB
  WECHAT_LOG_KEEP_MIB         recent complete-line tail retained after trimming, default 1 MiB
  WECHAT_LOG_RETENTION_DAYS   generated log retention, default 3 days
  WECHAT_WORKER_PENDING_TASK_TTL_SECONDS  expire ordinary task backlog, default 900
  WECHAT_WORKER_DEFERRED_SEND_TTL_SECONDS expire outbound backlog, default 600
  WECHAT_WORKER_DEFERRED_SEND_GLOBAL_COOLDOWN_SECONDS deferred-send spacing, default 30
EOF
}

list_session_panes() {
  tmux list-windows -t "$SESSION" -F '#{window_id}	#{window_name}' |
    while IFS=$'\t' read -r window_id window_name; do
      [[ -n "$window_id" ]] || continue
      tmux list-panes -t "$window_id" -F "${window_name}.#{pane_index}: #{pane_current_command} #{pane_pid}"
    done
}

window_id_by_name() {
  local name="$1"
  tmux list-windows -t "$SESSION" -F '#{window_id}|#{window_name}' 2>/dev/null |
    awk -F '|' -v name="$name" '$2 == name { print $1; exit }'
}

window_live() {
  local window_id
  window_id="$(window_id_by_name "$1")"
  [[ -n "$window_id" ]] || return 1
  tmux list-panes -t "$window_id" -F '#{pane_dead}' 2>/dev/null | grep -qx '0'
}

window_present() {
  [[ -n "$(window_id_by_name "$1")" ]]
}

remove_dead_window() {
  local name="$1"
  if window_present "$name" && ! window_live "$name"; then
    tmux kill-window -t "$(window_id_by_name "$name")" 2>/dev/null || true
  fi
}

start_missing_window() {
  local name="$1"
  local command="$2"
  remove_dead_window "$name"
  if ! window_live "$name"; then
    tmux new-window -t "$SESSION" -n "$name" "$command"
    echo "Started missing window: $name"
  fi
}

pane_start_command_exists() {
  local needle="$1"
  tmux list-panes -a -F '#{session_name}|#{pane_start_command}' 2>/dev/null |
    awk -F '|' -v session="$SESSION" -v needle="$needle" '$1 == session && index($2, needle) { found=1 } END { exit !found }'
}

respawn_or_new_window() {
  local window_name="$1"
  local command="$2"
  local window_id
  window_id="$(
    tmux list-windows -t "$SESSION" -F '#{window_id}	#{window_name}' |
      awk -F '\t' -v name="$window_name" '$2 == name { print $1; exit }'
  )"
  if [[ -n "$window_id" ]]; then
    tmux respawn-window -k -t "$window_id" "$command"
    echo "Reloaded window: $window_name"
  else
    tmux new-window -t "$SESSION" -n "$window_name" "$command"
    echo "Started missing window: $window_name"
  fi
}

respawn_or_new_pane_by_start_command() {
  local label="$1"
  local needle="$2"
  local command="$3"
  local pane_id
  pane_id="$(
    tmux list-panes -a -F '#{session_name}	#{pane_id}	#{pane_start_command}' |
      awk -F '\t' -v session="$SESSION" -v needle="$needle" '$1 == session && index($3, needle) { print $2; exit }'
  )"
  if [[ -n "$pane_id" ]]; then
    tmux respawn-pane -k -t "$pane_id" "$command"
    echo "Reloaded pane: $label"
  else
    tmux new-window -t "$SESSION" -n "$label" "$command"
    echo "Started missing window: $label"
  fi
}

unlock_watchdog_command() {
  local args=(python3 -u agentic_tools/wechat_gui_agent/scripts/wechat_desktop_unlock_watchdog.py --display "$WECHAT_DISPLAY" --interval "$UNLOCK_INTERVAL" --loop)
  if [[ "$UNLOCK_DRY_RUN" != "0" ]]; then
    args+=(--dry-run)
  elif [[ -n "$UNLOCK_ADB_SERIAL" ]]; then
    args+=(--serial "$UNLOCK_ADB_SERIAL")
  fi
  if [[ "$UNLOCK_FLUSH_DEFERRED" != "0" ]]; then
    args+=(--flush-deferred)
  fi
  printf "cd %q && agentic_tools/wechat_gui_agent/scripts/wechat_restart_loop.sh unlock-watchdog " "$ROOT"
  printf "%q " "${args[@]}"
  printf ">> %q 2>&1" "$LOG_DIR/supervisor-unlock-watchdog.log"
}

chat_sync_command() {
  printf "cd %q && agentic_tools/wechat_gui_agent/scripts/wechat_restart_loop.sh chat-sync python3 -u agentic_tools/wechat_gui_agent/scripts/wechat_chat_sync_loop.py --configs %q --display %q --interval %q --pause %q --timeout %q --priority %q --max-targets-per-cycle %q --loop >> %q 2>&1" \
    "$ROOT" "$CONFIGS" "$WECHAT_DISPLAY" "$CHAT_SYNC_INTERVAL" "$CHAT_SYNC_PAUSE" "$CHAT_SYNC_TIMEOUT" "$CHAT_SYNC_PRIORITY" "$CHAT_SYNC_MAX_TARGETS_PER_CYCLE" "$LOG_DIR/supervisor-chat-sync.log"
}

android_ingress_command() {
  local args=(python3 -u agentic_tools/wechat_gui_agent/scripts/wechat_android_ingress.py --configs "$CONFIGS" --interval "$ANDROID_INGRESS_INTERVAL" --loop)
  if [[ -n "$ANDROID_INGRESS_SERIAL" ]]; then
    args+=(--serial "$ANDROID_INGRESS_SERIAL")
  fi
  printf "cd %q && agentic_tools/wechat_gui_agent/scripts/wechat_restart_loop.sh android-ingress " "$ROOT"
  printf "%q " "${args[@]}"
  printf ">> %q 2>&1" "$LOG_DIR/supervisor-android-ingress.log"
}

android_screen_ingress_command() {
  local args=(python3 -u agentic_tools/wechat_gui_agent/scripts/wechat_android_screen_ingress.py --configs "$CONFIGS" --interval "$ANDROID_SCREEN_INGRESS_INTERVAL" --loop)
  if [[ -n "$ANDROID_INGRESS_SERIAL" ]]; then
    args+=(--serial "$ANDROID_INGRESS_SERIAL")
  fi
  printf "cd %q && agentic_tools/wechat_gui_agent/scripts/wechat_restart_loop.sh android-screen-ingress " "$ROOT"
  printf "%q " "${args[@]}"
  printf ">> %q 2>&1" "$LOG_DIR/supervisor-android-screen-ingress.log"
}

desktop_command() {
  # Existing tmux servers may predate the isolated profile environment.
  printf 'cd %q && { if [[ -f %q ]]; then set -a; source %q || exit; set +a; fi; while true; do agentic_tools/wechat_gui_agent/scripts/wechat_virtual_desktop.sh; sleep 60; done; } >> %q 2>&1' \
    "$ROOT" "$PRIVATE_ENV" "$PRIVATE_ENV" "$LOG_DIR/supervisor-desktop.log"
}

ocr_group_command() {
  # tmux starts child panes from the server environment, which may not include
  # the Agent profile's private paths and Cloud registry settings. The runner
  # reloads the private env file in the pane before starting OCR ingress.
  printf "cd %q && agentic_tools/wechat_gui_agent/scripts/wechat_ocr_group_runner.sh --env-file %q >> %q 2>&1" \
    "$ROOT" "$PRIVATE_ENV" "$LOG_DIR/supervisor-ocr-group.log"
}

log_maintenance_command() {
  printf "cd %q && python3 -u agentic_tools/wechat_gui_agent/scripts/wechat_output_retention.py --root %q --extra-root %q --max-log-mib %q --keep-log-mib %q --log-retention-days %q --interval %q --loop" \
    "$ROOT" "$ROOT/output/wechat_gui_agent" "$ROOT/output/wecom" "$LOG_MAX_MIB" "$LOG_KEEP_MIB" "$LOG_RETENTION_DAYS" "$LOG_MAINTENANCE_INTERVAL"
}

worker_window_name() {
  local index="$1"
  if [[ "$index" == "1" ]]; then
    printf "worker"
  else
    printf "worker-%s" "$index"
  fi
}

worker_command() {
  local index="$1"
  local label
  label="$(worker_window_name "$index")"
  printf "cd %q && agentic_tools/wechat_gui_agent/scripts/wechat_restart_loop.sh %q agentic_tools/wechat_gui_agent/scripts/wechat_worker_guarded_loop.sh --queue %q --loop --send >> %q 2>&1" \
    "$ROOT" "$label" "$QUEUE" "$LOG_DIR/supervisor-$label.log"
}

direct_monitor_command() {
  local direct_config="$1"
  local direct_name="$2"
  printf "cd %q && if [[ -f %q ]]; then set -a; source %q; set +a; fi; agentic_tools/wechat_gui_agent/scripts/wechat_restart_loop.sh %q %q -u agentic_tools/wechat_gui_agent/scripts/wechat_direct_chatops.py --config %q --worker-queue %q --loop --send --no-decrypt --quiet-idle --poll-seconds %q --catchup-poll-seconds %q >> %q 2>&1" \
    "$ROOT" "$PRIVATE_ENV" "$PRIVATE_ENV" "direct-chatops-$direct_name" "$PY" "$direct_config" "$QUEUE" "$DIRECT_POLL_SECONDS" "$DIRECT_CATCHUP_POLL_SECONDS" "$LOG_DIR/supervisor-direct-chatops-$direct_name.log"
}

tiny11_store_command() {
  printf "cd %q && agentic_tools/wechat_gui_agent/scripts/wechat_restart_loop.sh tiny11-store python3 -u agentic_tools/wechat_gui_agent/scripts/wechat_tiny11_bridge.py sync --loop >> %q 2>&1" \
    "$ROOT" "$LOG_DIR/supervisor-tiny11-store.log"
}

ensure_daily_brief_window() {
  if [[ -f "$DAILY_BRIEFS_CONFIG" ]]; then
    local brief_command
    printf -v brief_command 'cd %q && if [[ -f %q ]]; then set -a; source %q; set +a; fi; exec agentic_tools/wechat_gui_agent/scripts/wechat_restart_loop.sh daily-briefs python3 -u agentic_tools/wechat_gui_agent/scripts/wechat_daily_brief.py --config %q --loop >> %q 2>&1' \
      "$ROOT" "$PRIVATE_ENV" "$PRIVATE_ENV" "$DAILY_BRIEFS_CONFIG" "$LOG_DIR/supervisor-daily-briefs.log"
    start_missing_window daily-briefs "$brief_command"
  fi
}

ensure_runtime_windows() {
  if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "Session not running: $SESSION" >&2
    return 2
  fi
  if [[ "$TINY11" == "1" ]]; then
    start_missing_window tiny11-store "$(tiny11_store_command)"
  else
  start_missing_window desktop "$(desktop_command)"
  if ! pane_start_command_exists "wechat_decrypt_refresh_loop.sh"; then
    tmux split-window -h -t "$SESSION:desktop" \
      "cd '$ROOT' && agentic_tools/wechat_gui_agent/scripts/wechat_restart_loop.sh decrypt-refresh agentic_tools/wechat_gui_agent/scripts/wechat_decrypt_refresh_loop.sh >> '$LOG_DIR/supervisor-decrypt-refresh.log' 2>&1"
    echo "Started missing pane: decrypt-refresh"
  fi
  fi
  IFS=',' read -r -a DIRECT_CONFIGS <<< "$CONFIGS"
  for direct_config in "${DIRECT_CONFIGS[@]}"; do
    direct_config="$(echo "$direct_config" | xargs)"
    [[ -n "$direct_config" ]] || continue
    direct_name="$(basename "$direct_config" .json | tr -c 'A-Za-z0-9_.-' '-')"
    start_missing_window "direct-$direct_name" \
      "$(direct_monitor_command "$direct_config" "$direct_name")"
  done
  for worker_index in $(seq 1 "$WORKER_COUNT"); do
    worker_label="$(worker_window_name "$worker_index")"
    start_missing_window "$worker_label" "$(worker_command "$worker_index")"
  done
  ensure_daily_brief_window
  if [[ "$TINY11" == "0" ]]; then
  start_missing_window media-sync \
    "cd '$ROOT' && WECHAT_CHAT_NAME='$CHAT_NAME' WECHAT_MEDIA_CHATS='$MEDIA_CHATS' agentic_tools/wechat_gui_agent/scripts/wechat_restart_loop.sh media-sync agentic_tools/wechat_gui_agent/scripts/wechat_media_sync_loop.sh >> '$LOG_DIR/supervisor-media-sync.log' 2>&1"
  fi
  if [[ "$UNLOCK_WATCHDOG" != "0" ]]; then
    start_missing_window unlock-watchdog "$(unlock_watchdog_command)"
  fi
  if [[ "$CHAT_SYNC_WATCHDOG" != "0" ]]; then
    start_missing_window chat-sync "$(chat_sync_command)"
  fi
  if [[ "$ANDROID_INGRESS" != "0" ]]; then
    start_missing_window android-ingress "$(android_ingress_command)"
  fi
  if [[ "$ANDROID_SCREEN_INGRESS" != "0" ]]; then
    start_missing_window android-screen-ingress "$(android_screen_ingress_command)"
  fi
  if [[ "$OCR_GROUP_ENABLED" != "0" && ( -n "$OCR_GROUP_CONFIGS" || "$OCR_GROUP_REMOTE_SYNC" != "0" ) ]]; then
    start_missing_window ocr-group "$(ocr_group_command)"
  fi
  if [[ "$LOG_MAINTENANCE" != "0" ]]; then
    start_missing_window log-maintenance "$(log_maintenance_command)"
  fi
  tmux select-layout -t "$SESSION:desktop" tiled >/dev/null 2>&1 || true
  tmux select-window -t "$SESSION:desktop" >/dev/null 2>&1 || true
  echo "Session running and missing windows repaired: $SESSION"
}

stop_gui_senders_for_reload() {
  local pids
  pids="$(pgrep -f '[w]echat_gui_agent/scripts/wechat_gui_send.py' || true)"
  [[ -n "$pids" ]] || return 0
  echo "Stopping GUI send helper(s) before reload: $pids"
  for pid in $pids; do
    kill -TERM "$pid" 2>/dev/null || true
  done
  sleep 0.5
  for pid in $pids; do
    if kill -0 "$pid" 2>/dev/null; then
      kill -KILL "$pid" 2>/dev/null || true
    fi
  done
}

reload_worker_windows() {
  if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "Session not running: $SESSION" >&2
    echo "Use start or restart-all only when you intentionally want to launch the WeChat client." >&2
    return 1
  fi
  stop_gui_senders_for_reload
  if [[ "$TINY11" == "1" ]]; then
    start_missing_window tiny11-store "$(tiny11_store_command)"
  else
  respawn_or_new_pane_by_start_command "decrypt-refresh" "wechat_decrypt_refresh_loop.sh" \
    "cd '$ROOT' && agentic_tools/wechat_gui_agent/scripts/wechat_restart_loop.sh decrypt-refresh agentic_tools/wechat_gui_agent/scripts/wechat_decrypt_refresh_loop.sh >> '$LOG_DIR/supervisor-decrypt-refresh.log' 2>&1"
  fi
  IFS=',' read -r -a DIRECT_CONFIGS <<< "$CONFIGS"
  for direct_config in "${DIRECT_CONFIGS[@]}"; do
    direct_config="$(echo "$direct_config" | xargs)"
    [[ -n "$direct_config" ]] || continue
    direct_name="$(basename "$direct_config" .json | tr -c 'A-Za-z0-9_.-' '-')"
    respawn_or_new_window "direct-$direct_name" \
      "$(direct_monitor_command "$direct_config" "$direct_name")"
  done
  for worker_index in $(seq 1 "$WORKER_COUNT"); do
    worker_label="$(worker_window_name "$worker_index")"
    respawn_or_new_window "$worker_label" "$(worker_command "$worker_index")"
  done
  if [[ "$TINY11" == "0" ]]; then
  respawn_or_new_window "media-sync" \
    "cd '$ROOT' && WECHAT_CHAT_NAME='$CHAT_NAME' WECHAT_MEDIA_CHATS='$MEDIA_CHATS' agentic_tools/wechat_gui_agent/scripts/wechat_restart_loop.sh media-sync agentic_tools/wechat_gui_agent/scripts/wechat_media_sync_loop.sh >> '$LOG_DIR/supervisor-media-sync.log' 2>&1"
  fi
  if [[ "$UNLOCK_WATCHDOG" != "0" ]]; then
    respawn_or_new_window "unlock-watchdog" "$(unlock_watchdog_command)"
  fi
  if [[ "$CHAT_SYNC_WATCHDOG" != "0" ]]; then
    respawn_or_new_window "chat-sync" "$(chat_sync_command)"
  fi
  if [[ "$ANDROID_INGRESS" != "0" ]]; then
    start_missing_window android-ingress "$(android_ingress_command)"
  fi
  if [[ "$ANDROID_SCREEN_INGRESS" != "0" ]]; then
    start_missing_window android-screen-ingress "$(android_screen_ingress_command)"
  fi
  if [[ "$OCR_GROUP_ENABLED" != "0" && ( -n "$OCR_GROUP_CONFIGS" || "$OCR_GROUP_REMOTE_SYNC" != "0" ) ]]; then
    respawn_or_new_window "ocr-group" "$(ocr_group_command)"
  fi
  if [[ "$LOG_MAINTENANCE" != "0" ]]; then
    start_missing_window log-maintenance "$(log_maintenance_command)"
  fi
  tmux select-window -t "$SESSION:desktop" >/dev/null 2>&1 || true
  echo "Reloaded worker/monitor windows without restarting the WeChat desktop."
  echo "Logs: $LOG_DIR"
}

reload_monitor_windows() {
  if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    exec "$0" start
  fi
  if [[ "$TINY11" == "1" ]]; then
    start_missing_window tiny11-store "$(tiny11_store_command)"
  fi
  IFS=',' read -r -a DIRECT_CONFIGS <<< "$CONFIGS"
  for direct_config in "${DIRECT_CONFIGS[@]}"; do
    direct_config="$(echo "$direct_config" | xargs)"
    [[ -n "$direct_config" ]] || continue
    direct_name="$(basename "$direct_config" .json | tr -c 'A-Za-z0-9_.-' '-')"
    respawn_or_new_window "direct-$direct_name" \
      "$(direct_monitor_command "$direct_config" "$direct_name")"
  done
  if [[ "$CHAT_SYNC_WATCHDOG" != "0" ]]; then
    respawn_or_new_window "chat-sync" "$(chat_sync_command)"
  fi
  if [[ "$ANDROID_INGRESS" != "0" ]]; then
    respawn_or_new_window "android-ingress" "$(android_ingress_command)"
  fi
  if [[ "$ANDROID_SCREEN_INGRESS" != "0" ]]; then
    respawn_or_new_window "android-screen-ingress" "$(android_screen_ingress_command)"
  fi
  if [[ "$OCR_GROUP_ENABLED" != "0" && ( -n "$OCR_GROUP_CONFIGS" || "$OCR_GROUP_REMOTE_SYNC" != "0" ) ]]; then
    respawn_or_new_window "ocr-group" "$(ocr_group_command)"
  fi
  if [[ "$LOG_MAINTENANCE" != "0" ]]; then
    start_missing_window log-maintenance "$(log_maintenance_command)"
  fi
  echo "Reloaded direct monitors and chat sync without touching workers or WeChat desktop."
}

reload_unlock_watchdog() {
  if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    exec "$0" start
  fi
  if [[ "$UNLOCK_WATCHDOG" == "0" ]]; then
    echo "Unlock watchdog is disabled."
    return 0
  fi
  respawn_or_new_window "unlock-watchdog" "$(unlock_watchdog_command)"
  echo "Reloaded only the owner-authorized phone unlock watchdog."
}

action="${1:-start}"
case "$action" in
  start)
    if tmux has-session -t "$SESSION" 2>/dev/null; then
      echo "Session already running: $SESSION"
      list_session_panes
      exit 0
    fi
    if [[ "$TINY11" == "1" ]]; then
      tmux new-session -d -s "$SESSION" -n tiny11-store "$(tiny11_store_command)"
      ensure_runtime_windows
      exit 0
    fi
    tmux new-session -d -s "$SESSION" -n desktop "$(desktop_command)"
    tmux split-window -h -t "$SESSION:desktop" \
      "cd '$ROOT' && agentic_tools/wechat_gui_agent/scripts/wechat_restart_loop.sh decrypt-refresh agentic_tools/wechat_gui_agent/scripts/wechat_decrypt_refresh_loop.sh >> '$LOG_DIR/supervisor-decrypt-refresh.log' 2>&1"
    IFS=',' read -r -a DIRECT_CONFIGS <<< "$CONFIGS"
    for direct_config in "${DIRECT_CONFIGS[@]}"; do
      direct_config="$(echo "$direct_config" | xargs)"
      [[ -n "$direct_config" ]] || continue
      direct_name="$(basename "$direct_config" .json | tr -c 'A-Za-z0-9_.-' '-')"
      tmux new-window -t "$SESSION" -n "direct-$direct_name" \
        "$(direct_monitor_command "$direct_config" "$direct_name")"
    done
    for worker_index in $(seq 1 "$WORKER_COUNT"); do
      worker_label="$(worker_window_name "$worker_index")"
      tmux new-window -t "$SESSION" -n "$worker_label" "$(worker_command "$worker_index")"
    done
    tmux new-window -t "$SESSION" -n media-sync \
      "cd '$ROOT' && WECHAT_CHAT_NAME='$CHAT_NAME' WECHAT_MEDIA_CHATS='$MEDIA_CHATS' agentic_tools/wechat_gui_agent/scripts/wechat_restart_loop.sh media-sync agentic_tools/wechat_gui_agent/scripts/wechat_media_sync_loop.sh >> '$LOG_DIR/supervisor-media-sync.log' 2>&1"
    if [[ "$UNLOCK_WATCHDOG" != "0" ]]; then
      tmux new-window -t "$SESSION" -n unlock-watchdog "$(unlock_watchdog_command)"
    fi
    if [[ "$CHAT_SYNC_WATCHDOG" != "0" ]]; then
      tmux new-window -t "$SESSION" -n chat-sync "$(chat_sync_command)"
    fi
    if [[ "$ANDROID_INGRESS" != "0" ]]; then
      tmux new-window -t "$SESSION" -n android-ingress "$(android_ingress_command)"
    fi
    if [[ "$ANDROID_SCREEN_INGRESS" != "0" ]]; then
      tmux new-window -t "$SESSION" -n android-screen-ingress "$(android_screen_ingress_command)"
    fi
    if [[ "$OCR_GROUP_ENABLED" != "0" && ( -n "$OCR_GROUP_CONFIGS" || "$OCR_GROUP_REMOTE_SYNC" != "0" ) ]]; then
      tmux new-window -t "$SESSION" -n ocr-group "$(ocr_group_command)"
    fi
    if [[ "$LOG_MAINTENANCE" != "0" ]]; then
      tmux new-window -t "$SESSION" -n log-maintenance "$(log_maintenance_command)"
    fi
    tmux select-layout -t "$SESSION:desktop" tiled >/dev/null
    tmux select-window -t "$SESSION:desktop" >/dev/null
    ensure_daily_brief_window
    echo "Started tmux session: $SESSION"
    echo "Logs: $LOG_DIR"
    echo "Attach: tmux attach -t $SESSION"
    ;;
  ensure)
    if tmux has-session -t "$SESSION" 2>/dev/null; then
      ensure_runtime_windows
    else
      exec "$0" start
    fi
    ;;
  stop)
    if tmux has-session -t "$SESSION" 2>/dev/null; then
      tmux kill-session -t "$SESSION"
      echo "Stopped tmux session: $SESSION"
    else
      echo "Session not running: $SESSION"
    fi
    ;;
  reload-workers|restart)
    reload_worker_windows
    ;;
  reload-monitors)
    reload_monitor_windows
    ;;
  reload-unlock)
    reload_unlock_watchdog
    ;;
  restart-all)
    "$0" stop || true
    "$0" start
    ;;
  status)
    if tmux has-session -t "$SESSION" 2>/dev/null; then
      echo "running: $SESSION"
      list_session_panes
    else
      echo "not-running: $SESSION"
      exit 1
    fi
    ;;
  --help|-h|help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
