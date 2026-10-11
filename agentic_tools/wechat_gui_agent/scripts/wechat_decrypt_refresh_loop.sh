#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd -P)"
PRIVATE="${WECHAT_AGENT_PRIVATE_DIR:-$ROOT/agentic_tools/wechat_gui_agent/.private}"
PY="$PRIVATE/wechat_decrypt/.venv/bin/python"
BACKEND="$ROOT/agentic_tools/wechat_gui_agent/scripts/wechat_direct_backend.py"
LOCK="$PRIVATE/wechat_decrypt.refresh.lock"
STAMP="$PRIVATE/wechat_decrypt.refresh.source_stamp"
INTERVAL="${WECHAT_DECRYPT_REFRESH_INTERVAL:-1}"
TIMEOUT="${WECHAT_DECRYPT_TIMEOUT:-45}"
MODE="${WECHAT_DECRYPT_REFRESH_MODE:-incremental}"
SMART="${WECHAT_DECRYPT_REFRESH_SMART:-1}"
DB_ROOT="${WECHAT_DECRYPT_DB_DIR:-}"
HEARTBEAT_INTERVAL="${WECHAT_DECRYPT_HEARTBEAT_INTERVAL:-30}"
FORCE_INTERVAL="${WECHAT_DECRYPT_FORCE_INTERVAL:-10}"

if [[ ! -x "$PY" ]]; then
  PY="$(command -v python3)"
fi

mkdir -p "$PRIVATE"

discover_db_root() {
  "$PY" "$BACKEND" source-dir
}

latest_source_stamp() {
  local root="${DB_ROOT:-}"
  if [[ -z "$root" ]]; then
    root="$(discover_db_root || true)"
  fi
  [[ -n "$root" && -d "$root" ]] || return 1
  find "$root" -type f \( -name '*.db' -o -name '*-wal' -o -name '*-shm' \) -printf '%T@ %s %p\n' 2>/dev/null \
    | sort -nr \
    | head -n 1
}

decrypted_ready() {
  compgen -G "$PRIVATE/wechat_decrypt/decrypted/message/message_*.db" >/dev/null
}

last_skip_log=0
last_refresh_epoch=0

while true; do
  start="$(date --iso-8601=seconds)"
  now_epoch="$(date +%s)"
  source_stamp=""
  if [[ "$SMART" != "0" ]]; then
    source_stamp="$(latest_source_stamp || true)"
    last_stamp="$(cat "$STAMP" 2>/dev/null || true)"
    if [[ -n "$source_stamp" && "$source_stamp" == "$last_stamp" ]] && decrypted_ready && (( now_epoch - last_refresh_epoch < FORCE_INTERVAL )); then
      if (( now_epoch - last_skip_log >= HEARTBEAT_INTERVAL )); then
        echo "[$start] source DB unchanged; using decrypted WeChat cache"
        last_skip_log="$now_epoch"
      fi
      sleep "$INTERVAL"
      continue
    fi
  fi

  echo "[$start] refreshing decrypted WeChat cache via backend ($MODE)"
  command=("$PY" "$BACKEND")
  if [[ -n "$DB_ROOT" ]]; then
    command+=("--db-dir" "$DB_ROOT")
  fi
  command+=("decrypt")
  if [[ "$MODE" == "incremental" ]]; then
    command+=("--incremental")
  fi
  tmp="$(mktemp)"
  set +e
  flock -w "$TIMEOUT" "$LOCK" timeout "$TIMEOUT" "${command[@]}" >"$tmp" 2>&1
  code=$?
  set -e
  if [[ "$code" -eq 0 ]]; then
    last_refresh_epoch="$(date +%s)"
    if [[ -n "$source_stamp" ]]; then
      printf '%s\n' "$source_stamp" > "$STAMP"
    fi
    tail -n 1 "$tmp"
  else
    tail -n 80 "$tmp" || true
  fi
  rm -f "$tmp"
  echo "[$(date --iso-8601=seconds)] refresh exited with code $code"
  sleep "$INTERVAL"
done
