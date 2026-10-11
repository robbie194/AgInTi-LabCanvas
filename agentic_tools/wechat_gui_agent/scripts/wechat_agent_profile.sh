#!/usr/bin/env bash
set -euo pipefail

# Create and run the personal Agent-WeChat profile without changing the
# user's normal desktop WeChat HOME or LabCanvas state.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd -P)"
PROFILE_HOME="${WECHAT_AGENT_HOME:-$HOME/.local/share/overtree-wechat}"
PRIVATE_DIR="${WECHAT_AGENT_PRIVATE_DIR:-$PROFILE_HOME/labcanvas-private}"
CONFIG_DIR="${WECHAT_AGENT_XDG_CONFIG_HOME:-$PROFILE_HOME/config}"
DATA_DIR="${WECHAT_AGENT_XDG_DATA_HOME:-$PROFILE_HOME/data}"
CACHE_DIR="${WECHAT_AGENT_XDG_CACHE_HOME:-$PROFILE_HOME/cache}"
LOG_DIR="${WECHAT_AGENT_LOG_DIR:-$PROFILE_HOME/logs}"
ENV_FILE="${WECHAT_AGENT_ENV_FILE:-$PRIVATE_DIR/wechat_supervisor.local.env}"
ACTION="${1:-status}"

if [[ "$PROFILE_HOME" == "$HOME" || "$PRIVATE_DIR" == "$HOME" || "$PROFILE_HOME" == "/" ]]; then
  echo "Refusing to use the normal user HOME as the Agent-WeChat profile." >&2
  exit 2
fi

write_env() {
  mkdir -p "$PRIVATE_DIR" "$CONFIG_DIR" "$DATA_DIR" "$CACHE_DIR"
  mkdir -p "$LOG_DIR"
  chmod 700 "$PROFILE_HOME" "$PRIVATE_DIR" "$CONFIG_DIR" "$DATA_DIR" "$CACHE_DIR" "$LOG_DIR"
  cat >"$ENV_FILE" <<EOF
# Private Agent-WeChat profile. Keep this file mode 0600 and out of Git.
WECHAT_AGENT_HOME=$PROFILE_HOME
WECHAT_AGENT_PRIVATE_DIR=$PRIVATE_DIR
WECHAT_AGENT_XDG_CONFIG_HOME=$CONFIG_DIR
WECHAT_AGENT_XDG_DATA_HOME=$DATA_DIR
WECHAT_AGENT_XDG_CACHE_HOME=$CACHE_DIR
WECHAT_AGENT_LOG_DIR=$LOG_DIR
WECHAT_DISPLAY=:97
WECHAT_VNC_PORT=5917
WECHAT_NOVNC_PORT=6107
WECHAT_BINARY=/usr/bin/wechat
WECHAT_SUPERVISOR_SESSION=labcanvas-wechat-agent
WECHAT_DIRECT_CONFIGS=
WECHAT_PERSONAL_GROUP_ENABLED=false
WECHAT_PERSONAL_GROUP_CLOUD_URL=
WECHAT_PERSONAL_GROUP_ARTIFACT_BASE_URL=https://cloud.overtree.top
WECHAT_PERSONAL_GROUP_SHARED_SECRET=
WECHAT_PERSONAL_GROUP_AGENT_ACCOUNT_ID=personal-agent-main
WECHAT_PERSONAL_GROUP_ALLOWLIST=
EOF
  chmod 600 "$ENV_FILE"
}

require_agent_os_user() {
  local expected_user="${WECHAT_AGENT_OS_USER:-overtree-wechat}"
  if [[ "$(id -un)" != "$expected_user" ]]; then
    echo "Agent-WeChat profile must run as Linux user $expected_user; current user is $(id -un)." >&2
    echo "Run it through sudo -u $expected_user after the one-time user setup." >&2
    exit 2
  fi
}

run_profile() {
  require_agent_os_user
  if [[ ! -f "$ENV_FILE" ]]; then
    echo "Profile is not initialized: $ENV_FILE" >&2
    echo "Run: $0 init" >&2
    exit 2
  fi
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
  exec env HOME="$WECHAT_AGENT_HOME" \
    XDG_CONFIG_HOME="$WECHAT_AGENT_XDG_CONFIG_HOME" \
    XDG_DATA_HOME="$WECHAT_AGENT_XDG_DATA_HOME" \
    XDG_CACHE_HOME="$WECHAT_AGENT_XDG_CACHE_HOME" \
    WECHAT_AGENT_PRIVATE_DIR="$WECHAT_AGENT_PRIVATE_DIR" \
    WECHAT_AGENT_HOME="$WECHAT_AGENT_HOME" \
    WECHAT_AGENT_PROFILE_REQUIRED=1 \
    WECHAT_AGENT_ENV_FILE="$ENV_FILE" \
    "$ROOT/agentic_tools/wechat_gui_agent/scripts/wechat_supervisor_tmux.sh" "${1:-start}"
}

case "$ACTION" in
  init)
    write_env
    echo "Initialized Agent-WeChat profile"
    echo "  HOME:    $PROFILE_HOME"
    echo "  private: $PRIVATE_DIR"
    echo "  env:     $ENV_FILE"
    echo "Next: edit the env file, add a direct-chat config, then run:"
    echo "  $0 start"
    ;;
  env)
    [[ -f "$ENV_FILE" ]] || write_env
    sed -E 's/(SECRET=).*/\1<redacted>/' "$ENV_FILE"
    ;;
  start|ensure|stop|restart|reload-workers|reload-monitors|reload-unlock|restart-all|status)
    run_profile "$ACTION"
    ;;
  desktop)
    require_agent_os_user
    DESKTOP_ACTION="${2:-ensure}"
    case "$DESKTOP_ACTION" in
      ensure|restart-client) ;;
      *) echo "Usage: $0 desktop [ensure|restart-client]" >&2; exit 2 ;;
    esac
    [[ -f "$ENV_FILE" ]] || write_env
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
    exec env HOME="$WECHAT_AGENT_HOME" XDG_CONFIG_HOME="$WECHAT_AGENT_XDG_CONFIG_HOME" \
      XDG_DATA_HOME="$WECHAT_AGENT_XDG_DATA_HOME" XDG_CACHE_HOME="$WECHAT_AGENT_XDG_CACHE_HOME" \
      WECHAT_AGENT_PRIVATE_DIR="$WECHAT_AGENT_PRIVATE_DIR" WECHAT_AGENT_HOME="$WECHAT_AGENT_HOME" \
      WECHAT_AGENT_PROFILE_REQUIRED=1 \
      "$ROOT/agentic_tools/wechat_gui_agent/scripts/wechat_virtual_desktop.sh" "$DESKTOP_ACTION"
    ;;
  check)
    [[ -f "$ENV_FILE" ]] || { echo "Profile is not initialized: $ENV_FILE" >&2; exit 2; }
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
    printf 'profile_home=%s\nprivate_dir=%s\ndisplay=%s\nnovnc_port=%s\nwechat_binary=%s\n' \
      "$WECHAT_AGENT_HOME" "$WECHAT_AGENT_PRIVATE_DIR" "${WECHAT_DISPLAY:-:97}" "${WECHAT_NOVNC_PORT:-6107}" "${WECHAT_BINARY:-/usr/bin/wechat}"
    if pgrep -af '^/usr/bin/wechat([[:space:]]|$)' >/dev/null 2>&1; then
      echo "warning=an existing Linux WeChat process is running; verify it is a different account before starting Agent-WeChat"
    else
      echo "wechat_process=not_running"
    fi
    ;;
  add-group)
    require_agent_os_user
    [[ -f "$ENV_FILE" ]] || write_env
    exec env HOME="$WECHAT_AGENT_HOME" WECHAT_AGENT_PRIVATE_DIR="$WECHAT_AGENT_PRIVATE_DIR" WECHAT_AGENT_ENV_FILE="$ENV_FILE" \
      "$ROOT/agentic_tools/wechat_gui_agent/scripts/wechat_agent_group_config.py" "${@:2}"
    ;;
  *)
    echo "Usage: $0 init|env|check|desktop [ensure|restart-client]|add-group|start|ensure|stop|restart|reload-workers|reload-monitors|reload-unlock|restart-all|status" >&2
    exit 2
    ;;
esac
