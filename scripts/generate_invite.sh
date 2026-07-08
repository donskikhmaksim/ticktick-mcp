#!/bin/bash
# Запусти этот скрипт чтобы сгенерировать персональную команду для друга
# Usage: bash scripts/generate_invite.sh "Имя друга" "Europe/Moscow"

set -e

NAME="${1:-Друг}"
TIMEZONE="${2:-Europe/Moscow}"

# Читаем общие ключи приложения из oauth-proxy — он всегда существует и не
# зависит от инстансов конкретных людей (проект ticktick-mcp можно сносить и
# пересоздавать, генерация инвайтов от этого не сломается). Можно переопределить
# через SOURCE_SERVICE, если проект называется иначе.
SOURCE_SERVICE="${SOURCE_SERVICE:-ticktick-oauth-proxy}"
VARS=$(railway variables --service "$SOURCE_SERVICE" --json -- 2>/dev/null)
CLIENT_ID=$(echo "$VARS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('TICKTICK_CLIENT_ID',''))")
CLIENT_SECRET=$(echo "$VARS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('TICKTICK_CLIENT_SECRET',''))")

if [[ -z "$CLIENT_ID" || -z "$CLIENT_SECRET" ]]; then
  echo "❌ Не нашёл TICKTICK_CLIENT_ID/SECRET в сервисе '$SOURCE_SERVICE'." >&2
  echo "   Убедись, что залинкован нужный проект (railway link) или задай" >&2
  echo "   SOURCE_SERVICE=<имя-сервиса> перед запуском." >&2
  exit 1
fi

REPO="https://github.com/donskikhmaksim/ticktick-mcp"

cat <<EOF

Отправь другу команду ниже (одна строка, копируй целиком).
Он вставляет её в терминал и жмёт Enter — скрипт сделает всё сам.

bash <(curl -fsSL ${REPO}/raw/main/scripts/setup.sh) --client-id "${CLIENT_ID}" --client-secret "${CLIENT_SECRET}" --timezone "${TIMEZONE}"
EOF
