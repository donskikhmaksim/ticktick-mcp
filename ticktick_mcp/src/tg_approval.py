"""
tg_approval.py — опциональный внеполосный (out-of-band) Telegram-фактор
поверх текстового `user_reply` (docs/DESIGN_approval_gate.md §4.5: «модель
может сфабриковать user_reply — это не закрыто в рамках {без кнопок}»).

Портировано с TypeScript-модуля gmail-mcp/src/tg_approval.ts — тот же бот
(@maksim_mcp_approval_bot), та же таблица `tg_approvals` в ОБЩЕМ Postgres,
который уже используют gmail/sheets/calendar/docs/drive-mcp. gmail-mcp
остаётся ЕДИНСТВЕННЫМ владельцем вебхука (`TG_WEBHOOK_OWNER=true` только
там) — ticktick-mcp никогда не регистрирует `setWebhook` и не поднимает
`/tg/webhook`. Решение по кнопке доходит сюда через ту же таблицу: gmail-mcp's
`consumeTgDecisionAnyServer` уже server-agnostic (manifest_id — глобальный
PRIMARY KEY), так что строка с `server='ticktick'` обрабатывается ИМ без
единой правки на его стороне.

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
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional
from zoneinfo import ZoneInfo

import requests

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


def load_tg_approval_config() -> TgApprovalConfig:
    enabled = os.environ.get("TG_APPROVAL_ENABLED", "").strip().lower() == "true"
    bot_token = os.environ.get("TG_BOT_TOKEN", "").strip()
    owner_chat_id = os.environ.get("TG_OWNER_CHAT_ID", "").strip()
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
            n for n, v in (("TG_BOT_TOKEN", bot_token), ("TG_OWNER_CHAT_ID", owner_chat_id)) if not v
        )
        raise RuntimeError(
            f"TG_APPROVAL_ENABLED=true, но не задано: {missing}. Либо задай оба, либо "
            "убери TG_APPROVAL_ENABLED, чтобы работать без этого слоя."
        )
    return TgApprovalConfig(
        enabled=enabled, bot_token=bot_token, owner_chat_id=owner_chat_id,
        server="ticktick", tools_allowlist=tools_allowlist, ttl_s=ttl_s,
        reports_chat_id=reports_chat_id, reap_enabled=reap_enabled,
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

_SEND_ATTEMPTS = 3


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
                         disable_notification: bool = False) -> tuple[bool, list[int], str]:
    """Доставляет текст любой длины: режет на куски, шлёт по одному, кнопки
    вешает только на ПОСЛЕДНЕЕ сообщение (иначе вебхук gmail-mcp снимет их не
    с того сообщения, а «активной» останется висеть кнопка на обрубке плана).

    Возвращает (ok, message_ids, error). ok=True — только если дошли ВСЕ куски;
    message_ids содержит id всех реально доставленных сообщений даже при
    ok=False, чтобы вызывающий мог прибрать за собой.

    Защита от главного silent-fail Telegram: при 400 «can't parse entities»
    (кривая разметка внутри текста) кусок повторяется БЕЗ parse_mode —
    plain-текстом исходного markdown. Лучше некрасивое сообщение, чем
    потерянное. При 429 — сон на retry_after и повтор."""
    chunks = split_for_telegram(md_text, TELEGRAM_TEXT_LIMIT)
    if not chunks:
        return False, [], "пустой текст — отправлять нечего"
    if len(chunks) > 1:
        # пересчитываем с местом под «(часть N/M)»
        chunks = split_for_telegram(md_text, TELEGRAM_TEXT_LIMIT - _PART_SUFFIX_RESERVE)
    total = len(chunks)

    message_ids: list[int] = []
    for idx, chunk in enumerate(chunks, start=1):
        is_last = idx == total
        html = md_to_telegram_html(chunk)
        plain = chunk
        if total > 1:
            html += f"\n\n<i>(часть {idx}/{total})</i>"
            plain += f"\n\n(часть {idx}/{total})"

        sent = None
        use_plain = False
        error = ""
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
                logger.warning(f"TG: 429 от Telegram, жду {wait_s}s и повторяю "
                               f"кусок {idx}/{total}")
                time.sleep(wait_s)
                continue
            if _is_parse_error(res) and not use_plain and attempt < _SEND_ATTEMPTS:
                logger.warning(f"TG: Telegram не распарсил HTML куска {idx}/{total} "
                               f"({error}) — повторяю без parse_mode, plain-текстом")
                use_plain = True
                continue
            break
        if sent is None:
            return False, message_ids, error
        mid = (sent.get("result") or {}).get("message_id")
        if mid is not None:
            message_ids.append(mid)
    return True, message_ids, ""


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


# ───────────────────────── Postgres (общий с 5 TS-серверами) ─────────────────

_pg_pool = None


def init_store(database_url: str) -> None:
    """Ленивая инициализация — вызывается один раз при старте, если
    TG_APPROVAL_ENABLED=true и задан CONSENT_DATABASE_URL. psycopg2 — тот же
    выбор, что и остальной синхронный стиль этого сервера (requests вместо
    httpx, никакого asyncio Postgres-драйвера не требовалось до сих пор)."""
    global _pg_pool
    import psycopg2.pool

    _pg_pool = psycopg2.pool.SimpleConnectionPool(1, 5, dsn=database_url, sslmode="require")
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
                           убрать за собой весь след манифеста."""
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
    gmail-mcp правит именно `message_id`, а reaper обязан прибрать все."""
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
    кнопке, чтобы знать, КУДА писать итог (report_auto_execution_result)."""
    conn = _pg_pool.getconn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT status, expires_at, chat_id, message_id FROM tg_approvals "
                "WHERE manifest_id = %s AND server = 'ticktick'",
                (manifest_id,),
            )
            row = cur.fetchone()
    finally:
        _pg_pool.putconn(conn)
    if not row:
        return None
    return {"status": row[0], "expires_at": row[1], "chat_id": row[2],
            "message_id": row[3]}


# ───────────────────────── Публичное API для server.py ─────────────────────

def notify_plan(cfg: TgApprovalConfig, manifest_id: str, preview_body: str,
                 tool: str) -> tuple[bool, str]:
    """Отправляет план в Telegram с кнопками [✅ Подтвердить][🛑 Отклонить],
    пишет строку в tg_approvals. Fail-closed по духу gmail-mcp: если это
    вернуло (False, ...), вызывающий код ОБЯЗАН инвалидировать манифест — тот
    же контракт, что requireConsent's notifyPlan в TS."""
    if not store_ready():
        return False, "Postgres для TG-approval не настроен (CONSENT_DATABASE_URL)"
    text = f"{preview_body}\n\n{tool} · ticktick"
    ok, message_ids, err = send_message_chunked(
        cfg, cfg.owner_chat_id, text,
        reply_markup_on_last={
            "inline_keyboard": [[
                {"text": "✅ Подтвердить", "callback_data": f"a:{manifest_id}"},
                {"text": "🛑 Отклонить", "callback_data": f"r:{manifest_id}"},
            ]]
        },
    )
    if not ok:
        # Обрубок плана без кнопок в чате опаснее, чем ничего: человек может
        # принять его за полный и «подтвердить» в чате то, чего не видел.
        # Поэтому уже доставленные куски убираем и честно проваливаем гейт.
        for mid in message_ids:
            delete_message(cfg, cfg.owner_chat_id, mid)
        return False, err or "Telegram sendMessage failed"
    # Кнопки — на ПОСЛЕДНЕМ сообщении, его id и есть «тот самый» message_id,
    # с которым работает вебхук gmail-mcp; предыдущие куски идут в extra_*.
    message_id = message_ids[-1] if message_ids else None
    extra_ids = message_ids[:-1]
    expires_at = _now_ms() + cfg.ttl_s * 1000
    create_tg_approval(manifest_id, cfg.owner_chat_id, message_id, expires_at, extra_ids)
    return True, ""


def check_approval(manifest_id: str) -> str:
    """"approved" | "pending" | "rejected" | "none" — "none" покрывает и
    «никогда не спрашивали», и «TTL истёк» (та же семантика, что checkApproval
    в TS — фаза исполнения обрабатывает оба случая одинаково)."""
    if not store_ready():
        return "none"
    row = get_tg_approval(manifest_id)
    if not row:
        return "none"
    if row["status"] == "APPROVED":
        return "approved"
    if row["status"] == "REJECTED":
        return "rejected"
    if _now_ms() > row["expires_at"]:
        return "none"
    return "pending"


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

# Метка вместо `user_reply` человека — честно отражает происхождение (кнопка,
# не текст), видна в аудит-логе/журнале мутаций. Специально НЕ похожа на
# утвердительное слово из _CONSENT_AFFIRMATIVE_WORDS — если этот текст
# случайно попадёт в _is_affirmative_reply() напрямую (например, по ошибке
# передадут в обычный _require_consent), он НЕ должен пройти как настоящее
# «да» человека.
TG_AUTO_REPLY_MARKER = "[авто: подтверждено кнопкой в Telegram]"


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
    if tool and m.get("_auto_tool", tool) != tool:
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

_VERDICT_HEADERS = {
    "ok": "✅ Исполнено и подтверждено",
    "partial": "⚠️ Исполнено частично",
    "failed": "🛑 Ошибка исполнения",
    "unverified": "⚠️ Исполнено, НО независимой перепроверкой не подтверждено",
}


def _owner_now_str() -> str:
    """Время глазами владельца — America/Los_Angeles, никогда UTC."""
    return datetime.now(_resolve_owner_tz()).strftime("%Y-%m-%d %H:%M:%S %Z")


def post_report_to_group(cfg: TgApprovalConfig, manifest_id: str, report_md: str,
                         *, tool: str, verdict: str) -> list[int]:
    """Публикует ПОЛНЫЙ отчёт об исполнении в группу-архив и запоминает в БД,
    куда он ушёл. Возвращает id доставленных сообщений ([] — не доставлено).

    НИКОГДА не бросает: мутация в TickTick к этому моменту уже произошла, и
    падение на отправке отчёта не должно превращаться в ошибку тула — иначе
    вызывающий решит, что действие не выполнено, и повторит его."""
    try:
        chat_id = cfg.reports_chat_id or cfg.owner_chat_id
        if not chat_id:
            logger.warning("TG: некуда публиковать отчёт — ни TG_REPORTS_CHAT_ID, "
                           "ни TG_OWNER_CHAT_ID не заданы")
            return []
        header = _VERDICT_HEADERS.get(verdict) or _VERDICT_HEADERS["unverified"]
        text = (f"### {header}\n"
                f"{tool} · `{manifest_id}` · {_owner_now_str()}\n\n"
                f"{report_md}")
        ok, message_ids, err = send_message_chunked(cfg, chat_id, text)
        if not ok:
            # Частичная доставка — не повод «забыть» id: их всё равно надо
            # записать, иначе reaper не найдёт эти сообщения.
            logger.warning(f"TG: отчёт по {manifest_id} доставлен не полностью "
                           f"({len(message_ids)} сообщ.): {err}")
        if message_ids:
            record_report_messages(manifest_id, chat_id, message_ids)
        return message_ids
    except Exception as e:  # noqa: BLE001 — отчёт best-effort по определению
        logger.warning(f"TG: не смог опубликовать отчёт по {manifest_id}: {e}")
        return []


def summarize_in_owner_chat(cfg: TgApprovalConfig, chat_id: str,
                            message_id: Optional[int], short_md: str) -> None:
    """Заменяет план в личке КОРОТКОЙ сводкой и снимает кнопки одним
    `editMessageText` (Telegram позволяет менять текст и reply_markup вместе).
    Текст сводки формирует вызывающий — здесь только транспорт.

    Best-effort: если человек стёр сообщение руками или оно старше суток —
    просто лог, исключений наружу нет (мутация уже произошла)."""
    if message_id is None:
        logger.warning(f"TG: message_id отсутствует, сводку некуда вписать "
                       f"(chat={chat_id})")
        return
    # Бюджет сужен на длину приписки «(сводка сокращена…)»: без этого запаса
    # кусок ровно в лимит + приписка давали текст ДЛИННЕЕ 4096, Telegram
    # отвечал 400, и сводка не появлялась вовсе — а кнопки на исполненном
    # плане оставались висеть. Тихая потеря сводки, которую видно только в
    # логах, — ровно тот silent-fail, ради которого всё это переписывалось.
    _CUT_NOTE = "\n\n_(сводка сокращена — полный отчёт в группе)_"
    chunks = split_for_telegram(short_md, TELEGRAM_TEXT_LIMIT - len(_CUT_NOTE) - 8)
    if not chunks:
        return
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


def report_auto_execution_result(cfg: TgApprovalConfig, chat_id: str,
                                  message_id: Optional[int], report_text: str) -> None:
    """DEPRECATED (2026-08-06): осталась только ради обратной совместимости с
    существующими вызовами в server.py. Раньше вписывала ВЕСЬ отчёт в то же
    сообщение с кнопками (и резала его на 3500 символах); теперь полный отчёт
    уходит в группу через post_report_to_group(), а сюда идёт короткая сводка.
    Новый код должен звать summarize_in_owner_chat() напрямую."""
    summarize_in_owner_chat(cfg, chat_id, message_id, report_text)


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
