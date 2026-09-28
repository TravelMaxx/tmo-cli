#!/usr/bin/env bash
# one-line installer: curl -fsSL https://raw.githubusercontent.com/TravelMaxx/tmo-cli/main/quick-install.sh | bash
set -euo pipefail
DIR="${HOME}/.tmo-cli"
echo "[*] installing tmo to ${DIR}"
git clone -q https://github.com/TravelMaxx/tmo-cli.git "$DIR" 2>/dev/null || git -C "$DIR" pull -q
cd "$DIR"
python3 -m venv venv 2>/dev/null || true
./venv/bin/pip install --quiet -r requirements.txt
./venv/bin/python -m playwright install chromium >/dev/null 2>&1 || true
[ -f .env ] || cp .env.example .env
ln -sf "$DIR/tmo" /usr/local/bin/tmo 2>/dev/null || ln -sf "$DIR/tmo" "${HOME}/.local/bin/tmo 2>/dev/null" || true
echo
echo "=== installed ==="
echo "  1. ${EDITOR:-vi} ${DIR}/.env    # set TMOBILE_USERNAME / TMOBILE_PASSWORD"
echo "  2. tmo login && tmo register && tmo status"
echo
echo "  (if 'tmo' isn't on PATH: ${DIR}/tmo)"
