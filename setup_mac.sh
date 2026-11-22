#!/usr/bin/env bash
# Local test setup on macOS (Apple Silicon or Intel):   bash setup_mac.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

echo "==> Checking Google Chrome"
if [ ! -d "/Applications/Google Chrome.app" ]; then
  if command -v brew >/dev/null 2>&1; then
    brew install --cask google-chrome
  else
    echo "Google Chrome not found. Install it from https://www.google.com/chrome/ and run this script again."
    exit 1
  fi
fi

echo "==> Checking Python (3.10+ needed)"
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  if command -v brew >/dev/null 2>&1; then
    brew install python@3.12 && PY=python3.12
  else
    echo "Python 3.10+ not found. Install Homebrew (https://brew.sh) then: brew install python@3.12"
    exit 1
  fi
fi
echo "    using $($PY --version)"

echo "==> Creating virtual environment (.venv)"
"$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

[ -f config.local.yaml ] || cp config.local.example.yaml config.local.yaml

cat <<'MSG'

Ready. Try it:
  source .venv/bin/activate
  python harvester.py check  -c config.local.yaml
  python harvester.py run    -c config.local.yaml --max 2
  open data-local/exports/$(date +%F)/products.json
MSG
