#!/bin/bash
# TickTick MCP — автоматическая установка (single-tenant, auto-update)
#
# Разворачивает ТВОЙ личный сервер: ты форкаешь репозиторий на свой GitHub,
# Railway деплоит из ТВОЕГО форка (нативный GitHub-деплой), а форк сам
# подтягивает апдейты апстрима (workflow sync-upstream, каждые 5 минут) —
# Railway передеплоит на каждом пуше. Один сервер = один твой TickTick-аккаунт.
#
# Безопасно перезапускать: проект, сервис и форк переиспользуются, не плодятся.

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
  echo "   Зарегистрируй своё приложение на https://developer.ticktick.com"
  echo "   (Client ID / Client Secret) и передай их в команду."
  exit 1
fi

UPSTREAM_REPO="donskikhmaksim/ticktick-mcp"
SERVICE_NAME_DEFAULT="ticktick-mcp"

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
  local out code
  if out=$("$@" 2>&1); then
    echo "$out" | tail -1
  else
    code=$?
    echo ""
    echo "❌ Команда упала: $*"
    echo "── полный вывод ──────────────────────────"
    # Маскируем секреты перед печатью, чтобы токены не утекли в лог/скриншот.
    echo "$out" | sed -E \
      -e 's/(TICKTICK_ACCESS_TOKEN|TICKTICK_REFRESH_TOKEN|TICKTICK_V2_TOKEN|MCP_SECRET|GH_TOKEN|GITHUB_TOKEN)([=:] *)[^[:space:]]+/\1\2***/g' \
      -e 's/[A-Fa-f0-9]{32,}/***/g' \
      -e 's/[A-Za-z0-9_-]{40,}/***/g'
    echo "───────────────────────────────────────────"
    return $code
  fi
}

clear
echo -e "${BOLD}╔══════════════════════════════════════════╗"
echo -e "║   TickTick MCP — установка               ║"
echo -e "╚══════════════════════════════════════════╝${RESET}"
echo ""
echo "Скрипт форкнет репозиторий на твой GitHub, задеплоит твой персональный"
echo "сервер на Railway из этого форка и подключит его к Claude. ~5-7 минут."

# ── Шаг 1: Railway CLI ─────────────────────────────────────────────────────
step "1/5  Проверяю Railway CLI"

if ! command -v railway &>/dev/null; then
  echo "Устанавливаю Railway CLI..."
  if command -v brew &>/dev/null; then
    brew install railway
  elif command -v npm &>/dev/null; then
    npm install -g @railway/cli
  else
    curl -fsSL https://railway.app/install.sh | sh
    export PATH="$HOME/.railway/bin:$PATH"
  fi
fi

if ! command -v railway &>/dev/null; then
  echo "❌ Railway CLI не установился. Установи вручную: https://docs.railway.com/guides/cli"
  exit 1
fi

# Нужен Railway CLI 4.x+ (railway list --json, service source connect,
# variables set --service). На старой версии команды отличаются и всё тихо
# ломается — лучше сразу попросить обновиться.
RW_VERSION=$(set +o pipefail; railway --version 2>&1 | grep -oE '[0-9]+' | head -1 || echo 0)
if [[ "${RW_VERSION:-0}" -lt 4 ]]; then
  echo "❌ Слишком старая версия Railway CLI ($(railway --version 2>&1))."
  echo "   Обнови: brew upgrade railway  (или npm i -g @railway/cli@latest),"
  echo "   затем запусти команду ещё раз."
  exit 1
fi
ok "Railway CLI $(set +o pipefail; railway --version 2>&1 | head -1)"

# ── Шаг 2: GitHub CLI + форк ────────────────────────────────────────────────
step "2/5  Форкаю репозиторий на твой GitHub"

GH_USER=""
if command -v gh &>/dev/null; then
  # Логинимся в gh, только если сессии ещё нет.
  if ! gh auth status &>/dev/null; then
    echo "Войди в GitHub (откроется браузер)..."
    ask "Нажми Enter чтобы начать вход в GitHub..."
    read -r
    gh auth login || true
  fi

  if gh auth status &>/dev/null; then
    GH_USER=$(gh api user --jq .login 2>/dev/null || true)
    if [[ -n "$GH_USER" ]]; then
      echo "Форкаю $UPSTREAM_REPO в твой аккаунт $GH_USER (идемпотентно)..."
      # --clone=false: форк нам нужен только как источник для Railway, локальная
      # копия не требуется. Если форк уже есть — gh просто сообщит об этом.
      gh repo fork "$UPSTREAM_REPO" --clone=false &>/dev/null || true
      # На форках GitHub Actions выключены по умолчанию — включаем, чтобы
      # workflow sync-upstream мог подтягивать апдейты апстрима.
      gh api -X PUT "repos/$GH_USER/ticktick-mcp/actions/permissions" \
        -F enabled=true -f allowed_actions=all &>/dev/null || true
      ok "Форк готов: $GH_USER/ticktick-mcp"
    fi
  fi
fi

# Фолбэк: gh нет / не залогинен / форк не удался — просим форкнуть вручную.
if [[ -z "$GH_USER" ]]; then
  echo ""
  echo -e "${YELLOW}Не удалось форкнуть автоматически (нужен GitHub CLI 'gh').${RESET}"
  echo "Форкни репозиторий вручную в браузере:"
  echo ""
  echo -e "  ${CYAN}https://github.com/$UPSTREAM_REPO/fork${RESET}"
  echo ""
  echo "Затем на странице форка: вкладка Actions → «I understand… enable»."
  echo ""
  ask "Введи свой GitHub-логин (владельца форка):"
  read -r GH_USER
  if [[ -z "$GH_USER" ]]; then
    echo "❌ Без форка Railway не сможет автообновляться. Прерываю."
    exit 1
  fi
fi

FORK_REPO="$GH_USER/ticktick-mcp"

# ── Шаг 3: Логин в Railway ─────────────────────────────────────────────────
step "3/5  Войди в Railway"

if railway whoami &>/dev/null; then
  ok "Уже авторизован в Railway ($(set +o pipefail; railway whoami 2>/dev/null | tail -1))"
else
  echo ""
  echo "Сейчас откроется браузер — войди в свой аккаунт Railway."
  echo "(Если аккаунта нет — создай на railway.app, это бесплатно)"
  echo ""
  ask "Нажми Enter чтобы открыть браузер..."
  read -r
  railway login
  if ! railway whoami &>/dev/null; then
    echo "❌ Вход в Railway не удался. Попробуй ещё раз или войди вручную: railway login"
    exit 1
  fi
  ok "Авторизован в Railway"
fi

# ── Шаг 4: Деплой из форка ──────────────────────────────────────────────────
step "4/5  Деплою сервер из твоего форка"

MCP_SECRET=$(set +o pipefail; LC_ALL=C tr -dc 'a-zA-Z0-9' < /dev/urandom | head -c 32)

# Если скрипт уже запускался раньше и создал проект "ticktick-mcp" —
# переиспользуем его вместо создания нового. Иначе каждый повторный запуск
# плодит пустые проекты, которые впустую жгут лимиты аккаунта Railway.
# Заодно смотрим, сколько внутри сервисов: если 2+ (осталось от прежних
# неудачных попыток), Railway не может сам угадать нужный.
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

# Создаём сервис бота (если ещё нет) — источник (форк) подключается к
# существующему сервису, поэтому сервис нужен ДО source connect.
if [[ -z "$SERVICE_NAME" ]]; then
  SERVICE_NAME="$SERVICE_NAME_DEFAULT"
  echo "Создаю сервис $SERVICE_NAME..."
  if ! run_step railway add --service "$SERVICE_NAME"; then
    echo "❌ Не смог создать сервис на Railway."
    exit 1
  fi
fi

# Подключаем источник — ТВОЙ форк. Это нативный GitHub-деплой: Railway
# передеплоит на каждом пуше в main форка (в т.ч. когда sync-upstream
# подтянет апдейты апстрима). Никакого `railway up`.
echo "Подключаю источник кода $FORK_REPO (автообновление при пушах)..."
if ! run_step railway service source connect \
      --repo "$FORK_REPO" --branch main --service "$SERVICE_NAME"; then
  echo "❌ Не смог подключить GitHub-репозиторий $FORK_REPO."
  echo "   Проверь, что форк существует и доступен, затем запусти команду ещё раз."
  exit 1
fi

# Если сервис уже существует и в нём УЖЕ задан MCP_SECRET — переиспользуем его,
# а не генерируем новый. Иначе повторный запуск менял бы ссылку-коннектор, и
# пришлось бы переподключать коннектор в Claude.
EXISTING_SECRET=$(railway variables --service "$SERVICE_NAME" --json -- 2>/dev/null | python3 -c "
import sys, json
try:
    print(json.load(sys.stdin).get('MCP_SECRET', ''))
except Exception:
    pass
" 2>/dev/null || true)
if [[ -n "$EXISTING_SECRET" ]]; then
  MCP_SECRET="$EXISTING_SECRET"
  echo "Переиспользую существующий ключ — ссылка-коннектор не изменится."
fi

# Постоянный том для токенов — чтобы авторизация переживала перезапуски
# контейнера. Идемпотентно: если том уже есть на /data, Railway вернёт ошибку,
# которую мы молча глотаем.
echo "Подключаю постоянный диск для токенов..."
# NB: `railway volume add` НЕ принимает --service (берёт залинкованный сервис).
railway volume add -m /data &>/dev/null || true

echo "Задаю переменные окружения..."
run_step railway variables set \
  --service "$SERVICE_NAME" \
  MCP_TRANSPORT=streamable-http \
  MCP_SECRET="$MCP_SECRET" \
  TICKTICK_CLIENT_ID="$CLIENT_ID" \
  TICKTICK_CLIENT_SECRET="$CLIENT_SECRET" \
  USER_TIMEZONE="$TIMEZONE"

echo "Запускаю сборку из форка..."
run_step railway redeploy --service "$SERVICE_NAME" --from-source --yes || \
  run_step railway redeploy --service "$SERVICE_NAME" --from-source || true

echo "Генерирую домен..."
DOMAIN_RAW=$(set +o pipefail; railway domain --service "$SERVICE_NAME" --json 2>&1)
# Первый вызов для сервиса (создание домена) возвращает {"domain": "..."} —
# строку. Любой последующий вызов (домен уже есть) возвращает
# {"domains": ["..."]} — список. Обрабатываем оба варианта.
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
  echo "Попробуй вручную: railway domain --json --service $SERVICE_NAME"
  exit 1
fi

echo "Жду пока Railway поднимет контейнер (обычно 1–3 минуты)..."
ATTEMPTS=0
while true; do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "https://$DOMAIN/health" 2>/dev/null || echo 000)
  if [[ "$CODE" == "200" ]]; then
    break
  fi
  ATTEMPTS=$((ATTEMPTS + 1))
  if [[ $ATTEMPTS -gt 60 ]]; then
    echo ""
    echo "❌ Сервис не поднялся за 5 минут (последний ответ: $CODE)."
    echo "   Проверь логи: railway logs --service $SERVICE_NAME"
    exit 1
  fi
  sleep 5
done

ok "Сервер живёт на https://$DOMAIN"

# ── Шаг 5: Авторизация в TickTick (локальный auth-флоу) ─────────────────────
step "5/5  Войди в свой TickTick"

echo ""
echo "Сейчас откроется браузер с логином TickTick — войди в СВОЙ аккаунт и"
echo "нажми Allow. Токен получается локально (client_secret не покидает твою"
echo "машину) и записывается в переменную сервера."
echo ""

# Нужен uv для локального auth-флоу (uv run сам поставит зависимости пакета).
if ! command -v uv &>/dev/null; then
  echo "Устанавливаю uv (для локальной авторизации)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

WORK_DIR=$(mktemp -d)
# Гарантированно чистим временную папку на любом выходе.
trap 'rm -rf "$WORK_DIR"' EXIT
cd "$WORK_DIR"

echo "Скачиваю репозиторий для авторизации..."
git clone --depth 1 "https://github.com/$UPSTREAM_REPO" . --quiet

# Кладём client_id/secret в .env — их читает локальный auth-флоу.
cat > .env <<EOF
TICKTICK_CLIENT_ID=$CLIENT_ID
TICKTICK_CLIENT_SECRET=$CLIENT_SECRET
EOF

ask "Нажми Enter чтобы открыть браузер для входа в TickTick..."
read -r

# uv run сам создаёт venv и ставит зависимости; auth пишет токены в .env.
uv run --python 3.12 -m ticktick_mcp.cli auth || {
  echo "❌ Локальная авторизация не удалась. Проверь Client ID/Secret и повтори."
  exit 1
}

ACCESS_TOKEN=$(set +o pipefail; grep '^TICKTICK_ACCESS_TOKEN=' .env | head -1 | cut -d= -f2-)
REFRESH_TOKEN=$(set +o pipefail; grep '^TICKTICK_REFRESH_TOKEN=' .env | head -1 | cut -d= -f2-)

if [[ -z "$ACCESS_TOKEN" ]]; then
  echo "❌ Не нашёл TICKTICK_ACCESS_TOKEN после авторизации. Повтори попытку."
  exit 1
fi

echo "Сохраняю токены в переменные сервера (Railway передеплоит автоматически)..."
if [[ -n "$REFRESH_TOKEN" ]]; then
  run_step railway variables set --service "$SERVICE_NAME" \
    TICKTICK_ACCESS_TOKEN="$ACCESS_TOKEN" \
    TICKTICK_REFRESH_TOKEN="$REFRESH_TOKEN"
else
  run_step railway variables set --service "$SERVICE_NAME" \
    TICKTICK_ACCESS_TOKEN="$ACCESS_TOKEN"
fi
ok "TickTick подключён"

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
  echo -e "  1. Открой ${BOLD}ticktick.com${RESET} в Chrome и войди в свой аккаунт"
  echo -e "  2. Нажми ${BOLD}F12${RESET} (или Option+Cmd+I на Mac)"
  echo -e "  3. Выбери вкладку ${BOLD}Application${RESET}"
  echo "  4. Слева: Storage → Cookies → https://ticktick.com"
  echo -e "  5. В поле Filter введи: ${BOLD}t${RESET}"
  echo -e "  6. Найди строку с именем ${BOLD}t${RESET} (одна буква)"
  echo "  7. Двойной клик по значению в колонке Value → скопируй"
  echo ""
  ask "Вставь значение куки t:"
  read -r -s V2_TOKEN
  echo ""

  if [[ -n "$V2_TOKEN" ]]; then
    if run_step railway variables set --service "$SERVICE_NAME" TICKTICK_V2_TOKEN="$V2_TOKEN"; then
      ok "Расширенные функции включены"
    else
      echo "⚠️  Не получилось сохранить куку автоматически. Не страшно — добавь её"
      echo "   вручную в Railway → твой сервис → Variables → TICKTICK_V2_TOKEN."
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
echo -e "${YELLOW}Обновления прилетают сами:${RESET} Railway задеплоен из $FORK_REPO (ветка main),"
echo "а форк каждые 5 минут подтягивает апдейты апстрима — ничего делать не нужно."
echo ""
echo -e "${YELLOW}⚠️  Сохрани ссылку — она нужна если будешь переустанавливать коннектор.${RESET}"
