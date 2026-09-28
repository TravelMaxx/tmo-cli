#!/usr/bin/env bash
# tmo-cli deploy script — sets up the venv, dependencies, and config.
set -euo pipefail

cd "$(dirname "$0")"

echo "=== tmo-cli install ==="

# --- python check ---
if ! command -v python3 >/dev/null; then
  echo "[!] python3 not found. Install Python 3.10+ first." >&2
  exit 1
fi
PYV=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
echo "[*] python $PYV"

# --- venv ---
if [ ! -d venv ]; then
  echo "[*] creating venv..."
  python3 -m venv venv
fi
./venv/bin/pip install --quiet --upgrade pip

# --- deps ---
echo "[*] installing dependencies (a few minutes first run)..."
./venv/bin/pip install --quiet -r requirements.txt

# --- chromium for the WS channel + headless login ---
echo "[*] installing Playwright Chromium..."
./venv/bin/python -m playwright install chromium

# --- config ---
if [ ! -f .env ]; then
  cp .env.example .env
  echo "[*] created .env from .env.example"
  echo "    -> EDIT .env and set TMOBILE_USERNAME / TMOBILE_PASSWORD"
else
  echo "[*] .env exists — leaving as-is"
fi

# --- perms ---
chmod +x tmo

echo
echo "=== done ==="
echo "next steps:"
echo "  1. edit .env  (TMOBILE_USERNAME=your 10-digit number, TMOBILE_PASSWORD=...)"
echo "  2. ./tmo login        # browser + 2FA, saves tokens"
echo "  3. ./tmo register     # activate this device on your line"
echo "  4. ./tmo status       # verify"
echo
echo "then: ./tmo sms send --to 5551234567 --text 'hello'"
echo "      ./tmo calllogs"
echo "      ./tmo call --target 5551234567"
