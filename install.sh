#!/usr/bin/env bash
# One-time setup on Ubuntu 22.04 / 24.04:   bash install.sh
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="${SUDO_USER:-$USER}"
cd "$APP_DIR"

echo "==> system packages"
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip xvfb wget ca-certificates fonts-liberation fonts-noto-color-emoji

echo "==> Google Chrome"
if ! command -v google-chrome >/dev/null 2>&1; then
  wget -q -O /tmp/google-chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
  sudo apt-get install -y /tmp/google-chrome.deb
fi
google-chrome --version

echo "==> Python environment"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

echo "==> config"
[ -f config.yaml ] || cp config.example.yaml config.yaml
if [ ! -f /etc/harvester.env ]; then
  printf 'EBAY_CLIENT_ID=\nEBAY_CLIENT_SECRET=\n' | sudo tee /etc/harvester.env >/dev/null
  sudo chown "$RUN_USER" /etc/harvester.env && sudo chmod 600 /etc/harvester.env
fi
mkdir -p data && chown -R "$RUN_USER" data .venv 2>/dev/null || true

echo "==> systemd timer"
sed "s#__APP_DIR__#$APP_DIR#g; s#__USER__#$RUN_USER#g" deploy/harvester.service | sudo tee /etc/systemd/system/harvester.service >/dev/null
sudo cp deploy/harvester.timer /etc/systemd/system/harvester.timer
sudo systemctl daemon-reload

cat <<MSG

Installed. Next steps:
  1. edit   $APP_DIR/config.yaml        (your sites, categories, per_day)
            /etc/harvester.env          (eBay API keys, optional)
  2. test   cd $APP_DIR && .venv/bin/python harvester.py check --browser
            .venv/bin/python harvester.py run --max 2
  3. start  sudo systemctl enable --now harvester.timer
  4. watch  systemctl list-timers harvester.timer ; tail -f data/logs/harvester.log
MSG
