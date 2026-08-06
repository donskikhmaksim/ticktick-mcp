"""BUTTON-ONLY: план, ушедший в Telegram, исполняется ТОЛЬКО кнопкой
(2026-08-06, прямое требование Максима).

Почему это отдельный слой, а не «ещё одна проверка текста». Сервер физически
не может отличить сочинённое моделью «да» от настоящего ответа человека — он
видит одну строку. Классификатор `_classify_consent_reply` ловит ФОРМУЛИРОВКИ
(«нет», «ок, кроме последней», пересказ), но не намерение: фразу вида «делай,
я передумал насчёт третьей» регулярками не закрыть в принципе. Пока текстовый
путь способен ИСПОЛНЯТЬ, дыра только сужается.

Решение: если план реально ушёл в Telegram (манифест помечен `tg_notified` в
`_maybe_tg_notify_plan`) и у него есть авто-исполнитель, текстовый путь
исполнения ЗАКРЫВАЕТСЯ целиком — что бы ни было написано в `user_reply`.
Исполняет фоновый поллер по факту нажатия. Дыра не сужается, а исчезает:
способа исполнить операцию текстом больше не существует.

Что здесь закреплено навсегда:
  1. Кнопку не нажимали → текстовое «да» ничего не делает, план ЖИВ.
  2. Нажали ✅, поллер ещё не дошёл → текстовое «да» ничего не делает и НЕ
     гасит манифест (иначе поллеру нечего будет исполнять), ответ говорит
     «сервер исполняет сам».
  3. Поллер уже исполнил → внятное «уже исполнено кнопкой», без повтора.
  4. Нажали 🛑 → отказ, план сожжён.
  5. Слой ВЫКЛЮЧЕН (дефолт самохостера) → текстовое «да» исполняет, как
     раньше, и `tg_approval` не трогается вовсе. Это главный тест обратной
     совместимости: без него человек без Telegram остался бы вообще без
     способа что-либо сделать.
  6. Настройку выключили ПОСЛЕ отправки плана → путь всё равно закрыт
     (решение по пометке манифеста, а не по текущему значению env).
  7. Ключ автоматизации (headless-клиенты) работает как работал — у него своё
     основание, это не текстовое подтверждение.
  8. Планы БЕЗ авто-исполнителя (delete_project, слияние rename_tag) под
     button-only НЕ попадают — иначе их нельзя было бы выполнить никак.

Три семейства проверяются параметризацией: списочная мутация (_gate_batch),
одиночная мутация (_gate_single) и удаление задач со своим манифестом
(plan_task_deletion → execute_task_deletion).

Ни сети, ни Postgres: функции `tg_approval` и `_<tool>_impl` подменяются, как
в test_tg_gate_all_tools.py / test_gate_tg_determinism.py.
"""
import re

import pytest

import ticktick_mcp.src.server as s
import ticktick_mcp.src.tg_approval as tg


# ───────────────────────── обвязка ─────────────────────────

FAMILIES = ("batch", "single", "delete")

_GATED = {
    "batch": ("complete_tasks",
              dict(summary="Закрываю 1",
                   tasks=[{"taskId": "t1", "title": "A", "projectId": "p1"}])),
    "single": ("create_tag", dict(name="дом", color="#FF6161")),
}

_MID_RE = re.compile(r"Манифест `([0-9a-f]{6,})`")


def _mid_of(text):
    m = _MID_RE.search(text)
    assert m, f"в превью нет id манифеста:\n{text}"
    return m.group(1)


class _FakeV2Delete:
    def __init__(self, live):
        self._live = live
        self.deleted_ids = []

    def batch_delete_tasks(self, items):
        for it in items:
            self._live.pop(it["taskId"], None)
            self.deleted_ids.append(it["taskId"])
        return {}


@pytest.fixture(autouse=True)
def _isolate_manifests():
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


def _notify_ok(monkeypatch, seen=None):
    def _notify(cfg, manifest_id, preview, tool):
        if seen is not None:
            seen.append({"manifest_id": manifest_id, "tool": tool})
        return True, ""
    monkeypatch.setattr(tg, "notify_plan", _notify)


def _verdict(monkeypatch, value):
    """check_approval с фиксированным ответом + счётчик обращений (им
    проверяется, что при выключенном слое Postgres не дёргается вовсе)."""
    calls = {"n": 0}

    def _fake(manifest_id):
        calls["n"] += 1
        return value
    monkeypatch.setattr(tg, "check_approval", _fake)
    return calls


class _Case:
    """Один сценарий семейства: как построить план, как позвать execute и как
    узнать, ИСПОЛНИЛОСЬ ли на самом деле."""

    def __init__(self, tool, plan, execute, executed):
        self.tool = tool
        self.plan = plan
        self.execute = execute
        self.executed = executed


def _case(family, monkeypatch, tmp_path):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_ensure_official", lambda: None)

    if family == "delete":
        live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
        fake = _FakeV2Delete(live)
        monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
        monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Покупки"})
        monkeypatch.setattr(s, "ticktick_v2", fake)
        monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))

        async def _plan():
            out = await s.plan_task_deletion(
                "Удаляю 1", [{"taskId": "t1", "title": "Купить молоко"}])
            return out, _mid_of(out)

        async def _execute(mid, reply="да", **kw):
            return await s.execute_task_deletion(mid, user_reply=reply, **kw)

        return _Case("delete_tasks", _plan, _execute, lambda: list(fake.deleted_ids))

    tool, kwargs = _GATED[family]
    rec = []

    async def _impl(*a, **kw):
        rec.append((a, kw))
        return f"### ✅ {tool} исполнен (заглушка)"

    monkeypatch.setattr(s, f"_{tool}_impl", _impl)

    async def _plan():
        out = await getattr(s, tool)(**kwargs)
        return out, _mid_of(out)

    async def _execute(mid, reply="да", **kw):
        return await getattr(s, tool)(**kwargs, manifest_id=mid,
                                      user_reply=reply, **kw)

    return _Case(tool, _plan, _execute, lambda: list(rec))


# ═════════ 1. Кнопку не нажимали → текст не исполняет, план жив ═════════

@pytest.mark.parametrize("family", FAMILIES)
async def test_pending_button_text_yes_does_nothing_and_plan_survives(
        family, monkeypatch, tmp_path):
    c = _case(family, monkeypatch, tmp_path)
    _tg_on(monkeypatch)
    _notify_ok(monkeypatch)
    _, mid = await c.plan()
    assert s._MANIFESTS[mid]["tg_notified"] is True   # план ДЕЙСТВИТЕЛЬНО ушёл
    _verdict(monkeypatch, "pending")

    out = await c.execute(mid, "да")

    assert c.executed() == [], f"{family}: исполнилось текстовым путём"
    assert "кнопк" in out and "Telegram" in out
    assert "отключено" in out                          # объяснено, почему
    assert s._MANIFESTS[mid]["consumed"] is False      # план ждёт нажатия


@pytest.mark.parametrize("family", FAMILIES)
async def test_pending_button_ignores_what_the_reply_actually_says(
        family, monkeypatch, tmp_path):
    """Суть фикса: содержание реплики больше НЕ влияет ни на что. Любая
    формулировка (включая ту, что классификатор пропустил бы как согласие)
    даёт один и тот же отказ — исполнять текстом нечем."""
    c = _case(family, monkeypatch, tmp_path)
    _tg_on(monkeypatch)
    _notify_ok(monkeypatch)
    _verdict(monkeypatch, "pending")

    for reply in ("да", "давай, подтверждаю", "", "ага, делай"):
        _, mid = await c.plan()
        out = await c.execute(mid, reply)
        assert c.executed() == [], f"{family}: исполнилось на реплике {reply!r}"
        assert "кнопк" in out


# ═════════ 2. Нажали ✅, поллер ещё не дошёл ═════════

@pytest.mark.parametrize("family", FAMILIES)
async def test_approved_but_poller_pending_refuses_and_keeps_the_plan(
        family, monkeypatch, tmp_path):
    c = _case(family, monkeypatch, tmp_path)
    _tg_on(monkeypatch)
    _notify_ok(monkeypatch)
    _, mid = await c.plan()
    _verdict(monkeypatch, "approved")

    out = await c.execute(mid, "да")

    assert c.executed() == [], f"{family}: текстовый путь исполнил уже одобренный план"
    assert "САМ" in out                                 # «сервер исполняет сам»
    assert "не надо" in out                             # повторять не нужно
    # КРИТИЧНО: манифест обязан остаться живым, иначе поллеру нечего забирать
    assert s._MANIFESTS[mid]["consumed"] is False


@pytest.mark.parametrize("family", FAMILIES)
async def test_operation_is_not_lost_the_poller_still_executes_it(
        family, monkeypatch, tmp_path):
    """Обратная сторона теста выше: отказ текстового пути НЕ отменяет
    операцию. После него поллер по-прежнему видит план и исполняет его."""
    c = _case(family, monkeypatch, tmp_path)
    _tg_on(monkeypatch)
    _notify_ok(monkeypatch)
    _, mid = await c.plan()
    _verdict(monkeypatch, "approved")
    await c.execute(mid, "да")                          # текстом — отказ

    monkeypatch.setattr(tg, "get_tg_approvals", lambda ids, lost_scan_since_ms=None: {
        i: {"status": "APPROVED", "expires_at": tg._now_ms() + 3_600_000,
            "chat_id": "c1", "message_id": 7} for i in ids if i == mid})
    found = [x for x in s._find_tg_auto_execute_candidates()
             if x["manifest_id"] == mid]
    assert len(found) == 1, f"{family}: поллер потерял одобренный план"

    entry = s._resolve_auto_executor(found[0]["tool"], s._MANIFESTS[mid])
    consumed = tg.try_auto_execute(
        manifest_id=mid, tool=found[0]["tool"],
        get_manifest=lambda i: s._MANIFESTS.get(i),
        consume_manifest=s._consume_manifest_for_auto_execute,
        rehash=entry.rehash)
    assert consumed is not None
    await entry.execute(mid, consumed)

    assert c.executed(), f"{family}: кнопка не привела к исполнению"


# ═════════ 3. Поллер уже исполнил → внятное «уже сделано» ═════════

@pytest.mark.parametrize("family", FAMILIES)
async def test_after_the_poller_ran_a_repeat_call_is_explicit_and_idempotent(
        family, monkeypatch, tmp_path):
    c = _case(family, monkeypatch, tmp_path)
    _tg_on(monkeypatch)
    _notify_ok(monkeypatch)
    _, mid = await c.plan()

    # ровно то, что делает поллер: атомарно забирает манифест и оставляет
    # «надгробие» — исполнение самой операции здесь неважно
    assert s._consume_manifest_for_auto_execute(mid) is not None
    s._prune_manifests()
    _verdict(monkeypatch, "approved")

    out = await c.execute(mid, "да")

    assert c.executed() == [], f"{family}: операция повторилась"
    assert "УЖЕ исполнен" in out and "кнопкой в Telegram" in out


@pytest.mark.parametrize("family", FAMILIES)
async def test_tombstone_message_also_shown_before_pruning(
        family, monkeypatch, tmp_path):
    """Тот же случай, но `_prune_manifests` ещё не успел выбросить запись:
    ответ обязан быть таким же внятным, а не безликим «план протух»."""
    c = _case(family, monkeypatch, tmp_path)
    _tg_on(monkeypatch)
    _notify_ok(monkeypatch)
    _, mid = await c.plan()
    assert s._consume_manifest_for_auto_execute(mid) is not None
    _verdict(monkeypatch, "approved")

    out = await c.execute(mid, "да")

    assert c.executed() == []
    assert "УЖЕ исполнен" in out


# ═════════ 4. Нажали 🛑 → план сожжён ═════════

@pytest.mark.parametrize("family", FAMILIES)
async def test_rejected_button_kills_the_plan(family, monkeypatch, tmp_path):
    c = _case(family, monkeypatch, tmp_path)
    _tg_on(monkeypatch)
    _notify_ok(monkeypatch)
    _, mid = await c.plan()
    _verdict(monkeypatch, "rejected")

    out = await c.execute(mid, "да")

    assert c.executed() == []
    assert "Отклонено" in out
    assert s._MANIFESTS[mid]["consumed"] is True


# ═════════ 5. Слой ВЫКЛЮЧЕН → всё как раньше (главная совместимость) ═══════

@pytest.mark.parametrize("family", FAMILIES)
async def test_layer_off_text_yes_still_executes(family, monkeypatch, tmp_path):
    """Без Telegram текстовое подтверждение — ЕДИНСТВЕННЫЙ канал. Сломать его
    нельзя ни при каких условиях: иначе самохостер без бота остаётся вообще
    без способа что-либо выполнить."""
    c = _case(family, monkeypatch, tmp_path)
    _tg_off(monkeypatch)
    notified = []
    _notify_ok(monkeypatch, notified)
    calls = _verdict(monkeypatch, "none")

    preview, mid = await c.plan()
    assert notified == []                      # в Telegram ничего не ушло
    assert "Telegram" not in preview
    assert "tg_notified" not in s._MANIFESTS[mid]

    out = await c.execute(mid, "да")

    assert c.executed(), f"{family}: chat-«да» перестал исполнять — регресс"
    assert calls["n"] == 0                     # Postgres не дёргался вовсе
    assert "кнопк" not in out


@pytest.mark.parametrize("family", FAMILIES)
async def test_layer_off_negative_reply_still_refuses(family, monkeypatch, tmp_path):
    """Обратная совместимость — это не «всё проходит»: без Telegram
    классификатор текста остаётся единственной защитой и обязан работать."""
    c = _case(family, monkeypatch, tmp_path)
    _tg_off(monkeypatch)
    _notify_ok(monkeypatch)
    _, mid = await c.plan()

    out = await c.execute(mid, "нет, отмена")

    assert c.executed() == []
    assert "НЕ подтвердил" in out


# ═════════ 6. Настройку выключили ПОСЛЕ отправки плана ═════════

@pytest.mark.parametrize("family", FAMILIES)
async def test_disabling_the_layer_after_the_plan_does_not_reopen_the_text_path(
        family, monkeypatch, tmp_path):
    """Решение принимается по СОСТОЯНИЮ МАНИФЕСТА, а не по текущему значению
    настройки: иначе достаточно было бы выключить `TG_APPROVAL_ENABLED` между
    планом и исполнением, чтобы требование кнопки испарилось."""
    c = _case(family, monkeypatch, tmp_path)
    _tg_on(monkeypatch)
    _notify_ok(monkeypatch)
    _, mid = await c.plan()
    assert s._MANIFESTS[mid]["tg_notified"] is True

    _tg_off(monkeypatch)                       # настройку выключили
    _verdict(monkeypatch, "pending")

    out = await c.execute(mid, "да")

    assert c.executed() == [], f"{family}: выключение настройки открыло текстовый путь"
    assert "кнопк" in out
    assert "отключено" in out                  # именно button-only, не старая ветка


@pytest.mark.parametrize("family", FAMILIES)
async def test_disabling_the_layer_after_an_approved_press_still_blocks_text(
        family, monkeypatch, tmp_path):
    """Тот же инвариант в состоянии, где две реализации РАСХОДЯТСЯ. При
    «кнопку не нажимали» отказ дало бы и старое правило («нет approved-строки»)
    — то есть тест выше сам по себе не отличает «решаем по манифесту» от
    «решаем по текущей настройке». Отличает именно этот случай: кнопка нажата,
    настройка выключена. По манифесту — текстовый путь закрыт (исполняет
    поллер); по настройке — открылся бы и «да» исполнило бы операцию."""
    c = _case(family, monkeypatch, tmp_path)
    _tg_on(monkeypatch)
    _notify_ok(monkeypatch)
    _, mid = await c.plan()

    _tg_off(monkeypatch)
    _verdict(monkeypatch, "approved")

    out = await c.execute(mid, "да")

    assert c.executed() == [], (
        f"{family}: после выключения настройки текстовое «да» исполнило "
        "помеченный план — решение принимается по настройке, а не по манифесту")
    assert "САМ" in out
    assert s._MANIFESTS[mid]["consumed"] is False


# ═════════ 7. Ключ автоматизации не сломан ═════════

async def test_automation_key_still_bypasses_the_button(monkeypatch, tmp_path):
    """У headless-клиента (бот, пишущий задачи сам) своё основание — секрет
    подключения, а не текстовое «да». Он обязан проходить и при включённом
    слое, иначе автоматическая запись задач встанет."""
    c = _case("single", monkeypatch, tmp_path)
    _tg_on(monkeypatch)
    _notify_ok(monkeypatch)
    _, mid = await c.plan()
    _verdict(monkeypatch, "pending")           # кнопку никто не нажимал

    await c.execute(mid, "", automation_key=s.SECRET)

    assert c.executed(), "валидный automation_key перестал проходить"


async def test_wrong_automation_key_does_not_bypass_the_button(
        monkeypatch, tmp_path):
    c = _case("single", monkeypatch, tmp_path)
    _tg_on(monkeypatch)
    _notify_ok(monkeypatch)
    _, mid = await c.plan()
    _verdict(monkeypatch, "pending")

    # ключ намеренно ASCII: `hmac.compare_digest` не умеет сравнивать строки
    # с не-ASCII символами и бросает TypeError — отдельная (существовавшая до
    # этой правки) шероховатость, к button-only отношения не имеющая.
    out = await c.execute(mid, "да", automation_key="wrong-key")

    assert c.executed() == []
    assert "кнопк" in out


def test_automation_key_short_circuit_precedes_the_button_only_block(monkeypatch):
    """Юнит-уровень того же: ключ проверяется ДО всего остального, поэтому
    даже помеченный манифест ему не помеха."""
    _tg_on(monkeypatch)
    calls = _verdict(monkeypatch, "pending")
    m = {"kind": "delete", "consumed": False, "tg_notified": True,
         "_tg_tool": "delete_tasks", "_tg_manifest_id": "m-auto"}

    cr = s._require_consent(action="delete", tier=2, manifest=m,
                            user_reply="", automation_key=s.SECRET)

    assert cr.ok is True and cr.reason == "automation_key"
    assert calls["n"] == 0


# ═════════ 8. Границы: что под button-only НЕ попадает ═════════

def test_button_only_needs_both_the_mark_and_an_executor():
    """Формула целиком, без обращения к базе: пометка о реальной отправке И
    наличие авто-исполнителя."""
    assert s._tg_button_only(None) is False
    assert s._tg_button_only({}) is False
    # помечен, исполнитель есть → закрыт
    assert s._tg_button_only({"tg_notified": True, "_tg_tool": "delete_tasks"}) is True
    assert s._tg_button_only({"tg_notified": True, "tool": "create_tag",
                              "_gate": "single"}) is True
    assert s._tg_button_only({"tg_notified": True, "tool": "delete_project",
                              "_gate": "delete_project"}) is True
    assert s._tg_button_only({"tg_notified": True, "tool": "rename_tag",
                              "_gate": "rename_tag_merge"}) is True
    # помечен, но исполнителя нет → НЕ закрыт (иначе операция стала бы
    # невыполнимой вообще). Единственный такой случай сегодня — declutter:
    # он выключен владельцем НАМЕРЕННО, вместе со своей регистрацией в
    # реестре исполнителей.
    assert s._tg_button_only({"tg_notified": True, "tool": "execute_declutter",
                              "kind": "declutter"}) is False
    # исполнитель есть, но план в Telegram не уходил → обычный чат-путь
    assert s._tg_button_only({"tool": "create_tag", "_gate": "single"}) is False


def _wire_delete_project(monkeypatch, tmp_path):
    names = {"p1": "Работа"}
    deleted = []

    class _FakeOfficial:
        def get_project_with_data(self, project_id):
            return {"project": {"id": project_id}, "tasks": []}

        def delete_project(self, project_id):
            names.pop(project_id, None)
            deleted.append(project_id)
            return {}

    monkeypatch.setattr(s, "_ensure_official", lambda: None)
    monkeypatch.setattr(s, "ticktick", _FakeOfficial())
    monkeypatch.setattr(s, "ticktick_v2", None)
    monkeypatch.setattr(s, "_v2_project_names", lambda: names)
    monkeypatch.setattr(s, "_v2_project_names_or_none", lambda: dict(names))
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: {})
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    return deleted


async def test_delete_project_is_button_only_and_the_poller_executes_it(
        monkeypatch, tmp_path):
    """delete_project — самая крупная воронка на сервере (каскадом уходят все
    задачи проекта). До 2026-08-06 у него НЕ было авто-исполнителя: кнопка
    только разрешала, исполнял второй текстовый вызов. Закрыть текстовый путь,
    не заведя исполнитель, значило бы сделать удаление проекта неисполнимым
    вообще — поэтому исполнитель добавлен, и теперь работает полный контур."""
    deleted = _wire_delete_project(monkeypatch, tmp_path)
    _tg_on(monkeypatch)
    _notify_ok(monkeypatch)

    await s.delete_project("Работа", "p1")             # фаза плана
    mid = next(i for i, m in s._MANIFESTS.items() if m.get("kind") == "delete_project")
    assert s._MANIFESTS[mid]["tg_notified"] is True

    _verdict(monkeypatch, "approved")
    out = await s.delete_project("Работа", "p1", user_reply="да")
    assert deleted == [], "текстовый путь удалил проект при нажатой кнопке"
    assert "САМ" in out
    assert s._MANIFESTS[mid]["consumed"] is False      # ждёт поллера

    entry = s._resolve_auto_executor("delete_project", s._MANIFESTS[mid])
    assert entry is not None, "у delete_project нет авто-исполнителя"
    assert entry.rehash(s._MANIFESTS[mid]) == s._MANIFESTS[mid]["object_hash"]
    consumed = tg.try_auto_execute(
        manifest_id=mid, tool="delete_project",
        get_manifest=lambda i: s._MANIFESTS.get(i),
        consume_manifest=s._consume_manifest_for_auto_execute,
        rehash=entry.rehash)
    assert consumed is not None
    report = await entry.execute(mid, consumed)

    assert deleted == ["p1"], "кнопка не удалила проект"
    assert "удалён вместе с" in report


async def test_rename_tag_merge_is_button_only_and_the_poller_executes_it(
        monkeypatch):
    """То же для необратимого слияния тегов."""
    class _FakeV2Tags:
        def __init__(self, names):
            self._names = names

        def get_state(self, force=True):
            return {}

        def get_tags(self):
            return [{"name": n} for n in self._names]

        def rename_tag(self, old_name, new_name):
            self._names = [n for n in self._names if n != old_name.lower()]
            if new_name.lower() not in self._names:
                self._names.append(new_name.lower())
            return {}

    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    fake = _FakeV2Tags(["a", "b"])
    monkeypatch.setattr(s, "ticktick_v2", fake)
    _tg_on(monkeypatch)
    _notify_ok(monkeypatch)

    await s.rename_tag("a", "b", allow_merge=True)     # фаза плана
    mid = next(i for i, m in s._MANIFESTS.items()
               if m.get("kind") == "rename_tag_merge")
    _verdict(monkeypatch, "approved")

    out = await s.rename_tag("a", "b", allow_merge=True, user_reply="да, сливай")
    assert fake._names == ["a", "b"], "текстовый путь слил теги при нажатой кнопке"
    assert "САМ" in out
    assert s._MANIFESTS[mid]["consumed"] is False

    entry = s._resolve_auto_executor("rename_tag", s._MANIFESTS[mid])
    assert entry is not None, "у rename_tag нет авто-исполнителя"
    assert entry.rehash(s._MANIFESTS[mid]) == s._MANIFESTS[mid]["object_hash"]
    consumed = tg.try_auto_execute(
        manifest_id=mid, tool="rename_tag",
        get_manifest=lambda i: s._MANIFESTS.get(i),
        consume_manifest=s._consume_manifest_for_auto_execute,
        rehash=entry.rehash)
    assert consumed is not None
    report = await entry.execute(mid, consumed)

    assert fake._names == ["b"], "кнопка не слила теги"
    assert "renamed" in report and "слито" in report


# ═════════ 8b. Инвентаризация: у КАЖДОЙ кнопки есть чем исполнить ═════════

def _tools_that_send_a_button() -> set:
    """Список строится ИЗ КОДА, а не хранится в тесте: иначе двадцать восьмой
    тул с кнопкой, заведённый без исполнителя, проедет незамеченным ровно так
    же, как проехали delete_project и rename_tag (их расхождение нашёл только
    внешний разбор, потому что такой проверки не существовало).

    Три источника, все обязательны:
      • явные вызовы `_maybe_tg_notify_plan("<tool>", …)` — тулы со своим
        манифестом (delete_project, rename_tag…);
      • та же отправка, но вынесенная в поток —
        `_run_blocking(_maybe_tg_notify_plan, "<tool>", …)`: в async-тулах
        (delete_tasks, plan_task_deletion, plan_task_creation) вызов
        синхронный и на 429 спит, поэтому идёт через executor. Без этого
        шаблона инвентаризация молча теряла бы именно те тулы, у которых
        план самый длинный;
      • тулы, заведённые через общие гейты `_gate_batch`/`_gate_single` — их
        планы уходят в Telegram ИЗНУТРИ гейта, отдельного вызова в коде тула
        нет, поэтому по первым двум источникам они не находятся вовсе.
    """
    import inspect as _inspect
    src = _inspect.getsource(s)
    tools = set(re.findall(r'_maybe_tg_notify_plan\(\s*"([a-z_]+)"', src))
    tools |= set(re.findall(
        r'_run_blocking\(\s*_maybe_tg_notify_plan\s*,\s*"([a-z_]+)"', src))
    tools |= set(re.findall(r'_gate_batch\(\s*"[a-z_]+",\s*"([a-z_]+)"', src))
    tools |= set(re.findall(r'_gate_single\(\s*"[a-z_]+",\s*"([a-z_]+)"', src))
    return tools


def test_every_tool_with_a_button_has_a_way_to_be_executed_by_it():
    """Инвариант: если план тула уходит в Telegram, нажатие ОБЯЗАНО его
    исполнить. Иначе кнопка молча не работает — а с закрытым текстовым путём
    это ещё и означает «операцию нельзя выполнить никак».

    Оснований два, достаточно ЛЮБОГО (они независимы: у delete_tasks нет
    `_delete_tasks_impl`, он исполняется через реестр):
      • запись в реестре `_AUTO_EXECUTORS`;
      • функция `_<tool>_impl` по конвенции имени — по ней тул находит
        generic-исполнитель гейтов.
    """
    tools = _tools_that_send_a_button()
    assert len(tools) >= 24, f"инвентаризация нашла подозрительно мало тулов: {tools}"

    missing = []
    for tool in sorted(tools):
        # execute_declutter пропускается ИМЕНОВАННО, а не общим условием:
        # declutter выключен владельцем НАМЕРЕННО (закомментированы и
        # @mcp.tool(), и его регистрация в реестре — «оба слоя вместе»). Если
        # его вернут, он обязан снова попасть под эту проверку, а не остаться
        # в тихом исключении.
        if tool == "execute_declutter":
            continue
        if tool in s._AUTO_EXECUTORS:
            continue
        if callable(getattr(s, f"_{tool}_impl", None)):
            continue
        missing.append(tool)

    assert missing == [], (
        "у этих тулов кнопка в Telegram есть, а исполнить её нечем — нажатие "
        f"владельца ничего не сделает: {missing}")


def test_the_inventory_would_catch_a_button_without_an_executor(monkeypatch):
    """Мутационная проверка самой проверки: если убрать из реестра тул, у
    которого нет `_<tool>_impl`, тест выше обязан покраснеть."""
    assert "delete_project" in s._AUTO_EXECUTORS
    assert not callable(getattr(s, "_delete_project_impl_missing", None))
    patched = dict(s._AUTO_EXECUTORS)
    patched.pop("delete_tasks")           # у него нет `_delete_tasks_impl`
    monkeypatch.setattr(s, "_AUTO_EXECUTORS", patched)

    with pytest.raises(AssertionError, match="delete_tasks"):
        test_every_tool_with_a_button_has_a_way_to_be_executed_by_it()


# ═════════ 9. Текст приписки к плану больше не врёт ═════════

async def test_plan_note_no_longer_asks_for_a_text_yes(monkeypatch, tmp_path):
    """Раньше к превью дописывалось «подтвердите кнопкой, затем ответьте «да»
    здесь» — вторая половина неверна и подталкивала лезть в закрытый путь."""
    c = _case("batch", monkeypatch, tmp_path)
    _tg_on(monkeypatch)
    _notify_ok(monkeypatch)

    preview, _ = await c.plan()

    assert "затем ответьте" not in preview
    assert "автоматически" in preview
    assert "НЕ нужно" in preview


def test_plan_note_for_a_non_auto_executed_plan_asks_to_repeat(monkeypatch):
    """Зеркально: если у плана исполнителя НЕТ, приписка обязана честно
    просить повторить вызов после нажатия — обещать «выполнится само» там
    нельзя. Сегодня единственный такой случай — намеренно выключенный
    declutter; ветка проверяется прямым вызовом хука, потому что его тулы
    отключены на уровне @mcp.tool()."""
    _tg_on(monkeypatch)
    _notify_ok(monkeypatch)
    s._MANIFESTS["m-dc"] = {"kind": "declutter", "consumed": False,
                            "created": 0.0}

    out = s._maybe_tg_notify_plan("execute_declutter", "m-dc", "### план")

    assert "повторите вызов" in out
    assert "автоматически" not in out
