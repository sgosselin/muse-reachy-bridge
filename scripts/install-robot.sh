#!/usr/bin/env bash
#
# Install reachy-bridge directly ON a Reachy Mini (wireless).
#
# Run on the robot:
#   ssh pollen@reachy-mini.local        # password: root
#   curl -fsSLO https://raw.githubusercontent.com/sgosselin/muse-reachy-bridge/main/scripts/install-robot.sh
#   chmod +x install-robot.sh
#   ./install-robot.sh
#
# Env knobs:
#   TS_AUTHKEY    Tailscale auth key for headless login
#                 (https://login.tailscale.com/admin/settings/keys).
#                 If unset, run `sudo tailscale up` yourself when told.
#   AGENT_PUBKEY  the agent's ssh-ed25519 public key (else you're prompted)
#   INSTALL_DIR   checkout location (default: $HOME/muse-reachy-bridge)
set -euo pipefail

REPO="https://github.com/sgosselin/muse-reachy-bridge.git"
INSTALL_DIR="${INSTALL_DIR:-$HOME/muse-reachy-bridge}"
USER_NAME="$(id -un)"

echo "== Tailscale =="
if ! command -v tailscale >/dev/null 2>&1; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi
if ! tailscale status >/dev/null 2>&1; then
  if [ -n "${TS_AUTHKEY:-}" ]; then
    sudo tailscale up --authkey="$TS_AUTHKEY"
  else
    echo "Tailscale isn't logged in yet."
    echo "Run:  sudo tailscale up   (open the printed URL in a browser),"
    echo "then re-run this script."
    exit 1
  fi
fi
tailscale status --self=false 2>/dev/null | head -2 || true

echo "== repo =="
if ! command -v git >/dev/null 2>&1; then
  sudo apt-get update -qq && sudo apt-get install -y -qq git
fi
if [ -d "$INSTALL_DIR/.git" ]; then
  git -C "$INSTALL_DIR" pull -q
else
  git clone -q "$REPO" "$INSTALL_DIR"
fi
cd "$INSTALL_DIR"

echo "== python env =="
if [ ! -d .venv ]; then
  python3 -m venv --system-site-packages .venv 2>/dev/null || {
    sudo apt-get update -qq && sudo apt-get install -y -qq python3-venv
    python3 -m venv --system-site-packages .venv
  }
fi
# System GStreamer/GLib come from apt (python3-gi); the venv sees them via
# --system-site-packages so `pip install reachy-mini` doesn't try to build
# PyGObject from source.
sudo apt-get install -y -qq python3-gi gir1.2-gstreamer-1.0 2>/dev/null || true
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
.venv/bin/pip install -q reachy-mini 2>&1 | tail -1 || {
  echo "WARNING: could not install reachy-mini — media endpoints will 501."
  echo "The bridge still works for motion; fix media later with:"
  echo "  $INSTALL_DIR/.venv/bin/pip install reachy-mini"
}

echo "== agent key =="
mkdir -p clients
PUBKEY="${AGENT_PUBKEY:-}"
if [ -z "$PUBKEY" ] && [ ! -f clients/astro.pub ]; then
  read -rp "Paste the agent's ssh-ed25519 public key: " PUBKEY
fi
if [ -n "$PUBKEY" ]; then
  echo "$PUBKEY" > clients/astro.pub
  echo "registered in clients/astro.pub"
fi

echo "== config =="
if [ ! -f .env ]; then
  PANEL_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  printf 'BRIDGE_ROBOT=mini\nBRIDGE_MINI_URL=http://127.0.0.1:8000/api\nBRIDGE_PANEL_TOKEN=%s\n' \
    "$PANEL_TOKEN" > .env
  echo "Panel token (save this somewhere safe): $PANEL_TOKEN"
fi

echo "== services =="
sudo tee /etc/systemd/system/reachy-bridge.service >/dev/null <<EOF
[Unit]
Description=Reachy Bridge (on-robot)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/.venv/bin/python $INSTALL_DIR/bridge.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo tee /etc/systemd/system/reachy-bridge-funnel.service >/dev/null <<EOF
[Unit]
Description=Tailscale Funnel for Reachy Bridge
After=network-online.target tailscaled.service
Wants=network-online.target
Requires=tailscaled.service

[Service]
Type=simple
ExecStart=/usr/bin/tailscale funnel 8765
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now reachy-bridge.service reachy-bridge-funnel.service

echo
echo "bridge: $(systemctl is-active reachy-bridge.service) | funnel: $(systemctl is-active reachy-bridge-funnel.service)"
echo "--- public URL ---"
tailscale funnel status | head -6
echo "Panel: <url above>/panel   (token was printed during first install)"
