"""Аварийный выключатель гейта снимает ТОЛЬКО «спроси человека» — приёмка
разбора QA-2 (2026-08-19): «нет» человека и механика одноразовости манифеста
работают и при `TICKTICK_MCP_GATE_DISABLED`.

ЧТО БЫЛО СЛОМАНО. Обход по выключателю пришивался самым ранним `return` во
всех трёх точках (`_require_consent`, `_gate_batch`/`_gate_single` через
`_gate_bypass_channel`, kill-switch-ветка `delete_tasks`), и потому снимал
не только требование подтверждения, но и всё вокруг согласия:

  • явное «нет» человека игнорировалось — план построен, человек ответил
    «нет», модель честно передала user_reply="нет", а операция ИСПОЛНЯЛАСЬ;
  • one-shot/TTL/object_hash манифеста не проверялись — два параллельных
    execute одного плана (ретрай клиента после рестарта) оба проходили, и
    операция исполнялась ДВАЖДЫ; план старше часа исполнялся как свежий.

Пары «позитив + контроль» — как в tests/test_gate_killswitch.py: при
выключенном выключателе поведение прежнее до буквы, а при включённом
выключателе валидный свежий план по-прежнему исполняется с первого вызова
(сам обход эти правки не сузили).
"""
import asyncio
import time

import pytest

import ticktick_mcp.src.consent as consent
import ticktick_mcp.src.server as s


ENV = consent._GATE_DISABLED_ENV


@pytest.fixture(autouse=True)
def _isolate_manifests():
    before = dict(consent._MANIFESTS)
    yield
    consent._MANIFESTS.clear()
    consent._MANIFESTS.update(before)


@pytest.fixture(autouse=True)
def _gate_on_by_default(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    yield


def _fresh_create_manifest(mid="ks-core-cr", consumed=False, age_s=0.0):
    now = time.monotonic() - age_s
    m = {"kind": "create", "raw": [{"title": "Задача", "project_id": "p1"}],
         "created": now, "plan_shown_at": now, "summary": "Создаю задачу",
         "consumed": consumed, "tool": "create_tasks", "_gate": "create"}
    consent._MANIFESTS[mid] = m
    return mid, m


def _fresh_batch_manifest(mid="ks-core-b", age_s=0.0):
    now = time.monotonic() - age_s
    tasks = [{"taskId": "t1", "title": "A"}]
    m = {"kind": "update", "tool": "update_tasks", "_gate": "batch",
         "tasks": tasks, "summary": "Меняю A", "created": now,
         "plan_shown_at": now, "consumed": False,
         "object_hash": consent._manifest_object_hash("update", ["t1"])}
    consent._MANIFESTS[mid] = m
    return mid, m


def _describe(t):
    return f"задача {t.get('taskId')}"


# ===========================================================================
# 1. Явное «нет» человека главнее выключателя — `_require_consent`
# ===========================================================================

def test_require_consent_honours_an_explicit_no_even_with_the_switch_on(
        monkeypatch):
    monkeypatch.setenv(ENV, "1")
    mid, m = _fresh_create_manifest()
    cr = consent._require_consent(action="create", tier=1, manifest=m,
                                  user_reply="нет", manifest_id=mid)
    assert cr.ok is False, \
        "«нет» человека обязано побеждать аварийный выключатель"
    assert "НЕ подтвердил" in cr.reason
    assert m["consumed"] is True, \
        "отвергнутый план обязан гаснуть — как и при включённом гейте"


@pytest.mark.parametrize("reply", ["стоп", "отмена", "не надо",
                                   "ок, кроме последней"])
def test_require_consent_honours_every_refusal_shape(monkeypatch, reply):
    monkeypatch.setenv(ENV, "1")
    mid, m = _fresh_create_manifest()
    cr = consent._require_consent(action="create", tier=1, manifest=m,
                                  user_reply=reply, manifest_id=mid)
    assert cr.ok is False


def test_require_consent_switch_still_bypasses_on_a_valid_fresh_plan(
        monkeypatch):
    """Контроль: сам обход жив — валидный свежий план без отказа проходит с
    пустым user_reply, причина — прежняя строка `gate_disabled_switch`."""
    monkeypatch.setenv(ENV, "1")
    mid, m = _fresh_create_manifest()
    cr = consent._require_consent(action="create", tier=1, manifest=m,
                                  user_reply="", manifest_id=mid)
    assert cr.ok is True
    assert cr.reason == "gate_disabled_switch"


def test_require_consent_default_negative_path_is_untouched(monkeypatch):
    """Контроль: переменная не задана — «нет» отклоняет план ровно как
    раньше."""
    mid, m = _fresh_create_manifest()
    cr = consent._require_consent(action="create", tier=1, manifest=m,
                                  user_reply="нет", manifest_id=mid)
    assert cr.ok is False
    assert m["consumed"] is True


# ===========================================================================
# 2. Механика манифеста (one-shot / TTL / object_hash) работает и при
#    выключенном гейте — `_require_consent`
# ===========================================================================

def test_require_consent_switch_does_not_resurrect_a_consumed_manifest(
        monkeypatch):
    """Ядро сценария двойного исполнения: второй параллельный execute видит
    манифест уже погашенным — и обязан получить отказ, а не «гейт выключен,
    проходи». До правки первый же `return` по выключателю пропускал проверку
    `consumed`, и план исполнялся дважды."""
    monkeypatch.setenv(ENV, "1")
    mid, m = _fresh_create_manifest(consumed=True)
    cr = consent._require_consent(action="create", tier=1, manifest=m,
                                  user_reply="", manifest_id=mid)
    assert cr.ok is False
    assert "исполнен" in cr.reason or "протух" in cr.reason


def test_require_consent_switch_does_not_execute_an_expired_manifest(
        monkeypatch):
    monkeypatch.setenv(ENV, "1")
    mid, m = _fresh_create_manifest(age_s=consent._MANIFEST_TTL + 60)
    cr = consent._require_consent(action="create", tier=1, manifest=m,
                                  user_reply="", manifest_id=mid)
    assert cr.ok is False
    assert "истёк" in cr.reason
    assert m["consumed"] is True


def test_require_consent_switch_does_not_skip_the_object_hash_binding(
        monkeypatch):
    """Подмена набора объектов между планом и исполнением ловится и при
    выключенном гейте: object_hash — привязка «подтверждали ИМЕННО ЭТО»,
    к согласию человека она отношения не имеет."""
    monkeypatch.setenv(ENV, "1")
    mid, m = _fresh_batch_manifest()
    cr = consent._require_consent(action="update", tier=1, manifest=m,
                                  user_reply="", object_ids=["t1", "t-чужой"],
                                  manifest_id=mid)
    assert cr.ok is False
    assert "не совпадает" in cr.reason


def test_two_sequential_executes_of_one_plan_create_only_once(monkeypatch):
    """Интеграционно, через настоящий execute_task_creation: первый вызов
    исполняет, второй — отказ, движок создания вызван РОВНО один раз."""
    monkeypatch.setenv(ENV, "1")
    monkeypatch.setattr(s, "_ensure_official", lambda: None)
    calls = []

    async def _fake_create(summary, raw):
        calls.append(summary)
        return "✅ Создано 1 из 1 (двойник исполнителя)"

    monkeypatch.setattr(s, "_create_tasks_impl", _fake_create)
    mid, _ = _fresh_create_manifest()

    first = asyncio.run(s.execute_task_creation(mid, user_reply=""))
    second = asyncio.run(s.execute_task_creation(mid, user_reply=""))

    assert len(calls) == 1, \
        "one-shot обязан работать и при выключенном гейте"
    assert "двойник исполнителя" in first
    assert second.startswith("🛑") or "исполнен" in second


# ===========================================================================
# 3. `_gate_batch` / `_gate_single` — «нет» перехватывается до обхода
# ===========================================================================

def test_gate_batch_call2_with_switch_on_honours_the_no(monkeypatch):
    monkeypatch.setenv(ENV, "1")
    mid, m = _fresh_batch_manifest()
    outcome = asyncio.run(consent._gate_batch(
        "update", "update_tasks", None, "Меняю A", mid, "нет", _describe))
    assert outcome.proceed is False, \
        "«нет» при выключенном гейте не имеет права исполнять план"
    assert "НЕ подтвердил" in outcome.message
    assert m["consumed"] is True, "отвергнутый план обязан гаснуть"


def test_gate_batch_call1_with_switch_on_refuses_on_a_no_instead_of_executing(
        monkeypatch):
    monkeypatch.setenv(ENV, "1")
    outcome = asyncio.run(consent._gate_batch(
        "update", "update_tasks", [{"taskId": "t1"}], "Меняю A", "",
        "нет, не надо", _describe))
    assert outcome.proceed is False
    assert "НЕ подтвердил" in outcome.message


def test_gate_single_call1_with_switch_on_refuses_on_a_no(monkeypatch):
    monkeypatch.setenv(ENV, "1")
    outcome = asyncio.run(consent._gate_single(
        "create_tag", "create_tag", {"name": "тег"}, "", "нет",
        lambda p: f"Тег «{p['name']}»"))
    assert outcome.proceed is False
    assert "НЕ подтвердил" in outcome.message


def test_gate_batch_switch_on_without_a_no_still_executes_first_call(
        monkeypatch):
    """Контроль: обычный kill-switch-путь (пустой/утвердительный ответ) не
    сужен — первый вызов исполняет, как и до правки."""
    monkeypatch.setenv(ENV, "1")
    tasks = [{"taskId": "t1", "title": "A"}]
    outcome = asyncio.run(consent._gate_batch(
        "update", "update_tasks", tasks, "Меняю A", "", "", _describe))
    assert outcome.proceed is True
    assert outcome.tasks == tasks


def test_automation_key_contract_is_untouched_by_the_refusal_check(
        monkeypatch):
    """Контракт канала по ключу не тронут: headless-клиент с ВЕРНЫМ ключом
    проходит с первого вызова независимо от содержимого user_reply (он туда
    реплику человека и не кладёт) — ровно как до правки."""
    monkeypatch.setenv(ENV, "1")
    monkeypatch.setattr(consent, "_automation_key_channel",
                        lambda k: "static" if k == "верный" else "")
    tasks = [{"taskId": "t1", "title": "A"}]
    outcome = asyncio.run(consent._gate_batch(
        "update", "update_tasks", tasks, "Меняю A", "", "нет", _describe,
        automation_key="верный"))
    assert outcome.proceed is True


# ===========================================================================
# 4. `_claim_plan_for_automation` — TTL проверяется, протухший план гаснет
# ===========================================================================

def test_claim_plan_refuses_an_expired_manifest(monkeypatch):
    mid, m = _fresh_batch_manifest(age_s=consent._MANIFEST_TTL + 120)
    claimed = asyncio.run(consent._claim_plan_for_automation("update", mid))
    assert claimed is None, \
        "план старше TTL не имеет права исполняться по обходу"
    assert m["consumed"] is True, \
        "протухший план обязан гаснуть (иначе его исполнит кнопка в Telegram)"


def test_claim_plan_still_hands_out_a_fresh_manifest():
    """Контроль: свежий живой план по-прежнему выдаётся и гасится — сам
    механизм «ключ проводит старый план до конца» не сломан."""
    mid, m = _fresh_batch_manifest()
    claimed = asyncio.run(consent._claim_plan_for_automation("update", mid))
    assert claimed is m
    assert m["consumed"] is True


def test_gate_batch_switch_on_does_not_execute_an_expired_plan(monkeypatch):
    """Сквозной вариант: kill-switch-вызов с manifest_id протухшего плана —
    отказ с «истёк/не найден», не исполнение."""
    monkeypatch.setenv(ENV, "1")
    mid, _ = _fresh_batch_manifest(age_s=consent._MANIFEST_TTL + 120)
    outcome = asyncio.run(consent._gate_batch(
        "update", "update_tasks", None, "Меняю A", mid, "", _describe))
    assert outcome.proceed is False
    assert "не найден/истёк" in outcome.message or "истёк" in outcome.message


# ===========================================================================
# 5. `delete_tasks`, kill-switch-ветка — «нет» не исполняется
# ===========================================================================

@pytest.fixture
def _direct_deletion_stand(monkeypatch):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Проект"})
    monkeypatch.setattr(
        s, "_open_by_id",
        lambda fresh=False: {"t1": {"id": "t1", "title": "Мусор",
                                    "projectId": "p1"}})
    calls = []

    async def _fake_delete(manifest_id, m=None):
        calls.append(manifest_id)
        return "✅ Удалено 1 из 1 (двойник исполнителя)"

    monkeypatch.setattr(s, "_execute_task_deletion_impl", _fake_delete)
    return calls


_ONE_TASK = [{"taskId": "t1", "title": "Мусор", "projectId": "p1"}]


def test_delete_tasks_with_switch_on_honours_the_no(
        monkeypatch, _direct_deletion_stand):
    monkeypatch.setenv(ENV, "1")
    before_ids = set(consent._MANIFESTS)
    out = asyncio.run(s.delete_tasks.direct(
        "⚠️ Удаляю «Мусор»", list(_ONE_TASK), user_reply="нет"))
    assert _direct_deletion_stand == [], \
        "«нет» при выключенном гейте не имеет права удалять"
    assert "НЕ подтвердил" in out
    # Смотрим ТОЛЬКО манифесты, созданные ЭТИМ вызовом: в общем прогоне в
    # `_MANIFESTS` могут жить чужие (других тест-файлов) — они не предмет.
    live = [mid for mid, m in consent._MANIFESTS.items()
            if mid not in before_ids and not m.get("consumed")]
    assert live == [], "созданный по пути манифест обязан быть погашен"


def test_delete_tasks_with_switch_on_still_deletes_without_a_no(
        monkeypatch, _direct_deletion_stand):
    """Контроль: без отказа kill-switch-ветка исполняет с первого вызова, как
    и до правки."""
    monkeypatch.setenv(ENV, "1")
    out = asyncio.run(s.delete_tasks.direct("⚠️ Удаляю «Мусор»",
                                            list(_ONE_TASK)))
    assert len(_direct_deletion_stand) == 1
    assert "двойник исполнителя" in out
