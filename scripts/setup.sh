#!/bin/bash
# TickTick MCP — автоматическая установка
# Этот скрипт запускается один раз и настраивает всё за тебя

set -eo pipefail

# ── Парсинг аргументов ─────────────────────────────────────────────────────
CLIENT_ID=""
CLIENT_SECRET=""
TIMEZONE="Europe/Moscow"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --client-id)     CLIENT_ID="$2";     shift 2 ;;
    --client-secret) CLIENT_SECRET="$2"; shift 2 ;;
    --timezone)      TIMEZONE="$2";      shift 2 ;;
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
echo "Скрипт задеплоит твой персональный сервер на Railway"
echo "и подключит его к Claude. Займёт ~5-7 минут."

# ── Шаг 1: Railway CLI ─────────────────────────────────────────────────────
step "1/4  Проверяю Railway CLI"

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
step "2/4  Войди в Railway"
echo ""
echo "Сейчас откроется браузер — войди в свой аккаунт Railway."
echo "(Если аккаунта нет — создай на railway.app, это бесплатно)"
echo ""
ask "Нажми Enter чтобы открыть браузер..."
read -r

railway login

ok "Авторизован в Railway"

# ── Шаг 3: Деплой ───────────────────────────────────────────────────────────
step "3/4  Деплою сервер"

WORK_DIR=$(mktemp -d)
cd "$WORK_DIR"

echo "Скачиваю репозиторий..."
git clone --depth 1 https://github.com/donskikhmaksim/ticktick-mcp . --quiet

MCP_SECRET=$(LC_ALL=C tr -dc 'a-zA-Z0-9' < /dev/urandom | head -c 32)

echo "Создаю проект..."
railway init --name "ticktick-mcp" 2>&1 | tail -1

# Сервис на Railway появляется только после первого деплоя — переменные
# можно задавать только когда он уже существует, поэтому сначала up,
# потом variables (это вызовет автоматический рестарт с новыми значениями).
echo "Загружаю и собираю (займёт 1–2 минуты)..."
railway up --detach --quiet 2>&1 | tail -2

echo "Задаю переменные окружения..."
railway variables set \
  MCP_TRANSPORT=streamable-http \
  MCP_SECRET="$MCP_SECRET" \
  TICKTICK_CLIENT_ID="$CLIENT_ID" \
  TICKTICK_CLIENT_SECRET="$CLIENT_SECRET" \
  USER_TIMEZONE="$TIMEZONE"

echo "Генерирую домен..."
DOMAIN=$(railway domain --json 2>/dev/null | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    url = data['domains'][0]
    print(url.replace('https://', '').replace('http://', '').rstrip('/'))
except Exception:
    pass
" 2>/dev/null || true)

if [[ -z "$DOMAIN" ]]; then
  echo "❌ Не удалось получить домен. Проверь вручную: railway domain --json"
  exit 1
fi

echo "Жду запуска сервиса (после применения переменных, обычно 30–90 сек)..."
ATTEMPTS=0
until curl -sf "https://$DOMAIN/health" &>/dev/null; do
  ATTEMPTS=$((ATTEMPTS + 1))
  if [[ $ATTEMPTS -gt 40 ]]; then
    echo ""
    echo "❌ Сервис не отвечает больше 3 минут. Возможно, сборка упала."
    echo "   Проверь логи командой: railway logs"
    echo "   Или напиши тому, кто прислал тебе эту команду, и пришли вывод railway logs."
    exit 1
  fi
  sleep 5
done

ok "Сервер живёт на https://$DOMAIN"

# ── Шаг 4: Авторизация в TickTick ──────────────────────────────────────────
step "4/4  Войди в свой TickTick"

SETUP_URL="https://$DOMAIN/setup/$MCP_SECRET"

echo ""
echo "Сейчас откроется страница входа TickTick."
echo "Войди в ${BOLD}свой${RESET} аккаунт и нажми Allow — токены подхватятся"
echo "автоматически, без копирования и вставки."
echo ""
ask "Нажми Enter чтобы открыть браузер..."
read -r

if command -v open &>/dev/null; then
  open "$SETUP_URL"
elif command -v xdg-open &>/dev/null; then
  xdg-open "$SETUP_URL"
else
  echo "Открой в браузере: $SETUP_URL"
fi

echo ""
ask "Когда увидишь «TickTick подключён» в браузере, вернись сюда и нажми Enter..."
read -r

# ── Опционально: расширенные функции (кука v2) ─────────────────────────────
echo ""
echo -e "${BOLD}Хочешь включить расширенные функции${RESET} (теги, привычки, корзина,"
echo "завершённые задачи, перемещение между списками)?"
echo "Это требует один ручной шаг — куку из Chrome. Можно сделать позже."
echo ""
ask "Настроить сейчас? (y/n):"
read -r ENABLE_V2

if [[ "$ENABLE_V2" == "y" || "$ENABLE_V2" == "Y" ]]; then
  echo ""
  echo "  1. Открой ${BOLD}ticktick.com${RESET} в Chrome и войди в свой аккаунт"
  echo "  2. Нажми ${BOLD}F12${RESET} (или Option+Cmd+I на Mac)"
  echo "  3. Выбери вкладку ${BOLD}Application${RESET}"
  echo "  4. Слева: Storage → Cookies → https://ticktick.com"
  echo "  5. В поле Filter введи: ${BOLD}t${RESET}"
  echo "  6. Найди строку с именем ${BOLD}t${RESET} (одна буква)"
  echo "  7. Двойной клик по значению в колонке Value → скопируй"
  echo ""
  ask "Вставь значение куки t:"
  read -r -s V2_TOKEN
  echo ""

  if [[ -n "$V2_TOKEN" ]]; then
    cd "$WORK_DIR"
    if railway variables set TICKTICK_V2_TOKEN="$V2_TOKEN" 2>&1 | tail -1; then
      ok "Расширенные функции включены"
    else
      echo "⚠️  Не получилось сохранить куку автоматически. Не страшно — добавь её"
      echo "   вручную в Railway → твой сервис → Variables → TICKTICK_V2_TOKEN, или"
      echo "   попроси того, кто дал тебе инструкцию, помочь."
    fi
  fi
fi

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
