"""Telegram-фактор на пути СОЗДАНИЯ задач и на двух одноходовых 🔴-тулах
(delete_project, rename_tag) — 2026-08-06.

Что здесь фиксируется навсегда:

1. Создание задач было ЦЕЛИКОМ вне Telegram-контура: `plan_task_creation`
   строил манифест, но не звал `_maybe_tg_notify_plan`, а
   `execute_task_creation` звал `_require_consent` без `tool=`/`manifest_id=`.
   Эмпирически: после планирования создания сообщение в Telegram не
   приходило вовсе, и голого chat-«да» хватало, чтобы задачи создались.
2. Манифест создания не имел `object_hash` — единственный вид манифеста без
   binding'а. Между планом и нажатием кнопки содержимое `raw` можно было
   подменить, и ни `_require_consent`, ни `try_auto_execute` этого не
   заметили бы (оба сверяют хэш только `if stored_hash`).
3. `delete_project` и `rename_tag` (ветка слияния) звали `_require_consent`
   с `manifest=None` и без `tool=`. Дописать один `tool=` было НЕЛЬЗЯ: плана
   в Telegram нет → строки в `tg_approvals` нет → `check_approval` вернул бы
   "none" → тул отказывал бы ВСЕГДА. Поэтому оба переведены на манифест, и
   тесты ниже явно проверяют обе стороны: при включённом слое тул не
   отказывает навсегда (кнопка нажата → операция проходит), а при
   выключенном ведёт себя ровно как раньше.

Зеркальный инвариант везде: `TG_APPROVAL_ENABLED` не задан (дефолт
самохостера) → `tg_approval` не трогается ни разу, поведение прежнее.
"""
import re
import time

import pytest

import ticktick_mcp.src.server as s
import ticktick_mcp.src.tg_approval as tg


# ───────────────────────── общая обвязка ─────────────────────────

def _tg_on(monkeypatch, allowlist=None):
    monkeypatch.setattr(s, "_TG_CFG", tg.TgApprovalConfig(
        enabled=True, bot_token="x", owner_chat_id="1", server="ticktick",
        tools_allowlist=allowlist, ttl_s=3600))


def _tg_off(monkeypatch):
    monkeypatch.setattr(s, "_TG_CFG", tg.TgApprovalConfig(
        enabled=False, bot_token="", owner_chat_id="", server="ticktick",
        tools_allowlist=None, ttl_s=3600))


def _capture_notify(monkeypatch, ok=True, err=""):
    """Подменяет реальную отправку в Telegram; возвращает список вызовов
    вида (manifest_id, tool, preview_text)."""
    seen = []

    def _fake(cfg, manifest_id, preview_body, tool):
        seen.append((manifest_id, tool, preview_body))
        return (ok, err)

    monkeypatch.setattr(tg, "notify_plan", _fake)
    return seen


def _count_approval_calls(monkeypatch, verdict="none"):
    """check_approval, считающий обращения — так проверяется, что при
    выключенном слое Postgres не дёргается ВООБЩЕ."""
    calls = {"n": 0}

    def _fake(manifest_id):
        calls["n"] += 1
        return verdict

    monkeypatch.setattr(tg, "check_approval", _fake)
    return calls


@pytest.fixture(autouse=True)
def _clean_manifests():
    s._MANIFESTS.clear()
    s._MANIFEST_TOMBSTONES.clear()
    yield
    s._MANIFESTS.clear()
    s._MANIFEST_TOMBSTONES.clear()


# ═════════════════════ A. Путь создания задач ═════════════════════

def _wire_create(monkeypatch):
    """Минимальная обвязка plan_task_creation/execute_task_creation: реальный
    движок создания подменён — тесты про гейт, не про TickTick."""
    monkeypatch.setattr(s, "_ensure_official", lambda: None)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Работа"})
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: {})
    created = []

    async def _fake_impl(summary, tasks):
        created.append((summary, list(tasks)))
        return "✅ Создано (фейк)"

    monkeypatch.setattr(s, "_create_tasks_impl", _fake_impl)
    return created


def _tasks():
    """Свежие dict'ы на каждый вызов: план кладёт их в манифест ПО ССЫЛКЕ,
    а тесты про подмену содержимого эти объекты мутируют — общий модульный
    литерал протёк бы между тестами."""
    return [{"title": "Позвонить маме", "project_id": "p1"}]


_MID_RE = re.compile(r"Манифест `([0-9a-f]{6,})`")


def _only_manifest(plan_out):
    """Возвращает (id плана, его внутреннее состояние).

    Адресация идёт ТОЛЬКО через текст ответа: id вынимается из напечатанной
    клиенту строки, и лишь потом по нему берётся состояние манифеста (его
    проверять законно — это инвариант сервера, а не канал связи).

    Раньше id брался прямо из реестра планов в памяти процесса. Это ровно тот
    приём, из-за которого дыра «id снаружи не печатается» жила незамеченной:
    у сетевого клиента такой памяти нет, а тест ею пользовался и оставался
    зелёным."""
    m = _MID_RE.search(plan_out)
    assert m, f"в ответе нет id плана — снаружи его нечем назвать:\n{plan_out}"
    mid = m.group(1)
    assert mid in s._MANIFESTS, (
        f"напечатанный клиенту id {mid} не адресует ни один живой план")
    assert len(s._MANIFESTS) == 1, s._MANIFESTS
    return mid, s._MANIFESTS[mid]


# ---- 1. Обратная совместимость: слой выключен ----

async def test_plan_creation_unchanged_when_tg_off(monkeypatch):
    _wire_create(monkeypatch)
    _tg_off(monkeypatch)
    seen = _capture_notify(monkeypatch)

    out = await s.plan_task_creation("Создаю 1", _tasks())

    assert "План создания" in out
    assert "Telegram" not in out          # ни слова про кнопки
    assert seen == []                     # отправки не было вовсе
    _, m = _only_manifest(out)
    assert "tg_notified" not in m


async def test_execute_creation_passes_on_plain_yes_when_tg_off(monkeypatch):
    created = _wire_create(monkeypatch)
    _tg_off(monkeypatch)
    _capture_notify(monkeypatch)
    calls = _count_approval_calls(monkeypatch)

    plan_out = await s.plan_task_creation("Создаю 1", _tasks())
    mid, _ = _only_manifest(plan_out)
    out = await s.execute_task_creation(mid, user_reply="да")

    assert created and created[0][1][0]["title"] == "Позвонить маме"
    assert "Создано" in out
    assert calls["n"] == 0                # Postgres не дёргался


# ---- 2. Слой включён: план реально уходит и помечает манифест ----

async def test_plan_creation_notifies_telegram_as_create_tasks(monkeypatch):
    _wire_create(monkeypatch)
    _tg_on(monkeypatch)
    seen = _capture_notify(monkeypatch)

    out = await s.plan_task_creation("Создаю 1", _tasks())

    mid, m = _only_manifest(out)
    assert len(seen) == 1
    assert seen[0][0] == mid
    assert seen[0][1] == "create_tasks"          # имя тула в TG_APPROVAL_TOOLS
    assert "План создания" in seen[0][2]         # ушёл именно текст плана
    assert m["tg_notified"] is True
    assert m["_tg_tool"] == "create_tasks"
    assert "Telegram" in out                     # приписка для пользователя


# ---- 3. Кнопка не нажата → честное «да» не проходит, план жив ----

async def test_execute_creation_refuses_chat_yes_while_button_pending(monkeypatch):
    created = _wire_create(monkeypatch)
    _tg_on(monkeypatch)
    _capture_notify(monkeypatch)

    plan_out = await s.plan_task_creation("Создаю 1", _tasks())
    mid, m = _only_manifest(plan_out)
    # Пометка — ЖЁСТКОЕ основание требовать кнопку: она переживает даже
    # забытый `tool=` в execute (ровно та поломка, что была в
    # execute_task_deletion). Проверяем её здесь же, иначе тест зелёный и
    # тогда, когда план в Telegram вообще не уходил.
    assert m.get("tg_notified") is True
    _count_approval_calls(monkeypatch, verdict="pending")

    out = await s.execute_task_creation(mid, user_reply="да")

    assert created == []                  # НИЧЕГО не создано
    assert "Telegram" in out
    assert m["consumed"] is False         # план ещё активен


async def test_execute_creation_after_button_approved_is_left_to_the_poller(
        monkeypatch):
    """ПЕРЕПИСАН 2026-08-06 (button-only). Раньше проверялось «нажали кнопку →
    текстовое «да» проходит»; теперь исполняет ТОЛЬКО фоновый поллер, а
    текстовый вызов отвергается с объяснением. «Вечного отказа», ради страха
    перед которым тест и писался, не возникает: операция всё равно случится —
    просто без участия модели."""
    created = _wire_create(monkeypatch)
    _tg_on(monkeypatch)
    _capture_notify(monkeypatch)

    plan_out = await s.plan_task_creation("Создаю 1", _tasks())
    mid, m = _only_manifest(plan_out)
    _count_approval_calls(monkeypatch, verdict="approved")

    out = await s.execute_task_creation(mid, user_reply="да")

    assert created == []                  # текстом ничего не создано
    assert "исполняет" in out and "САМ" in out
    assert m["consumed"] is False         # план ждёт поллера, не сожжён


async def test_execute_creation_passes_tool_and_manifest_id_to_the_gate(monkeypatch):
    """Контракт вызова, а не только его наблюдаемый эффект.

    Сам по себе отказ при «pending» держится на пометке `tg_notified`
    (основание (а) в `_require_consent`) и переживёт даже забытые
    `tool=`/`manifest_id=` — именно поэтому их пропажу надо ловить ОТДЕЛЬНО:
    ровно эта пропажа в `execute_task_deletion` сделала самую опасную
    операцию недетерминированной, и повторять её здесь нельзя."""
    _wire_create(monkeypatch)
    _tg_on(monkeypatch)
    _capture_notify(monkeypatch)
    plan_out = await s.plan_task_creation("Создаю 1", _tasks())
    mid, _ = _only_manifest(plan_out)

    seen = {}

    def _spy(**kwargs):
        seen.update(kwargs)
        return s.ConsentResult(False, "стоп (шпион)")

    monkeypatch.setattr(s, "_require_consent", _spy)
    await s.execute_task_creation(mid, user_reply="да")

    assert seen.get("tool") == "create_tasks"
    assert seen.get("manifest_id") == mid


# ---- 4. Кнопка «Отклонить» гасит план ----

async def test_execute_creation_rejected_button_kills_the_plan(monkeypatch):
    created = _wire_create(monkeypatch)
    _tg_on(monkeypatch)
    _capture_notify(monkeypatch)

    plan_out = await s.plan_task_creation("Создаю 1", _tasks())
    mid, m = _only_manifest(plan_out)
    _count_approval_calls(monkeypatch, verdict="rejected")

    out = await s.execute_task_creation(mid, user_reply="да")

    assert created == []
    assert "Отклонено" in out
    assert m["consumed"] is True          # погашен, нужен новый план


# ---- 5. Не смогли отправить в Telegram → fail-closed ----

async def test_plan_creation_fails_closed_when_telegram_send_fails(monkeypatch):
    created = _wire_create(monkeypatch)
    _tg_on(monkeypatch)
    _capture_notify(monkeypatch, ok=False, err="bot blocked")

    out = await s.plan_task_creation("Создаю 1", _tasks())

    assert "Не смог отправить" in out
    assert "bot blocked" in out
    assert "План создания" not in out     # план наружу НЕ ушёл
    assert not _MID_RE.search(out), (
        "план не отправлен и погашен — печатать его id наружу незачем")
    # ЕДИНСТВЕННОЕ место в файле, где id берётся из памяти процесса, и это
    # намеренно: снаружи его нет вовсе, а проверить надо обратное — что даже
    # клиент, каким-то образом узнавший id, ничего им не исполнит. Двойник
    # здесь СИЛЬНЕЕ реальности, а не добрее её.
    mid, m = next(iter(s._MANIFESTS.items()))
    assert m["consumed"] is True          # манифест инвалидирован

    # и он больше не исполним даже с честным «да»
    _count_approval_calls(monkeypatch, verdict="approved")
    out2 = await s.execute_task_creation(mid, user_reply="да")
    assert created == []
    assert "🛑" in out2


# ---- 6. Binding: rehash обязан совпадать побайтово ----

async def test_rehash_create_manifest_matches_the_stored_hash(monkeypatch):
    _wire_create(monkeypatch)
    _tg_off(monkeypatch)

    plan_out = await s.plan_task_creation("Создаю 1", _tasks())
    _, m = _only_manifest(plan_out)

    assert m.get("object_hash")                       # он вообще есть
    assert s._rehash_create_manifest(m) == m["object_hash"]


async def test_rehash_create_manifest_detects_tampering(monkeypatch):
    _wire_create(monkeypatch)
    _tg_off(monkeypatch)

    plan_out = await s.plan_task_creation("Создаю 1", _tasks())
    _, m = _only_manifest(plan_out)
    stored = m["object_hash"]

    m["raw"][0]["title"] = "Перевести деньги мошеннику"

    assert s._rehash_create_manifest(m) != stored


async def test_tampered_create_manifest_is_not_auto_executed(monkeypatch):
    """Практический смысл п.6: подменённый между планом и нажатием манифест
    не должен исполниться по кнопке."""
    created = _wire_create(monkeypatch)
    _tg_on(monkeypatch)
    _capture_notify(monkeypatch)

    plan_out = await s.plan_task_creation("Создаю 1", _tasks())
    mid, m = _only_manifest(plan_out)
    m["raw"].append({"title": "Лишняя задача", "project_id": "p1"})

    consumed = tg.try_auto_execute(
        manifest_id=mid, tool="create_tasks",
        get_manifest=lambda i: s._MANIFESTS.get(i),
        consume_manifest=s._consume_manifest_for_auto_execute,
        rehash=s._rehash_create_manifest)

    assert consumed is None               # кандидат отброшен
    assert created == []
    assert m["consumed"] is False         # и не сожжён зря


async def test_create_kind_is_registered_for_button_auto_execute():
    """Kind→tool→исполнитель: без этой связки нажатие кнопки на плане
    создания не приводило бы ни к чему (сервер сам ничего не создаст)."""
    assert s._AUTO_EXECUTE_TOOL_FOR_KIND.get("create") == "create_tasks"
    assert "create_tasks" in s._AUTO_EXECUTORS


async def test_auto_executor_creates_exactly_the_planned_tasks(monkeypatch):
    created = _wire_create(monkeypatch)
    _tg_off(monkeypatch)

    plan_out = await s.plan_task_creation("Создаю 1", _tasks())
    mid, m = _only_manifest(plan_out)

    out = await s._AUTO_EXECUTORS["create_tasks"].execute(mid, m)

    assert "Создано" in out
    assert len(created) == 1
    assert [t["title"] for t in created[0][1]] == ["Позвонить маме"]


async def test_auto_executor_refuses_a_foreign_manifest_shape(monkeypatch):
    """Страховка на будущее: чужой манифест с kind='create', но без raw/_gate
    не должен быть распакован как план создания."""
    created = _wire_create(monkeypatch)
    now = time.monotonic()
    m = {"kind": "create", "consumed": True, "created": now, "summary": "x"}

    out = await s._AUTO_EXECUTORS["create_tasks"].execute("mid-foreign", m)

    assert "🛑" in out
    assert created == []


# ═════════════════════ B. delete_project ═════════════════════

class _FakeOfficial:
    def __init__(self, tasks):
        self._tasks = tasks
        self.deleted_ids = []

    def get_project_with_data(self, project_id):
        return {"project": {"id": project_id}, "tasks": self._tasks}

    def delete_project(self, project_id):
        self.deleted_ids.append(project_id)
        return {}


def _wire_delete_project(monkeypatch, tmp_path, names=None):
    names = names if names is not None else {"p1": "Работа"}
    fake = _FakeOfficial([{"id": "t1", "title": "Задача 1", "projectId": "p1",
                           "priority": 0}])

    def _delete(project_id):
        names.pop(project_id, None)
        fake.deleted_ids.append(project_id)
        return {}

    fake.delete_project = _delete
    monkeypatch.setattr(s, "ticktick", fake)
    monkeypatch.setattr(s, "ticktick_v2", None)
    monkeypatch.setattr(s, "_v2_project_names", lambda: names)
    monkeypatch.setattr(s, "_v2_project_names_or_none", lambda: dict(names))
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: {})
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    return fake


async def test_delete_project_unchanged_when_tg_off(monkeypatch, tmp_path):
    fake = _wire_delete_project(monkeypatch, tmp_path)
    _tg_off(monkeypatch)
    seen = _capture_notify(monkeypatch)
    calls = _count_approval_calls(monkeypatch)

    preview = await s.delete_project("Работа", "p1")
    assert fake.deleted_ids == []
    assert "user_reply" in preview
    assert "Telegram" not in preview
    assert seen == []

    await s.delete_project("Работа", "p1", user_reply="да")
    assert fake.deleted_ids == ["p1"]
    assert calls["n"] == 0


async def test_delete_project_plan_goes_to_telegram_when_on(monkeypatch, tmp_path):
    fake = _wire_delete_project(monkeypatch, tmp_path)
    _tg_on(monkeypatch)
    seen = _capture_notify(monkeypatch)

    preview = await s.delete_project("Работа", "p1")

    assert fake.deleted_ids == []
    assert len(seen) == 1
    assert seen[0][1] == "delete_project"
    assert "Telegram" in preview
    mid, m = _only_manifest(preview)
    assert m["kind"] == "delete_project"
    assert m["tg_notified"] is True
    assert seen[0][0] == mid


async def test_delete_project_chat_yes_alone_does_not_delete_when_tg_on(
        monkeypatch, tmp_path):
    """Главное: при включённом слое голого «да» в чате мало."""
    fake = _wire_delete_project(monkeypatch, tmp_path)
    _tg_on(monkeypatch)
    _capture_notify(monkeypatch)

    await s.delete_project("Работа", "p1")            # фаза плана
    _count_approval_calls(monkeypatch, verdict="pending")

    out = await s.delete_project("Работа", "p1", user_reply="да")

    assert fake.deleted_ids == []
    assert "Telegram" in out


async def test_delete_project_without_a_plan_falls_back_to_planning(
        monkeypatch, tmp_path):
    """Обход первой фазы: модель зовёт сразу с «да». При включённом слое это
    не удаление, а фаза плана (иначе кнопку можно было бы просто миновать)."""
    fake = _wire_delete_project(monkeypatch, tmp_path)
    _tg_on(monkeypatch)
    seen = _capture_notify(monkeypatch)

    out = await s.delete_project("Работа", "p1", user_reply="да")

    assert fake.deleted_ids == []
    assert len(seen) == 1                     # план всё-таки ушёл в Telegram
    assert "Telegram" in out


async def test_delete_project_after_button_approved_is_left_to_the_poller(
        monkeypatch, tmp_path):
    """ПЕРЕПИСАН 2026-08-06 (button-only). Раньше: «нажали кнопку → текстовое
    «да» удаляет проект». Теперь у delete_project есть собственный
    авто-исполнитель, поэтому исполняет поллер, а текстовый вызов отвергается.
    Сам факт «тул не отказывает навсегда» проверяется в
    tests/test_button_only_execution.py — там кнопка доводится до реального
    удаления."""
    fake = _wire_delete_project(monkeypatch, tmp_path)
    _tg_on(monkeypatch)
    _capture_notify(monkeypatch)

    plan_out = await s.delete_project("Работа", "p1")            # фаза плана
    _, m = _only_manifest(plan_out)
    _count_approval_calls(monkeypatch, verdict="approved")

    out = await s.delete_project("Работа", "p1", user_reply="да")

    assert fake.deleted_ids == []
    assert "САМ" in out
    assert m["consumed"] is False                     # план ждёт поллера


async def test_delete_project_rejected_button_kills_the_plan(monkeypatch, tmp_path):
    fake = _wire_delete_project(monkeypatch, tmp_path)
    _tg_on(monkeypatch)
    _capture_notify(monkeypatch)

    plan_out = await s.delete_project("Работа", "p1")
    _, m = _only_manifest(plan_out)
    _count_approval_calls(monkeypatch, verdict="rejected")

    out = await s.delete_project("Работа", "p1", user_reply="да")

    assert fake.deleted_ids == []
    assert "Отклонено" in out
    assert m["consumed"] is True


async def test_delete_project_negative_reply_never_notifies(monkeypatch, tmp_path):
    """«Нет» не должно рассылать планы в Telegram."""
    fake = _wire_delete_project(monkeypatch, tmp_path)
    _tg_on(monkeypatch)
    seen = _capture_notify(monkeypatch)

    out = await s.delete_project("Работа", "p1", user_reply="нет, отмена")

    assert fake.deleted_ids == []
    assert seen == []
    assert "НЕ подтвердил" in out


# ═════════════════════ C. rename_tag (ветка слияния) ═════════════════════

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


def _wire_tags(monkeypatch, names):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    fake = _FakeV2Tags(list(names))
    monkeypatch.setattr(s, "ticktick_v2", fake)
    return fake


async def test_rename_tag_plain_rename_untouched_by_tg_layer(monkeypatch):
    """Ветка обычного переименования гейта не имеет и не должна получить его
    как побочный эффект: даже при включённом слое — ни отправки, ни отказа."""
    fake = _wire_tags(monkeypatch, ["старый"])
    _tg_on(monkeypatch)
    seen = _capture_notify(monkeypatch)

    out = await s.rename_tag("старый", "новый")

    assert "renamed" in out
    assert seen == []
    assert fake._names == ["новый"]


async def test_rename_tag_merge_unchanged_when_tg_off(monkeypatch):
    fake = _wire_tags(monkeypatch, ["a", "b"])
    _tg_off(monkeypatch)
    seen = _capture_notify(monkeypatch)
    calls = _count_approval_calls(monkeypatch)

    refused = await s.rename_tag("a", "b", allow_merge=True)
    assert "🛑" in refused
    assert fake._names == ["a", "b"]
    assert seen == []

    out = await s.rename_tag("a", "b", allow_merge=True, user_reply="да, сливай")
    assert "renamed" in out
    assert fake._names == ["b"]
    assert calls["n"] == 0


async def test_rename_tag_merge_plan_goes_to_telegram_when_on(monkeypatch):
    fake = _wire_tags(monkeypatch, ["a", "b"])
    _tg_on(monkeypatch)
    seen = _capture_notify(monkeypatch)

    out = await s.rename_tag("a", "b", allow_merge=True)

    assert fake._names == ["a", "b"]
    assert len(seen) == 1
    assert seen[0][1] == "rename_tag"
    assert "Telegram" in out
    _, m = _only_manifest(out)
    assert m["kind"] == "rename_tag_merge"
    assert m["tg_notified"] is True


async def test_rename_tag_merge_chat_yes_alone_does_not_merge_when_tg_on(monkeypatch):
    fake = _wire_tags(monkeypatch, ["a", "b"])
    _tg_on(monkeypatch)
    _capture_notify(monkeypatch)

    await s.rename_tag("a", "b", allow_merge=True)     # фаза плана
    _count_approval_calls(monkeypatch, verdict="pending")

    out = await s.rename_tag("a", "b", allow_merge=True, user_reply="да, сливай")

    assert fake._names == ["a", "b"]                   # НЕ слито
    assert "Telegram" in out


async def test_rename_tag_merge_without_a_plan_falls_back_to_planning(monkeypatch):
    fake = _wire_tags(monkeypatch, ["a", "b"])
    _tg_on(monkeypatch)
    seen = _capture_notify(monkeypatch)

    out = await s.rename_tag("a", "b", allow_merge=True, user_reply="да, сливай")

    assert fake._names == ["a", "b"]
    assert len(seen) == 1
    assert "Telegram" in out


async def test_rename_tag_merge_after_button_approved_is_left_to_the_poller(
        monkeypatch):
    """ПЕРЕПИСАН 2026-08-06 (button-only), как и delete_project выше: слияние
    тегов теперь исполняет поллер по нажатию, текстовый вызов отвергается.
    Доведение кнопки до реального слияния — в
    tests/test_button_only_execution.py."""
    fake = _wire_tags(monkeypatch, ["a", "b"])
    _tg_on(monkeypatch)
    _capture_notify(monkeypatch)

    plan_out = await s.rename_tag("a", "b", allow_merge=True)     # фаза плана
    _, m = _only_manifest(plan_out)
    _count_approval_calls(monkeypatch, verdict="approved")

    out = await s.rename_tag("a", "b", allow_merge=True, user_reply="да, сливай")

    assert fake._names == ["a", "b"]                   # НЕ слито текстом
    assert "САМ" in out
    assert m["consumed"] is False


async def test_rename_tag_merge_rejected_button_kills_the_plan(monkeypatch):
    fake = _wire_tags(monkeypatch, ["a", "b"])
    _tg_on(monkeypatch)
    _capture_notify(monkeypatch)

    plan_out = await s.rename_tag("a", "b", allow_merge=True)
    _, m = _only_manifest(plan_out)
    _count_approval_calls(monkeypatch, verdict="rejected")

    out = await s.rename_tag("a", "b", allow_merge=True, user_reply="да, сливай")

    assert fake._names == ["a", "b"]
    assert "Отклонено" in out
    assert m["consumed"] is True


async def test_rename_tag_merge_negative_reply_never_notifies(monkeypatch):
    fake = _wire_tags(monkeypatch, ["a", "b"])
    _tg_on(monkeypatch)
    seen = _capture_notify(monkeypatch)

    out = await s.rename_tag("a", "b", allow_merge=True, user_reply="нет, не надо")

    assert fake._names == ["a", "b"]
    assert seen == []
    assert "НЕ подтвердил" in out
