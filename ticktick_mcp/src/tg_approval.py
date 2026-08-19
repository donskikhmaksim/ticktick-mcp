"""
tg_approval.py — опциональный внеполосный (out-of-band) Telegram-фактор
поверх текстового `user_reply` (docs/DESIGN_approval_gate.md §4.5: «модель
может сфабриковать user_reply — это не закрыто в рамках {без кнопок}»).

Портировано с TypeScript-модуля gmail-mcp/src/tg_approval.ts — тот же бот
(@maksim_mcp_approval_bot), та же таблица `tg_approvals` в ОБЩЕМ Postgres,
который уже используют gmail/sheets/calendar/docs/drive-mcp. По умолчанию
(`TG_BOT_TOKEN_OVERRIDE` не задан) gmail-mcp остаётся ЕДИНСТВЕННЫМ владельцем
вебхука (`TG_WEBHOOK_OWNER=true` только там) — ticktick-mcp не регистрирует
`setWebhook` и не поднимает `/tg/webhook`. Решение по кнопке доходит сюда
через ту же таблицу: gmail-mcp's `consumeTgDecisionAnyServer` уже
server-agnostic (manifest_id — глобальный PRIMARY KEY), так что строка с
`server='ticktick'` обрабатывается ИМ без единой правки на его стороне.

СВОЙ БОТ (2026-08-06, `TG_BOT_TOKEN_OVERRIDE`, портировано byte-for-byte по
духу с той же правки на TS-стороне — см. `ownBot` в gmail/drive-mcp's
`config.ts`/`tg_approval.ts`). Единственный флаг — переменная окружения
`TG_BOT_TOKEN_OVERRIDE`; её присутствие И ЕСТЬ включатель `own_bot`, отдельной
булевой переменной нет. Когда она задана:
  * `bot_token` резолвится в НЕЁ, а не в общий `TG_BOT_TOKEN` — сервер говорит
    со СВОИМ ботом, а не с общим `@maksim_mcp_approval_bot`;
  * `main()` (server.py) регистрирует `/tg/webhook` через `register_webhook()`
    и `setWebhook` — этот сервер сам становится вебхук-владельцем СВОЕГО
    токена (никакого конфликта с `TG_WEBHOOK_OWNER` общего бота: токены
    разные, Telegram не может перепутать, кому какой апдейт маршрутизировать);
  * `handle_webhook()` консьюмит решение SERVER-SCOPED
    (`WHERE server = 'ticktick'`), а не через общую `consumeTgDecisionAnyServer`-
    логику gmail-mcp: раз вебхук получает апдейты ТОЛЬКО своего бота, чужих
    манифестов здесь физически быть не может, и server-scoped фильтр —
    дополнительный (не единственный) пояс безопасности поверх глобально
    уникального `manifest_id`.
Без `TG_BOT_TOKEN_OVERRIDE` (по умолчанию) всё поведение выше отсутствует
БИТ В БИТ, как и до появления этого флага — обратная совместимость по
построению, а не по соглашению. Откат — снять переменную, без нового деплоя.

ИСПОЛНЕНИЕ ОСТАЁТСЯ ЗА ПОЛЛЕРОМ, А НЕ ЗА ВЕБХУКОМ (важно для защиты от
двойного исполнения — см. server.py's `_tg_auto_execute_poller_loop`/
`_consume_manifest_for_auto_execute`). `handle_webhook()` здесь только
переводит строку `tg_approvals` PENDING → APPROVED/REJECTED — ТОЧНО ТУ ЖЕ
роль, которую раньше (и сейчас, без own_bot) играл общий вебхук gmail-mcp.
Саму мутацию в TickTick по-прежнему исполняет фоновый поллер, который читает
статус APPROVED из той же таблицы и атомарно захватывает план через
`manifest_store.claim()` (`UPDATE … WHERE consumed_at IS NULL … RETURNING`) —
ЭТОТ захват один и тот же, кем бы ни был проставлен APPROVED (общим ботом
через gmail-mcp или своим ботом через этот вебхук), и он уже был единственной
защитой от двойного исполнения ДО этой правки. own_bot не добавляет и не
меняет ни одной строчки в поллере — см. `try_auto_execute` и
`_consume_manifest_for_auto_execute` в server.py, которые не знают о
существовании own_bot вовсе.

ВАЖНО про историю (docs/DESIGN_approval_gate.md §7): v1-дизайн с Telegram-
кнопками для ticktick-mcp был explicitly ОТВЕРГНУТ Максимом 2026-07-27
(«моё согласие в чате должно работать; без кнопок»). Этот модуль строится
2026-08-05 по прямому, более позднему указанию Максима того же дня («Все 5 +
тик тик», «тикток самое важное») — трактуется как обновление той позиции, а
не как игнорирование прежнего решения. Кнопка здесь — ОПЦИОНАЛЬНЫЙ ВТОРОЙ
фактор ПОВЕРХ существующего `_require_consent()` (chat «да» остаётся
обязательным первым фактором и работает без Telegram, если фича выключена):
`TG_APPROVAL_ENABLED` по умолчанию false — форк/деплой без бота ведёт себя
побайтово как раньше.

OFF BY DEFAULT: без `TG_APPROVAL_ENABLED=true` ни одна функция здесь не
делает сетевых обращений и не трогает Postgres.

ТРАНСПОРТ ОТЧЁТОВ (2026-08-06). Личка владельца — это «пульт»: план с
кнопками, а после исполнения — КОРОТКАЯ сводка вместо плана
(`summarize_in_owner_chat`), причём просроченные планы из неё удаляются
целиком (`reap_expired`). ПОЛНЫЙ отчёт уходит отдельным сообщением в
группу-архив (`post_report_to_group`, env `TG_REPORTS_CHAT_ID`, у Максима это
группа «MCP Отчёты» с chat_id вида `-100…`; не задан — отчёты идут в личку).
Искусственная обрезка текста убрана: и план, и отчёт бьются на несколько
сообщений (`split_for_telegram` + `send_message_chunked`) с честной проверкой
длины УЖЕ сконвертированного HTML.

Env этого слоя: TG_APPROVAL_ENABLED, TG_BOT_TOKEN, TG_OWNER_CHAT_ID,
TG_APPROVAL_TOOLS, TG_APPROVAL_TTL_S, TG_REPORTS_CHAT_ID, TG_REAP_ENABLED.
Свой бот (см. блок выше): TG_BOT_TOKEN_OVERRIDE, TG_APPROVAL_WEBHOOK_SECRET.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import (Any, Callable, Dict, Iterable, List, NamedTuple,
                    Optional, Tuple)
from zoneinfo import ZoneInfo

import requests

from . import automation_key
from . import log_redaction

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
# Настоящий лимит Bot API на sendMessage.text. Раньше здесь стоял ИСКУССТВЕННЫЙ
# PREVIEW_CAP=3500 с обрезкой «…» — Максим 2026-08-06 потребовал его убрать:
# длинный план/отчёт нельзя молча резать, его надо доставить целиком, разбив на
# несколько сообщений (см. split_for_telegram / send_message_chunked).
TELEGRAM_TEXT_LIMIT = 4096
TG_TIMEOUT_S = 8

# Отчёты датируются в часовом поясе владельца, НЕ в UTC (жёсткое требование
# Максима: он читает время глазами и не должен пересчитывать). Резолвится
# ЛЕНИВО и под try: в урезанном образе без пакета tzdata ZoneInfo бросает, а
# упасть на импорте модуля из-за форматирования даты в отчёте — несоразмерная
# цена; тогда честно откатываемся на фиксированный -08:00 и пишем в лог.
OWNER_TZ_NAME = "America/Los_Angeles"
_owner_tz = None


def _resolve_owner_tz():
    global _owner_tz
    if _owner_tz is None:
        try:
            _owner_tz = ZoneInfo(OWNER_TZ_NAME)
        except Exception as e:  # noqa: BLE001 — нет базы часовых поясов
            logger.warning(f"TG: не нашёл зону {OWNER_TZ_NAME} ({e}) — время в "
                           f"отчётах пойдёт с фиксированным -08:00")
            _owner_tz = timezone(timedelta(hours=-8), "PST")
    return _owner_tz


@dataclass
class TgApprovalConfig:
    enabled: bool
    bot_token: str
    owner_chat_id: str
    server: str  # константа "ticktick", как CONSENT_SERVER у TS-серверов
    tools_allowlist: Optional[set]  # None = все гейтованные тулы
    ttl_s: int
    # Новые поля — с дефолтами СОЗНАТЕЛЬНО: конфиг конструируется в тестах и в
    # server.py позиционно/по именам без них, и добавление обязательных полей
    # сломало бы существующие вызовы. Пустой reports_chat_id = «отчёты в личку».
    reports_chat_id: str = ""
    reap_enabled: bool = True
    # own_bot / webhook_secret (2026-08-06, TG_BOT_TOKEN_OVERRIDE) — та же
    # дисциплина дефолтов: оба поля добавлены ПОСЛЕДНИМИ и оба со значением,
    # воспроизводящим поведение ДО их появления (own_bot=False — общий бот и
    # поллер, ни байта нового HTTP-роута; webhook_secret="" — secret_token_
    # matches() всегда отказывает пустому секрету, так что случайно оставшийся
    # own_bot=True без секрета не открывает вебхук неаутентифицированным
    # запросам сам по себе — хотя load_tg_approval_config ниже такую
    # комбинацию и не пропускает, конструирование конфига вручную в тестах
    # остаётся безопасным по умолчанию).
    own_bot: bool = False
    webhook_secret: str = ""


def load_tg_approval_config() -> TgApprovalConfig:
    enabled = os.environ.get("TG_APPROVAL_ENABLED", "").strip().lower() == "true"
    # TG_BOT_TOKEN_OVERRIDE — единственный переключатель own_bot: сама его
    # непустота и есть флаг (отдельной булевой TG_OWN_BOT переменной нет,
    # см. блок в шапке файла и TS-референс gmail/drive-mcp's config.ts). Когда
    # он задан, ЭТОТ токен используется для ВСЕХ вызовов Telegram API — план,
    # отчёты, вебхук — сервер целиком переезжает на свой бот, а не только
    # вебхук.
    own_bot_token = os.environ.get("TG_BOT_TOKEN_OVERRIDE", "").strip()
    own_bot = bool(own_bot_token)
    bot_token = own_bot_token or os.environ.get("TG_BOT_TOKEN", "").strip()
    owner_chat_id = os.environ.get("TG_OWNER_CHAT_ID", "").strip()
    webhook_secret = os.environ.get("TG_APPROVAL_WEBHOOK_SECRET", "").strip()
    tools_raw = os.environ.get("TG_APPROVAL_TOOLS", "").strip()
    tools_allowlist = (
        {t.strip() for t in tools_raw.split(",") if t.strip()} if tools_raw else None
    )
    ttl_s = int(os.environ.get("TG_APPROVAL_TTL_S", "3600"))
    # Группа-архив отчётов (у Максима это «MCP Отчёты», id вида -100…). Если не
    # задана — отчёты идут в личку владельца, т.е. поведение как до 2026-08-06.
    reports_chat_id = os.environ.get("TG_REPORTS_CHAT_ID", "").strip() or owner_chat_id
    # Уборщик просроченных сообщений включён ПО УМОЛЧАНИЮ: висящий в чате план
    # без кнопок — мусор, который человек может принять за актуальный. Выключать
    # приходится явным TG_REAP_ENABLED=false (на время отладки).
    reap_enabled = os.environ.get("TG_REAP_ENABLED", "").strip().lower() != "false"
    if enabled and (not bot_token or not owner_chat_id):
        missing = ", ".join(
            n for n, v in (("TG_BOT_TOKEN (или TG_BOT_TOKEN_OVERRIDE)", bot_token),
                           ("TG_OWNER_CHAT_ID", owner_chat_id)) if not v
        )
        raise RuntimeError(
            f"TG_APPROVAL_ENABLED=true, но не задано: {missing}. Либо задай оба, либо "
            "убери TG_APPROVAL_ENABLED, чтобы работать без этого слоя."
        )
    # own_bot тянет за собой РЕАЛЬНЫЙ HTTP-эндпоинт (/tg/webhook) — без секрета
    # для проверки X-Telegram-Bot-Api-Secret-Token кто угодно, узнавший URL,
    # мог бы слать поддельные callback_query и решать подтверждения за
    # владельца. Тот же fail-fast принцип, что у bot_token/owner_chat_id
    # выше — не деградировать молча до незащищённого вебхука.
    if enabled and own_bot and not webhook_secret:
        raise RuntimeError(
            "TG_BOT_TOKEN_OVERRIDE задан (own_bot), но TG_APPROVAL_WEBHOOK_SECRET — нет. "
            "Без него /tg/webhook не сможет отличить настоящий запрос от Telegram от "
            "подделки. Задай TG_APPROVAL_WEBHOOK_SECRET (любая случайная строка, "
            "например `openssl rand -hex 24`) либо убери TG_BOT_TOKEN_OVERRIDE."
        )
    return TgApprovalConfig(
        enabled=enabled, bot_token=bot_token, owner_chat_id=owner_chat_id,
        server="ticktick", tools_allowlist=tools_allowlist, ttl_s=ttl_s,
        reports_chat_id=reports_chat_id, reap_enabled=reap_enabled,
        own_bot=own_bot, webhook_secret=webhook_secret,
    )


def enabled_for(cfg: TgApprovalConfig, tool: str) -> bool:
    if not cfg.enabled:
        return False
    if cfg.tools_allowlist is None:
        return True
    return tool in cfg.tools_allowlist


# ───────────────────────── markdown → Telegram HTML ─────────────────────────
# Порт mdToTelegramHtml из gmail-mcp/src/tg_approval.ts — та же логика и та же
# защита от intraword-подчёркиваний (имена файлов/тегов с "_" не должны
# раскурсивливаться), см. комментарий в TS-оригинале.

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_CODE_RE = re.compile(r"`([^`]+)`")
_ITALIC_RE = re.compile(r"(?<!\w)_([^_\n]+)_(?!\w)")


def md_to_telegram_html(text: str) -> str:
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    lines = []
    for line in escaped.split("\n"):
        m = _HEADING_RE.match(line)
        lines.append(f"<b>{m.group(2)}</b>" if m else line)
    joined = "\n".join(lines)
    joined = _BOLD_RE.sub(r"<b>\1</b>", joined)
    joined = _CODE_RE.sub(r"<code>\1</code>", joined)
    joined = _ITALIC_RE.sub(r"<i>\1</i>", joined)
    return joined


# ───────────────────────── предохранитель: инструкции модели не текут людям ──
#
# Служебные строки вида «[агенту: перепечатай это ДОСЛОВНО ...]» (см.
# output-format.md §5.3 / references/identity-postverify.md, шаблон
# post-verify) адресованы модели, которая вызвала MCP-инструмент, а не
# Максиму — они легитимны в ТЕКСТЕ, который возвращается моделью, но не имеют
# смысла для человека, читающего Telegram, и активно вводят его в
# заблуждение (буквально просят «агента» что-то перепечатать).
#
# 2026-08-06 (fix/agent-tail-in-verify-report): дефект №2 предыдущего фикса
# (3b14af0) развёл каналы только для ПЛАНА (`_maybe_tg_notify_plan`'s
# `agent_tail`), но не для отчёта независимой проверки
# (`_build_operation_report`) — та же строка утекала в Telegram и через
# самоотчёт модели (модель дословно исполняла «перепечатай»), и, что хуже,
# ДЕТЕРМИНИРОВАННО через сам сервер: `_verified_auto_execute_report` (кнопка
# ✅ на плане → автоисполнение без модели вообще) вклеивал сырой отчёт в
# `full_md`, который `post_report_to_group`/`send_message_chunked` отправляли
# в Telegram напрямую — там инструкцию не видел вообще НИКТО, кому она была
# адресована, только Максим.
#
# Эта функция — ПОСЛЕДНИЙ рубеж перед Telegram, а не единственный: правильные
# места (server.py's `_verified_auto_execute_report`) обязаны сами не класть
# служебные строки в текст, уходящий сюда (разведение каналов, как у
# `agent_tail`). Но если завтра кто-то добавит новую служебную вставку в
# новом месте и забудет об этом — она СТРУКТУРНО не пройдёт дальше этой
# функции, а не «повезёт/не повезёт». Матчится и «агенту:», и «agent:»
# (англоязычный вариант), регистронезависимо, и вырезается ВСЯ строка
# целиком (не только содержимое скобок) — формулировка может быть любой,
# признак утечки — что строка написана моделью для модели.
_AGENT_INSTRUCTION_LINE_RE = re.compile(
    r"^.*\[\s*(?:агенту|agent)\s*:[^\]\n]*\].*(?:\n|$)",
    re.IGNORECASE | re.MULTILINE,
)


def strip_agent_instructions(text: str) -> str:
    """Убирает из текста, уходящего человеку в Telegram, любую строку вида
    `[агенту: ...]` / `[agent: ...]` (регистронезависимо). Безопасна для
    текста без таких строк (возвращает его как есть) и для пустого текста."""
    if not text:
        return text
    cleaned = _AGENT_INSTRUCTION_LINE_RE.sub("", text)
    if cleaned == text:
        return text
    # Схлопываем пустые строки, которые могла оставить вырезанная строка
    # (она обычно стоит последней, после пустой строки-разделителя).
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip("\n")


# ───────────────────────── чанкинг под лимит Telegram ─────────────────────────
#
# Почему не «обрезать по 3500 символов исходника», как было раньше:
#   1) обрезка ТЕРЯЕТ данные — план/отчёт с хвостом в 200 задач превращался в
#      «…», и человек подтверждал кнопкой то, чего не видел;
#   2) считать длину ИСХОДНИКА в принципе неверно — Telegram считает символы
#      УЖЕ ОТКОНВЕРТИРОВАННОГО HTML, а конвертация только УДЛИНЯЕТ текст
#      (`&`→`&amp;` +4, `<`→`&lt;` +3, `**x**`→`<b>x</b>` +3, `` `x` ``→
#      `<code>x</code>` +11). Поэтому весь бюджет здесь считается по
#      len(md_to_telegram_html(кусок)), а не по len(кусок).
#
# Пары разметки (`**` и `` ` ``) нельзя рвать между сообщениями: Telegram
# распарсит первый кусок как HTML с незакрытым `<b>` и вернёт 400 «can't parse
# entities», т.е. сообщение просто НЕ ДОЙДЁТ. Поэтому кусок либо сбалансирован
# по построению (пару целиком уносим в следующий кусок), либо мы честно
# дозакрываем разметку в конце и переоткрываем её в начале следующего.

def _open_markers(s: str) -> list[str]:
    """Какие парные маркеры (`**`, `` ` ``) остались ОТКРЫТЫМИ в конце текста
    (стеком, в порядке открытия). Нужен и для решения «рвать / не рвать», и для
    дозакрытия куска.

    `_italic_` здесь НЕ учитывается намеренно: его regex требует non-word
    границы, поэтому одиночное подчёркивание в середине слова
    (`n8n_email_algo`) курсивом не становится и разметку не ломает."""
    stack: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        if s.startswith("**", i):
            if stack and stack[-1] == "**":
                stack.pop()
            else:
                stack.append("**")
            i += 2
        elif s[i] == "`":
            if stack and stack[-1] == "`":
                stack.pop()
            else:
                stack.append("`")
            i += 1
        else:
            i += 1
    return stack


def _closing_for(s: str) -> str:
    """Хвост, которым надо дозакрыть текст, чтобы разметка была парной."""
    return "".join(reversed(_open_markers(s)))


def _html_len(s: str) -> int:
    """Длина ПОСЛЕ конвертации — единственная величина, которую считает
    Telegram. Всё бюджетирование в этом модуле идёт через неё."""
    return len(md_to_telegram_html(s))


def _hard_split_word(prefix: str, word: str, limit: int) -> int:
    """Последний рубеж: слово само длиннее лимита (например, вставленный URL
    или base64) — режем по символам. Возвращает, сколько символов слова влезет
    в кусок, начинающийся с `prefix`. Бинарный поиск + линейная докрутка:
    HTML-длина почти монотонна по длине префикса, но не строго (курсив может
    «включиться» при обрезке), поэтому результат перепроверяется."""
    lo, hi, best = 1, len(word), 0
    while lo <= hi:
        mid = (lo + hi) // 2
        cand = prefix + word[:mid]
        if _html_len(cand + _closing_for(cand)) <= limit:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    while best > 1:
        cand = prefix + word[:best]
        if _html_len(cand + _closing_for(cand)) <= limit:
            break
        best -= 1
    # Не разрубать `**` пополам: осиротевшая одиночная `*` не ломает HTML, но
    # выглядит мусором и в следующем куске уже не составит пару.
    if 0 < best < len(word) and word[best - 1] == "*" and word[best] == "*":
        best -= 1
    return max(best, 1)  # хотя бы один символ, иначе бесконечный цикл


def _split_long_line(line: str, limit: int) -> list[str]:
    """Одна строка длиннее лимита — режем её по границам СЛОВ (пробел), а если
    и слово не влезает — по символам. Каждый выданный фрагмент самодостаточен:
    незакрытая разметка дозакрывается в конце и переоткрывается в начале
    следующего фрагмента."""
    words = line.split(" ")
    out: list[str] = []
    prefix = ""      # переоткрытая разметка, унаследованная от прошлого фрагмента
    cur = ""
    started = False
    i = 0
    while i < len(words):
        w = words[i]
        cand = (cur + " " + w) if started else (cur + w)
        if _html_len(cand + _closing_for(cand)) <= limit:
            cur, started = cand, True
            i += 1
            continue
        if started:
            out.append(cur + _closing_for(cur))
            prefix = "".join(_open_markers(cur))
            cur, started = prefix, False
            continue
        # даже одно слово (вместе с переоткрытой разметкой) не влезло
        k = _hard_split_word(cur, w, limit)
        piece = cur + w[:k]
        out.append(piece + _closing_for(piece))
        prefix = "".join(_open_markers(piece))
        cur, started = prefix, False
        words[i] = w[k:]
        if not words[i]:
            i += 1
    if started:
        out.append(cur + _closing_for(cur))
    return out


def _finalize_chunk(lines: list[str], limit: int) -> tuple[str, list[str], list[str]]:
    """Закрывает набранный кусок. Сначала пытается ОТДАТЬ хвостовые строки в
    следующий кусок, чтобы разметка внутри осталась парной (предпочтительный
    путь по ТЗ); если сбалансировать не выходит (например, весь кусок — одна
    гигантская строка с открытым `**`), сообщает наружу, что осталось
    открытым, — решение «дозакрывать или нет» принимает вызывающий, потому что
    только он знает, есть ли продолжение.

    Возвращает (текст_куска БЕЗ дозакрытия, строки_обратно_в_очередь,
    открытые_маркеры)."""
    for cut in range(len(lines), 0, -1):
        text = "\n".join(lines[:cut])
        # проверяем и баланс, и длину: откат хвоста в редком случае может
        # ВКЛЮЧИТЬ курсив («_x_bar» → «_x_») и удлинить HTML.
        if not _open_markers(text) and _html_len(text) <= limit:
            return text, lines[cut:], []
    text = "\n".join(lines)
    return text, [], _open_markers(text)


def _emergency_cut(line: str, limit: int) -> tuple[str, str]:
    """Аварийный рубеж — когда обычная дорезка НЕ СХОДИТСЯ.

    Режем строку по символам БЕЗ дозакрытия и переоткрытия разметки:
    возвращаем максимальный префикс, чей HTML укладывается в лимит, и остаток.
    Разметка в таком куске может остаться незакрытой — это осознанный размен,
    и он безопасен: `send_message_chunked` при ответе Telegram «can't parse
    entities» повторяет кусок plain-текстом, так что сообщение всё равно
    доходит. Терять данные или зависать — хуже, чем потерять жирный шрифт.

    Минимум один символ: иначе вызывающий цикл не сдвинется."""
    lo, hi, best = 1, len(line), 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if _html_len(line[:mid]) <= limit:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    while best > 1 and _html_len(line[:best]) > limit:
        best -= 1
    return line[:best], line[best:]


def split_for_telegram(md_text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Режет markdown так, чтобы КАЖДЫЙ кусок ПОСЛЕ md_to_telegram_html()
    укладывался в `limit` символов. Инвариант, который держит весь модуль:
    len(md_to_telegram_html(chunk)) <= limit для любого возвращённого chunk.

    Приоритет границ: перенос строки → пробел → символ (последний рубеж).
    Пустой/пробельный вход → [] (слать нечего)."""
    if not md_text or not md_text.strip():
        return []

    units = deque()
    for line in md_text.split("\n"):
        if _html_len(line) <= limit:
            units.append(line)
        else:
            units.extend(_split_long_line(line, limit))

    chunks: list[str] = []
    cur: list[str] = []
    carry: list[str] = []  # разметка, переоткрываемая в начале очередного куска
    reopened = False      # текущий кусок начат с НАШЕЙ переоткрытой разметки
    emergency_cuts = 0    # сколько раз пришлось резать грубо (для одного лога)

    def _emit(lines: list[str], more_ahead: bool) -> tuple[list[str], list[str]]:
        """Кладёт кусок в результат; возвращает то, что надо переоткрыть
        дальше. Дозакрываем разметку ТОЛЬКО если это наш собственный разрыв
        (есть продолжение или кусок начат с переоткрытой пары) — изначально
        кривой markdown («**» без пары в исходнике) правкой не «чиним», чтобы
        не менять смысл текста автора."""
        whole = "\n".join(lines)
        if not more_ahead and _html_len(whole + _closing_for(whole)) <= limit:
            # Резать НЕЧЕГО: продолжения нет и весь остаток влезает целиком.
            # _finalize_chunk в этом случае всё равно пытался «сбалансировать»
            # кусок, отдавая хвостовые строки следующему, — и на кривом
            # исходнике (одиночный `**`/`` ` `` в НЕ-первой строке: «Купить
            # 2**2 доски», «не забыть про ` в скрипте») это дробило короткий
            # отчёт на два сообщения с маркерами «(часть 1/2)» на пустом
            # месте. Живой прогон 2026-08-06: текст в 47 символов уезжал
            # двумя сообщениями. Отдавать хвост имеет смысл только когда
            # кусок реально переполнен.
            text, leftover, opened = whole, [], _open_markers(whole)
        else:
            text, leftover, opened = _finalize_chunk(lines, limit)
        if opened and (more_ahead or leftover or reopened):
            text += "".join(reversed(opened))
            nxt = list(opened)
        else:
            nxt = []
        if text.strip():
            chunks.append(text.strip("\n") or text)
        return nxt, leftover

    while units or cur:
        if not units:
            carry, leftover = _emit(cur, more_ahead=False)
            cur, reopened = [], bool(carry)
            units.extendleft(reversed(leftover))
            continue

        line = units.popleft()
        if not cur and carry:
            line = "".join(carry) + line
            carry, reopened = [], True
        cand = "\n".join(cur + [line])
        if _html_len(cand + _closing_for(cand)) <= limit:
            cur.append(line)
            continue
        if not cur:
            # Строка не влезла в ПУСТОЙ кусок (обычно из-за переоткрытой
            # разметки, добавленной к уже нарезанному фрагменту) — дорезаем.
            #
            # ЗАЩИТА ОТ ВЕЧНОГО ЦИКЛА (найдено фаззингом 2026-08-06). На
            # несбалансированной разметке `_split_long_line` может вернуть
            # фрагменты НЕ КОРОЧЕ входа: сколько отрезали — столько же
            # добавило дозакрытие в конце куска и переоткрытие в начале
            # следующего (вход 8 символов → части [6, 8, 8]). Такая строка
            # возвращалась в очередь снова и снова, и `split_for_telegram`
            # крутилась ВЕЧНО, съедая память под `units`. Воспроизводится на
            # БОЕВОМ лимите 4096: строка из ~1000 повторов «`**» — стек
            # открытых маркеров растёт линейно, потому что `` ` `` внутри
            # `**` не образует пары. Прод-цена была тяжёлой: этот же код
            # зовёт `notify_plan` (тул никогда не вернётся) и поллер
            # автоисполнения, а до выноса в поток — вешал весь event loop.
            # Текст такой формы реален: отчёт печатает НАЗВАНИЯ задач
            # дословно, а они приходят извне.
            #
            # Инвариант, который гарантирует завершение: в очередь уходят
            # только фрагменты СТРОГО КОРОЧЕ исходной строки. Иначе — грубая
            # посимвольная резка (`_emergency_cut`), которая тоже всегда
            # укорачивает остаток минимум на один символ.
            parts = _split_long_line(line, limit)
            if len(parts) > 1 and max(len(p) for p in parts) < len(line):
                units.extendleft(reversed(parts))
                continue
            head, tail = _emergency_cut(line, limit)
            emergency_cuts += 1
            if emergency_cuts == 1:
                # Ровно один раз за вызов: на мусорном вводе аварийных резов
                # бывают тысячи, и лог Railway забивался бы одинаковыми
                # строками (а его объём — деньги и место для НУЖНЫХ ошибок).
                logger.warning(
                    f"TG: строку длиной {len(line)} не удалось разбить по "
                    f"словам (несбалансированная разметка) — режу аварийно "
                    f"по символам, разметка в куске может остаться открытой")
            if head.strip():
                chunks.append(head)
            if tail:
                units.appendleft(tail)
            continue
        carry, leftover = _emit(cur, more_ahead=True)
        cur, reopened = [], bool(carry)
        units.extendleft(reversed(leftover + [line]))
    return chunks


# ───────────────────────── Telegram HTTP ─────────────────────────

def _tg_call(cfg: TgApprovalConfig, method: str, body: dict) -> dict:
    """error_code/parameters пробрасываются наружу намеренно: без них не
    отличить «разметка кривая» (400) от «слишком часто» (429 + retry_after), а
    от этого зависит, повторять запрос или менять формат (см.
    send_message_chunked)."""
    url = f"{TELEGRAM_API}/bot{cfg.bot_token}/{method}"
    try:
        res = requests.post(url, json=body, timeout=TG_TIMEOUT_S)
        data = res.json()
    except Exception as e:
        logger.warning(f"TG approval: {method} failed: {e}")
        return {"ok": False, "description": str(e)}
    return {"ok": bool(data.get("ok")) and res.ok, "result": data.get("result"),
            "description": data.get("description"),
            "error_code": data.get("error_code"),
            "parameters": data.get("parameters") or {}}


# Место под хвостовой маркер «(часть N/M)», который добавляется УЖЕ ПОСЛЕ
# нарезки, — поэтому при многокусковой отправке бюджет куска сужается на эту
# величину (с запасом на трёхзначные номера частей).
_PART_SUFFIX_RESERVE = 48

_PARSE_ERROR_HINTS = (
    "can't parse entities",
    "cant parse entities",
    "unsupported start tag",
    "unclosed start tag",
    "can't find end tag",
    "can't find end of the entity",
    "wrong http url",  # то же семейство: ссылка внутри разметки не распарсилась
)

# Сколько раз повторяем ОДИН кусок, прежде чем сдаться. Было 3 — живой прогон
# 2026-08-06 показал, что этого мало: отчёт по 200 задачам — это 9-13 сообщений
# подряд, и Telegram начинает отвечать «429 retry after 37» примерно на 13-м.
# Три 429 подряд по одному куску обрывали ВЕСЬ отчёт на середине. Пять попыток
# переживают серию флуд-отказов, не превращая доставку в бесконечную.
_SEND_ATTEMPTS = 5

# Профилактическая пауза МЕЖДУ соседними кусками. Это не магическое число и не
# «подождём на всякий случай»: Telegram душит именно ПЛОТНУЮ серию сообщений в
# один чат (лимит порядка 20 сообщений в минуту на группу и ~30 в секунду
# суммарно), и 0.5 с растягивает 13 сообщений на ~6 секунд — этого хватает,
# чтобы серия вообще не упёрлась в 429, вместо того чтобы честно отсиживать по
# 37 секунд ПОСЛЕ отказа. Дешевле предупредить, чем отлежать.
_INTER_CHUNK_PAUSE_S = 0.5

# Потолок СУММАРНОГО ожидания на ОДИН кусок (не на весь отчёт): даже при пяти
# попытках Telegram теоретически может каждый раз просить «retry after 60», и
# без потолка отправка одного сообщения растянулась бы на пять минут, держа
# поток пула занятым. 180 секунд = разумный максимум: типичный retry_after у
# бота — 1-40 с, так что реальные серии сюда не упираются, а патология
# обрывается предсказуемо и честно (ok=False + описание в error).
_MAX_SEND_WAIT_S = 180


class SendResult(NamedTuple):
    """Результат `send_message_chunked`.

    Раньше это был голый кортеж `(ok, message_ids, error)`, и вызывающий не мог
    отличить «доставлено 6 кусков из 6» от «доставлено 2 из 6»: и там, и там
    список id непустой. Именно из-за этого в личку владельца уходило бодрое
    «Подробный отчёт — в группе», когда в группу легли две части из шести.
    `total_chunks` закрывает эту дыру, не требуя пересчитывать нарезку
    заново."""
    ok: bool               # дошли ВСЕ куски
    message_ids: list      # id реально доставленных сообщений (и при ok=False)
    error: str             # текст последней ошибки Telegram ("" при успехе)
    total_chunks: int      # на сколько сообщений текст был разбит


def _is_parse_error(res: dict) -> bool:
    desc = (res.get("description") or "").lower()
    return any(h in desc for h in _PARSE_ERROR_HINTS)


def _retry_after_s(res: dict) -> Optional[int]:
    params = res.get("parameters") or {}
    ra = params.get("retry_after")
    if isinstance(ra, (int, float)) and ra > 0:
        return int(ra)
    if res.get("error_code") == 429:
        return 1  # 429 без параметра — подождём символическую секунду
    desc = (res.get("description") or "").lower()
    if "too many requests" in desc:
        m = re.search(r"retry after (\d+)", desc)
        return int(m.group(1)) if m else 1
    return None


def send_message_chunked(cfg: TgApprovalConfig, chat_id: str, md_text: str,
                         *, reply_markup_on_last: dict | None = None,
                         disable_notification: bool = False) -> SendResult:
    """Доставляет текст любой длины: режет на куски, шлёт по одному, кнопки
    вешает только на ПОСЛЕДНЕЕ сообщение (иначе вебхук gmail-mcp снимет их не
    с того сообщения, а «активной» останется висеть кнопка на обрубке плана).

    Возвращает `SendResult(ok, message_ids, error, total_chunks)`. ok=True —
    только если дошли ВСЕ куски; message_ids содержит id всех реально
    доставленных сообщений даже при ok=False, чтобы вызывающий мог прибрать за
    собой, а `total_chunks` — сколько кусков ПЛАНИРОВАЛОСЬ, чтобы он же мог
    сказать человеку честное «доставлено 2 из 6», а не молча выдать частичную
    доставку за полную.

    Защита от главного silent-fail Telegram: при 400 «can't parse entities»
    (кривая разметка внутри текста) кусок повторяется БЕЗ parse_mode —
    plain-текстом исходного markdown. Лучше некрасивое сообщение, чем
    потерянное. При 429 — сон на retry_after и повтор (до `_SEND_ATTEMPTS`
    попыток и не дольше `_MAX_SEND_WAIT_S` суммарно на один кусок).

    ПРЕДОХРАНИТЕЛЬ (2026-08-06): это единственная функция, которая реально
    шлёт `sendMessage` — поэтому здесь, на ВЕСЬ текст ДО нарезки (а не
    по кускам — строка с инструкцией не должна выжить, даже если чанкинг
    порвёт её пополам), в последний раз вырезаются служебные инструкции для
    модели (`strip_agent_instructions`, см. её докстринг). Вызывающий код
    обязан сам не класть их сюда — это страховка на случай, если не
    обязан."""
    md_text = strip_agent_instructions(md_text)
    chunks = split_for_telegram(md_text, TELEGRAM_TEXT_LIMIT)
    if not chunks:
        return SendResult(False, [], "пустой текст — отправлять нечего", 0)
    if len(chunks) > 1:
        # пересчитываем с местом под «(часть N/M)»
        chunks = split_for_telegram(md_text, TELEGRAM_TEXT_LIMIT - _PART_SUFFIX_RESERVE)
    total = len(chunks)

    message_ids: list[int] = []
    for idx, chunk in enumerate(chunks, start=1):
        is_last = idx == total
        # Пауза ПЕРЕД каждым куском, кроме первого: одиночное сообщение (самый
        # частый случай — короткая сводка, план на пару задач) не тормозится
        # вообще, а серия растягивается ровно настолько, чтобы не влететь в
        # флуд-лимит. Идёт через time.sleep того же модуля, что и сон на 429, —
        # значит тестовый monkeypatch на time.sleep накрывает и её.
        if idx > 1 and _INTER_CHUNK_PAUSE_S > 0:
            time.sleep(_INTER_CHUNK_PAUSE_S)

        sent = None
        use_plain = False
        error = ""
        waited_s = 0.0  # сколько уже отсидели по 429 ИМЕННО на этом куске
        html = md_to_telegram_html(chunk)
        plain = chunk
        if total > 1:
            html += f"\n\n<i>(часть {idx}/{total})</i>"
            plain += f"\n\n(часть {idx}/{total})"
        for attempt in range(1, _SEND_ATTEMPTS + 1):
            body: Dict[str, Any] = {"chat_id": chat_id,
                                    "text": plain if use_plain else html}
            if not use_plain:
                body["parse_mode"] = "HTML"
            if disable_notification:
                body["disable_notification"] = True
            if is_last and reply_markup_on_last is not None:
                body["reply_markup"] = reply_markup_on_last
            res = _tg_call(cfg, "sendMessage", body)
            if res.get("ok"):
                sent = res
                break
            error = res.get("description") or "Telegram sendMessage failed"
            wait_s = _retry_after_s(res)
            if wait_s is not None and attempt < _SEND_ATTEMPTS:
                if waited_s + wait_s > _MAX_SEND_WAIT_S:
                    # Потолок: дальше ждать дороже, чем честно сказать «не
                    # доставлено». Вызывающий увидит частичную доставку по
                    # total_chunks и напишет об этом владельцу словами.
                    error = (f"{error} (суммарное ожидание превысило "
                             f"{_MAX_SEND_WAIT_S}s — бросаю повторы)")
                    logger.warning(f"TG: кусок {idx}/{total} не отправлен: "
                                   f"Telegram просит ещё {wait_s}s, а уже "
                                   f"отсижено {waited_s:.0f}s")
                    break
                logger.warning(f"TG: 429 от Telegram, жду {wait_s}s и повторяю "
                               f"кусок {idx}/{total}")
                time.sleep(wait_s)
                waited_s += wait_s
                continue
            if _is_parse_error(res) and not use_plain and attempt < _SEND_ATTEMPTS:
                logger.warning(f"TG: Telegram не распарсил HTML куска {idx}/{total} "
                               f"({error}) — повторяю без parse_mode, plain-текстом")
                use_plain = True
                continue
            break
        if sent is None:
            return SendResult(False, message_ids, error, total)
        mid = (sent.get("result") or {}).get("message_id")
        if mid is not None:
            message_ids.append(mid)
    return SendResult(True, message_ids, "", total)


def delete_message(cfg: TgApprovalConfig, chat_id: str, message_id: int) -> bool:
    """Best-effort удаление. Сообщение, которое человек уже стёр руками, или
    старше 48 часов (Bot API их удалять не даёт) — это НЕ ошибка процесса:
    возвращаем False и пишем в debug, наружу ничего не бросаем."""
    if message_id is None:
        return False
    res = _tg_call(cfg, "deleteMessage", {"chat_id": chat_id, "message_id": message_id})
    if not res.get("ok"):
        logger.debug(f"TG: deleteMessage({chat_id}, {message_id}) не удалось: "
                     f"{res.get('description')}")
        return False
    return True


# ────────────── Исчезновение сообщений гейта (2026-08-10, TZ §5) ────────────
#
# Владелец: сообщения гейта, которые уже «ушли в отчёт» (результат доступен
# через operation_report/журнал), должны самоудаляться из чата примерно через
# 1 минуту после того, как их роль исполнена — иначе чат бесконечно копит
# историю подтверждений.
#
# ЧТО ИМЕННО И КОГДА (таймер стартует от МОМЕНТА, когда сообщение исполнило
# свою роль, НЕ от момента отправки — план может провисеть в ожидании
# нажатия сколько угодно):
#   • план/кнопки в личке владельца — таймер стартует, когда пришёл ОТВЕТ
#     (нажатие кнопки — `_handle_callback_query`, либо разбор текстового
#     «да»/отказа — `_require_consent` в consent.py), не раньше;
#   • отчёт в группе-архиве (`post_report_to_group`) — таймер стартует СРАЗУ
#     при отправке (он и так уже финальное сообщение).
#
# МЕХАНИЗМ. Небольшой список в памяти процесса — TZ явно говорит, что
# долговечность через рестарт НЕ нужна (потеря пары сообщений на «не
# удалилось секунда в секунду» не критична, в отличие от манифестов/решений).
# Существующий периодический поллер (`_tg_auto_execute_tick`,
# tg_auto_execute.py) на каждом проходе дополнительно зовёт
# `sweep_scheduled_deletes` — она сама решает, что просрочено, и убирает это
# из списка НЕЗАВИСИМО от того, удалилось сообщение реально или нет (сбой
# удаления — сообщение уже стёрто вручную, чат недоступен — НЕ считается
# сбоем поллера, тихо пропускается, см. `delete_message` выше, которая и так
# никогда не бросает).
_GATE_MESSAGE_DELETE_DELAY_S = float(
    os.environ.get("TG_GATE_MESSAGE_DELETE_DELAY_S", "60"))

# (chat_id, message_id, delete_after_epoch_s). Список, а не множество/словарь:
# один и тот же message_id теоретически может встретиться дважды (повторная
# уборка не страшна — второй deleteMessage на уже стёртое сообщение просто
# вернёт False и будет тихо пропущен).
_SCHEDULED_DELETES: List[Tuple[str, int, float]] = []


def schedule_message_delete(chat_id: Optional[str], message_id: Optional[int],
                            delay_s: Optional[float] = None) -> None:
    """Ставит сообщение в очередь на удаление примерно через `delay_s`
    секунд (по умолчанию `_GATE_MESSAGE_DELETE_DELAY_S` = 60, настраиваемо
    через `TG_GATE_MESSAGE_DELETE_DELAY_S`). Зовётся ИМЕННО в момент, когда
    сообщение исполнило свою роль (см. блок-комментарий выше) — не раньше,
    не позже. `chat_id`/`message_id` отсутствуют → тихий no-op (нечего
    удалять/некуда — та же дисциплина, что у `delete_message`)."""
    if not chat_id or message_id is None:
        return
    delay = _GATE_MESSAGE_DELETE_DELAY_S if delay_s is None else delay_s
    _SCHEDULED_DELETES.append((str(chat_id), int(message_id), time.time() + delay))


def sweep_scheduled_deletes(cfg: TgApprovalConfig) -> int:
    """Зовётся КАЖДЫЙ проход поллера (`_tg_auto_execute_tick`): удаляет всё,
    что просрочило свой таймер, снимает это из очереди НЕЗАВИСИМО от исхода
    (удалилось / не удалилось — не наша забота, `delete_message` best-effort
    сама по себе). Возвращает число реально удалённых сообщений (для
    логов/тестов, не для решений).

    Сбой ОДНОГО удаления (исключение из `_tg_call`, сеть, что угодно) не
    должен прерывать уборку остальных и тем более не должен ронять
    вызывающий поллер — отдельный `try/except` НА КАЖДОЕ сообщение, хотя
    `delete_message` и так не бросает наружу при обычных ошибках Telegram
    (не-2xx/`ok:false`); страховка на будущее, если это когда-нибудь
    изменится."""
    global _SCHEDULED_DELETES
    if not _SCHEDULED_DELETES:
        return 0
    now = time.time()
    due = [e for e in _SCHEDULED_DELETES if e[2] <= now]
    if not due:
        return 0
    _SCHEDULED_DELETES = [e for e in _SCHEDULED_DELETES if e[2] > now]
    deleted = 0
    for chat_id, message_id, _ in due:
        try:
            if delete_message(cfg, chat_id, message_id):
                deleted += 1
        except Exception as e:  # noqa: BLE001 — уборка НИКОГДА не роняет поллер
            logger.debug(f"TG: sweep_scheduled_deletes({chat_id}, {message_id}) "
                         f"упало: {e}")
    return deleted


# ───────────────────────── Postgres (общий с 5 TS-серверами) ─────────────────

_pg_pool = None

# Postgres живёт за ПУБЛИЧНЫМ прокси Railway: без явных таймаутов подвисшее
# соединение/запрос ждёт дефолтный TCP-таймаут ОС — это минуты, ровно тот
# масштаб задержки, который QA видел на живом проде. connect_timeout режет
# зависание на установлении соединения, statement_timeout — на самом запросе
# (значение с запасом на CREATE TABLE в _ensure_schema).
_PG_CONNECT_TIMEOUT_S = int(os.environ.get("CONSENT_PG_CONNECT_TIMEOUT_S", "10"))
_PG_STATEMENT_TIMEOUT_MS = int(os.environ.get("CONSENT_PG_STATEMENT_TIMEOUT_MS", "15000"))


def init_store(database_url: str) -> None:
    """Ленивая инициализация — вызывается один раз при старте, если
    TG_APPROVAL_ENABLED=true и задан CONSENT_DATABASE_URL. psycopg2 — тот же
    выбор, что и остальной синхронный стиль этого сервера (requests вместо
    httpx, никакого asyncio Postgres-драйвера не требовалось до сих пор).

    Пул — `ThreadedConnectionPool`, а не `SimpleConnectionPool` (#91).
    Simple-версия по собственной документации psycopg2 не рассчитана на
    использование из разных потоков: её `getconn`/`putconn` не защищены
    блокировкой. А обращения сюда идут через `_run_blocking`
    (`asyncio.to_thread`), то есть ровно из разных потоков — до сих пор это не
    выстреливало лишь потому, что поллер ходил в базу раз в 10 секунд."""
    global _pg_pool
    import psycopg2.pool

    # `sslmode` больше НЕ прибит гвоздями (#91). Прежний безусловный
    # `sslmode="require"` перебивал то, что указано в самой строке подключения,
    # и делал невозможным подключение к Postgres без TLS — в частности к
    # локальному, на котором гоняются интеграционные тесты долговечных планов.
    # В проде ничего не меняется: DSN от Railway идёт через публичный прокси, и
    # если режим в нём не задан явно, мы по-прежнему требуем TLS.
    extra = {} if "sslmode=" in database_url else {"sslmode": "require"}
    _pg_pool = psycopg2.pool.ThreadedConnectionPool(
        1, 5, dsn=database_url,
        connect_timeout=_PG_CONNECT_TIMEOUT_S,
        options=f"-c statement_timeout={_PG_STATEMENT_TIMEOUT_MS}",
        **extra,
    )
    _ensure_schema()


def store_ready() -> bool:
    return _pg_pool is not None


def _ensure_schema() -> None:
    """DDL таблицы `tg_approvals` ОБЩИЙ с gmail-mcp (`src/store.ts`, там он
    помечен FROZEN) — её создают и читают шесть серверов сразу, поэтому базовый
    CREATE TABLE обязан оставаться байт-в-байт совместимым и меняться только
    согласованно.

    Новые колонки добавляются отдельными идемпотентными `ADD COLUMN IF NOT
    EXISTS` и это БЕЗОПАСНО для TS-стороны: gmail-mcp читает строки через
    node-pg ПО ИМЕНАМ колонок (`row.manifest_id`, `row.chat_id`, …), нигде не
    полагаясь на порядок/количество полей, и во всех своих INSERT перечисляет
    колонки явно. Новая колонка для него просто не существует.

    Что добавляем (нужно транспорту отчётов, 2026-08-06):
      extra_message_ids  — id ДОПОЛНИТЕЛЬНЫХ сообщений плана, когда он не влез
                           в одно; кнопки всегда на последнем, его id лежит в
                           штатном message_id (иначе вебхук gmail-mcp снял бы
                           разметку не с того сообщения);
      report_chat_id     — куда ушёл отчёт (группа-архив или личка);
      report_message_ids — какими сообщениями ушёл, чтобы reap_expired() мог
                           убрать за собой весь след манифеста;
      lost_notified_at   — когда владельцу СКАЗАЛИ, что подтверждение принято,
                           а исполнять оказалось нечего (план жил только в
                           памяти процесса и не пережил перезапуск). Это
                           «уже сообщили», а не «просрочено»: без такой
                           отметки поллер писал бы одно и то же сообщение
                           каждые 10 секунд, пока строка не умрёт по TTL, —
                           и она обязана быть В БАЗЕ, а не в памяти, потому
                           что память как раз и потеряли."""
    conn = _pg_pool.getconn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS tg_approvals (
                  manifest_id TEXT PRIMARY KEY,
                  server      TEXT NOT NULL,
                  chat_id     TEXT NOT NULL,
                  message_id  BIGINT,
                  status      TEXT NOT NULL DEFAULT 'PENDING',
                  created_at  BIGINT NOT NULL,
                  expires_at  BIGINT NOT NULL,
                  decided_at  BIGINT
                )
                """
            )
            cur.execute(
                "ALTER TABLE tg_approvals ADD COLUMN IF NOT EXISTS extra_message_ids  BIGINT[]"
            )
            cur.execute(
                "ALTER TABLE tg_approvals ADD COLUMN IF NOT EXISTS report_chat_id     TEXT"
            )
            cur.execute(
                "ALTER TABLE tg_approvals ADD COLUMN IF NOT EXISTS report_message_ids BIGINT[]"
            )
            cur.execute(
                "ALTER TABLE tg_approvals ADD COLUMN IF NOT EXISTS lost_notified_at   BIGINT"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS tg_approvals_cleanup_idx "
                "ON tg_approvals (server, status, expires_at)"
            )
    finally:
        _pg_pool.putconn(conn)


def _now_ms() -> int:
    return int(time.time() * 1000)


def create_tg_approval(manifest_id: str, chat_id: str, message_id: Optional[int],
                        expires_at_ms: int,
                        extra_message_ids: Optional[list[int]] = None) -> None:
    """`message_id` — сообщение С КНОПКАМИ (последнее), `extra_message_ids` —
    предшествующие куски того же плана. Разделение не косметическое: вебхук
    gmail-mcp правит именно `message_id`, а reaper обязан прибрать все.

    `message_id=None` — штатный случай, а не заглушка: строка создаётся ДО
    отправки сообщений (см. `notify_plan`), чтобы нажатие кнопки не могло
    попасть в зазор «сообщение уже висит, строки ещё нет». Схема это
    допускает — колонка BIGINT без NOT NULL."""
    conn = _pg_pool.getconn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tg_approvals (manifest_id, server, chat_id, message_id, "
                "status, created_at, expires_at, extra_message_ids) "
                "VALUES (%s, %s, %s, %s, 'PENDING', %s, %s, %s)",
                (manifest_id, "ticktick", chat_id, message_id, _now_ms(), expires_at_ms,
                 list(extra_message_ids or [])),
            )
    finally:
        _pg_pool.putconn(conn)


def attach_plan_messages(manifest_id: str, message_id: Optional[int],
                         extra_message_ids: Optional[list[int]] = None) -> bool:
    """Дописывает в УЖЕ созданную строку id доставленных сообщений плана.

    Второй шаг схемы «INSERT → отправка → UPDATE» из `notify_plan`: строка
    существует с самого начала (иначе нажатие кнопки в зазоре пропало бы
    впустую), а реальные message_id известны только после отправки.

    Возвращает True, если запись прошла. False (и только лог) означает
    деградацию, а НЕ провал гейта: строка на месте и статус ей проставит
    вебхук, кнопки он снимет по message_id из самого callback_query — просто
    сводку в личку потом будет некуда вписать."""
    if not store_ready():
        return False
    conn = _pg_pool.getconn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE tg_approvals SET message_id = %s, extra_message_ids = %s "
                "WHERE manifest_id = %s AND server = 'ticktick'",
                (message_id, list(extra_message_ids or []), manifest_id),
            )
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"TG: не смог записать message_id для {manifest_id}: {e}")
        return False
    finally:
        _pg_pool.putconn(conn)


def delete_tg_approval(manifest_id: str) -> None:
    """Убирает строку подтверждения. Нужна ровно в одном месте — откат
    `notify_plan`, когда строку уже вставили, а сообщения в Telegram уйти не
    смогли: без этого в таблице осталась бы сирота, живущая до TTL и
    подтверждающая план, которого владелец никогда не видел."""
    if not store_ready():
        return
    conn = _pg_pool.getconn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM tg_approvals WHERE manifest_id = %s AND server = 'ticktick'",
                (manifest_id,),
            )
    except Exception as e:  # noqa: BLE001 — откат best-effort: строка всё равно
        # умрёт по TTL, а бросать отсюда нельзя (мы уже в ветке ошибки)
        logger.warning(f"TG: не смог удалить строку подтверждения {manifest_id}: {e}")
    finally:
        _pg_pool.putconn(conn)


def record_report_messages(manifest_id: str, chat_id: str, message_ids: list[int]) -> None:
    """Запоминает, куда и какими сообщениями ушёл отчёт. Фильтр
    `server='ticktick'` обязателен — чужие строки этот сервер не правит
    (manifest_id глобально уникален, но дисциплина одна на все шесть серверов).
    Best-effort: отчёт УЖЕ доставлен, и неудачная запись в БД не должна
    ронять вызывающего — она стоит ровно одного: reaper потом не найдёт эти
    сообщения и они переживут TTL."""
    if not store_ready():
        return
    conn = _pg_pool.getconn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE tg_approvals SET report_chat_id = %s, report_message_ids = %s "
                "WHERE manifest_id = %s AND server = 'ticktick'",
                (chat_id, list(message_ids or []), manifest_id),
            )
    except Exception as e:
        logger.warning(f"TG: не смог записать report_message_ids для {manifest_id}: {e}")
    finally:
        _pg_pool.putconn(conn)


def get_tg_approval(manifest_id: str) -> Optional[dict]:
    """Читает строку, ОБЯЗАТЕЛЬНО фильтруя server='ticktick' — сервер не
    должен читать чужие approval-строки (та же дисциплина, что у TS-серверов'
    getTgApproval; см. tg_approval.ts's комментарий про cross-server read).
    chat_id/message_id (добавлены 2026-08-05) нужны авто-исполнению по
    кнопке, чтобы знать, КУДА писать итог (report_auto_execution_result).
    extra_message_ids (добавлен 2026-08-06) — предыдущие куски длинного плана:
    после исполнения их надо УДАЛИТЬ из лички, иначе они остаются там навсегда
    (наш reaper строки APPROVED не трогает — это архив решения, а уборщик
    gmail-mcp знает только про message_id).

    Гард `store_ready()` (2026-08-19): с 6d36dbe эта функция зовётся прямо из
    вебхука кнопки (`_handle_callback_query` — таймеры удаления кусков
    длинного плана), то есть выполняется и в конфигурации, где Postgres не
    инициализирован. Без гарда это AttributeError на `_pg_pool.getconn()` —
    нажатие кнопки роняло весь `handle_webhook` (owner не получал даже
    `answerCallbackQuery`, «часики» крутились до таймаута). Нет store — нет
    строки: честный None, как у `consume_tg_decision`."""
    if not store_ready():
        return None
    conn = _pg_pool.getconn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT status, expires_at, chat_id, message_id, extra_message_ids "
                "FROM tg_approvals WHERE manifest_id = %s AND server = 'ticktick'",
                (manifest_id,),
            )
            row = cur.fetchone()
    finally:
        _pg_pool.putconn(conn)
    if not row:
        return None
    return {"status": row[0], "expires_at": row[1], "chat_id": row[2],
            "message_id": row[3],
            # У старых строк колонки может не быть в выборке (или быть NULL) —
            # приводим к списку здесь, чтобы вызывающему не приходилось
            # проверять None на каждом шаге.
            "extra_message_ids": list(row[4] or []) if len(row) > 4 else []}


def get_tg_approvals(manifest_ids: Iterable[str],
                     lost_scan_since_ms: Optional[int] = None) -> Dict[str, dict]:
    """ПАКЕТНОЕ чтение тех же строк, что и get_tg_approval, — ОДИН запрос в
    Postgres на весь список вместо одной поездки на каждый id.

    Зачем (2026-08-06): фоновый поллер авто-исполнения раньше на КАЖДЫЙ живой
    манифест делал check_approval() (→ 1 поездка) и, если одобрен, ещё
    get_tg_approval() (→ 2-я поездка). Пока кнопка была у 2 тулов, живых
    планов было единицы; после расширения на 22 тула их десятки, а база
    ходит через публичный прокси Railway (десятки-сотни мс на поездку) — один
    проход переставал укладываться в 10-секундный интервал, и подтверждённая
    кнопкой операция ждала исполнения минутами.

    Возвращает {manifest_id: {"status","expires_at","chat_id","message_id",
    "extra_message_ids"}} ТОЛЬКО для найденных строк; отсутствие ключа =
    «строки нет» (это то же самое, что None у одиночной версии, т.е. статус
    "none").
    Фильтр server='ticktick' — ровно тот же, что у get_tg_approval: сервер не
    читает чужие approval-строки.

    Набор полей ОБЯЗАН совпадать с одиночным get_tg_approval: с 2026-08-06
    поллер авто-исполнения читает строки только отсюда, и потерянный здесь
    `extra_message_ids` тихо сломал бы уборку предыдущих кусков длинного плана
    в личке (`_cleanup_plan_leftovers` в server.py) — они оставались бы в чате
    навсегда.

    `lost_scan_since_ms` (2026-08-06) — ПОИСК ПОТЕРЯННЫХ ПЛАНОВ в ТОМ ЖЕ
    запросе. Манифесты живут только в памяти процесса, а решение по кнопке —
    в этой таблице; если сервис перезапустился между отправкой плана и
    нажатием, строка станет APPROVED, а исполнять будет нечего, и поиск
    кандидатов «от памяти» (`manifest_id = ANY(...)`) такую строку не увидит
    НИКОГДА — по определению, её id в память уже не входит. Поэтому при
    заданном параметре к тому же WHERE добавляется вторая ветка: строки
    server='ticktick' со статусом APPROVED, про потерю которых ещё не
    сообщали (`lost_notified_at IS NULL`) и которые не слишком стары
    (`expires_at > lost_scan_since_ms`). Именно вторая ветка ТОГО ЖЕ запроса,
    а не отдельная поездка в базу: сохранить «одно обращение к Postgres на
    проход» тут так же важно, как и раньше (см. абзац выше).

    Отбор «кто из них правда потерян» здесь СОЗНАТЕЛЬНО не делается — он
    зависит от состояния памяти этого процесса (живые манифесты, надгробия) и
    живёт чистой функцией в server.py, где это состояние видно и тестируемо
    без базы."""
    ids = [str(i) for i in manifest_ids]
    if not store_ready():
        return {}
    if not ids and lost_scan_since_ms is None:
        return {}
    where = "server = 'ticktick' AND (manifest_id = ANY(%s)"
    params: List[Any] = [ids]
    if lost_scan_since_ms is None:
        where += ")"
    else:
        where += (" OR (status = 'APPROVED' AND lost_notified_at IS NULL "
                  "AND expires_at > %s))")
        params.append(int(lost_scan_since_ms))
    conn = _pg_pool.getconn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT manifest_id, status, expires_at, chat_id, message_id, "
                "extra_message_ids, created_at, decided_at, report_message_ids, "
                "lost_notified_at "
                f"FROM tg_approvals WHERE {where}",
                tuple(params),
            )
            rows: List[tuple] = cur.fetchall()
    finally:
        _pg_pool.putconn(conn)
    return {r[0]: {"status": r[1], "expires_at": r[2], "chat_id": r[3],
                   "message_id": r[4],
                   # Та же нормализация, что в get_tg_approval: NULL/старая
                   # строка без колонки → пустой список, а не None.
                   "extra_message_ids": list(r[5] or []) if len(r) > 5 else [],
                   # Поля ниже нужны ТОЛЬКО поиску потерянных планов; на пути
                   # обычного авто-исполнения их никто не читает, поэтому их
                   # появление ничего там не меняет.
                   "created_at": r[6] if len(r) > 6 else None,
                   "decided_at": r[7] if len(r) > 7 else None,
                   "report_message_ids": list(r[8] or []) if len(r) > 8 else [],
                   "lost_notified_at": r[9] if len(r) > 9 else None}
            for r in rows}


def claim_lost_manifests(manifest_ids: Iterable[str]) -> List[str]:
    """Атомарно занимает право СКАЗАТЬ владельцу «подтверждение принято, но
    план не сохранился» — по каждой строке ровно один раз за всю её жизнь.

    Почему именно `UPDATE ... WHERE lost_notified_at IS NULL ... RETURNING`, а
    не «проверил → отправил → пометил»: поллер тикает каждые 10 секунд, а при
    выкатке нового билда какое-то время работают ДВА процесса сразу. Проверка
    и пометка в разных запросах дали бы либо шквал одинаковых сообщений в
    личку, либо два сообщения об одной потере. Здесь же строку получает тот,
    чей UPDATE выиграл; остальным она просто не возвращается.

    Пометка ставится ДО отправки сообщения намеренно: не доставленное
    уведомление хуже, чем повторяющееся вечно каждые 10 секунд (второе Максим
    запретил прямо). Провал отправки виден в логе как ERROR.

    Возвращает id, которые достались НАМ (пустой список = сообщать нечего)."""
    ids = [str(i) for i in manifest_ids]
    if not ids or not store_ready():
        return []
    conn = _pg_pool.getconn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE tg_approvals SET lost_notified_at = %s "
                "WHERE manifest_id = ANY(%s) AND server = 'ticktick' "
                "AND status = 'APPROVED' AND lost_notified_at IS NULL "
                "RETURNING manifest_id",
                (_now_ms(), ids),
            )
            rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001 — не смогли занять = молчим, не спамим
        logger.warning(f"TG: не смог пометить потерянные планы {ids}: {e}")
        return []
    finally:
        _pg_pool.putconn(conn)
    return [r[0] for r in rows]


# ───────────────────── own_bot: собственный вебхук (2026-08-06) ─────────────
#
# Всё в этом блоке существует ТОЛЬКО ради `TG_BOT_TOKEN_OVERRIDE` (own_bot);
# см. блок в шапке файла. server.py вызывает эти функции из своего роута
# `/tg/webhook` и из `main()` — сам HTTP-роут (проверка секрета из заголовка,
# 404/401-гейт) живёт в server.py, потому что ему нужен сырой `Request`
# (заголовки), которого у этого модуля нет и не должно быть (та же
# дисциплина, что у TS-референса: секрет из заголовка читает http.ts,
# остальное — tg_approval.ts).

def secret_token_matches(provided: str, expected: str) -> bool:
    """Постоянное по времени сравнение `X-Telegram-Bot-Api-Secret-Token` с
    `TG_APPROVAL_WEBHOOK_SECRET`. Порт `secretTokenMatches` из TS-референса
    (gmail/drive-mcp's tg_approval.ts), но через SHA-256-дайджесты, а не
    `hmac.compare_digest` на сырых строках напрямую — та же причина, что у
    `server.py`'s `_automation_key_matches`: `compare_digest` на ДВУХ `str`
    требует ASCII с обеих сторон, иначе CPython бросает `TypeError` вместо
    честного «не совпало». Заголовок — вход снаружи (Telegram его не
    гарантирует, а тем более не гарантирует подделка), и не-ASCII байт в нём
    не должен ронять вебхук исключением вместо ответа 401."""
    if not (expected and provided):
        return False
    return hmac.compare_digest(
        hashlib.sha256(provided.encode("utf-8")).digest(),
        hashlib.sha256(expected.encode("utf-8")).digest(),
    )


def consume_tg_decision(manifest_id: str, status: str) -> Optional[Dict[str, Any]]:
    """Атомарный одноразовый захват РЕШЕНИЯ по кнопке — SERVER-SCOPED аналог
    того, что для общего бота делает `consumeTgDecisionAnyServer` на стороне
    gmail-mcp. Вызывается ТОЛЬКО из own_bot-вебхука (`handle_webhook` ниже):
    раз этот вебхук физически получает апдейты ИСКЛЮЧИТЕЛЬНО своего бота
    (Telegram не пришлёт сюда чужой callback_query — токены разные), строка
    ЗАВЕДОМО принадлежит `server='ticktick'`, и фильтр по нему — не единственная
    защита (ею остаётся глобально уникальный `manifest_id`, PRIMARY KEY), а
    дополнительный пояс, снимающий даже теоретическую путаницу.

    `WHERE status = 'PENDING'` в ТОМ ЖЕ операторе — то же самое anti-replay,
    что у `consumeTgDecisionAnyServer`: повторная доставка одного и того же
    Telegram-апдейта (Telegram ретраит недоставленные вебхуки) — не более чем
    no-op на второй раз, а не двойная запись решения.

    Возвращает {"chat_id", "message_id"} строки при успехе (нужно вызывающему,
    чтобы снять кнопки), None — если строки не было / она не 'ticktick' / уже
    не 'PENDING' (кто-то успел раньше — гонка с двойным тапом или ретраем)."""
    if not store_ready():
        return None
    conn = _pg_pool.getconn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE tg_approvals SET status = %s, decided_at = %s "
                "WHERE manifest_id = %s AND server = 'ticktick' AND status = 'PENDING' "
                "RETURNING chat_id, message_id",
                (status, _now_ms(), manifest_id),
            )
            row = cur.fetchone()
    except Exception as e:  # noqa: BLE001 — сбой БД не должен ронять вебхук
        logger.warning(f"TG own_bot: consume_tg_decision({manifest_id}) упал: {e}")
        return None
    finally:
        _pg_pool.putconn(conn)
    if not row:
        return None
    return {"chat_id": row[0], "message_id": row[1]}


_CALLBACK_DATA_RE = re.compile(r"^([ar]):(.+)$")
# Кнопки меню `/automation_key` (TZ_temp_automation_key.md §3.2/3.3,
# 2026-08-10) — отдельный, НЕ пересекающийся префикс: `_CALLBACK_DATA_RE`
# матчит РОВНО один символ группы 1 (`a`/`r`), "ak" под неё не подходит, так
# что два набора кнопок не могут случайно перепутаться в разборе.
# window_id — `secrets.token_hex(6)` (automation_key.py) = 12 hex-символов,
# отсюда `[0-9a-fA-F]+` у "revoke:<id>" (не жадный "любой символ" — id не
# несёт ничего, кроме hex, лишнее в callback_data просто не смэтчится и
# кнопка тихо не сработает, а не откроет что-то по чужому id).
_AK_CALLBACK_RE = re.compile(r"^ak:(new|list|offall|revoke:[0-9a-fA-F]+)$")
_AUTOMATION_KEY_COMMAND = "/automation_key"


def handle_webhook(cfg: TgApprovalConfig, update: Dict[str, Any]) -> None:
    """Обрабатывает ОДИН апдейт Telegram, уже прошедший проверку секрета в
    server.py's роуте (тому нужен сырой заголовок запроса, которого здесь
    нет — та же граница ответственности, что в TS-референсе между http.ts и
    tg_approval.ts). Порт `handleWebhook`, ветка `cfg.ownBot=true`.

    Диспетчер по типу апдейта (2026-08-10, TZ §3.2 расширила подписку с
    `["callback_query"]` на `["callback_query", "message"]` — см.
    `register_webhook`): callback_query → `_handle_callback_query` (кнопки
    подтверждения ✅/🛑 И кнопки меню `/automation_key`, см. ниже),
    message → `_handle_automation_key_message` (сама команда `/automation_key`
    — единственное сообщение, на которое этот вебхук вообще реагирует).
    Любой другой/будущий тип апдейта — тихо игнорируется, вебхук не роняем."""
    cq = (update or {}).get("callback_query")
    if cq:
        _handle_callback_query(cfg, cq)
        return
    msg = (update or {}).get("message")
    if msg:
        _handle_automation_key_message(cfg, msg)


def _handle_callback_query(cfg: TgApprovalConfig, cq: Dict[str, Any]) -> None:
    """Тело прежней `handle_webhook` (до 2026-08-10) — БЕЗ ИЗМЕНЕНИЙ в своей
    части ✅/🛑, плюс НОВАЯ ветка для кнопок меню `/automation_key`:

    1. owner-only — `callback_query.from.id` обязан совпадать с
       `cfg.owner_chat_id`; иначе тихо игнорируется (Telegram всё равно
       получит 200 от вызывающего роута — это не тот случай, чтобы
       предупреждать нажавшего, кем бы он ни был). ОБЩАЯ проверка для ОБОИХ
       наборов кнопок — TZ явно требует ту же owner-only дисциплину для
       `/automation_key`, что и для approve/reject, никаких новых путей
       авторизации не изобретаем;
    2. `ak:show|off|status` → `_handle_automation_key_callback` (новое);
    3. иначе — прежний разбор `a:`/`r:`: callback_data — это АДРЕС, не факт
       доверия (решение читается назад из БД по manifest_id), anti-replay —
       атомарный `consume_tg_decision`, кнопки снимаются ПОСЛЕ решения
       best-effort, `answerCallbackQuery` зовётся всегда, когда есть
       `callback_query.id` — иначе «часики» крутятся до таймаута.

    НЕ исполняет мутацию сама — см. блок-комментарий в шапке файла про то,
    что исполнение остаётся за поллером и его атомарным `manifest_store.
    claim()`; approve/reject-ветка здесь только переводит статус."""
    from_id = str((cq.get("from") or {}).get("id") or "")
    if not from_id or from_id != str(cfg.owner_chat_id):
        return
    data = cq.get("data") or ""

    ak_m = _AK_CALLBACK_RE.match(data)
    if ak_m:
        _handle_automation_key_callback(cfg, cq, ak_m.group(1))
        return

    m = _CALLBACK_DATA_RE.match(data)
    if not m:
        return
    decision = "APPROVED" if m.group(1) == "a" else "REJECTED"
    manifest_id = m.group(2)

    consumed = consume_tg_decision(manifest_id, decision)

    if consumed:
        # Снятие кнопок — по данным из САМОГО callback_query в приоритете
        # (совпадает с TS-версией): это те chat_id/message_id, где кнопка
        # реально была нажата, а данные из БД — запасной вариант на случай,
        # если Telegram их почему-то не прислал.
        chat_id = ((cq.get("message") or {}).get("chat") or {}).get("id") \
            or consumed.get("chat_id") or cfg.owner_chat_id
        message_id = (cq.get("message") or {}).get("message_id")
        if message_id is None:
            message_id = consumed.get("message_id")
        if message_id is not None:
            if decision == "REJECTED":
                # 2026-08-19: после «🔴 Отклонить» в чате обязан остаться
                # честный терминальный след, а не только эфемерный тост
                # answerCallbackQuery (исчезает через секунды) и снятые
                # кнопки на неизменённом тексте плана — иначе человек,
                # закрывший Telegram, не отличит «отклонил» от «подтвердил и
                # закрыл». Правка текста тем же транспортом, что у
                # approve-итога (`summarize_in_owner_chat`): один
                # editMessageText меняет текст И снимает кнопки. Правка не
                # удалась (сообщение старше 48ч/стёрто руками) — резервно
                # хотя бы снимаем кнопки, как раньше.
                plan_text = (cq.get("message") or {}).get("text") or ""
                if not mark_rejected_in_owner_chat(cfg, str(chat_id),
                                                   message_id, plan_text):
                    clear_inline_keyboard(cfg, str(chat_id), message_id)
            else:
                clear_inline_keyboard(cfg, str(chat_id), message_id)
        # TZ §5: ОТВЕТ (нажатие кнопки) только что пришёл — план исполнил
        # свою роль. Таймер на удаление стартует ИМЕННО здесь, не от момента
        # отправки плана (который мог провисеть сколько угодно). Куски
        # длинного плана (extra_message_ids) уходят тем же таймером.
        schedule_message_delete(chat_id, message_id)
        approval_row = get_tg_approval(manifest_id)
        for extra_mid in (approval_row or {}).get("extra_message_ids") or []:
            schedule_message_delete(chat_id, extra_mid)

    cq_id = cq.get("id")
    if cq_id:
        answer_text = "Уже обработано" if not consumed else (
            "Подтверждено" if decision == "APPROVED" else "Отклонено")
        _tg_call(cfg, "answerCallbackQuery",
                {"callback_query_id": cq_id, "text": answer_text})


# ───────────────────── /automation_key: команда + меню (2026-08-10, ─────────
# ───────────────────── переработано под НЕСКОЛЬКО окон 2026-08-11) ─────────
# TZ_temp_automation_key.md §3.2/3.3 + TZ_multi_automation_windows.md
# «Команды в Telegram». Owner-only на ОБОИХ шагах (команда И каждая кнопка) —
# та же проверка `from.id == cfg.owner_chat_id`, что уже стоит на
# approve/reject, никакого нового пути авторизации. Сама генерация/отзыв/
# список — в `automation_key.py` (этот файл только вызывает её публичные
# функции и знает, КАК говорить с Telegram; деталей схемы БД временных окон
# здесь нет — см. докстринг того модуля).
#
# Раньше `/automation_key` без аргумента открывало МЕНЮ («Показать ключ» /
# «Выключить» / «Статус») — при одном окне на сервер этого хватало. Теперь
# окон может быть несколько одновременно, и ТЗ прямо требует: команда БЕЗ
# аргумента (и кнопка «Новый ключ») сразу генерируют ОЧЕРЕДНОЕ окно, не
# трогая уже существующие — «Показать» вместо «сгенерировать» больше не
# нужно различать, различать нужно «какое из окон» (список/отзыв). Четыре
# точки входа — `_ak_do_generate`/`_ak_do_list`/`_ak_do_revoke`/
# `_ak_do_offall` — общие для текстовой команды И кнопок, чтобы поведение не
# разъезжалось между двумя путями.
_AUTOMATION_KEY_MENU_MARKUP = {"inline_keyboard": [
    [{"text": "Новый ключ", "callback_data": "ak:new"}],
    [{"text": "Список", "callback_data": "ak:list"}],
    [{"text": "Выключить всё", "callback_data": "ak:offall"}],
]}

# Сообщение с СЫРЫМ токеном стоит открытым в чате короче, чем обычные
# сообщения гейта (`_GATE_MESSAGE_DELETE_DELAY_S` = 60): здесь на экране
# лежит сам секрет, не план/отчёт, поэтому TZ_multi_automation_windows.md
# требует именно 10 секунд — передаётся В `schedule_message_delete` явным
# `delay_s`, дефолт остальных сообщений гейта (60с) этим не меняется.
_AK_TOKEN_MESSAGE_DELETE_DELAY_S = 10.0


def _format_windows_list(windows: List[Dict[str, Any]]) -> str:
    """Текст кнопки/команды «Список» (TZ_multi_automation_windows.md, тест
    4) — id, когда создано, сколько осталось у КАЖДОГО активного окна. Без
    токенов и без хэшей — те не восстановимы принципиально (см. `list_
    windows`'s докстринг)."""
    if not windows:
        return "🔑 Активных временных окон нет."
    lines = [f"🔑 <b>Активные временные окна</b> ({len(windows)}):"]
    for w in windows:
        hours_left = w["remaining_s"] / 3600
        label = f" — {w['label']}" if w.get("label") else ""
        lines.append(
            f"• <code>{w['window_id']}</code>{label}: создано "
            f"{automation_key.format_ms(w['created_at'])}, ещё ~{hours_left:.1f} ч "
            f"(до {automation_key.format_ms(w['expires_at'])})"
        )
    return "\n".join(lines)


def _ak_list_markup(windows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Меню + ОТДЕЛЬНАЯ кнопка «Отозвать» на КАЖДОЙ строке списка (TZ:
    «кнопка на конкретной строке списка» гасит РОВНО это окно, не все)."""
    rows = [[{"text": f"Отозвать {w['window_id']}",
             "callback_data": f"ak:revoke:{w['window_id']}"}] for w in windows]
    rows += _AUTOMATION_KEY_MENU_MARKUP["inline_keyboard"]
    return {"inline_keyboard": rows}


def _ak_send_menu(cfg: TgApprovalConfig, chat_id: str) -> None:
    _tg_call(cfg, "sendMessage", {
        "chat_id": chat_id,
        "text": "🔑 <b>Ключ автоматики</b>\nВыбери действие:",
        "parse_mode": "HTML",
        "reply_markup": _AUTOMATION_KEY_MENU_MARKUP,
    })


def _ak_do_generate(cfg: TgApprovalConfig, chat_id: str) -> str:
    """Генерирует ОЧЕРЕДНОЕ окно (не трогая уже существующие активные —
    `automation_key.generate_window` теперь INSERT, не UPSERT), присылает
    сырой токен ОТДЕЛЬНЫМ сообщением БЕЗ кнопок (кнопки на сообщении,
    которое исчезнет через 10 секунд, были бы бесполезны) и ставит его на
    самоудаление через `_AK_TOKEN_MESSAGE_DELETE_DELAY_S`. Меню для
    дальнейших действий (Список/Выключить всё) уходит СЛЕДОМ отдельным,
    НЕ самоудаляемым сообщением. Возвращает текст для `answerCallbackQuery`."""
    token = automation_key.generate_window(chat_id)
    if not token:
        text = ("🛑 Хранилище временных окон не поднято (проверь "
                "TG_APPROVAL_ENABLED/CONSENT_DATABASE_URL) — ключ не выдан.")
        _tg_call(cfg, "sendMessage", {"chat_id": chat_id, "text": text,
                                      "parse_mode": "HTML"})
        return "Не удалось выдать ключ"
    hours = automation_key.AUTOMATION_WINDOW_HOURS
    text = (f"🔑 Временный ключ (действует {hours:g} ч):\n"
            f"<code>{md_to_telegram_html(token)}</code>\n\n"
            "Хранится только его хэш — этот текст больше нигде не "
            "повторится. Передай его автоматике как automation_key. Другие "
            "уже выданные ключи (в других чатах) это НЕ трогает. Само это "
            "сообщение исчезнет через 10 секунд.")
    res = _tg_call(cfg, "sendMessage", {"chat_id": chat_id, "text": text,
                                        "parse_mode": "HTML"})
    message_id = (res.get("result") or {}).get("message_id") if res.get("ok") else None
    schedule_message_delete(chat_id, message_id, delay_s=_AK_TOKEN_MESSAGE_DELETE_DELAY_S)
    _ak_send_menu(cfg, chat_id)
    return "Ключ отправлен"


def _ak_do_list(cfg: TgApprovalConfig, chat_id: str) -> str:
    windows = automation_key.list_windows(chat_id)
    _tg_call(cfg, "sendMessage", {
        "chat_id": chat_id, "text": _format_windows_list(windows),
        "parse_mode": "HTML", "reply_markup": _ak_list_markup(windows),
    })
    return "Список отправлен"


def _ak_do_revoke(cfg: TgApprovalConfig, chat_id: str, window_id: str) -> str:
    ok = automation_key.revoke_window(window_id, chat_id)
    text = (f"🔒 Окно <code>{window_id}</code> выключено." if ok else
            f"Окно <code>{window_id}</code> не найдено или уже неактивно.")
    _tg_call(cfg, "sendMessage", {"chat_id": chat_id, "text": text,
                                  "parse_mode": "HTML",
                                  "reply_markup": _AUTOMATION_KEY_MENU_MARKUP})
    return "Готово"


def _ak_do_offall(cfg: TgApprovalConfig, chat_id: str) -> str:
    n = automation_key.revoke_all_windows(chat_id)
    text = (f"🔒 Выключено окон: {n}." if n else "Активных окон и так не было.")
    _tg_call(cfg, "sendMessage", {"chat_id": chat_id, "text": text,
                                  "reply_markup": _AUTOMATION_KEY_MENU_MARKUP})
    return "Готово"


# `/automation_key list`, `/automation_key revoke <id>`, `/automation_key off`
# — тот же самый разбор, что и у кнопок, только текстом. `<id>` — первое
# слово после `revoke` (window_id не содержит пробелов, см. `secrets.
# token_hex` в automation_key.py), лишний хвост после него игнорируется.
_AK_TEXT_ARG_RE = re.compile(r"^(list|off|revoke)(?:\s+(\S+))?$", re.IGNORECASE)


def _handle_automation_key_message(cfg: TgApprovalConfig, msg: Dict[str, Any]) -> None:
    """УСТАРЕЛО (TZ_automation_key_hub.md, 2026-08-11): генерация/список/
    отзыв временных окон переехали в gmail-mcp — он один держит вебхук после
    консолидации ботов, единый ключ действует на выбранные при генерации
    сервисы (scope), не только на ticktick. Ветка НЕ удалена (маленькая,
    безопасно оставить — она уже не единственная защита ни от чего), но
    вместо реального выполнения отвечает редиректом на новую команду —
    ИНАЧЕ, будь own_bot когда-нибудь снова включён, здесь тихо ожил бы
    старый одно-серверный путь генерации, который больше не согласован с
    общей scope-схемой (создал бы `scope='ticktick'`-only окно в обход
    выбора сервисов в gmail-mcp).

    Всё остальное (не команда, не владелец) — тихо игнорируется, как и
    раньше; это НЕ общий чат-бот, обычное текстовое «да» approval-гейта
    по-прежнему читает МОДЕЛЬ через MCP-инструмент, а не этот вебхук."""
    from_id = str((msg.get("from") or {}).get("id") or "")
    if not from_id or from_id != str(cfg.owner_chat_id):
        return
    text = (msg.get("text") or "").strip()
    # `@bot_username` — форма команды в группах; личка её не шлёт, но
    # own_bot's owner_chat_id теоретически может быть группой.
    parts = text.split(None, 1) if text else []
    command = parts[0].split("@")[0] if parts else ""
    if command != _AUTOMATION_KEY_COMMAND:
        return
    chat_id = str((msg.get("chat") or {}).get("id") or cfg.owner_chat_id)
    _tg_call(cfg, "sendMessage", {
        "chat_id": chat_id,
        "text": "Генерация ключей переехала в gmail-mcp (единый бот, выбор "
                "сервисов при генерации) — набери /automation_key там же, "
                "в этом чате.",
    })


def _handle_automation_key_callback(cfg: TgApprovalConfig, cq: Dict[str, Any],
                                    action: str) -> None:
    """УСТАРЕЛО (TZ_automation_key_hub.md, 2026-08-11) — тот же редирект, что
    у `_handle_automation_key_message`: старые кнопки меню могли остаться на
    экране у владельца от версии до переезда генерации в gmail-mcp; нажатие
    отвечает, куда идти, вместо того чтобы молча создавать одно-серверное
    (`scope='ticktick'`-only) окно в обход выбора сервисов. `_ak_do_*`
    функции ниже оставлены нетронутыми (используются тестами/на случай
    отката), просто эта точка входа их больше не зовёт."""
    cq_id = cq.get("id")
    if cq_id:
        _tg_call(cfg, "answerCallbackQuery", {
            "callback_query_id": cq_id,
            "text": "Генерация ключей переехала в gmail-mcp — набери "
                    "/automation_key там же.",
        })


def register_webhook(cfg: TgApprovalConfig, public_base_url: Optional[str]) -> None:
    """Регистрирует `/tg/webhook` в Telegram (`setWebhook`) — own_bot-аналог
    того, что для общего бота делает `TG_WEBHOOK_OWNER` на стороне gmail-mcp.
    Вызывается из server.py's `main()`, ТОЛЬКО когда `cfg.enabled and
    cfg.own_bot` (сам гейт — на вызывающей стороне; здесь дублируем его же
    первой строкой ради того же defense-in-depth, что у TS-референса
    `registerWebhook`: функция не должна полагаться на то, что единственный
    вызывающий её код когда-нибудь не забудет свою же проверку).

    Транспорт `stdio` сюда вообще не должен доходить (в нём нет HTTP-сервера,
    регистрировать вебхук физически некуда) — это ответственность
    вызывающего (server.py's `main()` логирует предупреждение и не зовёт эту
    функцию в таком случае); здесь на этот случай отдельной проверки нет,
    чтобы не дублировать то же решение в двух местах.

    Никогда не бросает — падение регистрации не должно ронять запуск
    сервера: сервер поднимется, просто кнопки own_bot не будут доходить, и
    это видно в логе как ERROR (тот же принцип, что у TS-версии — «громкий
    лог, а не тихий сбой»)."""
    if not (cfg.enabled and cfg.own_bot):
        return
    if not public_base_url:
        logger.error("TG own_bot: не знаю свой публичный адрес (PUBLIC_BASE_URL / "
                     "RAILWAY_PUBLIC_DOMAIN не заданы) — не могу зарегистрировать "
                     "/tg/webhook, кнопки own_bot работать не будут")
        return
    url = f"{public_base_url.rstrip('/')}/tg/webhook"
    try:
        res = _tg_call(cfg, "setWebhook", {
            "url": url,
            "secret_token": cfg.webhook_secret,
            # "message" добавлен 2026-08-10 (TZ_temp_automation_key.md §3.2)
            # ради команды `/automation_key` — до этого вебхук подписывался
            # ТОЛЬКО на нажатия кнопок (callback_query), обычные текстовые
            # сообщения Telegram сюда не присылал вовсе. `handle_webhook`
            # ниже по-прежнему игнорирует любое сообщение, которое не
            # начинается с `/automation_key` и не от владельца (см. её
            # докстринг) — расширение подписки НЕ открывает никакой новый
            # путь исполнения гейтованных операций, только сам этот один
            # новый набор кнопок.
            "allowed_updates": ["callback_query", "message"],
        })
    except Exception as e:  # noqa: BLE001 — регистрация best-effort на старте
        logger.error(f"TG own_bot: setWebhook упал: {e} — url={url}")
        return
    if not res.get("ok"):
        logger.error(f"TG own_bot: setWebhook не удался "
                     f"({res.get('description') or 'unknown error'}) — url={url}")
        return
    logger.info(f"TG own_bot: вебхук зарегистрирован на {url} (server=ticktick)")


# ───────────────────────── Публичное API для server.py ─────────────────────

def notify_plan(cfg: TgApprovalConfig, manifest_id: str, preview_body: str,
                 tool: str) -> tuple[bool, str]:
    """Отправляет план в Telegram с кнопками [✅ Подтвердить][🛑 Отклонить],
    пишет строку в tg_approvals. Fail-closed по духу gmail-mcp: если это
    вернуло (False, ...), вызывающий код ОБЯЗАН инвалидировать манифест — тот
    же контракт, что requireConsent's notifyPlan в TS.

    ПОРЯДОК ШАГОВ ВАЖЕН (исправлено 2026-08-06): сначала INSERT строки
    (message_id ещё NULL), потом отправка, потом UPDATE строки реальными id.
    Раньше было наоборот — сообщение с кнопками уходило РАНЬШЕ, чем в БД
    появлялась строка. Владелец, нажавший кнопку в этот зазор (доли секунды,
    но кнопка видна сразу и человек сидит в чате именно ради неё), терял
    нажатие впустую: вебхук gmail-mcp делает `UPDATE … WHERE manifest_id = …
    AND status = 'PENDING'`, строки не находил, обновлять было нечего — и
    сообщение оставалось висеть с живыми кнопками, а действие не наступало
    никогда. Теперь любое нажатие попадает в существующую строку."""
    if not store_ready():
        return False, "Postgres для TG-approval не настроен (CONSENT_DATABASE_URL)"
    expires_at = _now_ms() + cfg.ttl_s * 1000
    try:
        create_tg_approval(manifest_id, cfg.owner_chat_id, None, expires_at, [])
    except Exception as e:  # noqa: BLE001 — не смогли создать строку = гейта нет
        logger.warning(f"TG: не смог создать строку подтверждения {manifest_id}: {e}")
        # 2026-08-09 (независимый аудит, расширение П7 на файлы вне
        # server.py): `e` здесь может быть исключением psycopg на сбое
        # подключения — то есть DSN с логином:паролем в открытом виде (см.
        # test_log_redaction.py::test_postgres_dsn_credentials_are_redacted).
        # Единственный вызывающий (server.py's _maybe_tg_notify_plan) СЕГОДНЯ
        # прогоняет `err` через `_redact_for_user` перед показом — но
        # полагаться на то, что КАЖДЫЙ будущий вызывающий об этом не забудет,
        # неправильно: редактировать нужно там, где исключение поймано.
        return False, ("не удалось создать строку подтверждения в Postgres: "
                       f"{log_redaction.redact(str(e))}")

    text = f"{preview_body}\n\n{tool} · ticktick"
    res = send_message_chunked(
        cfg, cfg.owner_chat_id, text,
        reply_markup_on_last={
            "inline_keyboard": [[
                {"text": "✅ Подтвердить", "callback_data": f"a:{manifest_id}"},
                {"text": "🛑 Отклонить", "callback_data": f"r:{manifest_id}"},
            ]]
        },
    )
    if not res.ok:
        # Обрубок плана без кнопок в чате опаснее, чем ничего: человек может
        # принять его за полный и «подтвердить» в чате то, чего не видел.
        # Поэтому уже доставленные куски убираем и честно проваливаем гейт.
        for mid in res.message_ids:
            delete_message(cfg, cfg.owner_chat_id, mid)
        # ...и обязательно сносим строку, которую вставили первым шагом: иначе
        # осталась бы сирота PENDING без единого сообщения в чате — живая до
        # TTL и способная «подтвердить» план, которого никто не видел.
        delete_tg_approval(manifest_id)
        return False, res.error or "Telegram sendMessage failed"
    # Кнопки — на ПОСЛЕДНЕМ сообщении, его id и есть «тот самый» message_id,
    # с которым работает вебхук gmail-mcp; предыдущие куски идут в extra_*.
    message_id = res.message_ids[-1] if res.message_ids else None
    extra_ids = list(res.message_ids[:-1])
    if not attach_plan_messages(manifest_id, message_id, extra_ids):
        # Гейт НЕ проваливаем: план доставлен, строка есть, нажатие сработает
        # (вебхук снимает кнопки по message_id из самого callback_query, а не
        # из БД). Потеряна только уборка — сводку будет некуда вписать, а
        # лишние куски плана некому удалить. Это несоразмерно меньшая беда,
        # чем отменить уже показанный человеку план из-за сбоя UPDATE.
        logger.warning(f"TG: план {manifest_id} доставлен, но message_id в БД не "
                       f"записан — сводка в личку и уборка кусков не сработают")
    return True, ""


def approval_status_of(row: Optional[dict]) -> str:
    """Чистая (БЕЗ обращения к базе) классификация строки tg_approvals в тот
    же словарь статусов, что отдаёт check_approval. Вынесена отдельно, чтобы
    ОДНА формула обслуживала оба пути: одиночный (чат, _require_consent) и
    пакетный (поллер, get_tg_approvals) — иначе они могли бы разойтись,
    например в трактовке истёкшего TTL."""
    if not row:
        return "none"
    if row["status"] == "APPROVED":
        return "approved"
    if row["status"] == "REJECTED":
        return "rejected"
    if _now_ms() > row["expires_at"]:
        return "none"
    return "pending"


def check_approval(manifest_id: str) -> str:
    """"approved" | "pending" | "rejected" | "none" — "none" покрывает и
    «никогда не спрашивали», и «TTL истёк» (та же семантика, что checkApproval
    в TS — фаза исполнения обрабатывает оба случая одинаково).

    Одиночная версия: её зовёт чат-путь (_require_consent) для ОДНОГО
    манифеста. Поллер с 2026-08-06 её НЕ использует — он читает статусы всех
    живых манифестов одним get_tg_approvals()."""
    if not store_ready():
        return "none"
    return approval_status_of(get_tg_approval(manifest_id))


# ───────────────────── Авто-исполнение по кнопке (2026-08-05) ─────────────────
#
# Максим, ночь на 2026-08-05: «нажал кнопку в Telegram — должно сразу
# исполниться на бэке, не ждать повторного вызова моделью». До этого кнопка
# только переключала статус строки в `tg_approvals` — реальная мутация
# происходила ТОЛЬКО когда модель САМА второй раз звала гейтованный тул с
# `user_reply`; если человек ничего не писал в чат после нажатия, действие
# могло не наступить никогда. Портировано с gmail-mcp/src/consent.ts's
# `tryAutoExecute`/`TG_AUTO_REPLY_MARKER` и src/tg_approval.ts's
# `reportAutoExecutionResult` — тот же контракт, адаптированный под
# АРХИТЕКТУРНУЮ разницу: TS-сторона хранит и манифест, и tg_approvals в ОДНОМ
# Postgres (один SQL JOIN находит кандидатов, `consumeManifest` — атомарный
# `UPDATE ... RETURNING`). В ticktick-mcp манифесты (`_MANIFESTS` в server.py)
# — это IN-MEMORY dict одного процесса, НЕ Postgres; только tg_approvals живёт
# в общем Postgres. Поэтому здесь try_auto_execute() НЕ обращается к
# _MANIFESTS напрямую (это создало бы обратную зависимость tg_approval.py →
# server.py) — вместо этого server.py передаёт три callback'а
# (get_manifest/consume_manifest/rehash), а поиск кандидатов (перебор
# _MANIFESTS + check_approval на каждый) тоже живёт в server.py, где
# _MANIFESTS реально виден.
#
# Два независимых режима гейта (Максим подтвердил явно, см. docstring
# `_require_consent` в server.py) остаются нетронуты: обычный путь через
# `_require_consent()` (чат-«да», без TG) НЕ меняется НИ НА БИТ — это
# отдельная функция, вызываемая ТОЛЬКО фоновым поллером сервера, а не
# альтернативная ветка внутри `_require_consent`.

# TG_AUTO_REPLY_MARKER — БЫЛА здесь (порт TS-стороны, где `tryAutoExecute`
# подменял ею `user_reply` перед тем, как прогнать вызов через ОБЩИЙ
# `requireConsent`). Удалена (docs/DESIGN_approval_gate.md §9 п.7, аудит
# 2026-08-19): в ticktick-mcp `try_auto_execute` идёт МИМО `_require_consent`
# целиком (см. блок-комментарий выше — «классификация user_reply не нужна»),
# так что подставлять строку было НЕКУДА — константа была объявлена и НИ РАЗУ
# не читалась, а происхождение согласия («кнопка» vs «текстовое да» vs
# «аварийный выключатель гейта») в журнал мутаций и в operation_report не
# попадало вовсе.
#
# РАБОЧАЯ ЗАМЕНА — не строка-подмена, а честная метка на самой журнальной
# записи: `_tg_auto_execute_tick`/`pending_consents_decide` в server.py
# оборачивают вызов исполнителя в `consent._TG_AUTO_EXECUTE_MANIFEST.set(...)`,
# `_journal_write` (consent.py) кладёт это в поле `"tg_manifest"` КАЖДОЙ
# записи, сделанной внутри вызова, а `operation_report` (server.py's
# `_tg_button_note`) печатает по этому полю человекочитаемую строку «🔘
# Подтверждено кнопкой…» — ровно то же место, что уже показывает канал
# `automation_key`/аварийного выключателя (`_automation_channel_note`).
# Обычное текстовое «да» ни того, ни другого поля не оставляет — это и есть
# третье состояние («текстовый ответ»), различимое по ОТСУТСТВИЮ обеих меток.


def try_auto_execute(
    *,
    manifest_id: str,
    tool: str,
    get_manifest: Callable[[str], Optional[Dict[str, Any]]],
    consume_manifest: Callable[[str], Optional[Dict[str, Any]]],
    rehash: Callable[[Dict[str, Any]], str],
) -> Optional[Dict[str, Any]]:
    """Аналог TS `tryAutoExecute`, адаптированный под in-memory манифесты
    (см. блок-комментарий выше). Проверяет ТЕ ЖЕ инварианты, что и обычный
    execute-путь через `_require_consent`, — КРОМЕ классификации текстового
    `user_reply` (не нужна: нажатие кнопки уже было единственным доказанным
    согласием для этого тула — `tg.enabled_for(tool)` было истинно в момент
    постройки плана, иначе строки в `tg_approvals` не было бы вовсе):

    1. Манифест существует и НЕ `consumed` (жив).
    2. Манифест принадлежит именно этому `tool` (сверка, не слепое доверие
       кандидату — на случай будущего расхождения между тем, как поллер
       определил tool, и тем, что реально хранится в манифесте).
    3. Binding: `rehash(manifest)` совпадает с `object_hash`, сохранённым при
       планировании (тот же принцип, что у обычного `_require_consent`, —
       см. его собственный честный комментарий о том, что в ticktick-mcp это
       сверка с тем же самым сохранённым значением, а не с независимым живым
       состоянием, так что и здесь это не более сильная защита, чем есть в
       остальном коде; сохраняется ради единообразия и на случай будущего
       усиления в одном месте).
    4. Одноразовость: `consume_manifest(manifest_id)` — вызывающий (server.py)
       обязан атомарно (синхронно, без `await` между чтением и записью флага
       `consumed`) вернуть либо ту же живую копию манифеста с `consumed`
       выставленным в True, либо None, если кто-то (гонка/повторный тик
       поллера) уже успел его забрать.

    Возвращает манифест dict (то, что вернул `consume_manifest`) при успехе,
    иначе None (манифест неактуален — гонка/дрейф/просрочен/чужой tool) —
    вызывающий поллер просто пропускает кандидата, это не ошибка."""
    m = get_manifest(manifest_id)
    if m is None or m.get("consumed"):
        return None
    # Инвариант 2 (сверка тула). ДО 2026-08-06 читалось поле `_auto_tool`,
    # которого не писал НИКТО: `m.get("_auto_tool", tool)` всегда отдавал
    # дефолт `tool`, сравнение всегда было истинным — предохранитель не
    # срабатывал ни разу за всё время жизни кода. Пока гейт с кнопкой был у
    # одного тула (delete_tasks), это было безвредно; с расширением на 19
    # тулов ошибка диспетчеризации в поллере уже означала бы исполнение ЧУЖОЙ
    # операции по чужому подтверждению. Теперь сверяемся с полями, которые
    # реально существуют: `_tg_tool` (ставит _maybe_tg_notify_plan в момент
    # отправки кнопок — есть у любого манифеста, для которого кнопка вообще
    # существует) и `tool` (кладут _gate_batch/_gate_single при постройке
    # плана). `_auto_tool` оставлен последним в цепочке как явная ручная
    # метка. Если метки нет ни одной — поведение прежнее (проверка
    # пропускается), чтобы старые формы манифестов не перестали исполняться.
    owner = m.get("_tg_tool") or m.get("tool") or m.get("_auto_tool")
    if tool and owner and owner != tool:
        return None
    stored_hash = m.get("object_hash")
    if stored_hash:
        try:
            current_hash = rehash(m)
        except Exception as e:
            logger.warning(f"TG auto-execute: rehash failed for {manifest_id}: {e}")
            return None
        if current_hash != stored_hash:
            return None
    return consume_manifest(manifest_id)


# ─────────────── Отчёт в группу-архив + короткая сводка в личку ───────────────
#
# Разделение ролей (Максим, 2026-08-06): личка — это «пульт» (план, кнопки,
# короткая сводка «сделано/не сделано»), а ПОЛНЫЙ отчёт живёт в отдельной
# группе-архиве (у Максима — «MCP Отчёты», chat_id вида -100…, задаётся через
# TG_REPORTS_CHAT_ID). Личка при этом ещё и подчищается по TTL (reap_expired),
# а архив в группе остаётся.

# ЗАПАСНОЙ словарь (2026-08-06, дефект №1, часть 2). С правкой ниже
# (`has_own_header` в `post_report_to_group`) этот заголовок печатается
# ТОЛЬКО когда `report_md` сам не начинается с «###» — то есть для отчётов
# БЕЗ собственного заголовка (сейчас это только verdict="lost"). Все отчёты
# `_verified_auto_execute_report` (server.py) уже несут свой самодостаточный
# `### <эмодзи> Автоисполнение «tool» — <слово>» первой строкой, поэтому для
# них этот словарь больше НЕ читается — раньше читался ВСЕГДА, и в одном и
# том же сообщении печатались ДВА заголовка про один и тот же исход, притом
# РАЗНЫМИ словами и даже разным эмодзи для "unverified" (здесь было ⚠️, в
# server.py — ❓): два независимых словаря одного и того же понятия неизбежно
# разъезжаются. Значения оставлены как честный fallback, не удалены.
_VERDICT_HEADERS = {
    "ok": "✅ Исполнено и подтверждено",
    "partial": "⚠️ Исполнено частично",
    "mismatch": "❌ Исполнено, но результат расходится с ожиданием",
    "failed": "🛑 Ошибка исполнения",
    "unverified": "❓ Исполнено, НО независимой перепроверкой не подтверждено",
    # «lost» — это НЕ ошибка исполнения: исполнения вообще не было. Отдельный
    # заголовок нужен, чтобы в архиве такие случаи не смешивались с «🛑 Ошибка
    # исполнения» (там мутация могла пройти частично, здесь — не начиналась).
    "lost": "🛑 Подтверждено, но исполнять было нечего (план не сохранился)",
}


def _owner_now_str() -> str:
    """Время глазами владельца — America/Los_Angeles, никогда UTC."""
    return datetime.now(_resolve_owner_tz()).strftime("%Y-%m-%d %H:%M:%S %Z")


def owner_time_str(epoch_ms: Optional[int]) -> str:
    """Момент из epoch-миллисекунд (так время хранится в `tg_approvals`) в том
    же виде, в каком владелец читает всё остальное — его собственный часовой
    пояс, никогда UTC. Пустое/битое значение честно называется словами, а не
    подставляется «началом эпохи»."""
    if not epoch_ms:
        return "время неизвестно"
    try:
        return datetime.fromtimestamp(int(epoch_ms) / 1000,
                                      _resolve_owner_tz()).strftime(
            "%Y-%m-%d %H:%M:%S %Z")
    except Exception as e:  # noqa: BLE001 — форматирование даты не должно ронять отчёт
        logger.warning(f"TG: не смог отформатировать время {epoch_ms!r}: {e}")
        return "время неизвестно"


class ReportDelivery(NamedTuple):
    """Чем закончилась публикация отчёта в группу-архив.

    Раньше функция возвращала голый `list[int]`, и вызывающий проверял его
    через `if delivered:` — то есть считал УСПЕХОМ любую непустую доставку.
    На затяжном флуд-лимите Telegram это давало прямую неправду: в группу
    легли, скажем, 2 части из 6, а в личку владельца уходило бодрое
    «Подробный отчёт — в группе «MCP Отчёты»». Требование Максима дословно —
    «честная пост-верификация, не оптимистичный отчёт», поэтому полноту
    доставки теперь видно по полям, а не угадывают по длине списка.

    Осознанно НЕ притворяемся списком ради «дешёвой обратной совместимости»:
    у NamedTuple из четырёх полей `bool()` ВСЕГДА True, а `== []` ВСЕГДА
    False, так что старый код (`if delivered:`) молча читал бы неудачу как
    успех — ровно тот баг, который здесь и чинится. Поэтому все места вызова
    и все тесты обновлены явно."""
    message_ids: list      # id реально доставленных сообщений
    total_chunks: int      # на сколько частей отчёт был разбит
    delivered: int         # сколько частей реально дошло
    ok: bool               # дошло ВСЁ


def post_report_to_group(cfg: TgApprovalConfig, manifest_id: str, report_md: str,
                         *, tool: str, verdict: str) -> ReportDelivery:
    """Публикует ПОЛНЫЙ отчёт об исполнении в группу-архив и запоминает в БД,
    куда он ушёл. Возвращает `ReportDelivery` — вызывающий обязан смотреть на
    `.ok`, а не на непустоту `.message_ids` (см. докстринг ReportDelivery).

    НИКОГДА не бросает: мутация в TickTick к этому моменту уже произошла, и
    падение на отправке отчёта не должно превращаться в ошибку тула — иначе
    вызывающий решит, что действие не выполнено, и повторит его.

    Заголовок из `_VERDICT_HEADERS` печатается ТОЛЬКО когда `report_md` сам
    ещё не начинается с «###» (2026-08-06, дефект №1, часть 2, живой прогон
    на update_project). `_verified_auto_execute_report` в server.py уже
    кладёт самодостаточный заголовок первой строкой ровно по правилу 1
    output-format.md §7.1 («заголовок первым, одна строка, самодостаточный»)
    — раньше сюда всё равно приклеивался ВТОРОЙ, независимо сопровождаемый
    заголовок про тот же исход, и оба уходили в одном сообщении. Отчёты без
    собственного заголовка (сейчас — только verdict="lost", план не пережил
    перезапуск) по-прежнему получают заголовок отсюда — им больше неоткуда."""
    try:
        chat_id = cfg.reports_chat_id or cfg.owner_chat_id
        if not chat_id:
            logger.warning("TG: некуда публиковать отчёт — ни TG_REPORTS_CHAT_ID, "
                           "ни TG_OWNER_CHAT_ID не заданы")
            return ReportDelivery([], 0, 0, False)
        meta_line = f"{tool} · `{manifest_id}` · {_owner_now_str()}"
        if report_md.lstrip().startswith("###"):
            text = f"{meta_line}\n\n{report_md}"
        else:
            header = _VERDICT_HEADERS.get(verdict) or _VERDICT_HEADERS["unverified"]
            text = f"### {header}\n{meta_line}\n\n{report_md}"
        res = send_message_chunked(cfg, chat_id, text)
        if not res.ok:
            # Частичная доставка — не повод «забыть» id: их всё равно надо
            # записать, иначе reaper не найдёт эти сообщения.
            logger.warning(f"TG: отчёт по {manifest_id} доставлен не полностью "
                           f"({len(res.message_ids)} из {res.total_chunks} "
                           f"частей): {res.error}")
        if res.message_ids:
            record_report_messages(manifest_id, chat_id, res.message_ids)
            # TZ §5: отчёт — уже финальное сообщение, таймер стартует СРАЗУ
            # при отправке (в отличие от плана/кнопок, чей таймер ждёт
            # ответа человека).
            for mid in res.message_ids:
                schedule_message_delete(chat_id, mid)
        return ReportDelivery(list(res.message_ids), res.total_chunks,
                              len(res.message_ids), bool(res.ok))
    except Exception as e:  # noqa: BLE001 — отчёт best-effort по определению
        logger.warning(f"TG: не смог опубликовать отчёт по {manifest_id}: {e}")
        return ReportDelivery([], 0, 0, False)


def summarize_in_owner_chat(cfg: TgApprovalConfig, chat_id: str,
                            message_id: Optional[int], short_md: str) -> bool:
    """Заменяет план в личке КОРОТКОЙ сводкой и снимает кнопки одним
    `editMessageText` (Telegram позволяет менять текст и reply_markup вместе).
    Текст сводки формирует вызывающий — здесь только транспорт.

    Возвращает True, только если Telegram реально принял правку. Это не
    косметика: вызывающий по этому признаку решает, можно ли теперь удалять
    ПРЕДЫДУЩИЕ куски длинного плана (пока итог не вписан, стирать контекст
    нельзя — владелец остался бы вообще без информации).

    Best-effort: если человек стёр сообщение руками или оно старше суток —
    просто лог и False, исключений наружу нет (мутация уже произошла).

    ПРЕДОХРАНИТЕЛЬ (2026-08-06): `editMessageText` здесь — второй (и
    последний) путь текста в Telegram помимо `send_message_chunked`, поэтому
    `short_md` тоже проходит через `strip_agent_instructions` — см. её
    докстринг в `send_message_chunked`."""
    if message_id is None:
        logger.warning(f"TG: message_id отсутствует, сводку некуда вписать "
                       f"(chat={chat_id})")
        return False
    short_md = strip_agent_instructions(short_md)
    # Бюджет сужен на длину приписки «(сводка сокращена…)»: без этого запаса
    # кусок ровно в лимит + приписка давали текст ДЛИННЕЕ 4096, Telegram
    # отвечал 400, и сводка не появлялась вовсе — а кнопки на исполненном
    # плане оставались висеть. Тихая потеря сводки, которую видно только в
    # логах, — ровно тот silent-fail, ради которого всё это переписывалось.
    _CUT_NOTE = "\n\n_(сводка сокращена — полный отчёт в группе)_"
    chunks = split_for_telegram(short_md, TELEGRAM_TEXT_LIMIT - len(_CUT_NOTE) - 8)
    if not chunks:
        return False
    text = chunks[0]
    if len(chunks) > 1:
        # Сводка по контракту короткая; если вызывающий прислал длинную —
        # обрезаем ЯВНО и говорим, где лежит полный текст.
        text += _CUT_NOTE
    res = _tg_call(cfg, "editMessageText", {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": md_to_telegram_html(text),
        "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": []},
    })
    if not res.get("ok"):
        logger.warning(f"TG: editMessageText для сообщения {message_id} не удался: "
                       f"{res.get('description')}")
        return False
    return True


def clear_inline_keyboard(cfg: TgApprovalConfig, chat_id: str,
                          message_id: Optional[int]) -> bool:
    """Снимает кнопки [✅][🛑] с сообщения, НЕ трогая его текст.

    Отличие от `summarize_in_owner_chat` не косметическое, а смысловое: та
    ЗАМЕНЯЕТ текст плана итогом (после исполнения итог важнее плана). А когда
    исполнять было нечего, план — единственное, что у владельца осталось от
    его же просьбы: затерев текст, мы отняли бы у него возможность понять, что
    именно повторять. Поэтому текст остаётся, а вводящие в заблуждение кнопки
    убираются. Кто объясняет ситуацию словами — зависит от вызывающего
    (2026-08-19, раньше здесь стояло огульное «объяснение приходит отдельным
    сообщением», что было правдой только для одного пути из трёх):
      • lost-plan (`_publish_lost_manifest_outcome`) — да, шлёт отдельное
        сообщение с объяснением;
      • approve-путь вебхука — объяснит поллер: итог исполнения впишется в
        это же сообщение (`summarize_in_owner_chat`);
      • reject-путь вебхука — зовёт эту функцию только РЕЗЕРВНО, когда
        `mark_rejected_in_owner_chat` не смогла вписать в текст терминальное
        «🛑 Отклонено — ничего не сделано».

    Best-effort: сообщение стёрли руками / Telegram не в духе → False и лог."""
    if message_id is None:
        return False
    res = _tg_call(cfg, "editMessageReplyMarkup", {
        "chat_id": chat_id,
        "message_id": message_id,
        "reply_markup": {"inline_keyboard": []},
    })
    if not res.get("ok"):
        logger.warning(f"TG: не смог снять кнопки с сообщения {message_id} в "
                       f"чате {chat_id}: {res.get('description')}")
        return False
    return True


# Терминальный след отказа (2026-08-19). Формулировка сознательно НЕ похожа
# на успех: без ✅, без «подтверждено», без счётчиков «затронуто N объектов»
# — по замороженной эмодзи-легенде (output-format.md §7.2) 🛑 означает
# «ничего не изменено», и это ровно то, что случилось.
_REJECTED_MARK_MD = (
    "🛑 **Отклонено — ничего не сделано.**\n\n"
    "Изменений в TickTick нет: план закрыт, исполнить его больше нельзя. "
    "Если операция всё-таки нужна — попросите её заново, придёт новый план "
    "с кнопками.")

# Сколько символов отклонённого плана сохранять под терминальной строкой.
# Правка обязана влезть в ОДИН editMessageText (4096): длиннее — Telegram
# ответит 400 и следа не останется вовсе. 3000 + терминальная строка + запас
# на HTML-экранирование в `md_to_telegram_html` укладываются с запасом.
_REJECTED_PLAN_TAIL_CAP = 3000


def mark_rejected_in_owner_chat(cfg: TgApprovalConfig, chat_id: str,
                                message_id: Optional[int],
                                plan_text: str = "") -> bool:
    """Вписывает в сообщение плана терминальный след отказа: «🛑 Отклонено —
    ничего не сделано» ПЕРВОЙ строкой (заголовок первым — output-format.md
    §7.1), ниже — сам отклонённый план для справки (владелец, передумавший
    через полминуты, должен видеть, ЧТО он отклонил; текст берётся из
    `callback_query.message.text` — Telegram присылает его вместе с нажатием,
    отдельного чтения не нужно).

    Транспорт — `summarize_in_owner_chat`: тот же единственный путь
    `editMessageText`, которым approve-итог вписывается в это же сообщение
    (текст меняется И кнопки снимаются одним вызовом), а не параллельная
    копия. Отдельное сообщение здесь было бы хуже: план и так стоит в очереди
    самоудаления (TZ §5, таймер стартовал в `_handle_callback_query`), а
    новое сообщение пришлось бы ставить в очередь отдельно и оно звякнуло бы
    лишним уведомлением.

    Best-effort: False — вписать не удалось (нет message_id, сообщение старше
    48ч, стёрто руками), вызывающий тогда хотя бы снимает кнопки."""
    text = _REJECTED_MARK_MD
    plan = (plan_text or "").strip()
    if plan:
        if len(plan) > _REJECTED_PLAN_TAIL_CAP:
            plan = plan[:_REJECTED_PLAN_TAIL_CAP] + "…"
        text += "\n\nОтклонённый план (для справки):\n" + plan
    return summarize_in_owner_chat(cfg, chat_id, message_id, text)


def report_auto_execution_result(cfg: TgApprovalConfig, chat_id: str,
                                  message_id: Optional[int], report_text: str) -> bool:
    """DEPRECATED (2026-08-06): осталась только ради обратной совместимости с
    существующими вызовами в server.py. Раньше вписывала ВЕСЬ отчёт в то же
    сообщение с кнопками (и резала его на 3500 символах); теперь полный отчёт
    уходит в группу через post_report_to_group(), а сюда идёт короткая сводка.
    Новый код должен звать summarize_in_owner_chat() напрямую."""
    return summarize_in_owner_chat(cfg, chat_id, message_id, report_text)


# ───────────────────────── TTL-уборка (reaper) ─────────────────────────

def reap_expired(cfg: TgApprovalConfig) -> int:
    """Удаляет из чата просроченные планы, по которым решения так и не приняли
    (Максим: «не просто снятие кнопок, а полное удаление сообщения» — иначе в
    личке копятся мёртвые планы, неотличимые с виду от живых).

    Тонкости, за которые заплачено разбором чужого кода:
      • Забираем строки атомарно одним `DELETE … RETURNING` — два тика поллера
        (или два процесса) не смогут взять одну и ту же строку и удалить
        сообщение дважды.
      • Ловим не только 'PENDING', но и 'EXPIRED': sweep gmail-mcp не фильтрует
        по `server` и мог уже перевести НАШУ строку PENDING→EXPIRED, сняв
        кнопки, но САМО сообщение он в этом случае не удаляет — оно остаётся
        висеть, и добить его обязаны мы.
      • 'APPROVED'/'REJECTED' не трогаем СОЗНАТЕЛЬНО: кнопку нажали, значит
        отчёт в группе — это архив состоявшегося решения, он должен жить; те
        строки после TTL приберёт sweep gmail-mcp.
      • `message_id IS NULL` — штатная, а не битая строка: `notify_plan`
        вставляет её ДО отправки сообщений, поэтому сюда может попасть
        манифест, у которого отправка так и не состоялась (упал процесс,
        отвалилась сеть). Удалять в Telegram нечего — просто убираем строку,
        без единого лишнего вызова и без исключений (см. проверку
        `tgt_msg is not None` ниже).
    """
    if not cfg.reap_enabled or not store_ready():
        return 0
    try:
        conn = _pg_pool.getconn()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"TG reaper: нет соединения с Postgres: {e}")
        return 0
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM tg_approvals
                 WHERE server = 'ticktick'
                   AND status IN ('PENDING', 'EXPIRED')
                   AND expires_at <= %s
                RETURNING manifest_id, chat_id, message_id, extra_message_ids,
                          report_chat_id, report_message_ids
                """,
                (_now_ms(),),
            )
            rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"TG reaper: DELETE … RETURNING не удался: {e}")
        return 0
    finally:
        _pg_pool.putconn(conn)

    for row in rows:
        manifest_id, chat_id, message_id, extra_ids, report_chat, report_ids = row
        try:
            targets = [(chat_id, message_id)] + [(chat_id, m) for m in (extra_ids or [])]
            rchat = report_chat or chat_id
            targets += [(rchat, m) for m in (report_ids or [])]
            for tgt_chat, tgt_msg in targets:
                if tgt_chat and tgt_msg is not None:
                    delete_message(cfg, str(tgt_chat), tgt_msg)
        except Exception as e:  # noqa: BLE001 — один битый манифест не должен
            # оставить неубранными все остальные
            logger.warning(f"TG reaper: уборка {manifest_id} прервалась: {e}")
    if rows:
        logger.info(f"TG reaper: прибрано просроченных манифестов: {len(rows)}")
    return len(rows)
