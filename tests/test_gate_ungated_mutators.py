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

Сети нет: оба клиента TickTick подменены «растяжкой» (_TripWire), любое
обращение к которой валит тест — так вызов #1 доказывает не только «impl не
позвался», но и «в TickTick вообще не ходили»."""
import inspect
import re

import pytest

import ticktick_mcp.src.server as s


def _extract_manifest_id(preview: str) -> str:
    m = re.search(r'manifest_id="([0-9a-f]+)"', preview)
    assert m, f"no manifest_id found in preview: {preview!r}"
    return m.group(1)


class _TripWire:
    """Заглушка вместо клиента TickTick: ЛЮБОЕ обращение к атрибуту — это
    попытка сходить в сеть, и на фазе плана её быть не должно."""

    def __init__(self, label: str):
        self.label = label

    def __getattr__(self, name):
        raise AssertionError(
            f"фаза плана обратилась к {self.label}.{name} — вызов #1 обязан "
            "быть полностью read-free/сетевo-free")


class _ImplSpy:
    """Подменяет `_<tool>_impl`: запоминает, с какими именно kwargs его
    позвали (это и есть проверяемый контракт), сам ничего не делает."""

    def __init__(self):
        self.calls = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return "### ✅ выполнено (spy)"


def _wire_clients(monkeypatch):
    monkeypatch.setattr(s, "ticktick", _TripWire("ticktick"))
    monkeypatch.setattr(s, "ticktick_v2", _TripWire("ticktick_v2"))
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_ensure_official", lambda: None)


def _wire(monkeypatch, tool_name: str) -> _ImplSpy:
    """Клиенты — растяжка, исполнитель — шпион. monkeypatch.setattr здесь
    заодно и проверка существования `_<tool_name>_impl`: если функцию
    переименовали, тест падает прямо тут."""
    _wire_clients(monkeypatch)
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
