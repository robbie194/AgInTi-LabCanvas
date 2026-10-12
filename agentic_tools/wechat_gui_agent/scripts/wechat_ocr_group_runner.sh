#!/usr/bin/env bash
set -euo pipefail

# tmux panes do not reliably inherit custom Agent-WeChat environment values.
# Load the private profile here so OCR state and Cloud registry settings stay
# out of the read-only code checkout and are available after pane restarts.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd -P)"
PROFILE_HOME="${WECHAT_AGENT_HOME:-${HOME:-}/.local/share/overtree-wechat}"
PRIVATE_DIR="${WECHAT_AGENT_PRIVATE_DIR:-$PROFILE_HOME/labcanvas-private}"
ENV_FILE="${WECHAT_AGENT_ENV_FILE:-$PRIVATE_DIR/wechat_supervisor.local.env}"

if [[ -r "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

export WECHAT_AGENT_HOME="${WECHAT_AGENT_HOME:-$PROFILE_HOME}"
export WECHAT_AGENT_PRIVATE_DIR="${WECHAT_AGENT_PRIVATE_DIR:-$PRIVATE_DIR}"
export WECHAT_AGENT_ENV_FILE="${WECHAT_AGENT_ENV_FILE:-$ENV_FILE}"
export HOME="$WECHAT_AGENT_HOME"
export XDG_CONFIG_HOME="${WECHAT_AGENT_XDG_CONFIG_HOME:-$HOME/.config}"
export XDG_DATA_HOME="${WECHAT_AGENT_XDG_DATA_HOME:-$HOME/.local/share}"
export XDG_CACHE_HOME="${WECHAT_AGENT_XDG_CACHE_HOME:-$HOME/.cache}"

args=(
  --configs "${WECHAT_OCR_GROUP_CONFIGS:-}"
  --display "${WECHAT_DISPLAY:-:97}"
  --interval "${WECHAT_OCR_GROUP_INTERVAL:-3}"
  --active-rescan-seconds "${WECHAT_OCR_GROUP_ACTIVE_RESCAN_SECONDS:-180}"
  --loop --send
)
if [[ "${WECHAT_OCR_GROUP_REQUIRE_BADGE:-0}" != "0" ]]; then
  args+=(--require-unread-badge)
fi

exec "$ROOT/agentic_tools/wechat_gui_agent/scripts/wechat_restart_loop.sh" \
  ocr-group python3 -u "$ROOT/agentic_tools/wechat_gui_agent/scripts/wechat_ocr_group_ingress.py" \
  "${args[@]}"
