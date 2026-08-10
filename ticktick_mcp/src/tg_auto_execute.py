"""Исполнение по кнопке в Telegram: реестр исполнителей, поиск кандидатов,
фоновый поллер (2026-08-09, пункт 1.2.4 захода 1).

Вынесено из `server.py` ДОСЛОВНО — строки 16125–18016 на ревизии
0b3c04c62c93 с учётом уже применённого коммита 1. Ни одного изменённого
символа; совпадение доказывается контрольной суммой (`tools/verify_move.sh`),
а не чтением.

Почему отдельным файлом. Это единственный путь, по которому сервер УДАЛЯЕТ
задачи владельца без участия модели: владелец жмёт кнопку в Telegram, поллер
находит одобренный манифест и зовёт исполнителя напрямую, минуя слой команд
MCP. Такой код должен читаться отдельно от девятнадцати тысяч строк вокруг.

Связь с `server.py` двусторонняя и разрешается позицией импорта: `server.py`
импортирует этот модуль на том месте, где раньше лежал сам кусок, — то есть
после того, как определены все имена, которые кусок берёт снаружи.
"""
import asyncio
import json
import os
import re
import time
from typing import Dict, List, Optional, Tuple

from . import manifest_store
from . import tg_approval
# Пространство имён главного файла целиком — НЕ для удобства, а
# потому что все 32 функции `_<тул>_impl` остались в нём. До 1.2.4
# generic-исполнитель искал их через `globals()`, и это работало,
# пока код лежал в одном файле с ними. После переезда `globals()`
# здесь — словарь ЭТОГО модуля: `_update_tasks_impl` в нём нет, и
# поиск вернул бы None для КАЖДОЙ из 30 команд, проходящих через
# `_gate_single`/`_gate_batch`. Симптом на живом сервере —
# «нажал кнопку, ничего не произошло», без единой ошибки в журнале.
# Поэтому обращение стало явным: `getattr(_server_module, ...)`.
# Через модуль, а не `from .server import ...`, — чтобы подмена
# `server._create_tasks_impl` в тестах доезжала сюда как раньше.
from . import server as _server_module
from .server import (  # noqa: E402,F401 — имена, которые кусок берёт снаружи
    _JOURNAL_DIR, _MANIFEST_TOMBSTONES, _MANIFESTS, _TG_AUTO_EXECUTE_MANIFEST,
    _TG_CFG, _TOMBSTONE_CLAIMED, _TOMBSTONE_EXECUTED, _TOMBSTONE_FAILED,
    _build_operation_report, _create_object_hash, _create_tasks_impl,
    _delete_project_impl, _execute_task_deletion_impl, _manifest_from_payload,
    _manifest_object_hash, _manifest_params_hash, _prune_manifests,
    _redact_for_user, _rename_tag_impl, _restore_manifests_from_db,
    _run_blocking, _tombstone_manifest, _tombstone_reason_for_verdict, logger,
    mcp, ticktick)


# === MOVED-BLOCK BEGIN (server.py @ 0b3c04c62c93171106a294f7090f366b05ea43e7 + коммит 1, lines 16125–18016) ===
# ---------------------------------------------------------------------------
# TG auto-execute: registry + candidate finder + background poller
# (2026-08-05) — Python port of gmail-mcp's autoExecute.ts/http.ts
# runAutoExecutePoller. See tg_approval.py's block comment above
# try_auto_execute() for the architectural difference (in-memory _MANIFESTS
# here vs. one shared Postgres on the TS side) and why the candidate search
# below cannot be one SQL JOIN: the manifests simply aren't in Postgres. It is
# still ONE round-trip per pass, not one per manifest — the RAM walk collects
# the live ids, and tg_approval.get_tg_approvals() reads all their approval
# rows in a single `WHERE manifest_id = ANY(...)` query (2026-08-06).
# ---------------------------------------------------------------------------

class _AutoExecutorEntry:
    __slots__ = ("rehash", "execute")

    def __init__(self, rehash, execute):
        self.rehash = rehash    # Callable[[Dict], str]
        self.execute = execute  # async Callable[[str, Dict], Awaitable[str]]


# Registry keyed by the SAME `tool` string used at the manifest's
# _maybe_tg_notify_plan(tool, ...) call site (which is also what
# TG_APPROVAL_TOOLS allowlists against) — NOT by manifest "kind" directly,
# though in practice each kind maps to exactly one tool today (see
# _AUTO_EXECUTE_TOOL_FOR_KIND below). Populated once at import time, mirroring
# gmail-mcp's autoExecute.ts registerAutoExecutor() module-level calls.
_AUTO_EXECUTORS: Dict[str, _AutoExecutorEntry] = {}


def _register_auto_executor(tool: str, rehash, execute) -> None:
    _AUTO_EXECUTORS[tool] = _AutoExecutorEntry(rehash, execute)


def _rehash_delete_manifest(m: Dict) -> str:
    ids = [it["taskId"] for it in (m.get("items") or [])]
    return _manifest_object_hash("delete", ids)


async def _auto_execute_delete_tasks(manifest_id: str, m: Dict) -> str:
    return await _execute_task_deletion_impl(manifest_id, m)


def _rehash_create_manifest(m: Dict) -> str:
    """Пересчёт binding-хэша манифеста создания при нажатии кнопки. Зовёт
    РОВНО ту же `_create_object_hash`, что и plan_task_creation, — не
    «повторяет формулу», а буквально ту же функцию, поэтому побайтовое
    совпадение гарантировано конструкцией, а не аккуратностью."""
    return _create_object_hash(m.get("raw") or [])


async def _auto_execute_create_tasks(manifest_id: str, m: Dict) -> str:
    # Страховка от чужого манифеста с таким же kind: этот исполнитель умеет
    # распаковывать ТОЛЬКО форму, которую кладёт plan_task_creation
    # (summary + raw). Манифест на этот момент уже погашен вызывающим, так
    # что «отказ» здесь — это отказ ИСПОЛНЯТЬ, а не тихое исполнение не того.
    if m.get("_gate") != "create" or "raw" not in m:
        return ("🛑 Автоисполнение отменено: манифест не в формате плана "
                "создания задач (kind=create, но нет raw/_gate) — ничего не "
                "создано. Построй план заново через plan_task_creation.")
    return await _create_tasks_impl(m.get("summary") or "", m.get("raw") or [])


def _rehash_delete_project_manifest(m: Dict) -> str:
    """Та же формула, что в фазе плана delete_project (см. её `object_hash`)."""
    return _manifest_object_hash("delete_project", [m.get("project_id") or ""])


async def _auto_execute_delete_project(manifest_id: str, m: Dict) -> str:
    """Исполнение плана удаления ПРОЕКТА по нажатой кнопке (2026-08-06).

    Содержимое проекта перечитывается ЗАНОВО: журнал должен отражать то, что
    удаляется сейчас, а не снимок часовой давности. Не смогли прочитать —
    отказ, а не удаление вслепую (та же дисциплина, что на чат-пути)."""
    project_id = m.get("project_id") or ""
    name = m.get("project_name") or ""
    if not project_id:
        return ("🛑 Автоисполнение отменено: в манифесте нет id проекта — "
                "ничего не удалено. Построй план заново.")
    try:
        data = await _run_blocking(lambda: ticktick.get_project_with_data(project_id))
    except Exception as e:
        return (f"🛑 Автоисполнение отменено: не смог прочитать содержимое "
                f"проекта «{name}» ({_redact_for_user(e)}) — не удаляю вслепую. "
                "Ничего не изменено, построй план заново.")
    if isinstance(data, dict) and data.get("error"):
        return (f"🛑 Автоисполнение отменено: не смог прочитать содержимое "
                f"проекта «{name}» ({_redact_for_user(data['error'])}) — не "
                "удаляю вслепую. Ничего не изменено, построй план заново.")
    return await _delete_project_impl(project_id, name,
                                      (data or {}).get("tasks") or [])


def _rehash_rename_tag_manifest(m: Dict) -> str:
    """Та же формула, что в merge-ветке rename_tag (её `object_hash`)."""
    return _manifest_object_hash(
        "rename_tag_merge",
        [(m.get("old_name") or "").lower(), (m.get("new_name") or "").lower()])


async def _auto_execute_rename_tag(manifest_id: str, m: Dict) -> str:
    """Исполнение СЛИЯНИЯ тегов по нажатой кнопке (2026-08-06). Кнопка
    существует только у merge-ветки — обычное переименование гейта не имеет и
    манифеста не заводит, поэтому `merged=True` здесь всегда верно."""
    old_name, new_name = m.get("old_name") or "", m.get("new_name") or ""
    if not (old_name and new_name):
        return ("🛑 Автоисполнение отменено: в манифесте нет пары имён тегов — "
                "ничего не изменено. Построй план заново.")
    return await _rename_tag_impl(old_name, new_name, merged=True)


_register_auto_executor("delete_tasks", _rehash_delete_manifest, _auto_execute_delete_tasks)
_register_auto_executor("create_tasks", _rehash_create_manifest, _auto_execute_create_tasks)
# 2026-08-06 (button-only): без этих двух регистраций нажатие кнопки на плане
# удаления ПРОЕКТА и на плане СЛИЯНИЯ тегов не приводило ни к чему — поллеру
# нечего было позвать, а с закрытым текстовым путём операция стала бы
# неисполнимой вообще. Их манифесты строятся своим путём (естественный ключ +
# `_find_live_inline_manifest`), поэтому generic-исполнитель их не подхватывает
# (он умеет только `_gate` in batch/single) — нужна явная регистрация.
_register_auto_executor("delete_project", _rehash_delete_project_manifest,
                        _auto_execute_delete_project)
_register_auto_executor("rename_tag", _rehash_rename_tag_manifest,
                        _auto_execute_rename_tag)
# resume_declutter deliberately has NO separate registry entry: every
# sheet-persist declutter plan is tagged tool="execute_declutter" at the ONE
# place its manifest is created (plan_declutter, via _maybe_tg_notify_plan) —
# resume_declutter reuses that SAME manifest_id/tg_approvals row later, it
# never creates its own. So a RAM-manifest candidate for a declutter plan is
# always found and dispatched under "execute_declutter" above, regardless of
# which of the two tools a human would eventually call. The one case this
# poller genuinely cannot reach is the scenario resume_declutter itself
# exists for — the RAM pointer is GONE (process restarted between plan and
# button-press): there is nothing in _MANIFESTS left to scan, by definition.
# That gap is structural (a RAM-only candidate list can't see state that was
# never durable), not a missing registration, and is called out in full in
# the handoff report rather than worked around here.

# Maps a manifest's "kind" to the tool name it was created/tagged under, for
# the ONE spot below that needs to go from "found a _MANIFESTS entry" to
# "which _AUTO_EXECUTORS key" without re-deriving it from scratch. Extend
# this (and _AUTO_EXECUTORS) together when a future kind gets TG wiring.
# NOTE: since 2026-08-06 this is only the FALLBACK — _gate_batch/_gate_single
# manifests carry their own "tool" key, so they need no entry here at all. The
# three kinds below are the ones whose manifests are built OUTSIDE those two
# gates (plan_task_deletion / plan_task_creation / plan_declutter), so they
# still resolve through this map; "create" additionally carries tool=
# "create_tasks" on the manifest itself, making this entry belt-and-braces.
_AUTO_EXECUTE_TOOL_FOR_KIND = {"delete": "delete_tasks", "declutter": "execute_declutter",
                               "create": "create_tasks"}


# ───────────────── generic executor for the _gate_batch/_gate_single tools ──
# 2026-08-06: instead of 19 hand-written _register_auto_executor() calls (one
# per gated tool, each a copy of the same two lines and each a chance to get
# the argument order wrong), ONE pair of functions covers every manifest that
# _gate_batch/_gate_single created. That is possible because those two gates
# store everything needed to replay the call: `tool` (→ the `_<tool>_impl`
# function that the gated tool itself calls on the chat path — the SAME
# executor, not a second implementation) and `_gate` (→ the call shape).

async def _generic_gate_auto_execute(manifest_id: str, m: Dict) -> str:
    """Replays a _gate_batch/_gate_single plan through the tool's own
    `_<tool>_impl`, exactly as the gated tool would have after a chat «да»."""
    tool = m.get("tool") or m.get("_tg_tool") or ""
    impl = getattr(_server_module, f"_{tool}_impl", None)
    if not callable(impl):
        raise RuntimeError(f"нет исполнителя _{tool}_impl для манифеста {manifest_id}")
    if m.get("_gate") == "single":
        return await impl(**(m.get("params") or {}))
    return await impl(m.get("summary") or "", m.get("tasks") or [],
                      **(m.get("extra") or {}))


def _generic_gate_rehash(m: Dict) -> str:
    """MUST reproduce, byte for byte, the formula the hash was computed with
    at plan time (_gate_batch: _manifest_object_hash(kind, ids) over the same
    id-extraction expression; _gate_single: _manifest_params_hash(kind,
    params)) — a mismatch here silently disables the binding check, i.e. the
    button's protection against the plan drifting between show and press."""
    if m.get("_gate") == "single":
        return _manifest_params_hash(m.get("kind") or "", m.get("params") or {})
    ids = [str(t.get("taskId") or t.get("task_id") or "") for t in (m.get("tasks") or [])]
    return _manifest_object_hash(m.get("kind") or "", ids)


_GENERIC_GATE_ENTRY = _AutoExecutorEntry(_generic_gate_rehash,
                                         _generic_gate_auto_execute)


def _tool_registration_status(tool: str) -> str:
    """Источник истины о том, жив ли инструмент `tool` СЕЙЧАС (2026-08-09):
    реестр самого FastMCP (`mcp._tool_manager`), а не globals(). Отключение
    тула здесь делается комментированием ОДНОГО декоратора `@mcp.tool()`
    (см. плашки «DISABLED» у plan_declutter/execute_declutter/
    resume_declutter/set_declutter_decision выше) — сама функция и её `_impl`
    остаются в модуле нетронутыми, поэтому проверка `globals().get(...)` их
    по-прежнему находит и НИЧЕГО не говорит о том, отключён ли тул. Прямой
    `mcp._tool_manager.get_tool(...)` — синхронный (в отличие от публичного
    async `mcp.list_tools()`, которым пользуется tests/test_tool_registry.py)
    и не требует await из этой синхронной функции.

    Возвращает одну из трёх строк:
      "registered" — тул в реестре есть, исполнять можно как раньше;
      "disabled"   — тула в реестре нет (или имя пустое) — обычный,
                     ожидаемый случай отключённого/удалённого тула;
      "unknown"    — САМА ПРОВЕРКА упала (правка 2026-08-09, независимый
                     аудит нашёл дефект в первой версии этого фикса:
                     `_tool_manager` — приватный, без подчёркивания в имени
                     не гарантированный атрибут FastMCP, и `get_tool` теми же
                     основаниями может бросить при будущем обновлении
                     библиотеки). Различие "disabled" / "unknown" важно для
                     текста отказа ниже: «тул отключён» — это решение
                     владельца, а «проверка упала» — поломка сервера, и
                     звучать они обязаны по-разному. В обоих случаях —
                     fail-closed (план НЕ исполняется), но `unknown`
                     дополнительно уходит в лог с трассировкой, потому что
                     это баг, а не ожидаемое состояние."""
    if not tool:
        return "disabled"
    try:
        found = mcp._tool_manager.get_tool(tool) is not None
    except Exception:
        logger.exception(
            f"_tool_registration_status: проверка регистрации «{tool}» в "
            "реестре FastMCP упала — считаю тул НЕисполнимым (fail-closed), "
            "но это отдельная от «тул отключён» причина")
        return "unknown"
    return "registered" if found else "disabled"


def _auto_execute_tool_disabled_refusal(tool: str, reason: str):
    """Фабрика исполнителя-отказника для `_resolve_auto_executor`: вместо
    того чтобы молча вернуть None (кандидат просто выпал бы из очереди без
    единого слова владельцу — см. `continue` в `_tg_auto_execute_tick`),
    исполняет по ТОЙ ЖЕ схеме, что и обычный отказ (`_auto_execute_rename_tag`
    выше, начинается с 🛑) — а значит проходит весь штатный конвейер:
    `_auto_execute_report_is_failure` увидит 🛑, надгробие ляжет как
    «нажато, но не выполнено», и текст уйдёт владельцу в Telegram, а не в
    лог, который никто не читает в моменте.

    `reason` — "disabled" или "unknown" из `_tool_registration_status`; текст
    сознательно различается (см. её докстринг): владелец не должен читать
    сбой самой проверки как «я же его выключил»."""
    async def _refuse(manifest_id: str, m: Dict) -> str:
        if reason == "unknown":
            return (f"🛑 Автоисполнение отменено: не удалось проверить, "
                    f"зарегистрирован ли инструмент «{tool}» в сервере — "
                    "упала сама проверка реестра (см. лог с трассировкой), "
                    "а не решение владельца отключить инструмент — план "
                    "исполнен не будет. Это техническая проблема сервера, "
                    "которую нужно чинить, а не декоратор, который нужно "
                    "включать обратно.")
        return (f"🛑 Автоисполнение отменено: инструмент «{tool}» в сервере "
                "отсутствует (отключён или удалён) — план исполнен не "
                "будет. Если функция должна быть доступна, инструмент нужно "
                "сперва вернуть в реестр (раскомментировать @mcp.tool()).")
    return _refuse


def _resolve_auto_executor(tool: str, m: Dict) -> Optional[_AutoExecutorEntry]:
    """Which executor runs this manifest: an explicitly registered one wins
    (delete_tasks today — its manifest shape predates the shared gates), the
    generic gate executor covers anything _gate_batch/_gate_single produced,
    and everything else returns None (candidate skipped, nothing happens).
    Deliberately does NOT resolve declutter: its registration stays commented
    out above, and a declutter manifest has no `_gate` key, so it falls
    through to None here too — both layers stay disabled together.

    2026-08-09: раньше отсюда не проверялось НИЧЕГО, кроме факта, что где-то
    в модуле есть подходящая функция (`_AUTO_EXECUTORS` — регистрация в
    отдельном словаре; generic-путь — просто `globals()`). Ни то, ни другое
    не связано с тем, зарегистрирован ли тул в MCP-сервере СЕЙЧАС: отключение
    комментированием ОДНОГО декоратора `@mcp.tool()` не трогает ни функцию,
    ни (для generic-пути) `_<tool>_impl`, ни (для explicit-пути) саму запись
    в `_AUTO_EXECUTORS`, если её кто-то забыл закомментировать вместе с
    декоратором. ВАЖНО: для declutter эта дыра сегодня НЕ активна — двойная
    защита совпала независимо (регистрация в `_register_auto_executor`
    закомментирована ТОЖЕ, а его манифест вообще не содержит ключа `_gate`,
    так что generic-путь по нему не сработал бы, даже будь регистрация жива)
    — так что до этой правки `_resolve_auto_executor("execute_declutter", …)`
    и так возвращал None всегда. Ценность правки — не в закрытии дыры,
    которой сейчас нет, а в защите от ТОЙ ЖЕ самой забывчивости в будущем:
    для ЛЮБОГО тула, у которого декоратор сняли, а explicit-регистрацию или
    `_impl` — забыли. Раз нашёлся исполнитель-кандидат (explicit или
    generic), но `tool` не зарегистрирован — не отдаём его как есть:
    подменяем на явный отказ (`_auto_execute_tool_disabled_refusal`), который
    объясняет причину владельцу, а не просто исчезает молча."""
    if not tool:
        return None
    entry = _AUTO_EXECUTORS.get(tool)
    if entry is None and m.get("_gate") in ("batch", "single") and callable(
            getattr(_server_module, f"_{tool}_impl", None)):
        entry = _GENERIC_GATE_ENTRY
    if entry is None:
        return None
    status = _tool_registration_status(tool)
    if status == "registered":
        return entry
    return _AutoExecutorEntry(entry.rehash,
                              _auto_execute_tool_disabled_refusal(tool, status))


def _auto_execute_tool_of(m: Dict) -> str:
    """Tool name a live manifest belongs to: the TG notification's own tag
    first (set by _maybe_tg_notify_plan, the one place a button ever appears),
    then the gate's stored `tool`, then the legacy kind→tool map."""
    return (m.get("_tg_tool") or m.get("tool")
            or _AUTO_EXECUTE_TOOL_FOR_KIND.get(m.get("kind") or "") or "")


def _tg_auto_execute_pending() -> List[tuple]:
    """RAM-часть поиска кандидатов: живые (не consumed, не просроченные)
    манифесты, у которых есть авто-исполнитель и включён TG-гейт. НИ ОДНОГО
    обращения к базе и к сети — специально, потому что это единственный кусок,
    который трогает `_MANIFESTS`, и он обязан выполняться в event loop'е
    (иначе `_prune_manifests()` итерировал бы dict, который другая корутина
    в это же время пополняет → RuntimeError: dictionary changed size).
    Возвращает [(manifest_id, tool), …]."""
    _prune_manifests()
    out: List[tuple] = []
    for mid, m in list(_MANIFESTS.items()):
        tool = _auto_executable_tool(m)
        if tool:
            out.append((mid, tool))
    return out


def _auto_executable_tool(m: Optional[Dict]) -> str:
    """Имя тула, если этот живой план сервер УМЕЕТ исполнить по кнопке сам, и
    пустая строка иначе. Один предикат на два места: обычный обход памяти
    (`_tg_auto_execute_pending`) и планы, только что поднятые из базы после
    перезапуска, — разойдись эти два отбора, восстановленный план молча не
    попал бы в кандидаты."""
    if not m or m.get("consumed"):
        return ""
    tool = _auto_execute_tool_of(m)
    if _resolve_auto_executor(tool, m) is None:
        return ""
    if not tg_approval.enabled_for(_TG_CFG, tool):
        return ""
    return tool


def _tg_auto_execute_approved(pending: List[tuple],
                              rows: Dict[str, dict]) -> List[Dict]:
    """Чистая (без БД и сети) вторая половина поиска: из списка живых
    манифестов и УЖЕ прочитанных пачкой строк tg_approvals оставляет те, что
    подтверждены кнопкой. Статус считает tg_approval.approval_status_of —
    та же формула, что у одиночного check_approval на чат-пути."""
    out: List[Dict] = []
    for mid, tool in pending:
        row = rows.get(mid)
        if not row or tg_approval.approval_status_of(row) != "approved":
            continue
        out.append({"manifest_id": mid, "tool": tool,
                    "chat_id": row.get("chat_id"),
                    "message_id": row.get("message_id"),
                    # Предыдущие куски длинного плана. Нужны, чтобы после
                    # исполнения убрать их из лички: наш reaper строки
                    # APPROVED не трогает (архив решения), а уборщик gmail-mcp
                    # знает только про message_id — без этого куски 1..N-1
                    # оставались бы в личном чате навсегда.
                    "extra_message_ids": list(row.get("extra_message_ids") or [])})
    return out


def _find_tg_auto_execute_candidates() -> List[Dict]:
    """Candidates for auto-execution: this server's own (in-memory) manifests
    that are AWAITING_CONSENT-equivalent (not consumed, not TTL-expired —
    _prune_manifests already dropped anything past that) that resolve to an
    executor (_resolve_auto_executor: explicit registry, else the generic
    _gate_batch/_gate_single replay) AND whose Telegram approval row is
    already APPROVED.

    ОДНО обращение к Postgres на весь проход, сколько бы живых планов ни было
    (2026-08-06). До этого на КАЖДЫЙ живой манифест шли check_approval() и,
    для одобренных, ещё get_tg_approval() — то есть 1–2 поездки на план. С 2
    гейтованными тулами планов было единицы; с 22 их десятки, база ходит через
    публичный прокси Railway, и проход переставал укладываться в 10-секундный
    интервал — подтверждённое кнопкой действие ждало исполнения минутами.

    Синхронная версия (её зовут тесты и любой не-async вызывающий); поллер
    использует те же две половины напрямую, чтобы поездку в базу увести в
    отдельный поток и не морозить event loop (см. _tg_auto_execute_tick)."""
    pending = _tg_auto_execute_pending()
    if not pending:
        return []
    try:
        rows = tg_approval.get_tg_approvals([mid for mid, _ in pending])
    except Exception as e:
        logger.warning(f"TG auto-execute: get_tg_approvals failed: {e}")
        return []
    return _tg_auto_execute_approved(pending, rows)


# Потолок на ОДНОГО кандидата: без него один зависший сетевой вызов к TickTick
# держит очередь остальных подтверждённых планов бесконечно (манифест уже
# погашен, повтора не будет — а человек так и сидит перед несработавшей
# кнопкой). Щедрый по умолчанию: цель — не оборвать нормальную работу, а
# гарантировать, что очередь ВСЕГДА двигается.
_TG_AUTO_EXECUTE_CANDIDATE_TIMEOUT_S = float(
    os.environ.get("TG_AUTO_EXECUTE_CANDIDATE_TIMEOUT_S", "120"))


# Маркеры, которыми исполнители этого сервера НАЧИНАЮТ отчёт, когда операция
# НЕ состоялась: «🛑» — отказ до мутации («Автоисполнение отменено…», «НЕ
# переместил…»), «❌» — мутация ушла, но пост-проверка показала, что её нет.
# Оба означают одно: писать «исполнено» нельзя. Проверяется ТОЛЬКО начало
# отчёта (после возможных «### » заголовка), потому что внутри успешного
# многострочного отчёта «❌» может стоять у отдельного элемента пачки — это
# частичный результат, а не провал целиком.
#
# СООТНОШЕНИЕ С НЕЗАВИСИМОЙ ПЕРЕПРОВЕРКОЙ НИЖЕ (слияние двух веток,
# 2026-08-06). Это НЕ дубль `_EXEC_FAILURE_MARKERS`, и одно другого не
# заменяет: там маркеры ищутся ГДЕ УГОДНО в самоотчёте и лишь участвуют в
# вычислении вердикта, здесь — строго в НАЧАЛЕ отчёта, то есть это заявление
# самого исполнителя «я не сделал ничего». Надгробие манифеста ставится по
# СТРОЖАЙШЕМУ из двух сигналов (см. `_tg_auto_execute_tick`): «выполнено»
# требует и вердикта "ok" от независимого чтения, и отсутствия такого
# заявления.
_AUTO_EXECUTE_FAILURE_MARKS = ("🛑", "❌")


def _first_line(text: str, cap: int = 200) -> str:
    line = (text or "").strip().split("\n", 1)[0].strip()
    return line if len(line) <= cap else line[:cap] + "…"


def _auto_execute_report_is_failure(report_text: str) -> bool:
    """Вернул ли авто-исполнитель ОТКАЗ строкой (без исключения). Нужно ровно
    для одного решения: какое надгробие ставить — «выполнено» или «нажато, но
    не выполнено»."""
    head = (report_text or "").lstrip().lstrip("#").lstrip()
    return head.startswith(_AUTO_EXECUTE_FAILURE_MARKS)


def _auto_execute_report_is_success(report_text: str) -> bool:
    """Симметрично `_auto_execute_report_is_failure` выше: начинается ли
    голова СОБСТВЕННОГО отчёта исполнителя с ✅ — единственного статус-
    маркера успеха в замороженной эмодзи-легенде (output-format.md §7.2).

    Нужна для `_verified_auto_execute_report` (дефект 2026-08-06 №1):
    single-gate исполнители (update_project, create_tag, create_habit и
    ~40 таких же) не пишут в журнал мутаций (журналируются только операции
    над задачами, см. `_op_journal`), поэтому независимая перепроверка ниже
    для них СТРУКТУРНО никогда не найдёт запись. Но по стандарту КАЖДЫЙ из
    них уже обязан делать свой собственный post-verify — отдельным свежим
    чтением TickTick — и печатает ✅ ТОЛЬКО когда этот post-verify реально
    подтвердил результат. Голова его отчёта — уже готовое доказательство,
    просто не через журнал.

    ДЕФЕКТ 2026-08-06 №3 (найден живым прогоном на create_project_group,
    манифест ea79556baf0f — группа реально создалась, но пришло «❓ НЕ
    подтверждено»): проверка «starts with ✅» сама по себе честная, но
    ~7 из ~21 single-gate исполнителей возвращали успешный self-report БЕЗ
    ведущего ✅ (кто-то без эмодзи вовсе, кто-то с забытым ASCII «✓», который
    легенда прямо запрещает — output-format.md §7.2), поэтому эта функция
    ЧЕСТНО говорила "не доказано" — по факту, а не по ошибке распознавания.
    Полный список найденных и исправленных: create_project_group,
    delete_project_group, move_project_to_group, rename_tag (обе ветки),
    update_task_comment, delete_task_comment, attach_file_to_task (плюс
    ASCII «✓» → ✅ у unset_task_parent, не влиявший на вердикт — та операция
    журналируется). Все приведены к единому ведущему ✅ у своих impl-функций
    (server.py, см. `_*_impl` соответствующих тулов) — это и есть исправление,
    НЕ правка этой функции.

    Сознательно НЕ исправлено расширением этой функции (например, поиском
    подстроки «(проверено)» где угодно в тексте): такой поиск был бы менее
    строгим, чем ведущий маркер, И ЛОВУШКА — строка отказа «название НЕ
    проверено» САМА содержит подстроку «проверено» (без «НЕ» уже входит в
    «НЕ проверено»), так что подстрочный поиск дал бы ЛОЖНЫЙ ✅ ровно на
    предупреждении, для которого честный ответ — «не доказано». Явный
    положительный признак (ведущий ✅) без исключений — единственная
    проверка, которая не наступает на эти грабли; единообразие достигается
    на стороне текстов-источников, а не ослаблением детектора. См. также
    tests/test_tier0_gate_conversion.py, tests/test_gate_delete_tag_comment.py,
    tests/test_button_only_execution.py — happy-path каждого из
    перечисленных методов теперь проверяет именно
    `_auto_execute_report_is_success()`, а не только отсутствие ❌/🛑."""
    head = (report_text or "").lstrip().lstrip("#").lstrip()
    return head.startswith("✅")



# ---------------------------------------------------------------------------
# Честная пост-верификация автоисполнения (2026-08-06)
# ---------------------------------------------------------------------------
# ПОЧЕМУ это вообще появилось: до этой правки поллер брал ровно то, что вернул
# исполнитель (`entry.execute(...)`), и публиковал как ИТОГ. То есть словом
# «успешно» распоряжался тот же код, который мутацию и делал, — судья и
# подсудимый в одном лице. Теперь после исполнения обязательно делается
# НЕЗАВИСИМАЯ перепроверка живым чтением (`_build_operation_report`, который
# читает журнал мутаций и заново тянет состояние TickTick через
# `_open_by_id(fresh=True)`), и вердикт выносится ПО ЕЁ ФАКТАМ, а не по
# самоотчёту исполнителя.
#
# Ключевой принцип (Максим, пункт 3 ТЗ): «не удалось доказать» ≠ «получилось».
# Поэтому вердиктов ПЯТЬ, а не два: помимо ok/failed есть "partial" (часть
# подтвердилась, часть нет), "mismatch" (см. ниже) и "unverified" (мутация,
# возможно, прошла, но мы этого НЕ ДОКАЗАЛИ — журнал пуст, живое чтение
# недоступно, формат итога не распознан, исключение). "unverified" НИКОГДА не
# выдаётся за "ok".
#
# ВТОРОЙ ДЕФЕКТ (2026-08-06, найден живым прогоном на update_project): у
# БОЛЬШИНСТВА гейтованных методов (проект/тег/группа/привычка — всё, что
# гейтуется через `_gate_single`, а не через списочный `_gate_batch`) НЕТ
# записи в журнал мутаций вообще — журналируются только операции над задачами
# (`_op_journal`). Для них независимая перепроверка ниже СТРУКТУРНО никогда
# не найдёт запись, и старое правило («нет журнала → unverified») давало
# «❓ не подтверждено» на КАЖДОЙ успешной операции этого класса, хотя сам
# исполнитель уже доказал результат собственным (обязательным по стандарту)
# post-verify. Раз журнала нет, а голова самоотчёта исполнителя — ✅ (реальное
# доказательство, не хвастовство — см. `_auto_execute_report_is_success`),
# верим ЕЙ, а не молчанию журнала: verdict="ok" с честной оговоркой в
# основании («журнал недоступен, но эффект подтверждён отдельным чтением»).
# Если же голова самоотчёта — ❌ (тот же обязательный post-verify нашёл
# расхождение), это НАСТОЯЩИЙ провал — verdict="mismatch", а не "unverified":
# путать «не доказано» и «доказано, что не сработало» — та же нечестность в
# другую сторону.
#
# ЧЕСТНОЕ ОГРАНИЧЕНИЕ: `_build_operation_report` умеет выносить вердикт не для
# всех типов операций. Для незнакомого `op` его построчный `_verify_item()`
# пишет «тип … не проверяется автоматически» со статусом "warn" — такие строки
# попадают в СРЕДНИЙ счётчик итоговой строки («⚠️ N не проверено»), а не в ✅.
# Здесь это трактуется как "unverified"/"partial" (подтверждать было нечем),
# но НИКОГДА как "ok" — см. `_verdict_from_totals`.

# Итоговая строка независимого отчёта: «**Итог: ✅ 3 подтверждено, ⚠️ 1 не
# проверено, ❌ 1 расхождений.**». Разбираем регуляркой, но НЕ доверяем ей
# слепо: если формат поменяется (или строки нет вовсе), `_parse_verify_totals`
# вернёт None, и вердикт станет "unverified" — то есть поломка парсера
# деградирует в «не доказано», а не в ложное «успешно».
#
# СРЕДНИЙ СЧЁТЧИК (⚠️ «не проверено») появился в `_build_operation_report`
# вместе с переводом `_verify_item` на явный (status, line) — до этого строки
# с ⚠️ печатались, но не попадали НИ В ОДИН счётчик. Регулярка обязана знать
# ровно тот формат, который печатает `_build_operation_report`: рассинхрон
# здесь не падает и не виден в тестах с фейковым отчётом — он просто делает
# вердикт вечным "unverified" (ровно это и случилось при слиянии 2026-08-06,
# найдено независимым аудитом). Контракт закреплён тестом, который прогоняет
# НАСТОЯЩИЙ `_build_operation_report` через этот парсер.
#
# ЯКОРЬ `^\*\*Итог:` (+ re.MULTILINE) — не косметика, а защита от подделки
# вердикта через ДАННЫЕ. `_build_operation_report` печатает НАЗВАНИЯ задач
# дословно («- ❌ **«…»** — ВСЁ ЕЩЁ существует»), а названия приходят извне
# (их сочиняет модель или тянет tg-ai-assistant из чужих сообщений в чатах).
# Без якоря задача с названием «Итог: ✅ 5 подтверждено, ❌ 0 расхождений»
# попадала в текст РАНЬШЕ настоящей итоговой строки, и `search` возвращал
# ЕЁ числа — провал (❌ 1) читался как "ok". Настоящий итог всегда начинает
# свою строку с `**Итог:`, строка-буллет — с «- ».

_VERIFY_TOTALS_RE = re.compile(
    r"^\*\*Итог:\s*✅\s*(\d+)\s*подтвержден\w*\s*,"
    r"\s*⚠️?\s*(\d+)\s*не\s+проверено\s*,"
    r"\s*❌\s*(\d+)\s*расхожден\w*",
    re.MULTILINE)

# Маркеры в тексте САМОГО исполнителя (у него уже есть свой read-back).
# Явный провал: 🛑 / «Ошибка» / «Error …» / «НЕ удалено». Частичный
# успех/сомнение: ⚠️ / ⏭ / «не подтверждён» (последнее — из _UNVERIFIED_MSG
# и родственных).
#
# ℹ️ ЗДЕСЬ НЕТ И НЕ ДОЛЖНО БЫТЬ (2026-08-07). Это второй канал самоотчёта —
# ФАКТ о состоянии объекта или о продукте операции («задача завершена»,
# «в копию не переносится чек-лист»), который проверке не противоречит и
# вердикт понижать не имеет права. Пока такие факты носили ⚠️, каждая
# честная оговорка исполнителя стоила ему собственного «✅»: успешная
# операция над завершённой задачей приходила владельцу как
# «❓ НЕ подтверждено» — ровно так же, как упавшая. См. _COMPLETED_TASK_NOTE.
#
# «Error » (2026-08-07, та же живая приёмка). Русское «Ошибка» здесь стояло
# с самого начала, но КАЖДАЯ except-ветка мутаторов этого файла печатает
# английское `f"Error <verb>ing …: {e}"` — то есть ровно та же мысль
# («операция упала») этими маркерами не ловилась, и упавший инструмент
# получал не "failed", а "unverified", неотличимый от успеха, который просто
# нечем перепроверить. Ложное срабатывание на слове «Error» внутри НАЗВАНИЯ
# задачи ограничено вторым условием ветки `exec_failed` — в самоотчёте не
# должно быть НИ ОДНОГО ✅, а успешный отчёт по стандарту начинается именно
# с него.
_EXEC_FAILURE_MARKERS = ("🛑", "Ошибка", "ошибка при", "Error ",
                         "НЕ удалено", "не удалено")
_EXEC_WARN_MARKERS = ("⚠️", "⚠", "⏭", "не подтверждён", "не подтверждена",
                      "НЕ ПОДТВЕРЖДЁН", "не подтвердилось")

# Признаки того, что независимый отчёт НЕ является настоящей перепроверкой
# (журнала нет / записей нет / живое состояние недоступно / внутренняя
# ошибка). Все они = "unverified": мутация могла пройти, но мы не смотрели.
#
# Фразы намеренно взяты ДЛИННЫМИ и дословными из `_build_operation_report`:
# короткое «невозможен»/«нет записей» могло бы случайно совпасть с НАЗВАНИЕМ
# задачи внутри отчёта и превратить настоящее подтверждение в «не доказано».
# (Ошибка в эту сторону безопасна, но врать в другую сторону тоже не надо.)
_REPORT_UNUSABLE_MARKERS = ("Журнал не найден", "В журнале нет записей по",
                            "Живое состояние TickTick недоступно",
                            "Error building operation report")

# Страховочные признаки того, что перепроверка ЧАСТИЧНО ничего не доказала.
# Основной механизм теперь структурный — средний счётчик ⚠️ «не проверено» в
# итоговой строке (`_verify_item` возвращает явный status="warn"), поэтому
# эмодзи ⚠️/⚠ ЗДЕСЬ БОЛЬШЕ НЕТ: с трёхсчётчиковым форматом итоговая строка
# содержит «⚠️ 0 не проверено» ВСЕГДА, и маркер-эмодзи срабатывал бы на каждом
# отчёте, вечно понижая честное "ok" до "partial". Остались только текстовые
# формулировки — на случай, если какая-то ветка отчёта напишет их, минуя
# счётчик. Ложное срабатывание (фраза внутри НАЗВАНИЯ задачи) даёт "partial"
# вместо "ok" — ошибка в безопасную сторону, как и в _REPORT_UNUSABLE_MARKERS.
_REPORT_DOUBT_MARKERS = ("НЕ ПОДТВЕРЖДЁН", "не проверяется автоматически")

# Исполнитель (`_execute_task_deletion_impl` и родня) УЖЕ вклеивает
# независимый отчёт в конец СВОЕГО текста — «чтобы модель не могла его
# пропустить». Ниже тот же отчёт печатается отдельным разделом целиком, и без
# вырезания самая длинная часть сообщения уезжала в группу ДВАЖДЫ: на живом
# прогоне 2026-08-06 отчёт по 200 задачам занимал 13 сообщений вместо 9, а
# Telegram уже на 13-м подряд отвечает 429 «retry after 37» — то есть дубль
# бил не по красоте, а по доставке. Второй экземпляр вдобавок тащил в
# группу-архив инструкцию «[агенту: перепечатай…]», которой там некому
# следовать.
#
# Обрезанный текст используется и для показа, и для поиска маркеров
# исполнителя (_EXEC_FAILURE_MARKERS/_EXEC_WARN_MARKERS): самоотчёт обязан
# оцениваться по СВОИМ словам, а не по вклеенному в него отчёту — иначе «⚠️»
# и «✅» из отчёта подменяют оценку исполнителя (см. _verified_auto_execute_
# report, шаг 2).
_EXEC_TRAILING_REPORT_RE = re.compile(
    r"\n*(?:###\s*)?🧾 (?:Независимый отчёт|Отчёт по|Журнал не найден|"
    r"В журнале нет записей)[\s\S]*$")


def _strip_trailing_independent_report(text: str) -> str:
    """Убирает хвостовой блок независимого отчёта из текста исполнителя. Если
    после вырезания не осталось ничего осмысленного (весь вывод и БЫЛ этим
    отчётом), возвращает исходный текст — лучше дубль, чем пустой раздел."""
    stripped = _EXEC_TRAILING_REPORT_RE.sub("", text or "").rstrip()
    return stripped if stripped.strip() else text


# "mismatch" — НОВЫЙ пятый вердикт (2026-08-06, дефект №1, пункт 2 ТЗ
# Максима): собственный post-verify исполнителя (не журнал — журнала для
# этого тула нет) нашёл расхождение. Эмодзи ❌, а не 🛑 — по замороженной
# легенде (output-format.md §7.2) 🛑 значит «ничего не изменено», а ❌ значит
# «не получилось, либо пруф показал несоответствие»: мутация здесь РЕАЛЬНО
# происходила, просто не сошлась с ожиданием — это не то же самое, что отказ
# ДО мутации ("failed", когда исполнитель сам написал «🛑 Автоисполнение
# отменено…» и ничего не менял).
_VERDICT_EMOJI = {"ok": "✅", "partial": "⚠️", "mismatch": "❌",
                  "failed": "🛑", "unverified": "❓"}
_VERDICT_WORD = {"ok": "подтверждено живым чтением",
                 "partial": "подтверждено частично",
                 "mismatch": "расхождение с ожидаемым результатом",
                 "failed": "не выполнено",
                 "unverified": "НЕ подтверждено (проверить не удалось)"}


def _parse_verify_totals(text: str) -> Optional[Tuple[int, int, int]]:
    """(подтверждено, не проверено, расхождений) из итоговой строки
    независимого отчёта, либо None, если строку не нашли/не распознали.
    Вынесено отдельной чистой функцией специально ради тестируемости без сети
    и БД.

    Итоговая строка обязана быть РОВНО ОДНА: ноль — формат сменился или отчёт
    не тот; больше одной — текст неоднозначен (склеенные отчёты, подделка
    через данные), и какую из них считать «настоящей», мы не знаем. Оба
    случая = None, то есть "unverified" — а не догадка в пользу успеха."""
    if not text:
        return None
    found = _VERIFY_TOTALS_RE.findall(text)
    if len(found) != 1:
        return None
    try:
        return int(found[0][0]), int(found[0][1]), int(found[0][2])
    except (TypeError, ValueError):  # pragma: no cover — регулярка даёт цифры
        return None


def _verdict_from_totals(totals: Optional[Tuple[int, int, int]]) -> str:
    """Вердикт ТОЛЬКО по фактам независимой перепроверки.

    0/0/0 — это не успех: подтверждать было нечего (пустой набор), поэтому
    "unverified". >0 расхождений при 0 подтверждённых — операция провалилась
    целиком. Смешанный итог — "partial".

    Средний счётчик (⚠️ «не проверено») — это строки, по которым живое чтение
    исхода НЕ доказало: незнакомый движку тип операции, «восстановлена, но не
    в тот список», «не смогли перечитать проекты». Они не расхождения, но и не
    подтверждения, поэтому: есть хоть одно подтверждённое рядом → "partial";
    не подтверждено вообще ничего → "unverified" (это строже, чем "partial").

    "ok" выдаётся единственным сочетанием: расхождений 0, непроверенных 0 И
    подтверждённых больше нуля."""
    if totals is None:
        return "unverified"
    ok_n, warn_n, bad_n = totals
    if ok_n == 0 and warn_n == 0 and bad_n == 0:
        return "unverified"
    if bad_n > 0 and ok_n == 0:
        return "failed"
    if bad_n > 0:
        return "partial"
    if warn_n > 0:
        return "partial" if ok_n > 0 else "unverified"
    return "ok"


def _verified_auto_execute_report(manifest_id: str, tool: str,
                                  exec_output: str) -> Tuple[str, str]:
    """Возвращает (полный_markdown_отчёта, verdict).

    verdict ∈ {"ok", "partial", "mismatch", "failed", "unverified"} — см.
    блок-комментарий выше. Ничего не обрезает: длину теперь держит чанкинг в
    tg_approval (`send_message_chunked`), а не молчаливое обрезание отчёта по
    4096."""
    exec_output = exec_output or ""

    # 1. Независимая перепроверка. Сама `_build_operation_report` ловит свои
    #    исключения и возвращает текст, но монкейпатч/будущая правка может и
    #    бросить — ловим, чтобы автоисполнение никогда не падало на этапе
    #    «рассказать, что получилось».
    independent: Optional[str] = None
    independent_err: Optional[str] = None
    try:
        independent = _build_operation_report(manifest_id)
    except Exception as e:
        independent_err = _redact_for_user(e)
        logger.exception(f"TG auto-execute: независимая перепроверка "
                     f"{manifest_id} упала: {e}")

    report_usable = bool(independent) and not any(
        mark in independent for mark in _REPORT_UNUSABLE_MARKERS)
    totals = _parse_verify_totals(independent) if report_usable else None

    # 2. Явный провал по самоотчёту исполнителя перебивает всё: если он сам
    #    написал «🛑 / Ошибка / НЕ удалено» и при этом НИ ОДНОГО ✅ — это
    #    провал, независимо от того, что покажет журнал.
    #
    #    Маркеры ищутся в САМООТЧЁТЕ, то есть в тексте БЕЗ хвостового
    #    независимого отчёта, который часть исполнителей вклеивает в свой
    #    вывод (`_execute_task_deletion_impl` и родня). Иначе оценка
    #    исполнителя считалась бы по чужому тексту: итоговая строка отчёта
    #    содержит «⚠️ N не проверено» ВСЕГДА, поэтому любой вклеенный отчёт
    #    поднимал `exec_warn` и вечно понижал честный "ok" до "partial", а
    #    его же «✅» гасили `exec_failed` у реального провала.
    exec_self = _strip_trailing_independent_report(exec_output)
    exec_failed = (any(mark in exec_self for mark in _EXEC_FAILURE_MARKERS)
                   and "✅" not in exec_self)
    exec_warn = any(mark in exec_self for mark in _EXEC_WARN_MARKERS)
    # Сомнение В САМОЙ ПЕРЕПРОВЕРКЕ. Строки `_verify_item` вида «создана, но
    # раздел не применился» / «проект: проверка не удалась» / «тип … не
    # проверяется автоматически» теперь считаются СТРУКТУРНО — средним
    # счётчиком ⚠️ «не проверено» в итоговой строке, и его уже учёл
    # `_verdict_from_totals`. Этот флаг остался страховкой на текстовые
    # формулировки мимо счётчика (см. _REPORT_DOUBT_MARKERS).
    report_doubt = report_usable and any(
        mark in independent for mark in _REPORT_DOUBT_MARKERS)

    # НОВОЕ (2026-08-06, дефект №1, пункт 1-2 ТЗ Максима). Голова
    # САМОотчёта исполнителя — ✅/❌ — используется как источник истины
    # ТОЛЬКО когда журналу нечем ни подтвердить, ни опровергнуть операцию
    # (totals is None: записи нет / журнал недоступен / формат не
    # распознан / перепроверка упала). Когда totals ЕСТЬ, журнал остаётся
    # старшим источником — эта ветка его не трогает вообще.
    self_head = exec_self.lstrip().lstrip("#").lstrip()
    self_proves_ok = self_head.startswith("✅") and not exec_warn
    self_proves_mismatch = self_head.startswith("❌")
    self_verified_no_journal = False  # для basis ниже: "ok" пришёл САМ, без журнала

    if exec_failed:
        verdict = "failed"
    elif totals is None and self_proves_mismatch:
        # Исполнитель НЕ отказался (иначе сработала бы ветка exec_failed
        # выше) — он реально что-то поменял и его собственный обязательный
        # post-verify нашёл расхождение. Это провал ПОСЛЕ мутации, а не «мы
        # не смогли доказать» — честная разница между "unverified" и
        # "mismatch" (см. блок-комментарий выше).
        verdict = "mismatch"
    else:
        verdict = _verdict_from_totals(totals)
        # Даунгрейд, но НИКОГДА не апгрейд: сомнение исполнителя (⚠️/⏭/«не
        # подтверждён») ИЛИ неучтённые строки самой перепроверки понижают
        # "ok" до "partial". "unverified" при этом остаётся "unverified" —
        # оно строже "partial" (там хотя бы часть доказана, здесь не доказано
        # ничего).
        if verdict == "ok" and (exec_warn or report_doubt):
            verdict = "partial"
        if verdict == "unverified" and totals is None and self_proves_ok:
            # Журналу нечем подтвердить (нет записи для этого класса
            # тулов/недоступен/формат не распознан), но сам исполнитель уже
            # доказал результат ОТДЕЛЬНЫМ обязательным по стандарту
            # живым чтением — молчание журнала здесь не повод звать это
            # "не знаю". См. `_auto_execute_report_is_success`.
            verdict = "ok"
            self_verified_no_journal = True

    # 3. Полный markdown: оба раздела целиком + честное основание вердикта.
    if independent is None:
        independent_block = (f"⚠️ Перепроверку выполнить не удалось: "
                             f"{independent_err or 'неизвестная ошибка'}")
    else:
        independent_block = independent

    if self_verified_no_journal:
        basis = ("Основание вердикта: собственное живое чтение исполнителя "
                 "(обязательный post-verify после КАЖДОЙ мутации, "
                 "output-format.md §7.2) подтвердило результат — "
                 "расхождений в его отчёте нет. Журнал мутаций для этой "
                 "операции недоступен как источник независимого "
                 "постфактум-аудита, но это НЕ равно «не сработало»: "
                 "эффект уже подтверждён отдельным прямым чтением, просто "
                 "не журналом.")
    elif verdict == "mismatch":
        basis = ("Основание вердикта: собственное живое чтение исполнителя "
                 "нашло расхождение с ожидаемым результатом (детали — в "
                 "разделе «Что сделал исполнитель» выше). Журнал мутаций для "
                 "этой операции недоступен, но самоотчёт исполнителя уже "
                 "доказывает исход напрямую — это настоящий провал, а не "
                 "«не удалось проверить».")
    elif totals is not None:
        basis = (f"Основание вердикта: независимая перепроверка живым чтением — "
                 f"✅ {totals[0]} подтверждено, ⚠️ {totals[1]} не проверено, "
                 f"❌ {totals[2]} расхождений.")
        if totals[1] > 0:
            basis += (" Непроверенные пункты — это НЕ подтверждение: живое "
                      "чтение по ним исхода не доказало.")
        if report_doubt:
            basis += (" В отчёте есть формулировки «не подтверждено» вне "
                      "счётчиков — по ним исход не доказан, поэтому вердикт "
                      "понижен.")
    elif not report_usable:
        basis = ("Основание вердикта: независимую перепроверку выполнить НЕ "
                 "удалось (журнал/живое состояние недоступны), и собственный "
                 "отчёт исполнителя тоже не доказывает результат — исход "
                 "операции НЕ ПОДТВЕРЖДЁН. Это не то же самое, что «успешно»."
                 )
    else:
        basis = ("Основание вердикта: формат итоговой строки независимого "
                 "отчёта не распознан, и собственный отчёт исполнителя тоже "
                 "не доказывает результат — считаем исход НЕ "
                 "ПОДТВЕРЖДЁННЫМ.")
    if exec_failed:
        basis = ("Основание вердикта: исполнитель сам отрапортовал провал "
                 "(в его отчёте есть маркер ошибки и ни одного ✅). " + basis)

    full_md = "\n".join([
        f"### {_VERDICT_EMOJI.get(verdict, '❓')} Автоисполнение «{tool}» — "
        f"{_VERDICT_WORD.get(verdict, verdict)}",
        f"_manifest: `{manifest_id}`_",
        "",
        "#### Что сделал исполнитель",
        exec_self.strip() or "_(исполнитель не вернул текста)_",
        "",
        "#### Независимая перепроверка (живое чтение)",
        independent_block.strip(),
        "",
        basis,
    ])
    # РАЗВЕДЕНИЕ КАНАЛОВ (2026-08-06, fix/agent-tail-in-verify-report, тот же
    # принцип, что у `agent_tail` в `_maybe_tg_notify_plan`). `full_md` уходит
    # ТОЛЬКО в Telegram (`_publish_auto_execute_outcome` → post_report_to_group
    # / send_message_chunked) — этот путь запускает фоновый поллер кнопки ✅,
    # модели тут нет вообще, значит и служебной строке "[агенту: ...]" здесь
    # взяться неоткуда легитимно. `independent_block` выше — это дословный
    # вывод `_build_operation_report()`, который ЗАКОНОМЕРНО несёт такую
    # строку для СВОЕГО легитимного канала (возврат тула модели, см. вызовы
    # `_build_operation_report` в execute_task_creation/execute_task_deletion
    # и др.) — здесь она лишний груз, а не инструкция кому-то. Чистим ВЕСЬ
    # `full_md`, а не только `independent_block`, — так же ловится случайная
    # инструкция, просочившаяся через `exec_self` (самоотчёт исполнителя).
    full_md = tg_approval.strip_agent_instructions(full_md)
    return full_md, verdict


def _manifest_affected_count(m: Optional[Dict]) -> Optional[int]:
    """Сколько объектов ПЛАНИРОВАЛ манифест — только для короткой сводки в
    личку. Что не знаем — просто None, и в сводке этой строки не будет; врать
    числом здесь нельзя.

    def-111 (2026-08-07): это число из ПЛАНА (намерение ДО исполнения,
    `consumed` захватывается в try_auto_execute до вызова исполнителя), а НЕ
    факт того, сколько объектов реально изменилось — гейт мог законно
    отказаться менять что-либо (например unset_task_parent на задаче, у
    которой родителя уже нет: «Ничего не тронул», журнал не пишется вообще).
    Раньше это число подписывалось в сводке словом «Затронуто» — прямая ложь
    рядом с «Ничего не тронул». Сама эта функция ПРАВА в своём подсчёте (она
    и не претендует на факт исполнения, только на размер плана) — вызывающий
    код (`_short_auto_execute_summary`) теперь называет её результат «Объектов
    в плане» и рядом, когда доступно, печатает РЕАЛЬНОЕ число подтверждённых
    из `_parse_verify_totals` над уже готовым независимым отчётом.

    Форм манифеста несколько, и после 2026-08-06 (кнопка у 22 тулов) их надо
    знать все, иначе строка молча пропадала бы у всех новых исполнителей:
      * `items` — план удаления (plan_task_deletion / delete_tasks);
      * `tasks` — общий пакетный гейт `_gate_batch` (complete_tasks,
        move_tasks, set_task_tags, restore_tasks…);
      * `raw`   — план создания (plan_task_creation / create_tasks);
      * `_gate == "single"` — одиночный гейт (`_gate_single`): по определению
        РОВНО один объект В ПЛАНЕ (create_tag, delete_project, rename_tag…),
        поэтому 1 — это факт из конструкции гейта, а не догадка. Он не
        гарантирует, что этот один объект реально изменился — это отдельная
        проверка (см. выше)."""
    if not isinstance(m, dict):
        return None
    for key in ("items", "tasks", "raw"):
        value = m.get(key)
        if isinstance(value, list):
            return len(value)
    if m.get("_gate") == "single":
        return 1
    return None


def _short_auto_execute_summary(tool: str, verdict: str,
                                affected: Optional[int],
                                group_delivered: bool,
                                fallback_to_dm: bool = False,
                                partial: Optional[Tuple[int, int]] = None,
                                totals: Optional[Tuple[int, int, int]] = None,
                                group_configured: bool = True) -> str:
    """2-4 строки в ЛИЧКУ владельца. Максим просил не захламлять личный чат
    1:1 простынями — подробности живут в группе «MCP Отчёты», сюда идёт
    только вердикт. Если в группу отчёт НЕ доставился, сводка обязана сказать
    это вслух: молчаливая потеря подробностей — тот же самый оптимистичный
    отчёт, только в другой обёртке.

    `partial=(доставлено, всего)` — отдельный, ТРЕТИЙ исход между «дошло» и
    «не дошло»: на затяжном флуд-лимите Telegram в группу ложатся, например,
    2 части из 6. Раньше такой случай не отличался от полного успеха (список
    id ведь непустой), и владельцу писали «Подробный отчёт — в группе», хотя
    там лежала треть. Теперь это называется своими словами.

    def-118 (2026-08-07): `group_delivered` = `bool(delivery.ok)` — «ушло
    туда, куда сейчас указывает reports_chat_id» — а НЕ «ушло в РЕАЛЬНУЮ
    группу». Когда `TG_REPORTS_CHAT_ID` не задана, `load_tg_approval_config`
    (tg_approval.py) подставляет `reports_chat_id = owner_chat_id` — то есть
    «группа» технически ЭТО ЖЕ личка. Telegram успешно отправляет туда
    сообщение (`delivery.ok=True`), но группы «MCP Отчёты», о которой
    говорит следующая строка, не существует — владелец шёл искать отчёт не
    там. `group_configured` — честный флаг «группа реально отличается от
    лички» (то же сравнение `reports_chat != owner_chat`, что уже стоит в
    `_publish_auto_execute_outcome` для fallback_ok чуть ниже), передаётся
    вызывающей стороной. Дефолт `True` — для существующих прямых вызовов
    этой функции (тесты и любой будущий вызывающий, который явно не думал
    про этот кейс) ничего не меняет; единственный боевой вызывающий
    (`_publish_auto_execute_outcome`) передаёт настоящее значение явно.

    def-111 (2026-08-07): `affected` (см. `_manifest_affected_count`) — размер
    ПЛАНА, захваченного ДО исполнения, а не факт того, что реально изменилось
    — гейт мог законно ничего не менять («Ничего не тронул»). Раньше это число
    подписывалось словом «Затронуто», и рядом с «Ничего не тронул» получалась
    прямая ложь. Теперь оно подписано «Объектов в плане», а когда доступен
    `totals=(ok, warn, bad)` — РЕАЛЬНЫЙ исход независимой перепроверки,
    распарсенный `_parse_verify_totals` из уже готового `full_md` в
    `_publish_auto_execute_outcome` — рядом печатается факт «Подтверждено
    перепроверкой: N», чтобы план и факт не расходились молча."""
    lines = [f"{_VERDICT_EMOJI.get(verdict, '❓')} Автоисполнение «{tool}» — "
             f"{_VERDICT_WORD.get(verdict, verdict)}."]
    if affected is not None:
        if totals is not None:
            lines.append(f"Объектов в плане: {affected}. Подтверждено "
                         f"перепроверкой: {totals[0]}.")
        else:
            lines.append(f"Объектов в плане: {affected}.")
    if partial is not None:
        got, total = partial
        lines.append(f"⚠️ отчёт доставлен в группу частично ({got} из {total} "
                     f"частей) — остальное не дошло, подробности в логах "
                     f"сервера.")
    elif group_delivered and group_configured:
        lines.append("Подробный отчёт — в группе «MCP Отчёты».")
    elif group_delivered:
        # def-118: доставлено, но НЕ в группу (её нет — reports_chat_id ==
        # owner_chat_id) — ушло отдельным сообщением в этот же личный чат,
        # позже отредактированной сводки по времени отправки (см. порядок
        # вызовов в _publish_auto_execute_outcome: post_report_to_group идёт
        # ДО summarize_in_owner_chat) — «ниже» здесь честно, а не «в группе».
        lines.append("Подробный отчёт — ниже.")
    elif fallback_to_dm:
        lines.append("⚠️ В группу отчёт не ушёл (проверь TG_REPORTS_CHAT_ID и "
                     "что бот в группе) — полный текст прислан сюда отдельным "
                     "сообщением.")
    else:
        lines.append("⚠️ отчёт в группу не доставлен, подробности в логах "
                     "сервера.")
    return "\n".join(lines)


def _cleanup_plan_leftovers(candidate: Dict, tool: str) -> int:
    """Удаляет из ЛИЧКИ владельца предыдущие куски длинного плана, оставшиеся
    после того, как итог вписан в последнее сообщение. Возвращает, сколько
    удалить удалось (для лога и тестов).

    Три жёстких границы, каждая — из разбора реального инцидента:
      1. НИКОГДА не трогаем последнее сообщение (`message_id`) — в нём теперь
         живёт сводка; поэтому его id явно исключается, даже если он вдруг
         продублирован в extra_message_ids.
      2. НИКОГДА не трогаем группу-архив: чат берётся ТОЛЬКО из candidate
         (личка), `report_chat_id` сюда не попадает ни при каком условии.
      3. Best-effort целиком: любая неудача — только лог. Сообщение, стёртое
         человеком руками, или старше 48 часов (Bot API их удалять не даёт) —
         это не ошибка процесса и не повод менять вердикт."""
    chat_id = candidate.get("chat_id")
    extra = candidate.get("extra_message_ids") or []
    if not chat_id or not extra:
        return 0
    keep = candidate.get("message_id")
    removed = 0
    for mid in extra:
        if mid is None or (keep is not None and mid == keep):
            continue
        try:
            if tg_approval.delete_message(_TG_CFG, str(chat_id), mid):
                removed += 1
        except Exception as e:
            logger.warning(f"TG auto-execute: не смог удалить кусок плана "
                           f"{mid} в чате {chat_id} ({tool}/"
                           f"{candidate.get('manifest_id')}): {e}")
    if removed:
        logger.info(f"TG auto-execute: убрано лишних кусков плана в личке: "
                    f"{removed} ({tool}/{candidate.get('manifest_id')})")
    return removed


# Ключ в словаре кандидата: «итог по этому кандидату УЖЕ публиковался».
# Ставится ПЕРВОЙ строкой `_publish_auto_execute_outcome` — то есть фактом
# входа в публикацию, а не её успехом. Так и задумано: см. проверку в
# аварийной ветке `_tg_auto_execute_tick`.
_OUTCOME_PUBLISHED_KEY = "_outcome_published"


def _publish_auto_execute_outcome(candidate: Dict, tool: str, full_md: str,
                                  verdict: str,
                                  affected: Optional[int]) -> None:
    """Куда уходит итог (пункты 1-2 ТЗ): ПОЛНЫЙ отчёт — в группу «MCP Отчёты»
    (архив, чанкинг внутри `post_report_to_group`), КОРОТКАЯ сводка — в то же
    личное сообщение с кнопками (`summarize_in_owner_chat` редактирует текст
    и снимает кнопки).

    Обе публикации обёрнуты в try/except, хотя по контракту обе best-effort и
    не бросают: инвариант «упавший кандидат не рушит остальных» не должен
    зависеть от чужих гарантий. Функция СИНХРОННАЯ (requests + time.sleep на
    429) и обязана вызываться через `_run_blocking` — вызов напрямую из
    корутины блокирует event loop на всё время отправки отчёта."""
    # ПЕРВОЙ строкой, до любого выхода наружу: отметка «итог по этому
    # кандидату уже публикуется/опубликован». Она делает двойную публикацию
    # НЕВОЗМОЖНОЙ КОНСТРУКТИВНО, а не «по дисциплине внутренних try/except».
    #
    # Что она предотвращает: исключение, вылетевшее отсюда на УСПЕШНОМ пути
    # (после того как отчёт уже ушёл), ловится общим `except` в
    # `_tg_auto_execute_tick`, и тот публикует ВТОРОЙ отчёт — «🛑 ошибка
    # исполнения» про операцию, которая на самом деле выполнена и уже
    # отчиталась. В архив легла бы ложь, причём именно та, которую
    # человек читает как «ничего не произошло».
    #
    # Отметка ставится по факту ВХОДА, а не по успеху: если публикация упала
    # на середине, часть сообщений уже могла уйти — и второй отчёт с другим
    # вердиктом сделал бы картину не полнее, а противоречивее. Провал в этом
    # случае виден в логах как ERROR (внутренние except'ы ниже).
    candidate[_OUTCOME_PUBLISHED_KEY] = True
    # ЧЕСТНАЯ проверка полноты доставки (2026-08-06). Раньше здесь лежал
    # `delivered: List[int]`, и успех определялся как `bool(delivered)` —
    # то есть «хоть одно сообщение дошло» читалось как «отчёт опубликован».
    # На затяжном 429 в группу ложились 2 части из 6, а владелец получал в
    # личку «Подробный отчёт — в группе «MCP Отчёты»». Теперь полнота берётся
    # из `.ok`, а частичность называется вслух.
    delivery = tg_approval.ReportDelivery([], 0, 0, False)
    try:
        delivery = tg_approval.post_report_to_group(
            _TG_CFG, candidate["manifest_id"], full_md,
            tool=tool, verdict=verdict)
    except Exception:
        logger.exception(f"TG auto-execute: публикация отчёта в группу упала "
                         f"({tool}/{candidate['manifest_id']})")

    # Фолбэк на ЛИЧКУ, если в группу не ушло НИЧЕГО. Самый вероятный прод-
    # сценарий: TG_REPORTS_CHAT_ID вписали руками с опечаткой или бота не
    # добавили в группу — Telegram отвечает «chat not found» / «bot is not a
    # member of the ... chat», send_message_chunked честно возвращает ok=False,
    # и без этой ветки ПОЛНЫЙ отчёт исчезал бы навсегда (в логах Railway он
    # тоже не появляется — туда пишется только факт неудачи). Владелец при
    # этом видел бы «⚠️ отчёт не доставлен» и не имел бы никакого способа
    # узнать, что именно сделала мутация. Дублирования не будет: если группа
    # и есть личка, отправка уже провалилась ровно в этот чат.
    fallback_ok = False
    reports_chat = str(getattr(_TG_CFG, "reports_chat_id", "") or "")
    owner_chat = str(candidate.get("chat_id") or "")
    # def-118: «группа реально настроена ОТДЕЛЬНО от лички» — то же сравнение,
    # что чуть ниже решает нужен ли фолбэк на личку, но здесь оно нужно И для
    # случая, когда TG_REPORTS_CHAT_ID пуст, а post_report_to_group всё равно
    # технически «доставил» (Telegram просто отправил в тот же личный чат, а
    # не провалился) — тогда fallback_ok ниже даже не вычисляется.
    group_configured = bool(reports_chat) and reports_chat != owner_chat
    if not delivery.message_ids and owner_chat and reports_chat != owner_chat:
        try:
            fb = tg_approval.send_message_chunked(_TG_CFG, owner_chat, full_md)
            fallback_ok = bool(fb.ok)
            if not fallback_ok:
                logger.error(f"TG auto-execute: отчёт не удалось доставить ни в "
                             f"группу, ни в личку ({tool}/"
                             f"{candidate['manifest_id']}): {fb.error}")
        except Exception:
            logger.exception(f"TG auto-execute: фолбэк-отправка отчёта в личку "
                             f"упала ({tool}/{candidate['manifest_id']})")

    # Частичная доставка — отдельный исход, а не «успех» и не «ничего не
    # дошло»: часть отчёта в группе ЕСТЬ, поэтому дублировать его целиком в
    # личку (фолбэк выше) не надо, но и молчать об этом нельзя.
    partial = None
    if not delivery.ok and delivery.delivered > 0:
        partial = (delivery.delivered, delivery.total_chunks)
    # def-111: `full_md` уже содержит (внутри `independent_block`, дословно
    # вставленным `_verified_auto_execute_report`) ровно ОДНУ итоговую строку
    # «Итог: ✅ N подтверждено, ⚠️ M не проверено, ❌ K расхождений» — ту же,
    # что `_parse_verify_totals` уже умеет читать и что использовалась для
    # вычисления самого `verdict`. Переиспользуем её здесь, а не тащим
    # отдельный параметр через сигнатуру `_verified_auto_execute_report` (у
    # неё единственный вызывающий, и раздувать её возврат ради одной строки
    # короткой сводки — лишнее): `_parse_verify_totals` уже по конструкции
    # безопасна (None на любую неоднозначность), так что риска путаницы нет.
    totals = _parse_verify_totals(full_md)
    short_md = _short_auto_execute_summary(tool, verdict, affected,
                                           bool(delivery.ok), fallback_ok,
                                           partial, totals,
                                           group_configured=group_configured)
    summary_ok = False
    try:
        summary_ok = bool(tg_approval.summarize_in_owner_chat(
            _TG_CFG, candidate["chat_id"], candidate["message_id"], short_md))
    except Exception:
        logger.exception(f"TG auto-execute: сводка в личку не отправилась "
                         f"({tool}/{candidate['manifest_id']})")

    # Уборка «сирот» плана в ЛИЧКЕ (2026-08-06). Длинный план уходит
    # несколькими сообщениями: последнее (с кнопками) лежит в message_id и
    # именно в него вписана сводка, а куски 1..N-1 — в extra_message_ids.
    # После исполнения по кнопке строка становится APPROVED, и её не трогает
    # НИКТО: наш reaper обходит APPROVED сознательно (архив состоявшегося
    # решения), а уборщик соседнего gmail-mcp удаляет только message_id.
    # Итог — куски плана оставались в личном чате навсегда, вопреки прямому
    # требованию «не захламляя личный чат 1:1».
    #
    # Только ПОСЛЕ успешно вписанной сводки: пока итога нет, удалять контекст
    # нельзя — владелец остался бы вообще без информации о том, что случилось.
    # Строго best-effort: неудача ничего не ломает и не меняет вердикт.
    # Сообщения в ГРУППЕ-АРХИВЕ здесь не трогаются ни при каких условиях —
    # используется только chat_id кандидата (личка) и только extra-куски.
    #
    # Внешний try — не паранойя: исключение, вылетевшее ОТСЮДА, поймал бы
    # `except` в `_tg_auto_execute_tick`, и тот опубликовал бы ВТОРОЙ отчёт с
    # вердиктом «🛑 ошибка исполнения» — про операцию, которая на самом деле
    # успешно выполнена и уже отчиталась. Врать в эту сторону нельзя тем более.
    if summary_ok:
        try:
            _cleanup_plan_leftovers(candidate, tool)
        except Exception as e:
            logger.warning(f"TG auto-execute: уборка кусков плана не удалась "
                           f"({tool}/{candidate['manifest_id']}): {e}")


# ---------------------------------------------------------------------------
# «Подтверждено, но исполнять нечего» (2026-08-06)
# ---------------------------------------------------------------------------
# ПРОБЛЕМА, которую закрывает весь блок ниже. Манифест плана (что именно
# делать) живёт ТОЛЬКО в памяти процесса — `_MANIFESTS`. Решение по кнопке
# живёт в общем Postgres — `tg_approvals`. Сервис деплоится автоматически из
# `main`, то есть процесс перезапускается в произвольный момент. Отсюда
# сценарий: владелец получил план с кнопками → сервис перезапустился →
# владелец нажал ✅ → вебхук соседнего сервиса честно поставил APPROVED → наш
# поллер ищет кандидатов ОТ ПАМЯТИ, а памяти уже нет → тишина. Уборщик
# `reap_expired` строки APPROVED не трогает (и правильно — это архив
# состоявшегося решения), поэтому сообщение с кнопками висит вечно, отчёта
# нет, и владелец не узнаёт, что операция не состоялась. Раньше это добивалось
# текстовым «да» в чате — теперь для таких планов действует режим «только
# кнопка» (`_tg_button_only`), и операция становится принципиально
# неисполнимой.
#
# ЧТО ДЕЛАЕМ: замечаем такие строки и говорим владельцу правду. Ничего не
# исполняем — исполнять физически нечего.
#
# ГЛАВНАЯ СЛОЖНОСТЬ — не соврать в обратную сторону, то есть не сказать «не
# выполнено» про операцию, которая на самом деле выполнена. Защита слоями,
# от дешёвых к дорогим:
#   1. `lost_notified_at IS NULL` (в БД) — про эту строку ещё не сообщали;
#   2. `expires_at > now - lookback` (в БД) — не поднимаем со дна старьё:
#      исполненные строки живут в таблице до уборки соседним сервером, и без
#      этого фильтра после каждого рестарта мы бы «открывали» их заново;
#   3. grace-пауза после решения — между записью APPROVED и ближайшим тиком
#      поллера есть законное окно (до 10 секунд плюс сеть), паниковать в нём
#      нельзя;
#   4. память процесса — живой манифест ИЛИ надгробие
#      (`_MANIFEST_TOMBSTONES`): его ставит и `_consume_manifest_for_auto_
#      execute` перед исполнением по кнопке, и `_prune_manifests` при уборке
#      ЛЮБОГО погашенного манифеста — включая планы, которые исполняет
#      повторный вызов инструмента (delete_project, слияние в rename_tag).
#      Только что исполненный и вычищенный из `_MANIFESTS` план потерянным не
#      выглядит ни в одном из этих случаев;
#   5. `report_message_ids` в строке — отчёт по манифесту уже публиковался,
#      значит исполнение было (пусть и в другом процессе, например в старом
#      контейнере во время выкатки);
#   6. журнал мутаций на диске (`deletion_journal.jsonl`) — последняя и самая
#      надёжная проверка, потому что она ПЕРЕЖИВАЕТ рестарт: если по этому
#      манифесту есть хоть одна запись, мутация выполнялась. Она дорогая
#      (чтение файла), поэтому делается ТОЛЬКО для уже отобранных подозреваемых
#      и только в этой аварийной ветке.
#
# ЧЕСТНО ОБ ОСТАВШЕЙСЯ ДЫРЕ: если операция была исполнена, отчёт в группу не
# ушёл НИ ОДНИМ сообщением (Telegram лежал), И журнал мутаций оказался
# недоступен (нет тома /data), И процесс перезапустился — владелец получит
# ложное «не выполнено, повтори». Закрыть её полностью можно было бы отметкой
# «исполняю» в самой строке, но это правка УСПЕШНОГО пути, а он сейчас под
# приёмочным прогоном, и трогать его ради редкого тройного совпадения дороже,
# чем оставить описанным.

# Сколько ждём после принятого решения, прежде чем считать план потерянным.
# Не «на всякий случай»: тик поллера — 10 секунд, плюс поездка в Postgres
# через публичный прокси, плюс возможная выкатка, в которой старый процесс
# ещё дорабатывает своё. 120 секунд с запасом переживают этот стык и при этом
# не заставляют человека гадать перед молчащей кнопкой минутами.
_TG_LOST_GRACE_S = float(os.environ.get("TG_LOST_GRACE_S", "120"))
# Насколько СТАРЫЕ строки вообще рассматриваем — считается от `expires_at`
# (конца жизни плана), а не от создания. Час запаса поверх TTL нужен ровно для
# одного реального случая: владелец нажал кнопку в последнюю минуту жизни
# плана, и к моменту нашего разбора строка уже формально просрочена — молчать
# про такое нельзя, это тот же самый silent-fail.
_TG_LOST_LOOKBACK_S = float(os.environ.get("TG_LOST_LOOKBACK_S", "3600"))

# Манифесты, про которые ЭТОТ процесс уже выяснил, что они исполнялись (по
# журналу мутаций). Нужен, чтобы не перечитывать журнал каждые 10 секунд по
# одним и тем же строкам: пометки в БД они не получают (сообщать-то не о чем),
# так что без этого кэша проверка повторялась бы вечно. Обычный set: и запись
# (из рабочего потока), и проверка членства атомарны под GIL — в отличие от
# ИТЕРАЦИИ по общему словарю, которой здесь сознательно нет.
_TG_LOST_CLEARED: set = set()
_TG_LOST_CLEARED_CAP = 1000

# Чем подписан отчёт о потере в архиве. Инструмент восстановить НЕЛЬЗЯ: он
# хранился в манифесте, а манифест — это и есть то, что потеряно. Врать
# правдоподобным именем тула хуже, чем честно сказать «неизвестна».
_TG_LOST_TOOL_LABEL = "операция неизвестна"


def _tg_manifest_is_known(manifest_id: str) -> bool:
    """Знает ли ЭТОТ процесс что-нибудь про манифест: живой план в памяти,
    надгробие уже исполненного (ставится ДО исполнения, см.
    `_consume_manifest_for_auto_execute`) или отметка «проверено по журналу».

    Только проверки членства, без единой итерации по общим коллекциям: эта
    функция зовётся из event loop'а, а пополняются коллекции в том числе из
    рабочего потока (`_run_blocking`)."""
    m = _MANIFESTS.get(manifest_id)
    if m is not None and not m.get("consumed"):
        return True
    return (manifest_id in _MANIFEST_TOMBSTONES
            or manifest_id in _TG_LOST_CLEARED)


def _tg_lost_manifest_rows(rows: Dict[str, dict], *, now_ms: int,
                           grace_ms: Optional[int] = None,
                           lookback_ms: Optional[int] = None,
                           is_known=None) -> List[Dict]:
    """Строки `tg_approvals`, которые ВЫГЛЯДЯТ как «подтверждено, а исполнять
    нечего». Чистая функция: ни базы, ни сети, ни Telegram — только уже
    прочитанные строки плюс справка «знает ли процесс такой манифест».
    Поэтому её можно (и нужно) проверять тестами по каждому слою отдельно.

    Возвращает список в той же форме, что и кандидаты на исполнение
    (`_tg_auto_execute_approved`), чтобы дальше по коду не было второго
    формата «почти кандидата»."""
    grace = _TG_LOST_GRACE_S * 1000 if grace_ms is None else grace_ms
    lookback = _TG_LOST_LOOKBACK_S * 1000 if lookback_ms is None else lookback_ms
    known = _tg_manifest_is_known if is_known is None else is_known
    out: List[Dict] = []
    for mid, row in (rows or {}).items():
        if (row or {}).get("status") != "APPROVED":
            continue
        if row.get("lost_notified_at"):
            continue  # владельцу про эту потерю уже сказали — один раз и хватит
        if row.get("report_message_ids"):
            continue  # отчёт по манифесту публиковался → исполнение было
        expires_at = row.get("expires_at")
        if expires_at is not None and now_ms - expires_at > lookback:
            continue  # слишком старая строка, разбирать её поздно и незачем
        # Время РЕШЕНИЯ: `decided_at` ставит вебхук соседнего сервиса при
        # нажатии; если его почему-то нет — берём момент создания строки,
        # он в схеме NOT NULL. Ноль/None означает «не знаем, когда» — тогда
        # ждём, а не спешим обвинять.
        decided_at = row.get("decided_at") or row.get("created_at")
        if not decided_at or now_ms - decided_at < grace:
            continue
        if known(mid):
            continue
        out.append({"manifest_id": mid, "chat_id": row.get("chat_id"),
                    "message_id": row.get("message_id"),
                    "extra_message_ids": list(row.get("extra_message_ids") or []),
                    "decided_at": decided_at})
    return out


def _journal_mentions_manifests(manifest_ids: set) -> set:
    """Какие из этих манифестов вообще встречаются в журнале мутаций.

    Единственная проверка «исполнялось ли на самом деле», которая ПЕРЕЖИВАЕТ
    перезапуск процесса: журнал — файл на диске, а не память. Ключи ищем те
    же, по которым отчёт находит свои записи (`record`/`manifest`/
    `tg_manifest`), чтобы не разойтись с `_build_operation_report`.

    Никогда не бросает: журнала может не быть вовсе (том не подключён) — тогда
    просто нечего сказать, и решение принимается по остальным слоям."""
    if not manifest_ids:
        return set()
    path = os.path.join(_JOURNAL_DIR, "deletion_journal.jsonl")
    found: set = set()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                # Дешёвый отсев: id — это hex-строка, и если её нет в сырой
                # строке журнала, разбирать JSON незачем. Журнал длинный
                # (все мутации сервера за всё время), а эта ветка аварийная.
                if not any(mid in line for mid in manifest_ids):
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                for key in ("record", "manifest", "tg_manifest"):
                    value = rec.get(key)
                    if value in manifest_ids:
                        found.add(value)
    except FileNotFoundError:
        return set()
    except Exception as e:  # noqa: BLE001 — журнал недоступен: скажем, что не знаем
        logger.warning(f"TG lost-plan: не смог прочитать журнал мутаций "
                       f"({path}): {e}")
        return set()
    return found


def _lost_manifest_texts(candidate: Dict) -> Tuple[str, str]:
    """(полный текст для архива, короткое объяснение владельцу).

    Текст владельцу — принципиально без жаргона: он читает это в личном чате
    в ответ на своё нажатие и должен из двух строк понять три вещи —
    подтверждение дошло, операция НЕ выполнена, и что делать дальше."""
    mid = candidate.get("manifest_id") or "?"
    decided = tg_approval.owner_time_str(candidate.get("decided_at"))
    short_md = (
        "⚠️ **Подтверждение принято, но выполнять оказалось нечего.**\n\n"
        "Пока план ждал вашего нажатия, он пропал: план хранится только в "
        "памяти сервиса — и не переживает ни перезапуск (так бывает при "
        "обновлении), ни истечение своего срока.\n\n"
        "**Операция НЕ выполнена, в TickTick ничего не изменилось.**\n\n"
        "Что делать: попросите то же самое ещё раз — придёт новый план с "
        "новыми кнопками."
    )
    full_md = "\n".join([
        f"_manifest: `{mid}`_",
        "",
        "Владелец нажал ✅, но к этому моменту самого плана уже не "
        "существовало: план (что именно делать) хранится только в памяти "
        "процесса и не переживает ни перезапуск (обновление, деплой, сбой), "
        "ни истечение своего срока. Исполнять было нечего.",
        "",
        "**Операция НЕ выполнена. В TickTick ничего не изменено.**",
        "",
        "Что известно о подтверждении:",
        f"- решение принято: {decided};",
        f"- личный чат: `{candidate.get('chat_id')}`, сообщение с кнопками: "
        f"`{candidate.get('message_id')}`;",
        "- какая именно это была операция — восстановить нельзя: инструмент и "
        "содержимое плана жили вместе с планом в памяти.",
        "",
        "Владельцу отправлено сообщение с просьбой повторить запрос заново; "
        "кнопки с исходного сообщения сняты, чтобы больше не вводили в "
        "заблуждение.",
    ])
    return full_md, short_md


def _publish_lost_manifest_outcome(candidate: Dict) -> None:
    """Сообщает владельцу о потерянном плане и кладёт тот же факт в архив.

    Три шага, каждый независимо best-effort — падение одного не должно
    отменять остальные:
      1. снять кнопки с исходного сообщения (`clear_inline_keyboard`), НЕ
         затирая текст плана: это единственное, что осталось у владельца от
         его же просьбы, и оно нужно ему, чтобы понять, что повторять;
      2. отдельным сообщением в личку — объяснение простыми словами;
      3. полный текст в группу-архив.

    ПРО АРХИВ (сознательное решение, а не автоматизм). Кладём — потому что
    «MCP Отчёты» это журнал того, чем кончилось КАЖДОЕ подтверждение. План в
    архив не попадает, туда попадают только исходы; молчание в этом случае
    означало бы, что в архиве нет ни следа операции, которую владелец
    подтвердил, — и потом, разбирая «а что вообще было», отличить «не
    подтверждал» от «подтвердил, но пропало» стало бы нечем. Ровно тот же
    довод, по которому в архив уходят и ошибки исполнения."""
    full_md, short_md = _lost_manifest_texts(candidate)
    mid = candidate.get("manifest_id") or "?"
    chat_id = str(candidate.get("chat_id") or "")

    try:
        tg_approval.clear_inline_keyboard(_TG_CFG, chat_id,
                                          candidate.get("message_id"))
    except Exception as e:
        logger.warning(f"TG lost-plan: не смог снять кнопки ({mid}): {e}")

    if chat_id:
        try:
            res = tg_approval.send_message_chunked(_TG_CFG, chat_id, short_md)
            if not res.ok:
                # Отметку «сообщили» мы уже поставили в БД (иначе спамили бы
                # каждые 10 секунд), поэтому повтора не будет — и провал
                # обязан быть громким в логе, а не тихим.
                logger.error(f"TG lost-plan: сообщение владельцу о потерянном "
                             f"плане {mid} НЕ доставлено: {res.error}")
        except Exception:
            logger.exception(f"TG lost-plan: отправка владельцу упала ({mid})")
    else:
        logger.error(f"TG lost-plan: в строке {mid} нет chat_id — сказать "
                     f"владельцу некуда")

    try:
        tg_approval.post_report_to_group(_TG_CFG, mid, full_md,
                                         tool=_TG_LOST_TOOL_LABEL,
                                         verdict="lost")
    except Exception as e:
        logger.warning(f"TG lost-plan: публикация в архив не удалась ({mid}): {e}")


def _announce_lost_manifests(candidates: List[Dict]) -> int:
    """Разбирает подозреваемых и сообщает по тем, кто правда потерян.
    СИНХРОННАЯ (файл + Postgres + Telegram) — звать только через
    `_run_blocking`. Возвращает, о скольких сообщили (для лога и тестов).

    Порядок шагов важен: сначала дорогая, но решающая проверка по журналу
    (мутация могла быть исполнена ДО перезапуска), и только потом — атомарный
    захват права сказать. Захват идёт ДО отправки: см. `claim_lost_manifests`.
    """
    if not candidates:
        return 0
    ids = {c["manifest_id"] for c in candidates}
    executed = _journal_mentions_manifests(ids)
    if executed:
        # Запоминаем, чтобы не перечитывать журнал по этим же строкам каждые
        # 10 секунд: пометки в БД они не получат — сообщать-то не о чем.
        if len(_TG_LOST_CLEARED) > _TG_LOST_CLEARED_CAP:
            _TG_LOST_CLEARED.clear()
        _TG_LOST_CLEARED.update(executed)
        logger.info(f"TG lost-plan: {len(executed)} подтверждений без манифеста "
                    f"на самом деле исполнялись (нашлись в журнале мутаций) — "
                    f"владельцу не пишем")
    fresh = [c for c in candidates if c["manifest_id"] not in executed]
    if not fresh:
        return 0
    try:
        claimed = set(tg_approval.claim_lost_manifests(
            [c["manifest_id"] for c in fresh]))
    except Exception as e:
        logger.warning(f"TG lost-plan: не смог занять строки для уведомления: {e}")
        return 0
    told = 0
    for c in fresh:
        if c["manifest_id"] not in claimed:
            continue  # строку забрал другой процесс — он и сообщит
        try:
            _publish_lost_manifest_outcome(c)
            told += 1
        except Exception:  # noqa: BLE001 — один сбой не глушит остальных
            logger.exception(f"TG lost-plan: уведомление по {c['manifest_id']} упало")
    if told:
        logger.warning(f"TG lost-plan: подтверждено кнопкой, но исполнять было "
                       f"нечего — сообщено владельцу по {told} плану(ам)")
    return told


async def _rehydrate_approved_candidates(pending: List[tuple],
                                         rows: Dict[str, dict]) -> List[tuple]:
    """Планы, подтверждённые кнопкой, о которых ЭТОТ процесс ничего не знает,
    — поднять из базы и вернуть как обычных кандидатов [(manifest_id, tool)].

    Это и есть лечение того, что механизм «подтверждено, но исполнять нечего»
    (`_tg_lost_manifest_rows`) до сих пор мог только КОНСТАТИРОВАТЬ. Строки,
    для которых план нашёлся, до разбора потерянных уже не доживут: они будут
    исполнены здесь же, в этом проходе, а живой манифест в памяти сделает их
    «известными» (`_tg_manifest_is_known`). «Потерянными» остаются только те,
    чей план в базу так и не попал (долговечность выключена, план был слишком
    велик, сбой записи) — то есть механизм честно сузился до реальных потерь.

    Разделение труда прежнее: единственное обращение к базе — в потоке,
    запись в `_MANIFESTS` — в event loop'е."""
    if not manifest_store.store_ready():
        return []
    known = {mid for mid, _ in pending}
    missing = [mid for mid, row in (rows or {}).items()
               if mid not in known and mid not in _MANIFESTS
               and tg_approval.approval_status_of(row) == "approved"]
    if not missing:
        return []
    try:
        found = await _run_blocking(manifest_store.load_live, missing)
    except Exception as e:  # noqa: BLE001 — база подвела: ведём себя как раньше
        logger.warning(f"Манифесты: не смог поднять подтверждённые планы: {e}")
        return []
    restored = _restore_manifests_from_db(found)
    out: List[tuple] = []
    for mid in restored:
        tool = _auto_executable_tool(_MANIFESTS.get(mid))
        if tool:
            out.append((mid, tool))
    if out:
        logger.info("Манифесты: подняты из базы и готовы к исполнению планы, "
                    f"подтверждённые кнопкой: {[m for m, _ in out]} "
                    "(процесс перезапускался между планом и нажатием)")
    return out


async def _tg_auto_execute_tick() -> None:
    """One pass: find candidates, execute each via its registered executor,
    verify the outcome INDEPENDENTLY, then publish (full report → the «MCP
    Отчёты» group, short summary → the owner's original button message).
    Mirrors gmail-mcp's runAutoExecutePoller — errors from ONE candidate never
    abort the others.

    Три вещи, за которые здесь отвечает именно ЭТА функция (2026-08-06):
    1. Поиск кандидатов разделён: RAM-часть (_tg_auto_execute_pending) идёт в
       event loop'е, а ЕДИНСТВЕННАЯ поездка в Postgres — через _run_blocking,
       то есть в отдельном потоке. psycopg2 синхронный: вызов из корутины
       напрямую морозил ВЕСЬ сервер (не только поллер — и обычные MCP-запросы
       модели) на всё время запроса.
    2. Перепроверка и публикация отчёта в Telegram (requests + сон на 429) —
       тоже через _run_blocking, по той же причине.
    3. `try_auto_execute` тоже уходит В ПОТОК — и это ПЕРЕВЁРНУТОЕ правило
       (#91). Пока планы жили в памяти, его атомарность («проверил consumed →
       выставил consumed» без await между) держалась на однопоточности loop'а,
       и вынос был прямо запрещён. Теперь захват плана — это `UPDATE … WHERE
       consumed_at IS NULL … RETURNING` в Postgres, атомарный сам по себе и
       переживающий перезапуск процесса; зато он синхронный, и оставить его в
       loop'е значило бы морозить сервер на каждое нажатие кнопки.

    Пункт 5 ТЗ («несколько параллельных подтверждений») архитектурно уже
    поддержан, и это НЕ случайность, а свойство четырёх мест сразу:
      * `_MANIFESTS` — СЛОВАРЬ манифестов, а не единственный слот: сколько
        планов настроил — столько живых записей, они друг друга не вытесняют;
      * у КАЖДОЙ строки в `tg_approvals` свой `manifest_id` (PRIMARY KEY) и
        свой `expires_at` — TTL одного подтверждения не гасит соседние;
      * этот цикл обходит ВСЕХ кандидатов, а `try/except` стоит ВНУТРИ тела
        цикла: упавший кандидат съедает своё исключение и не прерывает
        обработку следующих;
      * `_consume_manifest_for_auto_execute` — атомарный compare-and-set без
        единого `await` между проверкой `consumed` и его выставлением, так что
        на однопоточном event loop два тика физически не могут исполнить один
        манифест дважды.
    Отдельного «один pending за раз» в коде нет — при ревизии 2026-08-06 такое
    место не найдено."""
    if not _TG_CFG.enabled:
        # Слой выключен — ни базы, ни сети, ни на байт работы. В проде поллер
        # при выключенном слое даже не запускается, но прямой вызов прохода
        # обязан оставаться бесплатным: это инвариант совместимости («форк без
        # бота ведёт себя побайтово как раньше»). Раньше эту роль играл пустой
        # список живых манифестов, теперь — явная проверка, потому что поиск
        # потерянных планов от наличия манифестов не зависит.
        return
    pending = _tg_auto_execute_pending()
    # Раннего выхода «нет живых манифестов — идти в базу незачем» здесь БОЛЬШЕ
    # НЕТ, и это принципиально: пустая память — это ровно то состояние, в
    # котором сервис оказывается ПОСЛЕ ПЕРЕЗАПУСКА, то есть именно тогда, когда
    # потерянные планы и появляются. Прежний ранний выход делал такую потерю
    # ненаблюдаемой по построению. Цена — один индексируемый SELECT раз в тик
    # в простое; поиск при этом остаётся ОДНИМ обращением к Postgres на проход:
    # обе половины (строки живых манифестов + строки без манифеста) читает
    # один и тот же запрос.
    # Те же миллисекунды epoch, в которых `tg_approvals` хранит время
    # (created_at/decided_at/expires_at) — сравнивать надо в одной шкале.
    now_ms = int(time.time() * 1000)
    lost_since = now_ms - int(_TG_LOST_LOOKBACK_S * 1000)
    try:
        rows = await _run_blocking(tg_approval.get_tg_approvals,
                                   [mid for mid, _ in pending], lost_since)
    except Exception as e:
        logger.warning(f"TG auto-execute: get_tg_approvals failed: {e}")
        return
    # ГЛАВНЫЙ СМЫСЛ #91 ЖИВЁТ ЗДЕСЬ. До этой правки кандидаты искались ТОЛЬКО
    # среди планов в памяти, и после перезапуска процесса подтверждённая
    # кнопкой строка не имела шанса быть исполненной: памяти нет — кандидата
    # нет. Теперь строки, одобренные кнопкой, но неизвестные этому процессу,
    # поднимают свой план из базы и становятся обычными кандидатами.
    pending += await _rehydrate_approved_candidates(pending, rows)
    for c in _tg_auto_execute_approved(pending, rows):
        mid, tool = c["manifest_id"], c["tool"]
        entry = _resolve_auto_executor(tool, _MANIFESTS.get(mid) or {})
        if entry is None:
            continue
        try:
            # В ПОТОК (#91): захват плана — это теперь `UPDATE … RETURNING` в
            # Postgres, а не переключение флага в памяти. Прежний комментарий
            # на этом месте требовал обратного — держать `try_auto_execute` в
            # event loop'е, потому что на его однопоточности держалась
            # одноразовость. С переездом планов в базу одноразовость
            # обеспечивает сам SQL (см. `_consume_manifest_for_auto_execute`),
            # а синхронный psycopg2 в event loop'е морозил бы весь сервер.
            consumed = await _run_blocking(
                tg_approval.try_auto_execute,
                manifest_id=mid, tool=tool,
                get_manifest=lambda i: _MANIFESTS.get(i),
                consume_manifest=_consume_manifest_for_auto_execute,
                rehash=entry.rehash,
            )
            if consumed is None:
                continue  # race/drift/already consumed — not an error
            # Потолок на одного кандидата (main): зависший сетевой вызов не
            # должен держать очередь остальных подтверждённых планов.
            #
            # Метка манифеста на время исполнения: всё, что исполнитель
            # запишет в журнал мутаций, получит `tg_manifest = mid`, и
            # независимая перепроверка ниже найдёт эти записи по manifest_id —
            # для ЛЮБОГО инструмента, а не только для delete_tasks (см.
            # _TG_AUTO_EXECUTE_MANIFEST). Снимается в finally: протёкшая метка
            # пометила бы чужие, не связанные с кнопкой мутации.
            token = _TG_AUTO_EXECUTE_MANIFEST.set(mid)
            try:
                report_text = await asyncio.wait_for(
                    entry.execute(mid, consumed),
                    _TG_AUTO_EXECUTE_CANDIDATE_TIMEOUT_S)
            finally:
                _TG_AUTO_EXECUTE_MANIFEST.reset(token)
            # Слово «успешно» больше не принадлежит исполнителю: сначала
            # независимая перепроверка, только потом вердикт (пункт 3 ТЗ).
            #
            # Оба шага СИНХРОННЫЕ и оба ходят в сеть: перепроверка перечитывает
            # живое состояние TickTick (`_open_by_id(fresh=True)`), публикация
            # шлёт до нескольких сообщений через `requests` и на 429 честно
            # спит `retry_after` секунд. Вызванные напрямую из этой корутины,
            # они держали event loop десятки секунд — на streamable-http это
            # значит зависший /health и заткнувшиеся MCP-сессии. Уносим их в
            # поток тем же `_run_blocking`, которым пользуются все остальные
            # блокирующие вызовы этого файла.
            full_md, verdict = await _run_blocking(
                _verified_auto_execute_report, mid, tool, report_text)
            # Надгробие манифеста — по ФАКТУ ИСХОДА, а не по факту нажатия
            # (вклад ветки silent-failures: до неё «исполнено» писалось при
            # ЗАХВАТЕ, и провалившаяся операция потом читалась как успех).
            #
            # ПО ВЕРДИКТУ, А НЕ ПО ПЕРВОМУ СИМВОЛУ САМООТЧЁТА (Д7,
            # 2026-08-09). Здесь стояла обратная развилка: надгробие ставилось
            # по `_auto_execute_report_is_failure(report_text)`, то есть по
            # тому, с какого значка исполнитель начал писать о себе. Отчёт
            # ручного разбора начинается с нейтрального «### 🧾», по этому
            # признаку он «не отказ» — и план, где независимое чтение не
            # подтвердило НИ ОДНОЙ из трёх отправленных мутаций, получал
            # надгробие «выполнено», а следующий вызов слышал «✅ УЖЕ
            # исполнен… Повторять нечего, ничего не потеряно». Прежнее
            # обоснование («вердиктов больше, чем состояний, а "unverified"
            # не равно ни тому, ни другому») было верным по сути и снято
            # правильным способом — добавлением ЧЕТВЁРТОГО состояния, а не
            # огрублением вердикта до двух значений.
            #
            # Самоотчёт из решения не выброшен: он может только УХУДШИТЬ
            # исход. Это и есть «строжайший из двух сигналов» — см.
            # `_tombstone_reason_for_verdict`.
            reason = _tombstone_reason_for_verdict(
                verdict, _auto_execute_report_is_failure(report_text))
            if reason == _TOMBSTONE_EXECUTED:
                _tombstone_manifest(mid, reason)
            else:
                _tombstone_manifest(
                    mid, reason,
                    f"вердикт независимой сверки — {verdict}; "
                    + _first_line(report_text))
            await _run_blocking(
                _publish_auto_execute_outcome, c, tool, full_md, verdict,
                _manifest_affected_count(consumed))
        except Exception as e:
            if isinstance(e, asyncio.TimeoutError):
                msg = (f"🛑 Автоисполнение «{tool}» не уложилось в "
                       f"{_TG_AUTO_EXECUTE_CANDIDATE_TIMEOUT_S:.0f} c и было "
                       "прервано. План уже погашен — проверьте состояние в "
                       "TickTick: часть операции могла успеть примениться.")
            else:
                msg = f"🛑 Ошибка при автоисполнении «{tool}»: {_redact_for_user(e)}"
            # Падение ПОСЛЕ нажатия кнопки: состояние «нажато, но НЕ
            # выполнено». Раньше здесь не менялось ничего, и надгробие,
            # поставленное при захвате, продолжало утверждать «исполнено» —
            # тихий отказ в чистом виде.
            _tombstone_manifest(mid, _TOMBSTONE_FAILED, _first_line(msg))
            logger.exception(f"TG auto-execute: ошибка при исполнении {tool}/{mid}")
            if c.get(_OUTCOME_PUBLISHED_KEY):
                # СТРУКТУРНАЯ защита от второго отчёта (2026-08-06). Исключение
                # прилетело уже ПОСЛЕ того, как итог по этому кандидату начал
                # публиковаться, — то есть операция выполнена и о ней уже
                # доложено. Публикация «🛑 ошибка исполнения» поверх этого
                # положила бы в архив прямую ложь про успешную операцию.
                # Раньше от этого удерживали только внутренние try/except
                # внутри `_publish_auto_execute_outcome`, то есть дисциплина;
                # теперь удерживает признак, который нельзя забыть.
                logger.exception(f"TG auto-execute: сбой ПОСЛЕ публикации итога "
                                 f"({tool}/{mid}) — второй отчёт не публикую, "
                                 "чтобы не отменять уже доложенный результат")
                continue
            try:
                # Ошибка исполнения идёт по ТОМУ ЖЕ пути: полный текст (с
                # трассировкой смысла) — в группу как "failed", короткая
                # сводка — в личку. Так у владельца не остаётся операций,
                # про которые в архиве вообще ничего нет.
                err_md = "\n".join([
                    f"### 🛑 Автоисполнение «{tool}» — ошибка исполнения",
                    f"_manifest: `{mid}`_",
                    "",
                    # `msg` уже различает обычное исключение и срабатывание
                    # потолка на кандидата (asyncio.TimeoutError) — второй
                    # случай обязан звучать иначе: план ПОГАШЕН, а операция
                    # могла успеть примениться частично. Без этой строки
                    # таймаут в архиве был бы неотличим от обычной ошибки.
                    msg,
                    "",
                    f"Исключение: `{type(e).__name__}: {_redact_for_user(e)}`",
                    "",
                    "Мутация могла быть выполнена ЧАСТИЧНО — исход не "
                    "подтверждён; проверь `operation_report` по этому "
                    "manifest_id.",
                ])
                await _run_blocking(_publish_auto_execute_outcome,
                                    c, tool, err_md, "failed", None)
            except Exception:
                pass

    # АВАРИЙНАЯ ветка — строки, подтверждённые кнопкой, под которые исполнять
    # нечего (план не пережил перезапуск процесса). Идёт ПОСЛЕ исполнения
    # кандидатов сознательно: сначала делаем то, что можно сделать, и только
    # потом объясняемся про то, чего сделать нельзя. Отдельным try — этот блок
    # ходит в файл, в Postgres и в Telegram, и ни один его сбой не имеет права
    # ни уронить поллер, ни повлиять на уже исполненных кандидатов.
    try:
        lost = _tg_lost_manifest_rows(rows, now_ms=now_ms)
        if lost:
            await _run_blocking(_announce_lost_manifests, lost)
    except Exception:  # noqa: BLE001
        logger.exception("TG lost-plan: разбор потерянных планов упал "
                         "(поллер продолжает работать)")


def _consume_manifest_for_auto_execute(manifest_id: str) -> Optional[Dict]:
    """Атомарный захват плана — один раз и только один, чего бы ни случилось.

    ЧТО ИЗМЕНИЛОСЬ В #91. Раньше одноразовость держалась на однопоточности
    event loop'а: между проверкой `consumed` и его простановкой не было ни
    одного `await`, значит перебить друг друга два тика физически не могли.
    Эта гарантия работала ровно в границах ОДНОГО процесса — а именно их
    задача #91 и ломает: план теперь переживает перезапуск, и претендовать на
    него могут старый и новый контейнеры во время выкатки или две реплики.

    Поэтому решает теперь ОДИН SQL-оператор `UPDATE … WHERE consumed_at IS
    NULL … RETURNING` (`manifest_store.claim`), а не флаг в памяти: Postgres
    блокирует строку, второй претендент дожидается коммита первого и уходит
    ни с чем. Флаг в RAM остаётся, но уже как кэш решения, а не как сам
    механизм.

    ЗОВЁТСЯ ИЗ РАБОЧЕГО ПОТОКА (`_run_blocking`), потому что ходит в базу, —
    в отличие от прежней версии, которой полагалось быть в event loop'е.
    Ключей в `_MANIFESTS` эта функция НЕ добавляет и НЕ удаляет (только правит
    поля уже существующего плана), поэтому итерации по словарю в event loop'е
    она не ломает.

    Три исхода захвата:
      • CLAIM_WON    — план наш, исполняем;
      • CLAIM_TAKEN  — строка есть, но занята (соседний тик, вторая реплика,
                       старый контейнер) или просрочена → молча пропускаем;
      • CLAIM_ABSENT — строки в базе нет вовсе (долговечность отключена или
                       план не сохранился): падаем на прежнюю проверку по
                       памяти, которая для видимого только нам плана и есть
                       полноценная защита."""
    outcome, payload = manifest_store.claim(manifest_id)
    if outcome == manifest_store.CLAIM_TAKEN:
        return None
    m = _MANIFESTS.get(manifest_id)
    if m is None:
        if outcome != manifest_store.CLAIM_WON:
            return None
        # Плана нет в памяти, но он есть в базе и достался нам: этот процесс
        # его не строил (перезапуск между планом и нажатием). Работаем по
        # восстановленной копии; в `_MANIFESTS` её кладёт event loop —
        # см. `_restore_manifests_from_db`.
        m = _manifest_from_payload(payload or {})
    elif m.get("consumed"):
        # Память говорит «уже израсходован», база — «свободен». Такое
        # расхождение возможно, если гашение по чат-пути не доехало до базы.
        # Верим ПАМЯТИ (fail-closed): пропустить исполнение безопаснее, чем
        # выполнить дважды. Строка при этом уже помечена израсходованной, что
        # и требуется — план больше никого не соблазнит.
        logger.warning(f"Манифесты: план {manifest_id} свободен в базе, но "
                       "погашен в памяти — исполнять не буду")
        return None
    m["consumed"] = True
    # Надгробие ставится и здесь, ДО исполнения, — но со статусом «ЗАХВАЧЕН,
    # исход неизвестен», а не «исполнен». Захват обязан фиксироваться сразу:
    # `consumed` уже выставлен, второго шанса у плана нет, и модель, честно
    # позвавшая execute_* по тому же id, должна получить внятный ответ, а не
    # безликое «не найден/истёк». Но именно «исполнено» на этом месте было
    # ВРАНЬЁМ (см. блок про три состояния над _tombstone_manifest): статус
    # переписывается на _TOMBSTONE_EXECUTED / _TOMBSTONE_FAILED в
    # `_tg_auto_execute_tick` — по факту исхода, а не по факту нажатия.
    _tombstone_manifest(manifest_id, _TOMBSTONE_CLAIMED)
    return m


_TG_AUTO_EXECUTE_INTERVAL_S = float(os.environ.get("TG_AUTO_EXECUTE_INTERVAL_S", "10"))
# Как часто чистим просроченные подтверждения (пункт 4 ТЗ: сообщение, по
# которому решения так и не приняли, УДАЛЯЕТСЯ целиком). Отдельный интервал, а
# не «на каждом тике»: тик — 10 секунд, а уборка ходит в Postgres и в Telegram
# — гонять её в 6 раз чаще, чем нужно, смысла нет.
_TG_REAP_INTERVAL_S = float(os.environ.get("TG_REAP_INTERVAL_S", "60"))


async def _tg_auto_execute_poller_loop() -> None:
    """Background loop started from main() (see _run_server() below) only
    when TG_APPROVAL_ENABLED — every ~10s (env-tunable), forever, for the
    life of the process. A single tick's exception must never kill the loop
    (Maksim would then lose auto-execute silently until the next deploy).

    Два независимых TTL — честно про то, что будет, если их развести:
      * `_MANIFEST_TTL` (3600 c) — жизнь манифеста в RAM ЭТОГО процесса;
      * `TG_APPROVAL_TTL_S` (тоже 3600 c) — `expires_at` строки в Postgres,
        по которому `check_approval` начинает отвечать "none", а reaper
        сносит сообщение.
    Они НЕ связаны кодом, совпадение значений — соглашение, а не инвариант.
    Если RAM-TTL сделать КОРОЧЕ: манифест умрёт раньше строки — кнопка в
    Telegram «сработает» (строка станет APPROVED), но исполнять будет уже
    нечего (`_find_tg_auto_execute_candidates` не найдёт манифест в
    `_MANIFESTS`), и позже reaper уберёт сообщение — операция тихо не
    произойдёт. Если RAM-TTL сделать ДЛИННЕЕ: строка протухнет первой,
    `check_approval` вернёт "none", манифест доживёт в памяти бесполезным
    грузом до `_prune_manifests`. Тот же эффект, что и при рестарте процесса
    между планом и нажатием: RAM пуста, а строка в Postgres жива.

    Интервал отсчитывается ОТ НАЧАЛА прохода (2026-08-06). Раньше `sleep`
    стоял ПЕРЕД работой, то есть реальный период = интервал + длительность
    прохода: медленный проход (десятки живых планов × поездка в базу каждый)
    молча растягивал заявленные «каждые 10 секунд» в разы. Проходы при этом
    не накладываются и сейчас: они последовательны внутри одной корутины, а
    отрицательный остаток обрезается до нуля. Уборка (reaper) считается в тот
    же бюджет прохода — она стоит перед замером `elapsed`, поэтому её время
    не добавляется к интервалу сверху."""
    logger.info(f"TG auto-execute: poller started (every {_TG_AUTO_EXECUTE_INTERVAL_S:.0f}s)")
    last_reap = time.monotonic()
    while True:
        started = time.monotonic()
        try:
            await _tg_auto_execute_tick()
        except Exception:
            logger.exception("TG auto-execute poller: unhandled error")
        # Уборка — отдельным таймером по монотонным часам (не по системным:
        # перевод времени/NTP-скачок не должен ни заморозить уборку на час,
        # ни устроить её на каждом тике). Падение уборки НИКОГДА не убивает
        # поллер — иначе просроченный мусор в Telegram стоил бы владельцу
        # всего автоисполнения до следующего деплоя.
        now = time.monotonic()
        if now - last_reap >= _TG_REAP_INTERVAL_S:
            last_reap = now
            # Флаг живёт в конфиге tg_approval (env TG_REAP_ENABLED); читаем
            # через getattr, чтобы старый конфиг без этого поля не ронял
            # поллер AttributeError'ом — по умолчанию уборка включена.
            if getattr(_TG_CFG, "reap_enabled", True):
                try:
                    # В ПОТОК (2026-08-06, #115): уборка — это `DELETE …
                    # RETURNING` в Postgres плюс ОТДЕЛЬНЫЙ синхронный
                    # deleteMessage на КАЖДОЕ прибираемое сообщение (а у
                    # длинного плана их несколько). Десяток просроченных
                    # планов = десятки HTTP-вызовов подряд; вызванные прямо из
                    # этой корутины, они морозили сервер ровно так же, как
                    # отправка плана — просто раз в час, а не на каждый план.
                    n = await _run_blocking(tg_approval.reap_expired, _TG_CFG)
                    if n:
                        logger.info("TG reaper: прибрано просроченных "
                                    f"подтверждений: {n}")
                    # Уборка просроченных ПЛАНОВ — в том же такте и тем же
                    # потоком (#91). Без неё таблица растёт вечно, а в ней
                    # лежит полное содержимое каждой показанной операции.
                    # Отсрочка ровно та же, с какой разбор потерянных планов
                    # ещё смотрит на строки подтверждений
                    # (`_TG_LOST_LOOKBACK_S`): снеси план раньше — и владелец
                    # вместо внятного объяснения получил бы «операция
                    # неизвестна».
                    gone = await _run_blocking(
                        manifest_store.purge_expired,
                        int(_TG_LOST_LOOKBACK_S * 1000))
                    if gone:
                        logger.info(f"Манифесты: убрано просроченных планов: {gone}")
                except Exception:
                    logger.exception("TG reaper: уборка упала (поллер продолжает "
                                     "работать)")
        elapsed = time.monotonic() - started
        if elapsed > _TG_AUTO_EXECUTE_INTERVAL_S:
            logger.warning(
                f"TG auto-execute: проход занял {elapsed:.1f} c — дольше "
                f"интервала {_TG_AUTO_EXECUTE_INTERVAL_S:.0f} c")
        await asyncio.sleep(max(0.0, _TG_AUTO_EXECUTE_INTERVAL_S - elapsed))


# === MOVED-BLOCK END ===
