"""ГЛАВНЫЙ ТЕСТ ПРИЁМКИ задачи #118: ключ автоматики обходит подтверждение
НА ПЕРВОМ ЖЕ ВЫЗОВЕ — плана нет, кнопки владельцу нет, действие выполнено.

Дефект, который здесь закрывается (найден живым прогоном приёмки): у
`delete_tasks`, `create_project`, `create_project_group`, `create_tag`,
`delete_tag`, `move_project_to_group` сверка ключа стояла ВНУТРИ ветки
второго вызова (`_require_consent`). Поэтому headless-клиент (n8n) на первом
вызове всё равно строил план и отправлял владельцу сообщение с кнопками,
которое никто никогда не нажмёт, а операция происходила только со второго
вызова. За один прогон так накопилось 13 висящих планов. Опасность не в
мусоре в личке: владелец приучается жать кнопку не глядя, и механизм
подтверждения обесценивается для тех операций, ради которых он и заведён.

ПОЧЕМУ ТЕСТ СМОТРИТ НАРУЖУ, А НЕ ВНУТРЬ. В этом репозитории уже трижды
подтверждалось, что добрый тестовый двойник прячет дефект. Поэтому здесь:
  • «строка ожидающего подтверждения» — SELECT из НАСТОЯЩЕГО Postgres
    (таблица `tg_approvals`), той же базы, что в проде; план в базе —
    таблица `mcp_manifests`;
  • «сообщение ушло в Telegram» — перехват на самом нижнем слое, `_tg_call`
    (единственное место, откуда уходит HTTP-запрос к Bot API): видно и метод,
    и тело с инлайн-кнопками;
  • «действие выполнено» — по эффекту у клиента: реальный вызов исполнителя
    (для удаления — исчезнувшая задача в фейковом TickTick), плюс сам текст
    ответа, который получает вызывающий.
Ни одна проверка не смотрит на `_MANIFESTS`, `tg_notified` и прочую кухню
сервера — они могут остаться какими угодно, лишь бы наружу ничего не ушло.

НЕГАТИВНАЯ ПОЛОВИНА ВАЖНЕЕ ПОЗИТИВНОЙ. Ровно здесь ошибка означала бы «любой
вызов исполняется без спроса», поэтому мимо подтверждения не должен
проскочить ни один из случаев: ключ неверный, пустой, с посторонними
символами (не-ASCII), обрезанный/удлинённый настоящий, ключ не передан вовсе.
"""


import pytest

import ticktick_mcp.src.server as s
import ticktick_mcp.src.tg_approval as tg
from tests.pg_helper import fresh_stores, requires_pg

pytestmark = requires_pg


# ─────────────────────────── обвязка ───────────────────────────

class _Sent:
    """Всё, что сервер попытался отправить в Telegram."""

    def __init__(self):
        self.calls = []          # (method, body)

    def messages(self):
        return [b for m, b in self.calls if m == "sendMessage"]

    def with_buttons(self):
        return [b for b in self.messages() if b.get("reply_markup")]


class _FakeV2Delete:
    def __init__(self, live):
        self._live = live
        self.deleted_ids = []

    def batch_delete_tasks(self, items):
        for it in items:
            self._live.pop(it["taskId"], None)
            self.deleted_ids.append(it["taskId"])
        return {}


def _rows_in_tg_approvals():
    """Сколько строк подтверждения этот сервер завёл в НАСТОЯЩЕЙ базе."""
    from ticktick_mcp.src import manifest_store
    with manifest_store._conn() as cur:
        cur.execute("SELECT status FROM tg_approvals WHERE server = 'ticktick'")
        return [r[0] for r in cur.fetchall()]


def _rows_in_mcp_manifests():
    from ticktick_mcp.src import manifest_store
    with manifest_store._conn() as cur:
        cur.execute("SELECT manifest_id FROM mcp_manifests")
        return [r[0] for r in cur.fetchall()]


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Живой Postgres + включённый Telegram-слой + заглушенный TickTick.

    Telegram включён НАМЕРЕННО: именно в этой конфигурации дефект и виден —
    при выключенном слое кнопки не уходят никому и проверять было бы нечего.
    """
    from ticktick_mcp.src import manifest_store

    fresh_stores()                      # обе таблицы: пустые, на живой базе
    monkeypatch.setattr(s, "_TG_CFG", tg.TgApprovalConfig(
        enabled=True, bot_token="test-token", owner_chat_id="4242",
        server="ticktick", tools_allowlist=None, ttl_s=3600))
    sent = _Sent()

    def _fake_tg_call(cfg, method, body):
        sent.calls.append((method, body))
        return {"ok": True, "result": {"message_id": 1000 + len(sent.calls)}}

    monkeypatch.setattr(tg, "_tg_call", _fake_tg_call)
    monkeypatch.setattr(tg, "_INTER_CHUNK_PAUSE_S", 0)
    monkeypatch.setattr(s, "_ensure_ready", lambda: "")
    monkeypatch.setattr(s, "_ensure_official", lambda: "")
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    s._MANIFESTS.clear()
    s._MANIFEST_TOMBSTONES.clear()
    yield sent
    s._MANIFESTS.clear()
    s._MANIFEST_TOMBSTONES.clear()
    manifest_store.close_store()
    tg._pg_pool = None


@pytest.fixture
def tag_spy(monkeypatch):
    """Исполнитель create_tag заменён шпионом: TickTick не трогаем вовсе."""
    calls = []

    async def _spy(name, color=None):
        calls.append({"name": name, "color": color})
        return f"✅ Тег «{name}» создан."

    monkeypatch.setattr(s, "_create_tag_impl", _spy)
    return calls


@pytest.fixture
def complete_spy(monkeypatch):
    calls = []

    async def _spy(summary, tasks):
        calls.append([t.get("taskId") for t in tasks])
        return "✅ Завершено 1 из 1"

    monkeypatch.setattr(s, "_complete_tasks_impl", _spy)
    return calls


@pytest.fixture
def deletion(monkeypatch):
    """Настоящий движок удаления поверх фейкового TickTick."""
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    fake = _FakeV2Delete(live)
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Покупки"})
    monkeypatch.setattr(s, "ticktick_v2", fake)
    return live, fake


_DELETE_ARGS = ("⚠️ Удаляю 1", [{"title": "Купить молоко", "taskId": "t1",
                                 "projectId": "p1", "projectName": "Покупки"}])

# Ключи, которые НЕ должны открывать дверь. Каждый — отдельный класс ошибки,
# а не «ещё одна неверная строка»: пустой (клиент не настроен), чужой,
# не-ASCII (модель «сочинила ключ» — на нём когда-то падало само сравнение),
# обрезанный и удлинённый настоящий (частичное совпадение), пробельный.
_BAD_KEYS = [
    pytest.param("", id="пустой"),
    pytest.param("not-the-real-secret", id="чужой"),
    pytest.param("секретный-ключ-🔑", id="не-ASCII"),
    pytest.param("test-secre", id="обрезанный-настоящий"),
    pytest.param("test-secretX", id="удлинённый-настоящий"),
    pytest.param(" test-secret ", id="с-пробелами"),
    pytest.param("TEST-SECRET", id="другой-регистр"),
]


# ═══════════ 1. Верный ключ: ни плана, ни кнопки — сразу дело ═══════════

async def test_valid_key_creates_the_tag_without_any_plan_or_button(
        wired, tag_spy):
    """_gate_single: create_tag с верным ключом — ПЕРВЫЙ вызов исполняет."""
    out = await s.create_tag(name="из-автоматики", automation_key=s.SECRET)

    assert tag_spy == [{"name": "из-автоматики", "color": None}], \
        "действие не выполнено — ключ должен исполнять сразу"
    assert wired.calls == [], f"в Telegram что-то ушло: {wired.calls}"
    assert _rows_in_tg_approvals() == [], "заведена строка ожидающего подтверждения"
    assert _rows_in_mcp_manifests() == [], "план сохранён в базе — значит он строился"
    # То, что видит клиент: результат, а не превью с просьбой позвать снова.
    assert "manifest_id" not in out and "📋" not in out, out


async def test_valid_key_completes_tasks_without_any_plan_or_button(
        wired, complete_spy):
    """_gate_batch: списочный тул ведёт себя ровно так же."""
    out = await s.complete_tasks("Закрываю 1", [{"title": "Отчёт", "taskId": "t9",
                                                 "projectId": "p1"}],
                                 automation_key=s.SECRET)

    assert complete_spy == [["t9"]]
    assert wired.calls == [], f"в Telegram что-то ушло: {wired.calls}"
    assert _rows_in_tg_approvals() == []
    assert _rows_in_mcp_manifests() == []
    assert "manifest_id" not in out and "📋" not in out, out


async def test_valid_key_deletes_the_task_without_any_plan_or_button(
        wired, deletion):
    """Собственный гейт delete_tasks (🔴): та же дисциплина, что у общих."""
    live, fake = deletion

    out = await s.delete_tasks(*_DELETE_ARGS, automation_key=s.SECRET)

    assert fake.deleted_ids == ["t1"], "задача не удалена"
    assert "t1" not in live
    assert wired.calls == [], f"в Telegram что-то ушло: {wired.calls}"
    assert _rows_in_tg_approvals() == []
    assert _rows_in_mcp_manifests() == []
    assert "manifest_id" not in out and "📋 Готов удалить" not in out, out


# ═══════ 1b. Буквально те шесть тулов, на которых дефект был найден ═══════

# (tool, kwargs, имя исполнителя, ожидаемый вызов исполнителя)
_SIX = [
    pytest.param("delete_tasks", {}, None, None, id="delete_tasks"),
    pytest.param("create_project", {"name": "Новый"}, "_create_project_impl",
                 {"name": "Новый", "color": "#F18181", "view_mode": "list"},
                 id="create_project"),
    pytest.param("create_project_group", {"name": "Группа"},
                 "_create_project_group_impl", {"name": "Группа"},
                 id="create_project_group"),
    pytest.param("create_tag", {"name": "тег"}, "_create_tag_impl",
                 {"name": "тег", "color": None}, id="create_tag"),
    pytest.param("delete_tag", {"name": "тег"}, "_delete_tag_impl",
                 {"name": "тег"}, id="delete_tag"),
    pytest.param("move_project_to_group",
                 {"project_name": "Проект", "project_id": "p1",
                  "group_id": "g1"}, "_move_project_to_group_impl",
                 {"project_name": "Проект", "project_id": "p1",
                  "group_id": "g1"}, id="move_project_to_group"),
]


@pytest.mark.parametrize("tool_name,kwargs,impl_name,expected", _SIX)
async def test_each_reported_tool_executes_on_the_first_call_with_a_valid_key(
        wired, deletion, monkeypatch, tool_name, kwargs, impl_name, expected):
    """Дословная приёмка симптома: у КАЖДОГО из шести названных инструментов
    верный ключ исполняет с первого вызова, не отправив владельцу кнопку."""
    live, fake = deletion
    calls = []
    if impl_name:
        async def _spy(**kw):
            calls.append(kw)
            return "✅ Готово."
        monkeypatch.setattr(s, impl_name, _spy)
        args = ()
    else:
        args = _DELETE_ARGS          # у delete_tasks аргументы позиционные

    out = await getattr(s, tool_name)(*args, automation_key=s.SECRET, **kwargs)

    if impl_name:
        assert calls == [expected], f"{tool_name}: исполнитель не отработал"
    else:
        assert fake.deleted_ids == ["t1"], "delete_tasks: задача не удалена"
    assert wired.calls == [], f"{tool_name} отправил в Telegram: {wired.calls}"
    assert _rows_in_tg_approvals() == [], f"{tool_name} завёл строку подтверждения"
    assert _rows_in_mcp_manifests() == [], f"{tool_name} сохранил план в базе"
    assert "manifest_id" not in out, out


@pytest.mark.parametrize("tool_name,kwargs,impl_name,expected", _SIX)
async def test_each_reported_tool_still_asks_when_the_key_is_wrong(
        wired, deletion, monkeypatch, tool_name, kwargs, impl_name, expected):
    """Зеркало предыдущего: неверный ключ у тех же шести — план и кнопка."""
    live, fake = deletion
    calls = []
    if impl_name:
        async def _spy(**kw):
            calls.append(kw)
            return "✅ Готово."
        monkeypatch.setattr(s, impl_name, _spy)
        args = ()
    else:
        args = _DELETE_ARGS

    out = await getattr(s, tool_name)(*args, automation_key="почти-настоящий",
                                      **kwargs)

    assert calls == [] and fake.deleted_ids == [], \
        f"{tool_name}: исполнено с неверным ключом"
    assert len(wired.with_buttons()) == 1, f"{tool_name}: кнопка не ушла"
    assert _rows_in_tg_approvals() == ["PENDING"]
    assert "manifest_id" in out, out


# ═══════════ 2. Любой другой ключ: план и кнопка, как раньше ═══════════

@pytest.mark.parametrize("key", _BAD_KEYS)
async def test_bad_key_still_goes_through_plan_and_button_single(
        wired, tag_spy, key):
    out = await s.create_tag(name="из-автоматики", automation_key=key)

    assert tag_spy == [], f"ключ {key!r} исполнил операцию без подтверждения"
    assert len(wired.with_buttons()) == 1, "владельцу не ушло сообщение с кнопками"
    assert _rows_in_tg_approvals() == ["PENDING"]
    assert "manifest_id" in out, out


@pytest.mark.parametrize("key", _BAD_KEYS)
async def test_bad_key_still_goes_through_plan_and_button_batch(
        wired, complete_spy, key):
    out = await s.complete_tasks("Закрываю 1", [{"title": "Отчёт", "taskId": "t9",
                                                 "projectId": "p1"}],
                                 automation_key=key)

    assert complete_spy == [], f"ключ {key!r} исполнил операцию без подтверждения"
    assert len(wired.with_buttons()) == 1
    assert _rows_in_tg_approvals() == ["PENDING"]
    assert "manifest_id" in out, out


@pytest.mark.parametrize("key", _BAD_KEYS)
async def test_bad_key_still_goes_through_plan_and_button_delete(
        wired, deletion, key):
    live, fake = deletion

    out = await s.delete_tasks(*_DELETE_ARGS, automation_key=key)

    assert fake.deleted_ids == [], f"ключ {key!r} удалил задачу без подтверждения"
    assert "t1" in live
    assert len(wired.with_buttons()) == 1
    assert _rows_in_tg_approvals() == ["PENDING"]
    assert "manifest_id" in out, out


async def test_no_key_at_all_still_goes_through_plan_and_button(
        wired, tag_spy, complete_spy, deletion):
    """Аргумент не передан вовсе — самый частый случай (интерактивный клиент)."""
    live, fake = deletion

    single = await s.create_tag(name="из-чата")
    batch = await s.complete_tasks("Закрываю 1", [{"title": "Отчёт", "taskId": "t9",
                                                   "projectId": "p1"}])
    delete = await s.delete_tasks(*_DELETE_ARGS)

    assert tag_spy == [] and complete_spy == [] and fake.deleted_ids == []
    assert len(wired.with_buttons()) == 3, "не все три плана ушли владельцу"
    assert _rows_in_tg_approvals() == ["PENDING"] * 3
    for out in (single, batch, delete):
        assert "manifest_id" in out, out


async def test_server_without_a_secret_does_not_open_on_an_empty_key(
        wired, tag_spy, monkeypatch):
    """Вырожденный, но смертельный случай: MCP_SECRET на сервере не задан.

    Тогда «пустой ключ равен пустому секрету» открыло бы дверь КАЖДОМУ вызову
    без аргумента вовсе. Проверяем на самом сервере, а не на сравнивалке."""
    monkeypatch.setattr(s, "SECRET", "")

    out = await s.create_tag(name="без-секрета", automation_key="")

    assert tag_spy == []
    assert len(wired.with_buttons()) == 1
    assert "manifest_id" in out, out


# ═══════════ 3. Совместимость: старый двухшаговый headless-клиент ═══════════

async def test_key_on_the_second_call_still_works_and_kills_the_plan(
        wired, tag_spy):
    """Клиент, который по старой схеме сначала построил план, а ключ прислал
    вторым вызовом, обязан доехать — И оставшийся в Telegram план обязан быть
    погашен, иначе владелец нажмёт кнопку и операция выполнится ВТОРОЙ раз."""
    from ticktick_mcp.src import manifest_store

    preview = await s.create_tag(name="из-двух-шагов")
    mid = next(iter(s._MANIFESTS))
    assert mid in preview and _rows_in_tg_approvals() == ["PENDING"]

    out = await s.create_tag(name="из-двух-шагов", manifest_id=mid,
                             automation_key=s.SECRET)

    assert tag_spy == [{"name": "из-двух-шагов", "color": None}]
    assert "🛑" not in out, out
    assert manifest_store.load(mid)["consumed_at"] is not None, \
        "план остался живым — кнопка в Telegram исполнит операцию повторно"


async def test_key_does_not_resurrect_an_expired_plan(wired, tag_spy):
    """Ключ — это «человека спрашивать не у кого», а не «манифест вечен».
    Мёртвый/чужой manifest_id по-прежнему отказ, а не тихое исполнение."""
    out = await s.create_tag(name="протухший", manifest_id="нет-такого-плана",
                             automation_key=s.SECRET)

    assert tag_spy == [], "исполнили по несуществующему плану"
    assert "🛑" in out, out


async def test_key_uses_the_stored_plan_not_the_second_call_arguments(
        wired, tag_spy):
    """No-swap: подменить содержимое между планом и исполнением нельзя даже
    с верным ключом — работают СОХРАНЁННЫЕ параметры плана."""
    await s.create_tag(name="что-показали-человеку")
    mid = next(iter(s._MANIFESTS))

    await s.create_tag(name="что-подсунули-потом", manifest_id=mid,
                       automation_key=s.SECRET)

    assert tag_spy == [{"name": "что-показали-человеку", "color": None}]


# ═══════════ 4. Ключ не ослабляет сверку личности объекта ═══════════

async def test_key_does_not_skip_the_identity_guard_on_delete(wired, deletion):
    """Ключ снимает вопрос «человек согласен?», но не «та ли это задача».
    Название разошлось с живым состоянием → удаления нет и по ключу."""
    live, fake = deletion
    live["t1"]["title"] = "Совсем другая задача"

    out = await s.delete_tasks("⚠️ Удаляю 1",
                               [{"title": "Купить молоко", "taskId": "t1",
                                 "projectId": "p1", "projectName": "Покупки"}],
                               automation_key=s.SECRET)

    assert fake.deleted_ids == [], "удалили задачу, не совпавшую по названию"
    assert "t1" in live
    assert wired.calls == []
    assert "Купить молоко" in out


async def test_key_does_not_lift_the_bulk_delete_cap(wired, deletion,
                                                     monkeypatch):
    """Потолок прямого удаления — тоже не про согласие человека: пакет
    по-прежнему только через plan_task_deletion, ключ его не снимает."""
    live, fake = deletion
    monkeypatch.setenv("DIRECT_DELETE_CAP", "1")

    out = await s.delete_tasks("⚠️ Удаляю 2",
                               [{"title": "Купить молоко", "taskId": "t1",
                                 "projectId": "p1"},
                                {"title": "Ещё одна", "taskId": "t2",
                                 "projectId": "p1"}],
                               automation_key=s.SECRET)

    assert fake.deleted_ids == []
    assert "🛑" in out and "манифест" in out.lower()


# ═══════════ 5. Гейт остаётся гейтом для интерактивного пути ═══════════

async def test_chat_yes_alone_still_cannot_execute_a_telegram_plan(
        wired, tag_spy):
    """Контрольный выстрел по главному страху: правка #118 НЕ должна была
    открыть текстовый путь. План ушёл в Telegram → «да» в чате по-прежнему
    ничего не исполняет (исполнит только нажатие кнопки, через поллер)."""
    await s.create_tag(name="только-кнопкой")
    mid = next(iter(s._MANIFESTS))

    out = await s.create_tag(name="только-кнопкой", manifest_id=mid,
                             user_reply="да, давай")

    assert tag_spy == [], "чат-«да» исполнило операцию мимо кнопки"
    assert "кнопк" in out.lower(), out
    assert _rows_in_tg_approvals() == ["PENDING"], "план погашен — кнопке нечего исполнять"


async def test_plan_row_stays_pending_after_a_refused_bad_key(wired, tag_spy):
    """Неверный ключ не должен ни исполнять, ни ломать уже созданный план:
    строка остаётся PENDING и живой ровно до решения человека."""
    await s.create_tag(name="живой-план", automation_key="not-the-real-secret")
    mid = next(iter(s._MANIFESTS))

    assert _rows_in_tg_approvals() == ["PENDING"]
    assert tag_spy == []
    # ...и tg-строка адресует именно этот план (её id — ключ callback-кнопки).
    assert tg.check_approval(mid) == "pending"
