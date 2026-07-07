#!/bin/bash
# Запусти этот скрипт чтобы сгенерировать персональную команду для друга
# Usage: bash scripts/generate_invite.sh "Имя друга" "Europe/Moscow"

set -e

NAME="${1:-Друг}"
TIMEZONE="${2:-Europe/Moscow}"
OAUTH_URL="https://ticktick-oauth-proxy-production.up.railway.app/start"

# Читаем ключи из Railway
VARS=$(cd "$(dirname "$0")/.." && railway variables --service ticktick-mcp --json -- 2>/dev/null)
CLIENT_ID=$(echo "$VARS" | python3 -c "import sys,json; print(json.load(sys.stdin)['TICKTICK_CLIENT_ID'])")
CLIENT_SECRET=$(echo "$VARS" | python3 -c "import sys,json; print(json.load(sys.stdin)['TICKTICK_CLIENT_SECRET'])")

REPO="https://github.com/donskikhmaksim/ticktick-mcp"

cat <<EOF

Отправь другу следующую команду (одной строкой):

────────────────────────────────────────────────────────
bash <(curl -fsSL ${REPO}/raw/main/scripts/setup.sh) \\
  --client-id "${CLIENT_ID}" \\
  --client-secret "${CLIENT_SECRET}" \\
  --timezone "${TIMEZONE}" \\
  --oauth-url "${OAUTH_URL}"
────────────────────────────────────────────────────────

Он запускает в терминале — и скрипт сделает всё сам.
EOF
