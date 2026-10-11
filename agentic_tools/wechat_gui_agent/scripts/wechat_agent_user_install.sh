#!/usr/bin/env bash
set -euo pipefail

# Root-run one-time host preparation for a second Linux WeChat account.
# Runtime remains owned by the unprivileged overtree-wechat user.
if [[ "$(id -u)" != "0" ]]; then
  echo "Run this one-time preparation with sudo; it does not start WeChat." >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd -P)"
AGENT_USER="${WECHAT_AGENT_OS_USER:-overtree-wechat}"
AGENT_HOME="${WECHAT_AGENT_OS_HOME:-/home/$AGENT_USER}"
CODE_ROOT="${WECHAT_AGENT_CODE_ROOT:-/opt/overtree-labcanvas}"

if ! id "$AGENT_USER" >/dev/null 2>&1; then
  useradd --create-home --home-dir "$AGENT_HOME" --shell /bin/bash "$AGENT_USER"
fi

# The normal user's home is intentionally not traversable by the Agent user.
# Copy only repository code to a root-owned path so Agent processes can run
# without gaining access to the life-WeChat profile or private workspace data.
if [[ "$CODE_ROOT" != "$ROOT" ]]; then
  install -d -o root -g root -m 755 "$CODE_ROOT"
  rsync -a --delete \
    --exclude='.git/' \
    --exclude='.aginti/' \
    --exclude='.aginti-sessions/' \
    --exclude='.sessions/' \
    --exclude='output/' \
    --exclude='agentic_tools/wechat_gui_agent/.private/' \
    --exclude='agentic_tools/wechat_gui_agent/output/' \
    "$ROOT/" "$CODE_ROOT/"
  chown -R root:root "$CODE_ROOT"
  chmod -R a+rX "$CODE_ROOT"
fi
install -d -o "$AGENT_USER" -g "$AGENT_USER" -m 700 "$AGENT_HOME/.local/share/overtree-wechat"
install -d -o "$AGENT_USER" -g "$AGENT_USER" -m 700 "$AGENT_HOME/.config" "$AGENT_HOME/.local/share" "$AGENT_HOME/.cache"
printf 'Prepared OS user %s at %s\n' "$AGENT_USER" "$AGENT_HOME"
printf 'Prepared read-only code root at %s\n' "$CODE_ROOT"
printf 'Next, as that user, initialize the profile:\n'
printf '  sudo -u %q env HOME=%q WECHAT_AGENT_HOME=%q %q init\n' \
  "$AGENT_USER" "$AGENT_HOME" "$AGENT_HOME/.local/share/overtree-wechat" \
  "$CODE_ROOT/agentic_tools/wechat_gui_agent/scripts/wechat_agent_profile.sh"
