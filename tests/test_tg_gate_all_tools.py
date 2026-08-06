"""Telegram-кнопка для ВСЕХ тулов, проходящих через `_gate_batch`/
`_gate_single` (2026-08-06).

До этой правки сообщение с кнопками ✅/🛑 уходило владельцу ровно для двух
операций (`delete_tasks` и намеренно отключённый `execute_declutter`), а
остальные 19 мутирующих тулов исполнялись по одному лишь текстовому
`user_reply` — то есть по строке, которую модель физически способна сочинить
сама. Теперь план любого из этих 19 уходит в Telegram, а нажатие кнопки
исполняет операцию на сервере САМО, через ОДИН generic-исполнитель
(`_generic_gate_auto_execute`), а не через 19 ручных регистраций.

Что здесь закреплено навсегда:
  1. Слой ВЫКЛЮЧЕН (дефолт самохостера) → текст call #1 побайтово прежний,
     `tg_approval` не трогается вовсе, chat-«да» работает.
  2. Слой включён → call #1 реально зовёт notify_plan с ИМЕНЕМ СВОЕГО тула,
     манифест помечен `tg_notified`.
  3. Кнопка не нажата (pending) → честное «да» в чате НЕ исполняет ничего и
     НЕ гасит план.
  4. Кнопка «Отклонить» → отказ + план аннулирован.
  5. Отправка в Telegram упала → fail-closed: манифест инвалидирован.
  6. `_generic_gate_rehash` побайтово воспроизводит формулу, по которой хэш
     считался при планировании (иначе binding-проверка кнопки — фикция).
  7. Для КАЖДОГО гейтованного тула кнопочный путь зовёт ТУ ЖЕ `_<tool>_impl`
     с ТЕМИ ЖЕ аргументами, что и обычный чат-путь.
  8. Поллер видит такие манифесты (approved) и не видит их при pending.
  9. Регресс-страховка через inspect.signature: `_<tool>_impl` существует и
     его обязательные параметры покрыты тем, что лежит в манифесте.

Ни сети, ни Postgres: `tg_approval`-функции и все `_<tool>_impl`
монкейпатчатся, как в test_gate_tg_determinism.py / test_tg_auto_execute.py.

2026-08-06 (позже в тот же день): к таблице добавлен 20-й тул —
`create_attachment_upload_url`. Он ничего не пишет сам, но ВЫДАЁТ
предъявительскую ссылку на запись в аккаунт владельца (публичный маршрут
PUT /ul/{token}, многоразовый токен до 120 минут), поэтому подтверждение
нужно ровно в момент выдачи — см. комментарий над тулом в server.py.
"""
import inspect
import re

import pytest

import ticktick_mcp.src.server as s
import ticktick_mcp.src.tg_approval as tg


# ───────────────────────── таблица 19 гейтованных тулов ─────────────────────
# kwargs call #1 — ровно то, чем реальный вызывающий позвал бы тул. Форма
# гейта ("batch"/"single") здесь ОЖИДАНИЕ теста, а не копия кода: если тул
# переедет с одного гейта на другой, тесты об этом скажут.
BATCH_TOOLS = {
    "update_tasks": dict(
        summary="Правлю 1", tasks=[{"taskId": "t1", "title": "A", "new_title": "B"}]),
    "complete_tasks": dict(
        summary="Закрываю 1", tasks=[{"taskId": "t1", "title": "A", "projectId": "p1"}]),
    "move_tasks": dict(
        summary="Переношу 1", tasks=[{"taskId": "t1", "title": "A"}],
        to_project_id="p2", to_project_name="Проект-2"),
    "set_task_parent": dict(
        summary="Вкладываю 1", tasks=[{"taskId": "t1", "title": "A"}],
        parent_task_id="par1", project_id="p1", parent_task_title="Родитель"),
    "set_task_tags": dict(
        summary="Перетегиваю 1", tasks=[{"taskId": "t1", "title": "A", "tags": ["дом"]}]),
    "restore_tasks": dict(
        summary="Восстанавливаю 1", tasks=[{"taskId": "t1", "title": "A"}],
        to_project_id="p2"),
    # manual_triage — единственный в таблице, чей call #1 читает ЖИВОЕ
    # состояние ещё до гейта (identity guard по каждому переданному id),
    # поэтому `_no_client_checks` ниже подменяет `_open_by_id` /
    # `_v2_project_names` детерминированным состоянием ровно с этой задачей.
    "manual_triage": dict(
        summary="Разбираю 1",
        operations=[{"op": "complete", "task_id": "t1", "title": "A",
                     "said": "уже сделал"}]),
}

SINGLE_TOOLS = {
    "create_project": dict(name="Проект", color="#F18181", view_mode="list"),
    "create_subtask": dict(parent_task_title="Родитель", subtask_title="Подзадача",
                           parent_task_id="par1", project_id="p1",
                           content="текст", priority=3),
    "checkin_habit": dict(habit_name="Зарядка", habit_id="h1", date="2026-08-06",
                          status=2, value=1.0),
    "unset_task_parent": dict(task_title="A", parent_task_title="Родитель",
                              task_id="t1", parent_task_id="par1", project_id="p1"),
    "create_project_group": dict(name="Группа"),
    "delete_project_group": dict(group_name="Группа", group_id="g1"),
    "move_project_to_group": dict(project_name="Проект", project_id="p1", group_id="g1"),
    "add_task_comment": dict(task_title="A", text="коммент", project_id="p1", task_id="t1"),
    "attach_file_to_task": dict(task_title="A", task_id="t1", project_id="p1",
                                url="https://example.com/f.pdf", content_base64=None,
                                filename="f.pdf"),
    "create_tag": dict(name="дом", color="#FF6161"),
    "duplicate_task": dict(summary="Дублирую", task_id="t1", task_title="A"),
    "update_task_comment": dict(task_title="A", text="новый текст", project_id="p1",
                                task_id="t1", comment_id="c1"),
    "create_project_column": dict(project_id="p1", name="Колонка", project_name="Проект"),
    "create_attachment_upload_url": dict(task_id="t1", project_id="p1",
                                         filename="scan.png", ttl_minutes=15),
}

ALL_TOOLS = {**BATCH_TOOLS, **SINGLE_TOOLS}
GATE_OF = {**{t: "batch" for t in BATCH_TOOLS}, **{t: "single" for t in SINGLE_TOOLS}}


def test_the_table_covers_exactly_twenty_one_tools():
    """Если кто-то добавит/уберёт гейтованный тул, эта таблица обязана
    поехать вместе с ним — иначе новый тул молча останется без кнопки.
    19 → 21 (2026-08-06): +1 batch = `manual_triage`, +1 single =
    `create_attachment_upload_url` (пришёл из feat/tg-button-...)."""
    assert len(BATCH_TOOLS) == 7
    assert len(SINGLE_TOOLS) == 14
    assert len(ALL_TOOLS) == 21


# ───────────────────────── обвязка ─────────────────────────

@pytest.fixture(autouse=True)
def _public_base_url(monkeypatch):
    """`create_attachment_upload_url` отказывает ДО гейта, если сервер не знает
    своего публичного адреса (проверка конфигурации, а не согласия). Здесь
    проверяется гейт, поэтому адрес задан — иначе call #1 вернул бы не план,
    а «задайте PUBLIC_BASE_URL». Остальным 20 тулам эта переменная безразлична."""
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://tt.example.com")
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)


@pytest.fixture(autouse=True)
def _isolate_manifests():
    """_MANIFESTS — общий модульный глобал на всю сессию: снимаем копию,
    работаем на чистом, возвращаем как было (порядок файлов в прогоне не
    должен влиять ни на нас, ни на нас — на соседей)."""
    before = dict(s._MANIFESTS)
    tombs = dict(s._MANIFEST_TOMBSTONES)
    s._MANIFESTS.clear()
    s._MANIFEST_TOMBSTONES.clear()
    yield
    s._MANIFESTS.clear()
    s._MANIFESTS.update(before)
    s._MANIFEST_TOMBSTONES.clear()
    s._MANIFEST_TOMBSTONES.update(tombs)


def _tg_on(monkeypatch, allowlist=None):
    monkeypatch.setattr(s, "_TG_CFG", tg.TgApprovalConfig(
        enabled=True, bot_token="x", owner_chat_id="1", server="ticktick",
        tools_allowlist=allowlist, ttl_s=3600))


def _tg_off(monkeypatch):
    monkeypatch.setattr(s, "_TG_CFG", tg.TgApprovalConfig(
        enabled=False, bot_token="", owner_chat_id="", server="ticktick",
        tools_allowlist=None, ttl_s=3600))


# Детерминированное «живое состояние» для тулов, которые сверяют переданные
# id ДО гейта (сегодня это только manual_triage — его identity guard обязан
# работать на фазе плана, иначе человек увидел бы план по несуществующим
# задачам). Остальные 20 тулов сюда не заглядывают вовсе.
_LIVE_TASKS = {"t1": {"id": "t1", "title": "A", "projectId": "p1"}}
_LIVE_PROJECTS = {"p1": "Проект-1", "p2": "Проект-2"}


def _no_client_checks(monkeypatch):
    """Ни один из 20 тулов не должен ходить в СЕТЬ до гейта — глушим проверки
    готовности клиента и подменяем чтение живого состояния локальным dict."""
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_ensure_official", lambda: None)
    monkeypatch.setattr(s, "_open_by_id",
                        lambda fresh=False: {k: dict(v) for k, v in _LIVE_TASKS.items()})
    monkeypatch.setattr(s, "_v2_project_names", lambda: dict(_LIVE_PROJECTS))


def _stub_impl(monkeypatch, tool, recorder):
    """Подменяет `_<tool>_impl` записывающей заглушкой. Именно её вызов (а не
    просто «что-то исполнилось») и есть проверяемый факт."""
    async def _impl(*a, **kw):
        recorder.append((a, kw))
        return f"### ✅ {tool} исполнен (заглушка)"
    monkeypatch.setattr(s, f"_{tool}_impl", _impl)
    return recorder


_MID_RE = re.compile(r"Манифест `([0-9a-f]{6,})`")


def _mid_of(text):
    m = _MID_RE.search(text)
    assert m, f"в превью нет id манифеста:\n{text}"
    return m.group(1)


def _notify_ok(monkeypatch, seen=None):
    def _notify(cfg, manifest_id, preview, tool):
        if seen is not None:
            seen.append({"manifest_id": manifest_id, "tool": tool, "preview": preview})
        return True, ""
    monkeypatch.setattr(tg, "notify_plan", _notify)


async def _plan(tool, monkeypatch):
    """call #1: возвращает (текст превью, id манифеста)."""
    out = await getattr(s, tool)(**ALL_TOOLS[tool])
    return out, _mid_of(out)


# ═══════ 1. Слой ВЫКЛЮЧЕН — поведение обязано быть побайтово прежним ═══════

@pytest.mark.parametrize("tool", sorted(ALL_TOOLS))
async def test_layer_off_preview_is_unchanged_and_yes_still_works(tool, monkeypatch):
    _no_client_checks(monkeypatch)
    _tg_off(monkeypatch)
    calls = {"notify": 0, "check": 0}
    monkeypatch.setattr(tg, "notify_plan",
                        lambda *a, **k: calls.__setitem__("notify", calls["notify"] + 1) or (True, ""))
    monkeypatch.setattr(tg, "check_approval",
                        lambda *a, **k: calls.__setitem__("check", calls["check"] + 1) or "none")
    rec = _stub_impl(monkeypatch, tool, [])

    preview, mid = await _plan(tool, monkeypatch)

    # Ровно тот текст, что был до правки: ни строчки про Telegram, и хвост —
    # прежний («…действует 1 час.»), т.е. суффикс уведомления не приклеен.
    assert "Telegram" not in preview
    assert preview.rstrip().endswith("Манифест одноразовый, действует 1 час.")
    assert calls == {"notify": 0, "check": 0}
    assert "tg_notified" not in s._MANIFESTS[mid]

    out = await getattr(s, tool)(**ALL_TOOLS[tool], manifest_id=mid, user_reply="да")

    assert len(rec) == 1, f"chat-«да» не исполнил {tool}: {out}"
    assert calls == {"notify": 0, "check": 0}   # tg_approval не тронут вовсе


# ═══════ 2. Слой включён — план реально уходит в Telegram ═══════

@pytest.mark.parametrize("tool", sorted(ALL_TOOLS))
async def test_plan_is_sent_to_telegram_with_its_own_tool_name(tool, monkeypatch):
    _no_client_checks(monkeypatch)
    _tg_on(monkeypatch)
    seen = []
    _notify_ok(monkeypatch, seen)

    preview, mid = await _plan(tool, monkeypatch)

    assert len(seen) == 1, f"план {tool} не ушёл в Telegram"
    assert seen[0]["tool"] == tool          # имя СВОЕГО тула, не чужого
    assert seen[0]["manifest_id"] == mid
    m = s._MANIFESTS[mid]
    assert m["tg_notified"] is True
    assert m["_tg_tool"] == tool
    assert m["tool"] == tool
    assert m["_gate"] == GATE_OF[tool]
    assert "Telegram" in preview            # модель предупреждена про кнопку


# ═══════ 3. Кнопка не нажата → честное «да» ничего не исполняет ═══════

@pytest.mark.parametrize("tool", sorted(ALL_TOOLS))
async def test_pending_button_blocks_execution_and_keeps_the_plan(tool, monkeypatch):
    _no_client_checks(monkeypatch)
    _tg_on(monkeypatch)
    _notify_ok(monkeypatch)
    rec = _stub_impl(monkeypatch, tool, [])
    _, mid = await _plan(tool, monkeypatch)
    monkeypatch.setattr(tg, "check_approval", lambda manifest_id: "pending")

    out = await getattr(s, tool)(**ALL_TOOLS[tool], manifest_id=mid, user_reply="да")

    assert rec == [], f"{tool} исполнился БЕЗ нажатой кнопки"
    assert "Telegram" in out
    assert s._MANIFESTS[mid]["consumed"] is False  # план ещё жив, не сожжён


# ═══════ 4. Кнопка «Отклонить» → отказ и план аннулирован ═══════

@pytest.mark.parametrize("tool", sorted(ALL_TOOLS))
async def test_rejected_button_kills_the_plan(tool, monkeypatch):
    _no_client_checks(monkeypatch)
    _tg_on(monkeypatch)
    _notify_ok(monkeypatch)
    rec = _stub_impl(monkeypatch, tool, [])
    _, mid = await _plan(tool, monkeypatch)
    monkeypatch.setattr(tg, "check_approval", lambda manifest_id: "rejected")

    out = await getattr(s, tool)(**ALL_TOOLS[tool], manifest_id=mid, user_reply="да")

    assert rec == []
    assert "Отклонено" in out
    assert s._MANIFESTS[mid]["consumed"] is True


async def test_approved_button_plus_chat_yes_executes(monkeypatch):
    """Зеркало теста 3: с нажатой кнопкой обычный execute проходит — иначе
    фикс превратил бы все 21 тул в вечный отказ."""
    _no_client_checks(monkeypatch)
    _tg_on(monkeypatch)
    _notify_ok(monkeypatch)
    for tool in ("complete_tasks", "create_tag"):
        rec = _stub_impl(monkeypatch, tool, [])
        _, mid = await _plan(tool, monkeypatch)
        monkeypatch.setattr(tg, "check_approval", lambda manifest_id: "approved")
        await getattr(s, tool)(**ALL_TOOLS[tool], manifest_id=mid, user_reply="да")
        assert len(rec) == 1, tool


# ═══════ 5. Отправка в Telegram упала → fail-closed ═══════

@pytest.mark.parametrize("tool", sorted(ALL_TOOLS))
async def test_failed_telegram_send_invalidates_the_plan(tool, monkeypatch):
    _no_client_checks(monkeypatch)
    _tg_on(monkeypatch)
    monkeypatch.setattr(tg, "notify_plan", lambda *a, **k: (False, "boom"))
    rec = _stub_impl(monkeypatch, tool, [])

    out = await getattr(s, tool)(**ALL_TOOLS[tool])

    assert "Не смог отправить" in out and "boom" in out
    assert "НЕ запланировано" in out
    assert rec == []
    # Манифест существует, но сожжён — «да» по нему не пройдёт никогда.
    mids = [mid for mid, m in s._MANIFESTS.items() if m.get("tool") == tool]
    assert len(mids) == 1 and s._MANIFESTS[mids[0]]["consumed"] is True
    monkeypatch.setattr(tg, "check_approval", lambda manifest_id: "approved")
    out2 = await getattr(s, tool)(**ALL_TOOLS[tool], manifest_id=mids[0], user_reply="да")
    assert rec == [], f"{tool} исполнился по сожжённому манифесту: {out2}"


# ═══════ 6. rehash побайтово воспроизводит формулу планирования ═══════

@pytest.mark.parametrize("tool", sorted(ALL_TOOLS))
async def test_generic_rehash_reproduces_the_stored_hash(tool, monkeypatch):
    _no_client_checks(monkeypatch)
    _tg_off(monkeypatch)
    _, mid = await _plan(tool, monkeypatch)
    m = s._MANIFESTS[mid]

    assert m.get("object_hash"), f"{tool}: манифест без object_hash — кнопка без привязки"
    assert s._generic_gate_rehash(m) == m["object_hash"]


async def test_rehash_changes_when_a_batch_task_is_swapped(monkeypatch):
    _no_client_checks(monkeypatch)
    _tg_off(monkeypatch)
    _, mid = await _plan("complete_tasks", monkeypatch)
    m = s._MANIFESTS[mid]
    before = s._generic_gate_rehash(m)

    m["tasks"] = [{"taskId": "ДРУГАЯ-ЗАДАЧА", "title": "A", "projectId": "p1"}]

    assert s._generic_gate_rehash(m) != before
    assert s._generic_gate_rehash(m) != m["object_hash"]


async def test_rehash_changes_when_a_single_param_is_swapped(monkeypatch):
    _no_client_checks(monkeypatch)
    _tg_off(monkeypatch)
    _, mid = await _plan("delete_project_group", monkeypatch)
    m = s._MANIFESTS[mid]
    before = s._generic_gate_rehash(m)

    m["params"] = dict(m["params"], group_id="ДРУГАЯ-ГРУППА")

    assert s._generic_gate_rehash(m) != before
    assert s._generic_gate_rehash(m) != m["object_hash"]


def test_params_hash_is_order_insensitive_but_value_sensitive():
    a = s._manifest_params_hash("create_tag", {"name": "дом", "color": "#FF6161"})
    b = s._manifest_params_hash("create_tag", {"color": "#FF6161", "name": "дом"})
    c = s._manifest_params_hash("create_tag", {"name": "дом", "color": "#000000"})
    d = s._manifest_params_hash("create_project", {"name": "дом", "color": "#FF6161"})
    assert a == b            # порядок ключей не должен ломать сверку
    assert a != c and a != d  # значение и действие — должны


# ═══════ 7. Кнопочный путь = чат-путь, для КАЖДОГО из 19 ═══════

@pytest.mark.parametrize("tool", sorted(ALL_TOOLS))
async def test_button_path_calls_the_same_impl_with_the_same_arguments(tool, monkeypatch):
    """Главный тест покрытия: `_generic_gate_auto_execute` обязан позвать ТУ
    ЖЕ `_<tool>_impl` и с теми же значениями В ТЕХ ЖЕ ПАРАМЕТРАХ, что и
    обычный execute по chat-«да». Любое расхождение — кнопка, делающая не то,
    что показали человеку.

    Сравнение семантическое (через настоящую сигнатуру `_impl`), а не
    «args/kwargs дословно»: чат-путь часть значений передаёт позиционно, а
    generic-исполнитель — по имени из `extra`; это одно и то же обращение.
    Перестановка же значений между параметрами (например summary↔tasks)
    здесь ловится — она меняет именно связку имя→значение."""
    _no_client_checks(monkeypatch)
    _tg_off(monkeypatch)
    real_sig = inspect.signature(getattr(s, f"_{tool}_impl"))

    def _bound(call):
        b = real_sig.bind(*call[0], **call[1])
        b.apply_defaults()
        return dict(b.arguments)

    rec = _stub_impl(monkeypatch, tool, [])

    # (а) обычный чат-путь
    _, mid = await _plan(tool, monkeypatch)
    await getattr(s, tool)(**ALL_TOOLS[tool], manifest_id=mid, user_reply="да")
    assert len(rec) == 1, f"chat-путь не позвал _{tool}_impl"
    chat_call = rec[-1]

    # (б) путь кнопки: тот же план, исполненный поллером
    _, mid2 = await _plan(tool, monkeypatch)
    out = await s._generic_gate_auto_execute(mid2, s._MANIFESTS[mid2])
    assert len(rec) == 2, f"кнопка не позвала _{tool}_impl: {out}"
    button_call = rec[-1]

    assert _bound(button_call) == _bound(chat_call), (
        f"{tool}: кнопка зовёт _{tool}_impl иначе, чем чат.\n"
        f"  чат:    {_bound(chat_call)!r}\n"
        f"  кнопка: {_bound(button_call)!r}")


async def test_generic_auto_execute_refuses_a_manifest_without_impl(monkeypatch):
    _no_client_checks(monkeypatch)
    with pytest.raises(RuntimeError, match="нет исполнителя"):
        await s._generic_gate_auto_execute("m-ghost", {
            "tool": "no_such_tool", "_gate": "single", "params": {}})


# ═══════ 8. Поллер видит новые манифесты ═══════

def _approved(monkeypatch, mids):
    """С 2026-08-06 поллер читает статусы ПАЧКОЙ (один get_tg_approvals на
    проход) вместо check_approval+get_tg_approval на каждый живой манифест —
    подменяем именно пакетную функцию. Одиночные никуда не делись, их всё так
    же зовёт чат-путь (_require_consent), см. тесты выше в этом файле."""
    monkeypatch.setattr(tg, "get_tg_approvals", lambda ids: {
        mid: {"status": "APPROVED", "expires_at": tg._now_ms() + 3_600_000,
              "chat_id": "c1", "message_id": 7}
        for mid in ids if mid in mids})


@pytest.mark.parametrize("tool", ["complete_tasks", "move_tasks", "create_tag",
                                  "create_project_column"])
async def test_poller_finds_approved_gate_manifests(tool, monkeypatch):
    _no_client_checks(monkeypatch)
    _tg_on(monkeypatch)
    _notify_ok(monkeypatch)
    _, mid = await _plan(tool, monkeypatch)
    _approved(monkeypatch, {mid})

    found = [c for c in s._find_tg_auto_execute_candidates() if c["manifest_id"] == mid]

    assert len(found) == 1, f"поллер не видит одобренный план {tool}"
    assert found[0]["tool"] == tool
    assert found[0]["chat_id"] == "c1" and found[0]["message_id"] == 7


@pytest.mark.parametrize("tool", ["complete_tasks", "create_tag"])
async def test_poller_ignores_pending_gate_manifests(tool, monkeypatch):
    _no_client_checks(monkeypatch)
    _tg_on(monkeypatch)
    _notify_ok(monkeypatch)
    _, mid = await _plan(tool, monkeypatch)
    monkeypatch.setattr(tg, "get_tg_approvals", lambda ids: {
        mid: {"status": "PENDING", "expires_at": tg._now_ms() + 3_600_000,
              "chat_id": "c1", "message_id": 7} for mid in ids})

    ids = {c["manifest_id"] for c in s._find_tg_auto_execute_candidates()}

    assert mid not in ids


@pytest.mark.parametrize("tool", ["set_task_tags", "add_task_comment"])
async def test_tick_executes_and_reports_back_into_the_message(tool, monkeypatch):
    """Сквозной проход поллера: нашёл → исполнил через generic-исполнитель →
    вписал отчёт в то же сообщение → манифест погашен и оставил надгробие."""
    _no_client_checks(monkeypatch)
    _tg_on(monkeypatch)
    _notify_ok(monkeypatch)
    rec = _stub_impl(monkeypatch, tool, [])
    _, mid = await _plan(tool, monkeypatch)
    _approved(monkeypatch, {mid})
    reports = []
    monkeypatch.setattr(tg, "report_auto_execution_result",
                        lambda cfg, chat_id, message_id, text: reports.append((chat_id, message_id, text)))

    await s._tg_auto_execute_tick()

    assert len(rec) == 1, f"тик не исполнил {tool}"
    assert s._MANIFESTS[mid]["consumed"] is True
    assert len(reports) == 1
    assert reports[0][0] == "c1" and reports[0][1] == 7
    assert "🛑" not in reports[0][2]
    # Модель, позвавшая execute следом, получает внятное «уже исполнено», а не
    # безликое «не найден/истёк».
    s._prune_manifests()
    out = await getattr(s, tool)(**ALL_TOOLS[tool], manifest_id=mid, user_reply="да")
    assert "УЖЕ исполнен" in out and "кнопкой в Telegram" in out


async def test_tick_does_not_touch_declutter_manifests(monkeypatch):
    """Declutter отключён владельцем намеренно — generic-исполнитель не
    должен его «вернуть»: у declutter-манифеста нет `_gate`, а его
    регистрация в _AUTO_EXECUTORS закомментирована."""
    _tg_on(monkeypatch)
    import time as _t
    s._MANIFESTS["dc1"] = {"kind": "declutter", "consumed": False,
                           "created": _t.monotonic(), "actions": {}}
    _approved(monkeypatch, {"dc1"})

    ids = {c["manifest_id"] for c in s._find_tg_auto_execute_candidates()}

    assert "dc1" not in ids
    assert "execute_declutter" not in s._AUTO_EXECUTORS


# ═══════ 8b. Манифест одного тула нельзя исполнить под именем другого ═══════
# Предохранитель «инвариант 2» в tg_approval.try_auto_execute читал поле
# `_auto_tool`, которого не писал НИКТО (см. комментарий там же) — то есть не
# срабатывал никогда. Пока кнопка была у одного тула, это было безвредно; с
# 21 тулом ошибка диспетчеризации означала бы исполнение ЧУЖОЙ операции по
# чужому подтверждению. Тестов на это не было — потому дыра и не всплыла.

async def test_manifest_is_not_executable_under_another_tools_name(monkeypatch):
    _no_client_checks(monkeypatch)
    _tg_on(monkeypatch)
    _notify_ok(monkeypatch)
    _, mid = await _plan("create_tag", monkeypatch)

    consumed = tg.try_auto_execute(
        manifest_id=mid, tool="complete_tasks",          # НЕ тот тул
        get_manifest=lambda i: s._MANIFESTS.get(i),
        consume_manifest=s._consume_manifest_for_auto_execute,
        rehash=s._generic_gate_rehash,
    )

    assert consumed is None
    assert s._MANIFESTS[mid]["consumed"] is False        # план цел, не сожжён


async def test_manifest_is_executable_under_its_own_tools_name(monkeypatch):
    """Зеркало: та же сверка не должна мешать законному исполнению."""
    _no_client_checks(monkeypatch)
    _tg_on(monkeypatch)
    _notify_ok(monkeypatch)
    _, mid = await _plan("create_tag", monkeypatch)

    consumed = tg.try_auto_execute(
        manifest_id=mid, tool="create_tag",
        get_manifest=lambda i: s._MANIFESTS.get(i),
        consume_manifest=s._consume_manifest_for_auto_execute,
        rehash=s._generic_gate_rehash,
    )

    assert consumed is not None
    assert s._MANIFESTS[mid]["consumed"] is True


async def test_tick_with_a_misdispatched_tool_executes_nothing(monkeypatch):
    """Сквозной вариант: поллер по ошибке определил тул как чужой — ни один
    `_impl` не должен быть вызван, план обязан остаться нетронутым."""
    _no_client_checks(monkeypatch)
    _tg_on(monkeypatch)
    _notify_ok(monkeypatch)
    rec_tag = _stub_impl(monkeypatch, "create_tag", [])
    rec_other = _stub_impl(monkeypatch, "create_project_group", [])
    _, mid = await _plan("create_tag", monkeypatch)
    _approved(monkeypatch, {mid})
    # симулируем ровно ошибку диспетчеризации
    monkeypatch.setattr(s, "_auto_execute_tool_of", lambda m: "create_project_group")
    reports = []
    monkeypatch.setattr(tg, "report_auto_execution_result",
                        lambda cfg, chat_id, message_id, text: reports.append(text))

    await s._tg_auto_execute_tick()

    assert rec_tag == [] and rec_other == []
    assert s._MANIFESTS[mid]["consumed"] is False
    assert reports == []


# ═══════ 9. Регресс-страховка по сигнатурам ═══════

@pytest.mark.parametrize("tool", sorted(ALL_TOOLS))
async def test_impl_signature_is_satisfied_by_what_the_manifest_stores(tool, monkeypatch):
    """Сломается, если кто-то переименует `_<tool>_impl`, добавит ему
    обязательный параметр или уберёт ключ из манифеста — то есть ровно
    тогда, когда кнопка начнёт падать в проде."""
    _no_client_checks(monkeypatch)
    _tg_off(monkeypatch)
    impl = getattr(s, f"_{tool}_impl", None)
    assert callable(impl), f"нет _{tool}_impl — generic-исполнителю нечего звать"
    assert inspect.iscoroutinefunction(impl)

    _, mid = await _plan(tool, monkeypatch)
    m = s._MANIFESTS[mid]
    sig = inspect.signature(impl)

    if m["_gate"] == "single":
        sig.bind(**(m.get("params") or {}))       # бросит TypeError при расхождении
    else:
        sig.bind(m.get("summary") or "", m.get("tasks") or [], **(m.get("extra") or {}))
