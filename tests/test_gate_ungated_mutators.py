"""2026-08-06: три мутирующих тула, у которых гейта согласия не было ВООБЩЕ —
`abandon_task`, `update_project`, `archive_project`. Аудит показал, что они
меняли живое состояние TickTick с ОДНОГО вызова, без манифеста и без единой
проверки `user_reply`: модель могла «не буду делать» задачу, переименовать
проект или заархивировать его, ни разу не спросив человека.

Теперь каждый из них — обычный двухфазный тул поверх общей `_gate_single`
(тот же шаблон, что у 13 уже гейтованных): вызов #1 (без `manifest_id`)
кладёт параметры в одноразовый манифест и возвращает план, НИЧЕГО не трогая;
вызов #2 (`manifest_id` + `user_reply`) проходит `_require_consent(tier=1)`
и только после этого зовёт `_<имя_тула>_impl(**params_из_манифеста)`.
При включённом Telegram-слое план этой же операции уходит владельцу
сообщением с кнопками, и её исполняет сам сервер по нажатию — здесь это
поведение не тестируется (оно общее для `_gate_single` и покрыто
`test_gate_tg_determinism.py`), при выключенном слое (дефолт самохостера)
достаточно текстового «да», что и проверяется ниже.

Контракт имён, который эти тесты держат ЖЁСТКО (тест
`test_impl_name_and_param_contract`): фоновый поллер TG-кнопки и `_gate_single`
находят исполнителя по имени `_<имя_тула>_impl` через `globals()`, а зовут
его как `_<имя_тула>_impl(**manifest["params"])` — то есть само имя функции и
буквальное совпадение ключей `params` с её параметрами и ЕСТЬ интерфейс.
Неаккуратное переименование или опечатка в ключе ломают авто-исполнение
молча, поэтому проверяются не по хардкоду в тесте, а по реально переданному
в `_gate_single` словарю.

Сети нет: оба клиента TickTick подменены заглушкой (`_ReadOnlyStub`, см. её
докстринг ниже про правку 2026-08-07) — вызов #1 доказывает «impl не
позвался» и «в TickTick НИЧЕГО НЕ ЗАПИСАНО» (не «TickTick вообще не
трогали» — с 2026-08-07 это уже не так, см. ниже)."""
import inspect
import re

import pytest

import ticktick_mcp.src.server as s


def _extract_manifest_id(preview: str) -> str:
    m = re.search(r'manifest_id="([0-9a-f]+)"', preview)
    assert m, f"no manifest_id found in preview: {preview!r}"
    return m.group(1)


_DEFAULT_PROJECTS = [{"id": "p1", "name": "Работа"}]
_DEFAULT_TASKS = [{"id": "t1", "title": "Купить молоко", "projectId": "p1"}]


class _ReadOnlyStub:
    """2026-08-07 (def-116 follow-up, group B): ЗАМЕНА прежней `_TripWire`.

    ДО этой правки `_TripWire` валила тест на ЛЮБОЕ обращение к атрибуту —
    это было корректно, пока вызов #1 (план) для этих трёх тулов был
    буквально read-free: identity-guard стоял только в `_<tool>_impl`
    (исполнение), а он в этом файле ВСЕГДА подменён `_ImplSpy` и потому не
    вызывается на call #1 вовсе. После группы B (см. коммит этого файла):
    abandon_task/update_project/archive_project ТЕПЕРЬ сверяют
    task_id/project_id с ЖИВЫМ состоянием (_guard_task/_guard_project) уже на
    этапе ПЛАНА — той же логикой, что уже стояла на исполнении, перенесённой
    раньше по времени (тот же перенос, что для create_subtask/
    unset_task_parent/move_project_to_group и остальных 6 методов этой
    ветки). `_TripWire` поэтому стала неверным тестовым допущением — «план
    read-free» было СЛЕДСТВИЕМ отсутствия identity-guard на плане, а не
    намеренным свойством дизайна (тот же вывод, что и в delete_habit/
    ea2a47c: «план в TickTick не ходит вовсе» фиксировало баг, а не защищало
    от него).

    `_ReadOnlyStub` сохраняет ИСХОДНЫЙ смысл проверки — «вызов #1 не мутирует
    TickTick» — но перестаёт путать «не мутирует» с «не читает»: отдаёт
    ТОЛЬКО read-методы, которыми реально пользуются `_guard_task`/
    `_guard_project` при построении плана (`get_state`/`get_open_tasks`/
    `invalidate_cache`/`get_projects`), и по-прежнему валит тест на ЛЮБОЙ
    другой атрибут (в т.ч. любой WRITE-метод) — та же страховка, что была у
    `_TripWire`, просто больше не путает чтение ради сверки личности с
    записью. `projects`/`tasks` по умолчанию совпадают с `_SPECS` ниже, так
    что все ОБЫЧНЫЕ тесты файла (гейт/manifest/automation_key — не про
    identity-guard) видят СОВПАДАЮЩУЮ пару и план строится штатно; тесты
    identity-guard передают свои (несовпадающие/пустые) данные."""

    def __init__(self, label: str, projects=None, tasks=None):
        self.label = label
        self._projects = projects if projects is not None else list(_DEFAULT_PROJECTS)
        self._tasks = tasks if tasks is not None else list(_DEFAULT_TASKS)

    def __getattr__(self, name):
        if name == "get_state":
            return lambda force=False: {"projectProfiles": list(self._projects)}
        if name == "get_open_tasks":
            return lambda: list(self._tasks)
        if name == "invalidate_cache":
            return lambda: None
        if name == "get_projects":
            return lambda: list(self._projects)
        raise AssertionError(
            f"фаза плана обратилась к {self.label}.{name} — вызов #1 обязан "
            "быть МУТАЦИЯ-free (чтение ради identity-guard — ожидаемо и "
            "разрешено этой заглушкой, запись — нет)")


class _ImplSpy:
    """Подменяет `_<tool>_impl`: запоминает, с какими именно kwargs его
    позвали (это и есть проверяемый контракт), сам ничего не делает."""

    def __init__(self):
        self.calls = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return "### ✅ выполнено (spy)"


def _wire_clients(monkeypatch, projects=None, tasks=None):
    monkeypatch.setattr(s, "ticktick",
                        _ReadOnlyStub("ticktick", projects, tasks))
    monkeypatch.setattr(s, "ticktick_v2",
                        _ReadOnlyStub("ticktick_v2", projects, tasks))
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_ensure_official", lambda: None)


def _wire(monkeypatch, tool_name: str, projects=None, tasks=None) -> _ImplSpy:
    """Клиенты — read-only заглушка, исполнитель — шпион. monkeypatch.setattr
    здесь заодно и проверка существования `_<tool_name>_impl`: если функцию
    переименовали, тест падает прямо тут."""
    _wire_clients(monkeypatch, projects, tasks)
    spy = _ImplSpy()
    monkeypatch.setattr(s, f"_{tool_name}_impl", spy)
    return spy


def _capture_gate(monkeypatch) -> dict:
    """Перехватывает аргументы `_gate_single`, не меняя его поведения —
    нужен, чтобы сверять контракт с РЕАЛЬНЫМ словарём params из server.py,
    а не с его копией в тесте."""
    seen: dict = {}
    real = s._gate_single

    def spy_gate(kind, tool_name, params, *args, **kwargs):
        seen.setdefault("kind", kind)
        seen.setdefault("tool_name", tool_name)
        if params is not None:
            seen["params"] = params
        return real(kind, tool_name, params, *args, **kwargs)

    monkeypatch.setattr(s, "_gate_single", spy_gate)
    return seen


# (имя тула, позиционные аргументы, именованные аргументы, ожидаемые params)
_SPECS = [
    (
        "abandon_task",
        ("Отмечаю «не буду делать» задачу «Купить молоко»", "t1"),
        {"task_title": "Купить молоко"},
        {"summary": "Отмечаю «не буду делать» задачу «Купить молоко»",
         "task_id": "t1", "task_title": "Купить молоко"},
    ),
    (
        "update_project",
        ("Работа", "p1"),
        {"name": "Новое имя", "color": "#111111"},
        {"project_name": "Работа", "project_id": "p1", "name": "Новое имя",
         "color": "#111111", "view_mode": None},
    ),
    (
        "archive_project",
        ("Работа", "p1"),
        {"archived": True},
        {"project_name": "Работа", "project_id": "p1", "archived": True},
    ),
]

_IDS = [spec[0] for spec in _SPECS]
parametrized = pytest.mark.parametrize("spec", _SPECS, ids=_IDS)


# ===========================================================================
# 1. Вызов #1 не мутирует и не ходит в сеть
# ===========================================================================

@parametrized
async def test_call1_only_plans_and_touches_nothing(monkeypatch, spec):
    name, args, kwargs, _expected = spec
    spy = _wire(monkeypatch, name)

    preview = await getattr(s, name)(*args, **kwargs)

    assert spy.calls == [], f"{name}: вызов #1 позвал исполнителя — гейт не держит"
    assert "📋 План" in preview
    assert "ничего ещё не изменено" in preview
    mid = _extract_manifest_id(preview)
    assert s._MANIFESTS[mid]["kind"] == name
    assert s._MANIFESTS[mid]["consumed"] is False


# ===========================================================================
# 2. Вызов #2 с честным «да» исполняет РОВНО те параметры, что были в плане
# ===========================================================================

@parametrized
async def test_call2_yes_runs_impl_with_planned_params(monkeypatch, spec):
    name, args, kwargs, expected = spec
    spy = _wire(monkeypatch, name)
    tool = getattr(s, name)

    preview = await tool(*args, **kwargs)
    mid = _extract_manifest_id(preview)

    result = await tool(*args, manifest_id=mid, user_reply="да", **kwargs)

    assert "🛑" not in result
    assert spy.calls == [expected], (
        f"{name}: исполнитель получил {spy.calls!r}, ожидалось [{expected!r}]")


@parametrized
async def test_call2_ignores_swapped_arguments(monkeypatch, spec):
    """Данные берутся из манифеста, а не из аргументов вызова #2 — подменить
    цель между планом и подтверждением нельзя."""
    name, args, kwargs, expected = spec
    spy = _wire(monkeypatch, name)
    tool = getattr(s, name)

    preview = await tool(*args, **kwargs)
    mid = _extract_manifest_id(preview)

    swapped = tuple("ПОДМЕНА" if isinstance(a, str) else a for a in args)
    await tool(*swapped, manifest_id=mid, user_reply="да", **kwargs)

    assert spy.calls == [expected]


# ===========================================================================
# 3. Вызов #2 с отказом: ничего не делается, манифест гасится
# ===========================================================================

@parametrized
async def test_call2_negative_reply_burns_the_manifest(monkeypatch, spec):
    name, args, kwargs, _expected = spec
    spy = _wire(monkeypatch, name)
    tool = getattr(s, name)

    preview = await tool(*args, **kwargs)
    mid = _extract_manifest_id(preview)

    refused = await tool(*args, manifest_id=mid, user_reply="нет", **kwargs)
    assert "🛑" in refused
    assert spy.calls == []

    # «нет» — одноразовое: последующее «да» по тому же плану уже не проходит.
    retry = await tool(*args, manifest_id=mid, user_reply="да", **kwargs)
    assert "🛑" in retry
    assert spy.calls == []


@parametrized
async def test_call2_without_reply_is_refused(monkeypatch, spec):
    name, args, kwargs, _expected = spec
    spy = _wire(monkeypatch, name)
    tool = getattr(s, name)

    preview = await tool(*args, **kwargs)
    mid = _extract_manifest_id(preview)

    refused = await tool(*args, manifest_id=mid, user_reply="", **kwargs)
    assert "🛑" in refused
    assert spy.calls == []


# ===========================================================================
# 4. Чужой/протухший manifest_id — внятный отказ
# ===========================================================================

@parametrized
async def test_call2_unknown_manifest_is_refused_clearly(monkeypatch, spec):
    name, args, kwargs, _expected = spec
    spy = _wire(monkeypatch, name)

    result = await getattr(s, name)(*args, manifest_id="deadbeefcafe",
                                    user_reply="да", **kwargs)

    assert spy.calls == []
    assert "🛑" in result
    assert "deadbeefcafe" in result
    assert "заново" in result


@parametrized
async def test_call2_manifest_of_another_tool_is_refused(monkeypatch, spec):
    """Манифест другого тула не подходит: `kind` сверяется явно."""
    name, args, kwargs, _expected = spec
    spy = _wire(monkeypatch, name)

    other = s.uuid.uuid4().hex[:12]
    s._MANIFESTS[other] = {"kind": "create_tag", "params": {"name": "x"},
                           "created": s.time.monotonic(),
                           "plan_shown_at": s.time.monotonic(),
                           "consumed": False}
    try:
        result = await getattr(s, name)(*args, manifest_id=other,
                                        user_reply="да", **kwargs)
    finally:
        s._MANIFESTS.pop(other, None)

    assert spy.calls == []
    assert "🛑" in result


# ===========================================================================
# 5. Одноразовость: тот же manifest_id не срабатывает дважды
# ===========================================================================

@parametrized
async def test_manifest_is_one_shot(monkeypatch, spec):
    name, args, kwargs, expected = spec
    spy = _wire(monkeypatch, name)
    tool = getattr(s, name)

    preview = await tool(*args, **kwargs)
    mid = _extract_manifest_id(preview)

    first = await tool(*args, manifest_id=mid, user_reply="да", **kwargs)
    assert "🛑" not in first

    again = await tool(*args, manifest_id=mid, user_reply="да", **kwargs)
    assert "🛑" in again
    assert spy.calls == [expected], "исполнитель отработал больше одного раза"


# ===========================================================================
# 6. automation_key — headless-путь, как у остальных 13
# ===========================================================================

@parametrized
async def test_automation_key_bypasses_user_reply(monkeypatch, spec):
    name, args, kwargs, expected = spec
    spy = _wire(monkeypatch, name)
    tool = getattr(s, name)

    preview = await tool(*args, **kwargs)
    mid = _extract_manifest_id(preview)

    result = await tool(*args, manifest_id=mid, automation_key=s.SECRET,
                        **kwargs)

    assert "🛑" not in result
    assert spy.calls == [expected]


@parametrized
async def test_wrong_automation_key_still_refused(monkeypatch, spec):
    name, args, kwargs, _expected = spec
    spy = _wire(monkeypatch, name)
    tool = getattr(s, name)

    preview = await tool(*args, **kwargs)
    mid = _extract_manifest_id(preview)

    result = await tool(*args, manifest_id=mid,
                        automation_key="not-the-real-secret", **kwargs)

    assert "🛑" in result
    assert spy.calls == []


# ===========================================================================
# 7. Контракт имён: `_<tool>_impl` существует и его параметры покрыты params
# ===========================================================================

@parametrized
async def test_impl_name_and_param_contract(monkeypatch, spec):
    name, args, kwargs, _expected = spec
    _wire_clients(monkeypatch)          # НЕ подменяем impl — он тут и изучается
    seen = _capture_gate(monkeypatch)

    preview = await getattr(s, name)(*args, **kwargs)
    assert "manifest_id" in preview

    # (1) kind и tool_name одинаковы и равны имени python-функции тула —
    # по ним поллер TG-кнопки сопоставляет манифест с исполнителем.
    assert seen["kind"] == name, f"kind={seen['kind']!r}, ожидалось {name!r}"
    assert seen["tool_name"] == name

    # (2) исполнитель называется РОВНО `_<имя_тула>_impl`.
    impl = getattr(s, f"_{name}_impl", None)
    assert impl is not None, (
        f"нет функции _{name}_impl — авто-исполнение по кнопке ищет её в "
        "globals() именно по этому имени")
    assert inspect.iscoroutinefunction(impl)

    # (3) ключи params дословно совпадают с параметрами исполнителя: его
    # зовут как _impl(**manifest["params"]), лишний/опечатанный ключ = TypeError.
    sig = inspect.signature(impl)
    assert not any(p.kind is p.VAR_KEYWORD for p in sig.parameters.values()), (
        f"_{name}_impl принимает **kwargs — опечатка в params пролезет молча")
    param_names = set(sig.parameters)
    keys = set(seen["params"])
    assert keys <= param_names, (
        f"_{name}_impl не принимает ключи {sorted(keys - param_names)} — "
        "params и сигнатура разошлись")
    required = {n for n, p in sig.parameters.items() if p.default is p.empty}
    assert required <= keys, (
        f"params не покрывает обязательные параметры _{name}_impl: "
        f"{sorted(required - keys)}")


@parametrized
def test_tool_accepts_gate_arguments(spec):
    """Публичная сигнатура тула должна нести все три служебных аргумента —
    иначе клиент физически не сможет сделать вызов #2."""
    name, _args, _kwargs, _expected = spec
    sig = inspect.signature(getattr(s, name))
    for arg in ("manifest_id", "user_reply", "automation_key"):
        assert arg in sig.parameters, f"{name} не принимает {arg}"
        assert sig.parameters[arg].default == "", (
            f"{name}.{arg} должен быть необязательным (пустая строка по "
            "умолчанию), иначе вызов #1 сломается")


# ===========================================================================
# Локальные валидации update_project остаются ПЕРЕД гейтом
# ===========================================================================

async def test_update_project_empty_fields_refused_before_gate(monkeypatch):
    spy = _wire(monkeypatch, "update_project")
    result = await s.update_project("Работа", "p1")
    assert "🛑" in result
    assert "manifest_id" not in result
    assert spy.calls == []


async def test_update_project_bad_view_mode_refused_before_gate(monkeypatch):
    spy = _wire(monkeypatch, "update_project")
    result = await s.update_project("Работа", "p1", view_mode="галерея")
    assert "Invalid view_mode" in result
    assert "manifest_id" not in result
    assert spy.calls == []


# ===========================================================================
# 2026-08-07: plan-phase identity-guard (def-116 follow-up, group B) —
# update_project's project_id↔project_name is now cross-checked BEFORE the
# plan is built too (see server.py and tests/test_slice6_projects.py's own
# `test_update_project_plan_identity_guard_*` for the wrong-name/missing
# cases). This one extra check belongs HERE because it needs THIS file's
# automation_key/_SPECS plumbing: a valid automation_key runs on the FIRST
# call with no plan card and no Telegram button ever shown, so if the
# identity check only lived inside _gate_single/execution, a false
# project_id/project_name pair would sail through silently on a single valid
# key. The check sits BEFORE _gate_single, so it applies here too.
# ===========================================================================

async def test_update_project_automation_key_mismatch_is_refused_before_plan(monkeypatch):
    spy = _wire(monkeypatch, "update_project",
               projects=[{"id": "p1", "name": "Личное"}])
    monkeypatch.setattr(s, "SECRET", "test-secret")

    result = await s.update_project("Работа", "p1", name="X",
                                    automation_key="test-secret")

    assert result.startswith("🛑 План НЕ построен")
    assert "«Личное»" in result
    assert "manifest_id" not in result
    assert spy.calls == []


# ===========================================================================
# 2026-08-07: same headless-path check as above, for archive_project — see
# tests/test_slice6_projects.py's own `test_archive_project_plan_identity_
# guard_*` for the wrong-name/missing/unarchive-leniency cases (that file
# already owns _guard_project/_v2_project_names-style testing for this tool).
# ===========================================================================

async def test_archive_project_automation_key_mismatch_is_refused_before_plan(monkeypatch):
    spy = _wire(monkeypatch, "archive_project",
               projects=[{"id": "p1", "name": "Личное"}])
    monkeypatch.setattr(s, "SECRET", "test-secret")

    result = await s.archive_project("Работа", "p1", archived=True,
                                     automation_key="test-secret")

    assert result.startswith("🛑 План НЕ построен")
    assert "«Личное»" in result
    assert "manifest_id" not in result
    assert spy.calls == []


# ===========================================================================
# 2026-08-07: plan-phase identity-guard (def-116 follow-up, group B) —
# abandon_task's task_id↔task_title is now cross-checked BEFORE the plan is
# built too, same _guard_task shape as duplicate_task (mismatch AND missing
# both block — "не буду делать" can only be marked on an OPEN task;
# _abandon_task_impl already treats a non-open id as a hard 🛑 on execution,
# unlike e.g. add_task_comment where a missing task only warns). Unlike
# _guard_project (used by update_project/archive_project above), _guard_task
# DOES have a soft "unavailable" branch, so this block also covers the
# read-failure-warns/still-catches pair — same shape as the analogous tests
# in tests/test_slice1_real_gates.py for set_task_parent.
# ===========================================================================

async def test_abandon_task_plan_identity_guard_blocks_wrong_title(monkeypatch):
    """task_id resolves to a REAL task ("Купить хлеб"), caller's task_title
    claims a DIFFERENT one ("Купить молоко") — before this fix, call #1
    would have built and shown a plan card describing the WRONG task."""
    spy = _wire(monkeypatch, "abandon_task",
               tasks=[{"id": "t1", "title": "Купить хлеб", "projectId": "p1"}])

    result = await s.abandon_task("Отказываюсь", "t1", task_title="Купить молоко")

    assert result.startswith("🛑 План НЕ построен")
    assert "«Купить хлеб»" in result
    assert "manifest_id" not in result
    assert spy.calls == []


async def test_abandon_task_plan_identity_guard_blocks_missing_task(monkeypatch):
    """_abandon_task_impl treats a task NOT among open tasks as a hard 🛑 too
    (в отличие от add_task_comment, где комментировать завершённую задачу
    разрешено) — «не буду делать» можно пометить только ОТКРЫТУЮ задачу, и
    the plan-phase transfer must reproduce that same severity."""
    spy = _wire(monkeypatch, "abandon_task", tasks=[])

    result = await s.abandon_task("Отказываюсь", "t-нет-такой", task_title="Купить молоко")

    assert result.startswith("🛑 План НЕ построен")
    assert "manifest_id" not in result
    assert spy.calls == []


async def test_abandon_task_plan_read_failure_does_not_block_but_warns(monkeypatch):
    """A live-read hiccup while BUILDING the plan (call #1) must not block
    every abandon — fail-open here is cheaper than refusing everyone whose
    network is briefly flaky. The plan is still built, honestly warns that
    the task was not verified, and call #2 still reaches the (stubbed)
    execution normally."""
    spy = _wire(monkeypatch, "abandon_task",
               tasks=[{"id": "t1", "title": "Купить молоко", "projectId": "p1"}])
    real_open_by_id = s._open_by_id
    calls = {"n": 0}

    def _flaky(fresh=False):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real_open_by_id(fresh=fresh)
    monkeypatch.setattr(s, "_open_by_id", _flaky)

    preview = await s.abandon_task("Отказываюсь", "t1", task_title="Купить молоко")
    assert "🛑" not in preview, "временный сбой чтения не должен блокировать план"
    assert "НЕ удалось сверить" in preview
    mid = _extract_manifest_id(preview)

    result = await s.abandon_task("Отказываюсь", "t1", task_title="Купить молоко",
                                  manifest_id=mid, user_reply="да")
    assert "🛑" not in result
    assert spy.calls == [{"summary": "Отказываюсь", "task_id": "t1",
                          "task_title": "Купить молоко"}]


async def test_abandon_task_plan_read_failure_still_lets_execution_catch_a_real_mismatch(
        monkeypatch):
    """Same read failure on the plan as above, but this time the pair
    actually DOESN'T match. The plan-phase check couldn't run (so it warns
    instead of refusing), but the execution-phase guard inside the REAL
    `_abandon_task_impl` (NOT stubbed in this one test, unlike the rest of
    this file — needed to prove the real guard, not a spy, catches it) still
    catches the real mismatch: a network blip on planning must not weaken
    the protection at execution time."""
    _wire_clients(monkeypatch,
                 tasks=[{"id": "t1", "title": "Купить хлеб", "projectId": "p1"}])
    real_open_by_id = s._open_by_id
    calls = {"n": 0}

    def _flaky(fresh=False):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real_open_by_id(fresh=fresh)
    monkeypatch.setattr(s, "_open_by_id", _flaky)

    preview = await s.abandon_task("Отказываюсь", "t1", task_title="Купить молоко")
    assert "🛑" not in preview
    mid = _extract_manifest_id(preview)

    result = await s.abandon_task("Отказываюсь", "t1", task_title="Купить молоко",
                                  manifest_id=mid, user_reply="да")
    assert result.startswith("🛑")
    assert "«Купить хлеб»" in result


async def test_abandon_task_automation_key_mismatch_is_refused_before_plan(monkeypatch):
    """Headless path (#118): a valid automation_key runs on the FIRST call,
    with no plan card and no Telegram button ever shown — so if the identity
    check only lived inside _gate_single/execution, a false name+id pair
    would sail through silently on a single valid key."""
    spy = _wire(monkeypatch, "abandon_task",
               tasks=[{"id": "t1", "title": "Купить хлеб", "projectId": "p1"}])
    monkeypatch.setattr(s, "SECRET", "test-secret")

    result = await s.abandon_task("Отказываюсь", "t1", task_title="Купить молоко",
                                  automation_key="test-secret")

    assert result.startswith("🛑 План НЕ построен")
    assert "«Купить хлеб»" in result
    assert "manifest_id" not in result
    assert spy.calls == []
