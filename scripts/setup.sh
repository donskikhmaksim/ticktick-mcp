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

# Runs a command, shows only its last line on success but the FULL output on
# failure — so real errors (e.g. Railway account/resource limits) are never
# silently swallowed by a trailing `tail -1`.
run_step() {
  local out
  if out=$("$@" 2>&1); then
    echo "$out" | tail -1
  else
    local code=$?
    echo ""
    echo "❌ Команда упала: $*"
    echo "── полный вывод ──────────────────────────"
    echo "$out"
    echo "───────────────────────────────────────────"
    return $code
  fi
}

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

if ! command -v railway &>/dev/null; then
  echo "❌ Railway CLI не установился. Установи вручную: https://docs.railway.com/guides/cli"
  exit 1
fi

# Весь скрипт использует синтаксис Railway CLI 4.x+ (railway list --json,
# deployment list, variables set --service). На более старой версии команды
# отличаются и всё тихо ломается — лучше сразу попросить обновиться.
RW_VERSION=$(set +o pipefail; railway --version 2>&1 | grep -oE '[0-9]+' | head -1 || echo 0)
if [[ "${RW_VERSION:-0}" -lt 4 ]]; then
  echo "❌ Слишком старая версия Railway CLI ($(railway --version 2>&1))."
  echo "   Обнови: brew upgrade railway  (или переустанови по ссылке"
  echo "   https://docs.railway.com/guides/cli), затем запусти команду ещё раз."
  exit 1
fi
ok "Railway CLI $(set +o pipefail; railway --version 2>&1 | head -1)"

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
# Гарантированно чистим временную папку на любом выходе (в т.ч. при ошибке).
trap 'rm -rf "$WORK_DIR"' EXIT
cd "$WORK_DIR"

echo "Скачиваю репозиторий..."
git clone --depth 1 https://github.com/donskikhmaksim/ticktick-mcp . --quiet

# `head -c` closes the pipe as soon as it has enough bytes, so `tr` gets
# SIGPIPE (exit 141) even though it worked correctly — under `pipefail` that
# makes the whole line "fail" and silently kills the script right here.
# Disable pipefail just for this one pipeline.
MCP_SECRET=$(set +o pipefail; LC_ALL=C tr -dc 'a-zA-Z0-9' < /dev/urandom | head -c 32)

# Если скрипт уже запускался раньше и создал проект "ticktick-mcp" — переиспользуем
# его вместо создания нового. Иначе каждый повторный запуск плодит пустые проекты,
# которые впустую жгут лимиты аккаунта Railway (особенно на бесплатном плане).
# Заодно смотрим, сколько внутри сервисов: если их 2+ (осталось от прежних
# неудачных попыток), Railway не может сам угадать нужный — раньше это роняло
# скрипт с непонятной ошибкой "Multiple services found".
EXISTING=$(railway list --json 2>/dev/null | python3 -c "
import sys, json
try:
    projects = json.load(sys.stdin)
    for p in projects:
        if p.get('name') == 'ticktick-mcp' and not p.get('deletedAt'):
            services = [e['node']['name'] for e in p.get('services', {}).get('edges', [])]
            print(p['id'] + '\t' + ','.join(services))
            break
except Exception:
    pass
" 2>/dev/null || true)

EXISTING_PROJECT_ID="${EXISTING%%$'\t'*}"
EXISTING_SERVICES="${EXISTING#*$'\t'}"
SERVICE_NAME=""

if [[ -n "$EXISTING_PROJECT_ID" ]]; then
  echo "Нашёл существующий проект ticktick-mcp — переиспользую его..."
  SERVICE_COUNT=$(echo "$EXISTING_SERVICES" | tr ',' '\n' | grep -c . || true)
  if [[ "$SERVICE_COUNT" -gt 1 ]]; then
    echo "❌ В проекте ticktick-mcp несколько сервисов от прошлых попыток: $EXISTING_SERVICES"
    echo "   Зайди на railway.app → проект ticktick-mcp → удали лишние сервисы"
    echo "   (оставь один или ни одного), затем запусти команду ещё раз."
    exit 1
  fi
  if ! run_step railway link -p "$EXISTING_PROJECT_ID"; then
    echo "Не смог переиспользовать — создаю новый проект..."
    EXISTING_PROJECT_ID=""
  elif [[ "$SERVICE_COUNT" -eq 1 ]]; then
    SERVICE_NAME="$EXISTING_SERVICES"
  fi
fi

if [[ -z "$EXISTING_PROJECT_ID" ]]; then
  echo "Создаю проект..."
  if ! run_step railway init --name "ticktick-mcp"; then
    echo ""
    echo "Не получилось создать проект на Railway. Частая причина — исчерпан"
    echo "лимит бесплатного плана (Railway trial) или не привязана карта."
    echo "Зайди на railway.app → Account Settings → Billing и проверь план,"
    echo "затем запусти команду ещё раз."
    exit 1
  fi
fi

# Сервис на Railway появляется только после первого деплоя — переменные
# можно задавать только когда он уже существует, поэтому сначала up,
# потом variables (это вызовет автоматический рестарт с новыми значениями).
echo "Загружаю и собираю (займёт 1–2 минуты)..."
# NB: вызываем run_step напрямую в if — нельзя оборачивать в $(...), иначе
# stdout самой команды попадёт в переменную вместе с yes/no и сломает проверку.
if [[ -n "$SERVICE_NAME" ]]; then
  UP_CMD=(railway up --detach --service "$SERVICE_NAME")
else
  UP_CMD=(railway up --detach)
fi
if ! run_step "${UP_CMD[@]}"; then
  echo ""
  echo "Деплой не прошёл. Частая причина — лимит ресурсов на бесплатном"
  echo "плане Railway. Проверь: railway.app → Account Settings → Billing."
  exit 1
fi

# Если сервис только что создался с нуля — узнаём его настоящее имя и
# закрепляем его во всех следующих командах, чтобы Railway больше никогда
# не пришлось гадать, какой сервис имеется в виду.
if [[ -z "$SERVICE_NAME" ]]; then
  SERVICE_NAME=$(railway status --json 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d['services']['edges'][0]['node']['name'])
except Exception:
    pass
" 2>/dev/null || true)
fi

# Запоминаем ID деплоя ДО того как меняем переменные — иначе после
# variables set (который асинхронно запускает новый деплой) можно легко
# спутать старый контейнер (ещё без правильного MCP_SECRET) с новым, потому
# что /health отвечает 200 у обоих, пока Railway не переключит трафик.
PRE_VARS_DEPLOY_ID=$(railway deployment list --json --service "$SERVICE_NAME" --limit 1 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d[0]['id'] if d else '')
except Exception:
    pass
" 2>/dev/null || true)

echo "Задаю переменные окружения..."
run_step railway variables set \
  --service "$SERVICE_NAME" \
  MCP_TRANSPORT=streamable-http \
  MCP_SECRET="$MCP_SECRET" \
  TICKTICK_CLIENT_ID="$CLIENT_ID" \
  TICKTICK_CLIENT_SECRET="$CLIENT_SECRET" \
  USER_TIMEZONE="$TIMEZONE"

# Ждём НОВЫЙ деплой (с правильными переменными), а не просто "сервис отвечает" —
# старый контейнер ещё какое-то время продолжает отвечать на /health со
# старыми (пустыми) переменными, пока Railway не переключит трафик на новый.
echo "Жду пока Railway применит переменные и пересоберёт контейнер (может занять до 5 минут)..."
DEPLOY_ATTEMPTS=0
DEPLOY_LIST_RAW=""
while true; do
  DEPLOY_ATTEMPTS=$((DEPLOY_ATTEMPTS + 1))
  DEPLOY_LIST_RAW=$(set +o pipefail; railway deployment list --json --service "$SERVICE_NAME" --limit 5 2>&1)
  if [[ $DEPLOY_ATTEMPTS -gt 60 ]]; then
    echo "❌ Новый деплой не подтвердился за 5 минут. Вот что видит Railway:"
    echo "── полный вывод ──────────────────────────"
    echo "$DEPLOY_LIST_RAW"
    echo "───────────────────────────────────────────"
    echo "Проверь также: railway logs"
    exit 1
  fi
  LATEST=$(echo "$DEPLOY_LIST_RAW" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d[0]['id'] + ' ' + d[0]['status'] if d else '')
except Exception:
    pass
" 2>/dev/null || true)
  LATEST_ID="${LATEST%% *}"
  LATEST_STATUS="${LATEST##* }"
  if [[ -n "$LATEST_ID" && "$LATEST_ID" != "$PRE_VARS_DEPLOY_ID" && "$LATEST_STATUS" == "SUCCESS" ]]; then
    break
  fi
  if [[ "$LATEST_STATUS" == "FAILED" || "$LATEST_STATUS" == "CRASHED" ]]; then
    echo "❌ Новый деплой упал (статус: $LATEST_STATUS). Вот последние деплои:"
    echo "── полный вывод ──────────────────────────"
    echo "$DEPLOY_LIST_RAW"
    echo "───────────────────────────────────────────"
    exit 1
  fi
  sleep 5
done

echo "Генерирую домен..."
DOMAIN_RAW=$(set +o pipefail; railway domain --json 2>&1)
# The first-ever call for a service (creating its domain) returns
# {"domain": "https://..."} — a single string. Any later call (domain
# already exists) returns {"domains": ["https://..."]} — a list. Since this
# script always hits the "first call" path for a brand new service, only
# handling the plural shape meant this failed on every single fresh install.
DOMAIN=$(echo "$DOMAIN_RAW" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    if data.get('domains'):
        url = data['domains'][0]
    else:
        url = data['domain']
    print(url.replace('https://', '').replace('http://', '').rstrip('/'))
except Exception:
    pass
" 2>/dev/null || true)

if [[ -z "$DOMAIN" ]]; then
  echo "❌ Не удалось получить домен. Вот что ответил Railway:"
  echo "── полный вывод ──────────────────────────"
  echo "$DOMAIN_RAW"
  echo "───────────────────────────────────────────"
  echo "Попробуй вручную: railway domain --json --service ticktick-mcp"
  exit 1
fi

echo "Проверяю, что домен уже отвечает..."
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
    if run_step railway variables set --service "$SERVICE_NAME" TICKTICK_V2_TOKEN="$V2_TOKEN"; then
      ok "Расширенные функции включены"
    else
      echo "⚠️  Не получилось сохранить куку автоматически. Не страшно — добавь её"
      echo "   вручную в Railway → твой сервис → Variables → TICKTICK_V2_TOKEN, или"
      echo "   попроси того, кто дал тебе инструкцию, помочь."
    fi
  fi
fi

# WORK_DIR удаляется автоматически через trap EXIT.
cd /

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
