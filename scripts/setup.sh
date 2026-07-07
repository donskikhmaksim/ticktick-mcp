#!/bin/bash
# TickTick MCP — автоматическая установка
# Этот скрипт запускается один раз и настраивает всё за тебя

set -e

# ── Парсинг аргументов ─────────────────────────────────────────────────────
CLIENT_ID=""
CLIENT_SECRET=""
TIMEZONE="Europe/Moscow"
OAUTH_URL="https://ticktick-oauth-proxy-production.up.railway.app/start"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --client-id)     CLIENT_ID="$2";     shift 2 ;;
    --client-secret) CLIENT_SECRET="$2"; shift 2 ;;
    --timezone)      TIMEZONE="$2";      shift 2 ;;
    --oauth-url)     OAUTH_URL="$2";     shift 2 ;;
    *) shift ;;
  esac
done

if [[ -z "$CLIENT_ID" || -z "$CLIENT_SECRET" ]]; then
  echo "❌ Скрипт должен быть запущен с ключами --client-id и --client-secret"
  echo "   Получи персональную команду у того, кто тебе прислал эту инструкцию."
  exit 1
fi

# ── Цвета ──────────────────────────────────────────────────────────────────
BOLD="\033[1m"
GREEN="\033[0;32m"
YELLOW="\033[0;33m"
CYAN="\033[0;36m"
RESET="\033[0m"

step() { echo -e "\n${BOLD}${CYAN}▶ $1${RESET}"; }
ok()   { echo -e "${GREEN}✓ $1${RESET}"; }
ask()  { echo -e "${YELLOW}➜ $1${RESET}"; }

clear
echo -e "${BOLD}╔══════════════════════════════════════════╗"
echo -e "║   TickTick MCP — установка               ║"
echo -e "╚══════════════════════════════════════════╝${RESET}"
echo ""
echo "Скрипт настроит твой персональный сервер на Railway"
echo "и подключит его к Claude. Займёт ~10 минут."

# ── Шаг 1: Railway CLI ─────────────────────────────────────────────────────
step "1/5  Проверяю Railway CLI"

if ! command -v railway &>/dev/null; then
  echo "Устанавливаю Railway CLI..."
  if command -v brew &>/dev/null; then
    brew install railway
  else
    curl -fsSL https://railway.app/install.sh | sh
    export PATH="$HOME/.railway/bin:$PATH"
  fi
fi
ok "Railway CLI $(railway --version 2>&1 | head -1)"

# ── Шаг 2: Логин в Railway ─────────────────────────────────────────────────
step "2/5  Войди в Railway"
echo ""
echo "Сейчас откроется браузер — войди в свой аккаунт Railway."
echo "(Если аккаунта нет — создай на railway.app, это бесплатно)"
echo ""
ask "Нажми Enter чтобы открыть браузер..."
read -r

railway login

ok "Авторизован в Railway"

# ── Шаг 3: Токены TickTick (OAuth) ─────────────────────────────────────────
step "3/5  Получи токены TickTick"
echo ""
echo "Сейчас откроется страница входа TickTick."
echo "Войди в ${BOLD}свой${RESET} аккаунт TickTick и нажми Allow."
echo "После этого ты увидишь страницу с двумя токенами — скопируй их."
echo ""
ask "Нажми Enter чтобы открыть браузер..."
read -r

# Открываем браузер
if command -v open &>/dev/null; then
  open "$OAUTH_URL"
elif command -v xdg-open &>/dev/null; then
  xdg-open "$OAUTH_URL"
else
  echo "Открой в браузере: $OAUTH_URL"
fi

echo ""
ask "Вставь TICKTICK_ACCESS_TOKEN (длинная строка с eyJ...):"
read -r -s ACCESS_TOKEN
echo ""

ask "Вставь TICKTICK_REFRESH_TOKEN:"
read -r -s REFRESH_TOKEN
echo ""

if [[ -z "$ACCESS_TOKEN" || -z "$REFRESH_TOKEN" ]]; then
  echo "❌ Токены не могут быть пустыми. Запусти скрипт заново."
  exit 1
fi
ok "Токены получены"

# ── Шаг 4: V2 токен (кука) ─────────────────────────────────────────────────
step "4/5  Получи токен v2 (кука из Chrome)"
echo ""
echo "Этот токен нужен для расширенных функций."
echo ""
echo "  1. Открой ${BOLD}ticktick.com${RESET} в Chrome и войди в свой аккаунт"
echo "  2. Нажми ${BOLD}F12${RESET} (или Option+Cmd+I на Mac)"
echo "  3. Выбери вкладку ${BOLD}Application${RESET}"
echo "  4. Слева: Storage → Cookies → https://ticktick.com"
echo "  5. В поле Filter введи: ${BOLD}t${RESET}"
echo "  6. Найди строку с именем ${BOLD}t${RESET} (одна буква)"
echo "  7. Двойной клик по значению в колонке Value → скопируй"
echo ""
ask "Вставь значение куки t (TICKTICK_V2_TOKEN):"
read -r -s V2_TOKEN
echo ""

if [[ -z "$V2_TOKEN" ]]; then
  echo "❌ Токен v2 не может быть пустым. Запусти скрипт заново."
  exit 1
fi
ok "Токен v2 получен"

# ── Шаг 5: Создаём проект на Railway ───────────────────────────────────────
step "5/5  Создаю проект на Railway"

WORK_DIR=$(mktemp -d)
cd "$WORK_DIR"

echo "Скачиваю репозиторий..."
git clone --depth 1 https://github.com/donskikhmaksim/ticktick-mcp . --quiet

MCP_SECRET=$(LC_ALL=C tr -dc 'a-zA-Z0-9' < /dev/urandom | head -c 32)

echo "Создаю проект..."
railway init --name "ticktick-mcp" 2>&1 | tail -1

echo "Задаю переменные окружения..."
railway variables set \
  MCP_TRANSPORT=streamable-http \
  MCP_SECRET="$MCP_SECRET" \
  TICKTICK_ACCESS_TOKEN="$ACCESS_TOKEN" \
  TICKTICK_REFRESH_TOKEN="$REFRESH_TOKEN" \
  TICKTICK_V2_TOKEN="$V2_TOKEN" \
  TICKTICK_CLIENT_ID="$CLIENT_ID" \
  TICKTICK_CLIENT_SECRET="$CLIENT_SECRET" \
  USER_TIMEZONE="$TIMEZONE"

echo "Деплою сервис (займёт 1–2 минуты)..."
railway up --detach --quiet 2>&1 | tail -2

echo "Генерирую домен..."
DOMAIN_OUTPUT=$(railway domain 2>&1)
DOMAIN=$(echo "$DOMAIN_OUTPUT" | grep -oE '[a-z0-9-]+\.up\.railway\.app' | head -1)

echo "Жду запуска сервиса..."
until curl -sf "https://$DOMAIN/health" &>/dev/null; do
  sleep 5
done

cd /
rm -rf "$WORK_DIR"

# ── Готово ──────────────────────────────────────────────────────────────────
CONNECTOR_URL="https://$DOMAIN/mcp/$MCP_SECRET"

echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════╗"
echo -e "║   ✅  Всё готово!                        ║"
echo -e "╚══════════════════════════════════════════╝${RESET}"
echo ""
echo -e "${BOLD}Ссылка для Claude:${RESET}"
echo ""
echo -e "  ${CYAN}$CONNECTOR_URL${RESET}"
echo ""
echo -e "${BOLD}Как добавить в Claude:${RESET}"
echo "  1. Открой claude.ai → профиль → Settings → Connectors"
echo "  2. Нажми Add custom connector"
echo "  3. Вставь ссылку выше → Save"
echo ""
echo -e "${BOLD}Проверка:${RESET} напиши Claude «Покажи мои проекты в TickTick»"
echo ""
echo -e "${YELLOW}⚠️  Сохрани ссылку — она нужна если будешь переустанавливать коннектор.${RESET}"
