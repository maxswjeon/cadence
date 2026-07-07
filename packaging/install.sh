#!/usr/bin/env bash
# Install Cadence as user systemd services, using uv for the environment.
#
#   packaging/install.sh
#
# Creates:
#   ~/.local/share/cadence/venv     the uv-managed virtualenv (editable install)
#   ~/.config/cadence/cadence.env   your config (copied from the example; kept if present)
#   ~/.local/state/cadence          runtime data (D1 / NAS / R2 / vault under var/)
#   ~/.config/systemd/user/cadence-runtime.service, cadence-devbox.service
#
# It does NOT enable/start anything — it prints the commands so you stay in control.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/cadence/venv"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/cadence"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/cadence"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv is not installed. Install it first:" >&2
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi

echo "==> Building the service environment from the lockfile (uv sync) at $VENV_DIR"
# Runtime deps only (--no-dev), pinned by uv.lock. Add FCM push with: --extra fcm
( cd "$REPO_ROOT" && UV_PROJECT_ENVIRONMENT="$VENV_DIR" uv sync --locked --no-dev )

echo "==> Creating data + config directories"
mkdir -p "$CONFIG_DIR" "$STATE_DIR" "$UNIT_DIR"

if [ ! -f "$CONFIG_DIR/cadence.env" ]; then
  cp "$REPO_ROOT/packaging/systemd/cadence.env.example" "$CONFIG_DIR/cadence.env"
  echo "    wrote $CONFIG_DIR/cadence.env (edit it to taste)"
else
  echo "    kept existing $CONFIG_DIR/cadence.env"
fi

echo "==> Installing user systemd units into $UNIT_DIR"
cp "$REPO_ROOT/packaging/systemd/cadence-runtime.service" "$UNIT_DIR/"
cp "$REPO_ROOT/packaging/systemd/cadence-devbox.service" "$UNIT_DIR/"

systemctl --user daemon-reload

cat <<EOF

Done. Next steps:

  1. Review your config:   \$EDITOR $CONFIG_DIR/cadence.env
  2. Enable + start:       systemctl --user enable --now cadence-runtime cadence-devbox
  3. Watch logs:           journalctl --user -u cadence-runtime -f
  4. Check it's serving:   curl http://127.0.0.1:3245/healthz

To keep the services running when you're logged out:
     loginctl enable-linger "\$USER"
EOF
