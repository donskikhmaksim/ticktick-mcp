"""Аварийный общий выключатель гейта `TICKTICK_MCP_GATE_DISABLED` — приёмка
правки 2026-08-14 «выключатель отключает гейт ЦЕЛИКОМ, а не наполовину».

ЧТО БЫЛО СЛОМАНО. `_gate_disabled()` проверялся ровно в одном месте —
внутри `_require_consent`, — а тот зовётся ТОЛЬКО в фазе исполнения (вызов с
`manifest_id`). Фаза плана (первый вызов инструмента) про выключатель не
знала вовсе, поэтому при `TICKTICK_MCP_GATE_DISABLED=1` инструмент всё равно
строил план, отправлял владельцу сообщение с кнопками и требовал второго
вызова. То есть выключатель снимал вторую половину гейта и не снимал первую.

ГЛАВНОЕ УСЛОВИЕ ПРИЁМКИ — НЕГАТИВНАЯ ПОЛОВИНА: при НЕзаданной переменной
поведение обязано быть прежним до буквы (план на первом вызове, исполнение
на втором, отправка в Telegram при включённом слое). Поэтому каждый
позитивный тест здесь идёт в паре с контрольным, где выключатель выключен.

Ни сети, ни Postgres: Telegram-слой перехватывается на уровне
`tg_approval.enabled_for`/`notify_plan` (единственная дверь наружу из
`_maybe_tg_notify_plan`), TickTick — фейком клиента.
"""
import asyncio
import json

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
    """Переменная не задана = гейт включён. Каждый тест, которому нужен
    выключенный гейт, задаёт её сам."""
    monkeypatch.delenv(ENV, raising=False)
    yield


class _TgSpy:
    """Перехват ЕДИНСТВЕННОЙ двери в Telegram (`tg_approval.notify_plan`) +
    включённый allowlist, чтобы «ничего не ушло» доказывалось при ВКЛЮЧЁННОМ
    слое подтверждения, а не потому, что слой выключен по умолчанию."""

    def __init__(self, monkeypatch):
        self.sent = []
        monkeypatch.setattr(consent.tg_approval, "enabled_for",
                            lambda cfg, tool: True)
        monkeypatch.setattr(consent.tg_approval, "notify_plan",
                            self._notify)

    def _notify(self, cfg, manifest_id, preview_text, tool):
        self.sent.append((tool, manifest_id, preview_text))
        return True, ""


class _FakeTicktickV2:
    def __init__(self):
        self.created = []
        self._tags = []

    def create_tag(self, name, color=None):
        self.created.append((name, color))
        self._tags.append({"name": name})
        return {}

    def get_state(self, force=False):
        return {}

    def get_tags(self):
        return list(self._tags)


# ===========================================================================
# 1. Разбор значения переменной
# ===========================================================================

@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " on ",
                                   "On", "Yes"])
def test_truthy_values_disable_the_gate(monkeypatch, value):
    monkeypatch.setenv(ENV, value)
    assert consent._gate_disabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "y",
                                   "enabled", "да", "2", "on1"])
def test_garbage_values_do_not_disable_the_gate(monkeypatch, value):
    """Мусор НЕ выключает гейт: опечатка в переменной окружения не имеет
    права молча снять подтверждение со всех мутаций."""
    monkeypatch.setenv(ENV, value)
    assert consent._gate_disabled() is False


def test_unset_variable_means_gate_enabled(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    assert consent._gate_disabled() is False


# ===========================================================================
# 2. `_gate_single` (create_tag и ещё ~20 тулов) — один вызов вместо двух
# ===========================================================================

def test_gate_single_executes_on_first_call_and_sends_nothing_to_telegram(
        monkeypatch, caplog):
    monkeypatch.setenv(ENV, "1")
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    fake_v2 = _FakeTicktickV2()
    monkeypatch.setattr(s, "ticktick_v2", fake_v2)
    tg = _TgSpy(monkeypatch)

    with caplog.at_level("WARNING"):
        result = asyncio.run(s.create_tag(name="выключатель-тег", color=None))

    assert fake_v2.created == [("выключатель-тег", None)], \
        "при выключенном гейте первый же вызов обязан ИСПОЛНИТЬ операцию"
    assert "Манифест `" not in result, "плана быть не должно"
    assert tg.sent == [], "в Telegram при выключенном гейте ничего не уходит"
    assert any("ГЕЙТ ВЫКЛЮЧЕН" in r.message for r in caplog.records), \
        "факт обхода обязан быть в логе WARNING"


def test_gate_single_default_behaviour_is_untouched(monkeypatch):
    """Контроль: переменная не задана — прежние две фазы, план уходит в
    Telegram, мутации на первом вызове нет."""
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    fake_v2 = _FakeTicktickV2()
    monkeypatch.setattr(s, "ticktick_v2", fake_v2)
    tg = _TgSpy(monkeypatch)

    plan = asyncio.run(s.create_tag(name="обычный-тег", color=None))
    assert fake_v2.created == []
    assert "Манифест `" in plan
    assert len(tg.sent) == 1 and tg.sent[0][0] == "create_tag"


def test_gate_single_bypass_is_marked_in_the_mutation_journal(monkeypatch, tmp_path):
    """Обход помечается каналом `gate_disabled` в НАСТОЯЩЕМ журнале мутаций
    (`_journal_write` — единственная дверь туда): запись об операции обязана
    отличаться и от обычной подтверждённой, и от обхода по `automation_key`.

    Всё внутри ОДНОЙ корутины намеренно: метка живёт в `contextvars`, а
    `asyncio.run` исполняет корутину в КОПИИ контекста — снаружи `.set()`
    изнутри гейта не виден."""
    monkeypatch.setenv(ENV, "1")
    monkeypatch.setattr(consent, "_JOURNAL_DIR", str(tmp_path))

    async def _gate_then_journal():
        outcome = await consent._gate_single(
            "create_tag", "create_tag", {"name": "журнальный"}, "", "",
            lambda p: f"Тег «{p['name']}»")
        assert outcome.proceed is True
        return consent._op_journal("tags", [{"name": "журнальный"}],
                                   "создание тега")

    assert asyncio.run(_gate_then_journal())
    rows = [json.loads(line) for line in
            (tmp_path / "deletion_journal.jsonl").read_text(
                encoding="utf-8").splitlines() if line.strip()]
    assert rows[-1].get("automation_channel") == "gate_disabled"


# ===========================================================================
# 3. `_gate_batch` — то же самое для списочных тулов
# ===========================================================================

def _describe(t):
    return f"задача {t.get('taskId')}"


def test_gate_batch_executes_on_first_call(monkeypatch):
    monkeypatch.setenv(ENV, "1")
    tasks = [{"taskId": "t1", "title": "A"}]
    outcome = asyncio.run(consent._gate_batch(
        "update", "update_tasks", tasks, "Меняю A", "", "", _describe))
    assert outcome.proceed is True
    assert outcome.tasks == tasks
    assert outcome.summary == "Меняю A"


def test_gate_batch_default_still_builds_a_plan(monkeypatch):
    tasks = [{"taskId": "t1", "title": "A"}]
    outcome = asyncio.run(consent._gate_batch(
        "update", "update_tasks", tasks, "Меняю A", "", "", _describe))
    assert outcome.proceed is False
    assert "Манифест `" in outcome.message


def test_gate_batch_with_switch_on_never_calls_the_sync_wait(monkeypatch):
    """Гибридное ожидание — часть интерактивного пути: при выключенном гейте
    ждать нечего и некого."""
    monkeypatch.setenv(ENV, "1")
    monkeypatch.setattr(consent, "CONSENT_SYNC_WAIT_MS", 5000)
    calls = {"n": 0}

    async def _counting(mid):
        calls["n"] += 1
        return None

    monkeypatch.setattr(consent, "_sync_wait_for_decision", _counting)
    outcome = asyncio.run(consent._gate_batch(
        "update", "update_tasks", [{"taskId": "t1"}], "s", "", "", _describe))
    assert outcome.proceed is True
    assert calls["n"] == 0


# ===========================================================================
# 4. Самостоятельные инструменты фазы плана (plan_task_creation /
#    plan_task_deletion) — те, на которые выключатель раньше не влиял вовсе
# ===========================================================================

@pytest.fixture
def _creation_stand(monkeypatch):
    monkeypatch.setattr(s, "_ensure_official", lambda: None)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Проект"})
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: {})
    calls = []

    async def _fake_create(summary, raw):
        calls.append((summary, list(raw)))
        return "✅ Создано 1 из 1 (двойник исполнителя)"

    monkeypatch.setattr(s, "_create_tasks_impl", _fake_create)
    return calls


def test_plan_task_creation_executes_immediately_when_switch_is_on(
        monkeypatch, _creation_stand):
    monkeypatch.setenv(ENV, "1")
    tg = _TgSpy(monkeypatch)
    out = asyncio.run(s.plan_task_creation(
        "Создаю задачу", [{"title": "Купить молоко", "project_id": "p1"}]))

    assert len(_creation_stand) == 1, "создание должно исполниться сразу"
    assert "двойник исполнителя" in out
    assert "План создания" not in out
    assert tg.sent == []


def test_plan_task_creation_default_is_a_plan_only(monkeypatch, _creation_stand):
    tg = _TgSpy(monkeypatch)
    out = asyncio.run(s.plan_task_creation(
        "Создаю задачу", [{"title": "Купить молоко", "project_id": "p1"}]))
    assert _creation_stand == [], "на первом вызове ничего не создаётся"
    assert "### 📋 План создания" in out
    assert len(tg.sent) == 1


@pytest.fixture
def _deletion_stand(monkeypatch):
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


def test_plan_task_deletion_executes_immediately_when_switch_is_on(
        monkeypatch, _deletion_stand):
    monkeypatch.setenv(ENV, "1")
    tg = _TgSpy(monkeypatch)
    out = asyncio.run(s.plan_task_deletion(
        "Удаляю мусор", [{"taskId": "t1", "title": "Мусор", "projectId": "p1"}]))

    assert len(_deletion_stand) == 1, "удаление должно исполниться сразу"
    assert "двойник исполнителя" in out
    assert "План удаления" not in out
    assert tg.sent == []


def test_plan_task_deletion_default_is_a_plan_only(monkeypatch, _deletion_stand):
    tg = _TgSpy(monkeypatch)
    out = asyncio.run(s.plan_task_deletion(
        "Удаляю мусор", [{"taskId": "t1", "title": "Мусор", "projectId": "p1"}]))
    assert _deletion_stand == []
    assert "### 📋 План удаления" in out
    assert len(tg.sent) == 1


# ===========================================================================
# 5. `_maybe_tg_notify_plan` — при выключенном гейте не шлёт и НЕ гасит план
#    своим fail-closed
# ===========================================================================

def test_notify_plan_is_a_no_op_with_switch_on_even_if_sending_would_fail(
        monkeypatch):
    monkeypatch.setenv(ENV, "1")
    monkeypatch.setattr(consent.tg_approval, "enabled_for",
                        lambda cfg, tool: True)

    def _explode(*a, **kw):
        raise AssertionError("отправки быть не должно")

    monkeypatch.setattr(consent.tg_approval, "notify_plan", _explode)
    consent._MANIFESTS["ks-notify"] = {"kind": "x", "consumed": False,
                                       "created": 0.0}
    out = asyncio.run(consent._maybe_tg_notify_plan(
        "delete_tasks", "ks-notify", "### план", "хвост для модели"))

    assert out == "### план\nхвост для модели"
    assert consent._MANIFESTS["ks-notify"]["consumed"] is False, \
        "fail-closed не должен гасить план — отправки не было вовсе"
    assert "tg_notified" not in consent._MANIFESTS["ks-notify"]
