#!/bin/bash
# TickTick MCP — автоматическая установка (single-tenant, auto-update)
#
# Разворачивает ТВОЙ личный сервер на Railway, напрямую из апстрима (без
# форка) и подключает его к Claude. Автообновление — через маленький
# сервис-апдейтер (см. donskikhmaksim/sheets-mcp/updater/), который раз в час
# сам сверяет последний коммит и сам передеплоивает. Никакого GitHub-приложения
# Railway, никакого форка — раньше это требовало ручной авторизации доступа к
# репозиторию в каждом аккаунте, которая на практике часто не срабатывает
# ("GitHub Repo not found").
#
# Безопасно перезапускать: проект и сервис переиспользуются, не плодятся.

set -eo pipefail

# ── Часовой пояс по умолчанию — берём с ЭТОГО компьютера ────────────────────
# Раньше был захардкожен Europe/Moscow — если человек ставит себе сервер не из
# Москвы (и не передаёт --timezone явно), TickTick получал ЧУЖОЙ часовой пояс,
# и "сегодня"/даты у него уезжали на день. Берём системную таймзону машины, на
# которой реально выполняется установка — она почти всегда верна для того,
# кто установку и запускает. Fallback на Europe/Moscow только если ничего не
# удалось определить (например, редкий минимальный Linux без обоих файлов).
detect_local_timezone() {
  local tz=""
  if [[ -L /etc/localtime ]]; then
    tz=$(readlink /etc/localtime | sed -E 's#.*/zoneinfo/##')
  elif [[ -f /etc/timezone ]]; then
    tz=$(cat /etc/timezone 2>/dev/null)
  fi
  [[ -n "$tz" && "$tz" != "/etc/localtime" ]] && echo "$tz" || echo "Europe/Moscow"
}

# ── Парсинг аргументов ─────────────────────────────────────────────────────
CLIENT_ID=""
CLIENT_SECRET=""
TIMEZONE="$(detect_local_timezone)"
RAILWAY_UPDATER_TOKEN=""
GITHUB_UPDATER_TOKEN=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --client-id)     CLIENT_ID="$2";     shift 2 ;;
    --client-secret) CLIENT_SECRET="$2"; shift 2 ;;
    --timezone)      TIMEZONE="$2";      shift 2 ;;
    --railway-token) RAILWAY_UPDATER_TOKEN="$2"; shift 2 ;;
    --github-token)  GITHUB_UPDATER_TOKEN="$2";  shift 2 ;;
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
UPDATER_SOURCE_REPO="donskikhmaksim/sheets-mcp"  # updater/ живёт там — общая инфра
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
      -e 's/(TICKTICK_ACCESS_TOKEN|TICKTICK_REFRESH_TOKEN|TICKTICK_V2_TOKEN|MCP_SECRET|GH_TOKEN|GITHUB_TOKEN|RAILWAY_TOKEN)([=:] *)[^[:space:]]+/\1\2***/g' \
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
echo "Скрипт задеплоит твой персональный сервер на Railway и подключит его"
echo "к Claude. ~5-7 минут."

# ── Шаг 1: Railway CLI ─────────────────────────────────────────────────────
step "1/4  Проверяю Railway CLI"

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

# Нужен Railway CLI 4.x+ (railway list --json, variables set --service).
# На старой версии команды отличаются и всё тихо ломается — лучше сразу
# попросить обновиться.
RW_VERSION=$(set +o pipefail; railway --version 2>&1 | grep -oE '[0-9]+' | head -1 || echo 0)
if [[ "${RW_VERSION:-0}" -lt 4 ]]; then
  echo "❌ Слишком старая версия Railway CLI ($(railway --version 2>&1))."
  echo "   Обнови: brew upgrade railway  (или npm i -g @railway/cli@latest),"
  echo "   затем запусти команду ещё раз."
  exit 1
fi
ok "Railway CLI $(set +o pipefail; railway --version 2>&1 | head -1)"
ok "Часовой пояс для дат в TickTick: $TIMEZONE (определён по этому компьютеру; если не тот — перезапусти с --timezone)"

# ── Шаг 2: Логин в Railway ─────────────────────────────────────────────────
step "2/4  Войди в Railway"

if railway whoami &>/dev/null; then
  ok "Уже авторизован в Railway ($(set +o pipefail; railway whoami 2>/dev/null | tail -1))"
else
  echo ""
  echo "Сейчас откроется браузер — войди в свой аккаунт Railway."
  echo "(Если аккаунта нет — создай на railway.app, это бесплатно)"
  echo ""
  if [[ -t 0 ]]; then
    ask "Нажми Enter чтобы открыть браузер..."
    read -r
  fi
  railway login
  if ! railway whoami &>/dev/null; then
    echo "❌ Вход в Railway не удался. Попробуй ещё раз или войди вручную: railway login"
    exit 1
  fi
  ok "Авторизован в Railway"
fi

# ── Шаг 3: Деплой ────────────────────────────────────────────────────────────
step "3/4  Деплою сервер"

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
  for s in $(echo "$EXISTING_SERVICES" | tr ',' ' '); do
    [[ "$s" != "updater" ]] && SERVICE_NAME="$s" && break
  done
  if ! run_step railway link -p "$EXISTING_PROJECT_ID"; then
    echo "Не смог переиспользовать — создаю новый проект..."
    EXISTING_PROJECT_ID=""
    SERVICE_NAME=""
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

# ID проекта нужен явно (не только через ambient `railway link` в этой
# директории) — деплой ниже клонирует репозиторий в СВОЮ ВРЕМЕННУЮ папку, где
# никакого линка нет, и передаёт -p/-e напрямую в `railway up`. Резолвим
# заново по имени — работает и для переиспользованного, и для нового проекта.
PROJECT_ID=$(railway list --json 2>/dev/null | python3 -c "
import sys, json
try:
    for p in json.load(sys.stdin):
        if p.get('name') == 'ticktick-mcp' and not p.get('deletedAt'):
            print(p['id']); break
except Exception:
    pass
" 2>/dev/null || true)
if [[ -z "$PROJECT_ID" ]]; then
  echo "❌ Не смог определить ID проекта ticktick-mcp."
  exit 1
fi

if [[ -z "$SERVICE_NAME" ]]; then
  SERVICE_NAME="$SERVICE_NAME_DEFAULT"
  echo "Создаю сервис $SERVICE_NAME..."
  if ! run_step railway add --service "$SERVICE_NAME"; then
    echo "❌ Не смог создать сервис на Railway."
    exit 1
  fi
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

# Доставляет СЕГОДНЯШНИЙ код гарантированно. `railway redeploy` (даже с
# --from-source) ЗДЕСЬ НЕ ГОДИТСЯ — на практике на других сервисах он тихо
# перезапускал СТАРЫЙ собранный образ вместо того чтобы подтянуть свежий
# коммит. Клонируем апстрим напрямую (форк больше не используется — код
# обновляется апдейтером, см. ниже) и заливаем `railway up`.
echo "Загружаю и собираю свежий код..."
FRESH_DIR=$(mktemp -d)
if ! (cd "$FRESH_DIR" && git clone --depth 1 "https://github.com/$UPSTREAM_REPO.git" . --quiet) 2>&1; then
  echo "❌ Не смог скачать $UPSTREAM_REPO для деплоя."
  exit 1
fi
DEPLOY_ATTEMPT=0
DEPLOY_MAX=24  # до ~4 минут ожидания
while true; do
  # -p/-e напрямую: команда идёт из СВЕЖЕЙ временной папки, где нет своего
  # `railway link` — без этого падает NO_LINKED_PROJECT. Вывод НЕ прячем в
  # переменную — иначе на экране пусто по 1-3 минуты, пока идёт сборка, и
  # выглядит как зависание, хотя всё работает.
  if (cd "$FRESH_DIR" && railway up --service "$SERVICE_NAME" -p "$PROJECT_ID" -e production --detach); then
    break
  fi
  DEPLOY_ATTEMPT=$((DEPLOY_ATTEMPT + 1))
  if [[ $DEPLOY_ATTEMPT -ge $DEPLOY_MAX ]]; then
    echo "❌ Не получилось задеплоить свежий код после $DEPLOY_MAX попыток (см. вывод выше)."
    rm -rf "$FRESH_DIR"
    exit 1
  fi
  sleep 10
done
rm -rf "$FRESH_DIR"

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

# ── Авторизация в TickTick (локальный auth-флоу) ────────────────────────────
step "3/4  Войди в свой TickTick (продолжение)"

# Идемпотентность: не гоняем повторный вход, если аккаунт уже подключён (сам
# /health это знает). Раньше этот шаг выполнялся БЕЗУСЛОВНО при каждом
# запуске — даже когда просто обновляли код на уже работающем сервере.
ALREADY_CONNECTED=$(curl -s --max-time 10 "https://$DOMAIN/health" 2>/dev/null | grep -o '"ticktick_connected":[[:space:]]*true' || true)

if [[ -n "$ALREADY_CONNECTED" ]]; then
  ok "TickTick уже подключён (аккаунт авторизован ранее) — пропускаю повторный вход."
else
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
fi

# ── Опционально: расширенные функции (кука v2) ─────────────────────────────
# Идемпотентность: если кука уже стоит — не спрашиваем заново при каждом
# обновлении. Проверяем только НАЛИЧИЕ переменной, значение не читаем/не печатаем.
HAS_V2_TOKEN=$(railway variables --service "$SERVICE_NAME" --kv 2>/dev/null | grep -c '^TICKTICK_V2_TOKEN=' || true)

if [[ "$HAS_V2_TOKEN" -gt 0 ]]; then
  ok "Расширенные функции уже включены (кука v2 уже задана) — пропускаю."
else
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
fi

cd /

# ── Шаг 4: Апдейтер (автообновления без форков и без GitHub App) ────────────
step "4/4  Настраиваю автообновление"

echo ""
echo "Чтобы код обновлялся сам (без повторного запуска этой команды), нужен"
echo "маленький фоновый сервис. Ему нужны 2 токена — единственный ручной шаг,"
echo "который нельзя автоматизировать (ни Railway, ни GitHub не дают создать"
echo "токен программно, только руками, один раз, навсегда)."
echo ""

EXISTING_RAILWAY_TOKEN=$(railway variable list --service updater --kv 2>/dev/null | grep '^RAILWAY_TOKEN=' | head -1 | cut -d= -f2- || true)
EXISTING_GITHUB_TOKEN=$(railway variable list --service updater --kv 2>/dev/null | grep '^GITHUB_TOKEN=' | head -1 | cut -d= -f2- || true)
[[ -z "$RAILWAY_UPDATER_TOKEN" && -n "$EXISTING_RAILWAY_TOKEN" ]] && RAILWAY_UPDATER_TOKEN="$EXISTING_RAILWAY_TOKEN"
[[ -z "$GITHUB_UPDATER_TOKEN" && -n "$EXISTING_GITHUB_TOKEN" ]] && GITHUB_UPDATER_TOKEN="$EXISTING_GITHUB_TOKEN"

if [[ -n "$EXISTING_RAILWAY_TOKEN" && -n "$EXISTING_GITHUB_TOKEN" ]]; then
  ok "Токены апдейтера уже заданы — переиспользую."
fi

if [[ -z "$RAILWAY_UPDATER_TOKEN" ]]; then
  echo ""
  echo -e "1. Открой ${CYAN}https://railway.com/account/tokens${RESET}"
  echo "   Впиши любое Name, выбери свой Workspace → Create → скопируй значение."
  echo ""
  if [[ -t 0 ]]; then
    ask "Вставь Railway-токен:"
    read -r -s RAILWAY_UPDATER_TOKEN
    echo ""
  fi
fi

if [[ -z "$GITHUB_UPDATER_TOKEN" ]]; then
  echo ""
  echo -e "2. Открой ${CYAN}https://github.com/settings/tokens/new${RESET}"
  echo "   Note — любой, галочки scope НЕ отмечай — Generate token → скопируй."
  echo ""
  if [[ -t 0 ]]; then
    ask "Вставь GitHub-токен:"
    read -r -s GITHUB_UPDATER_TOKEN
    echo ""
  fi
fi

if [[ -z "$RAILWAY_UPDATER_TOKEN" || -z "$GITHUB_UPDATER_TOKEN" ]]; then
  echo -e "${YELLOW}⚠️  Токены не заданы — автообновление НЕ настроено.${RESET}"
  echo "   Сервер работает, но код придётся обновлять, перезапуская эту команду."
  echo "   Чтобы включить позже: запусти команду ещё раз с --railway-token и --github-token,"
  echo "   или добавь их вручную в Variables сервиса updater в Railway."
else
  ALREADY_EXISTS=$(railway service list --json 2>/dev/null | grep -c '"name": *"updater"' || true)
  if [[ "$ALREADY_EXISTS" -eq 0 ]]; then
    echo "Создаю сервис updater..."
    railway add --service updater --json >/dev/null 2>&1 || { echo "❌ Не смог создать сервис updater."; exit 1; }
  fi

  SERVICES_JSON="[{\"service\":\"$SERVICE_NAME\",\"repo\":\"$UPSTREAM_REPO\"}]"

  echo "Задаю переменные апдейтера..."
  run_step railway variable set "RAILWAY_TOKEN=$RAILWAY_UPDATER_TOKEN" --service updater --skip-deploys
  run_step railway variable set "GITHUB_TOKEN=$GITHUB_UPDATER_TOKEN" --service updater --skip-deploys
  run_step railway variable set "PROJECT_ID=$PROJECT_ID" --service updater --skip-deploys
  run_step railway variable set "SERVICES=$SERVICES_JSON" --service updater --skip-deploys

  echo "Деплою апдейтер (updater/ из $UPDATER_SOURCE_REPO)..."
  UPDATER_DIR=$(mktemp -d)
  if ! (cd "$UPDATER_DIR" && git clone --depth 1 "https://github.com/$UPDATER_SOURCE_REPO.git" . --quiet) 2>&1; then
    echo "❌ Не смог скачать апдейтер из $UPDATER_SOURCE_REPO."
    exit 1
  fi
  UPDATER_ATTEMPT=0
  while true; do
    if (cd "$UPDATER_DIR/updater" && railway up --service updater -p "$PROJECT_ID" -e production --detach); then
      break
    fi
    UPDATER_ATTEMPT=$((UPDATER_ATTEMPT + 1))
    if [[ $UPDATER_ATTEMPT -ge 24 ]]; then
      echo "❌ Не получилось задеплоить апдейтер после 24 попыток (см. вывод выше)."
      rm -rf "$UPDATER_DIR"
      exit 1
    fi
    sleep 10
  done
  rm -rf "$UPDATER_DIR"

  ok "Апдейтер настроен — раз в час сам проверяет и обновляет сервер."
fi

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
