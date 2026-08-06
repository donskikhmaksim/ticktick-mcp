"""Гейт согласия на `create_attachment_upload_url` (аудит 2026-08-06, второй
заход).

Тул сам НИЧЕГО не пишет в TickTick — его тело локальное. Но то, что он
отдаёт, — предъявительский пропуск на запись в аккаунт владельца: маршрут
`PUT /ul/{token}` публичен и проверяет РОВНО ОДНО, подпись токена
(`_verify_attachment_token`); ни заголовка, ни куки, ни `MCP_SECRET`
предъявлять не нужно; заливает файл сам сервер сессией владельца; токен
многоразовый (ни nonce, ни реестра использованных) и живёт до
`ATTACHMENT_LINK_TTL_MAX_MIN` = 120 минут. Довод «модель не сделает raw PUT»
ограничивает интерфейс, а не возможности — агент с оболочкой шлёт обычный
HTTP-запрос, и тул сам печатает готовую команду `curl`. Выдача ссылки —
единственный момент, когда сервер вообще способен спросить человека, поэтому
подтверждение стоит здесь.

Главный тест файла — первый: план НЕ должен содержать ни рабочей ссылки, ни
команды curl. Если пропуск утечёт уже в превью (а превью уходит и в
Telegram), гейт бессмыслен — подтверждать будет нечего, ссылка уже выдана.

Ни сети, ни Postgres: клиенты TickTick подменены «растяжкой».
"""
import inspect
import re

import pytest

import ticktick_mcp.src.server as s


TASK_ID = "task1"
PROJECT_ID = "proj1"
TOOL = "create_attachment_upload_url"
KWARGS = dict(task_id=TASK_ID, project_id=PROJECT_ID, filename="scan.png",
              ttl_minutes=15)
# То, что обязано лежать в манифесте и уехать в `_impl` (size_bytes НЕ едет:
# это проверка размера ДО плана, а не часть операции).
EXPECTED_PARAMS = dict(task_id=TASK_ID, project_id=PROJECT_ID,
                       filename="scan.png", ttl_minutes=15)


@pytest.fixture(autouse=True)
def public_base(monkeypatch):
    """Сервер знает свой публичный адрес — иначе тул отказывает ДО гейта."""
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://tt.example.com")
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)


@pytest.fixture(autouse=True)
def isolate_manifests():
    before = dict(s._MANIFESTS)
    tombs = dict(s._MANIFEST_TOMBSTONES)
    s._MANIFESTS.clear()
    s._MANIFEST_TOMBSTONES.clear()
    yield
    s._MANIFESTS.clear()
    s._MANIFESTS.update(before)
    s._MANIFEST_TOMBSTONES.clear()
    s._MANIFEST_TOMBSTONES.update(tombs)


class _TripWire:
    """Любое обращение к клиенту TickTick на пути этого тула — ошибка."""

    def __init__(self, label: str):
        self.label = label

    def __getattr__(self, name):
        raise AssertionError(f"обращение к {self.label}.{name} — этот тул "
                             "в TickTick не ходит вовсе")


class _ImplSpy:
    def __init__(self):
        self.calls = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return "https://tt.example.com/ul/ССЫЛКА-ЗАГЛУШКА"


def _wire_clients(monkeypatch):
    monkeypatch.setattr(s, "ticktick", _TripWire("ticktick"))
    monkeypatch.setattr(s, "ticktick_v2", _TripWire("ticktick_v2"))
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_ensure_official", lambda: None)


def _wire(monkeypatch) -> _ImplSpy:
    _wire_clients(monkeypatch)
    spy = _ImplSpy()
    monkeypatch.setattr(s, f"_{TOOL}_impl", spy)   # заодно проверка имени
    return spy


def _mid(preview: str) -> str:
    m = re.search(r'manifest_id="([0-9a-f]+)"', preview)
    assert m, f"в превью нет id манифеста:\n{preview}"
    return m.group(1)


# ===========================================================================
# 1. Вызов #1 не выдаёт НИЧЕГО — ни ссылки, ни curl-команды
# ===========================================================================

async def test_call1_hands_out_no_link_and_no_curl(monkeypatch):
    """Самый важный тест файла: превью — это описание намерения, а не сам
    пропуск. Ни `/ul/`, ни базового адреса, ни `curl` в нём быть не может."""
    spy = _wire(monkeypatch)

    preview = await s.create_attachment_upload_url(**KWARGS)

    assert spy.calls == [], "вызов #1 позвал исполнителя — гейт не держит"
    assert "/ul/" not in preview, f"в плане утекла сама ссылка:\n{preview}"
    assert "https://tt.example.com" not in preview
    assert "curl" not in preview.lower(), f"в плане утекла команда:\n{preview}"
    assert "📋 План" in preview and "ничего ещё не изменено" in preview
    mid = _mid(preview)
    assert s._MANIFESTS[mid]["kind"] == TOOL
    assert s._MANIFESTS[mid]["consumed"] is False


async def test_call1_signs_no_token_at_all(monkeypatch):
    """Не «ссылки нет в тексте», а «токен вообще не подписывался»: подпись —
    единственный шаг, после которого пропуск существует."""
    _wire_clients(monkeypatch)
    signed = []
    real_sign = s._sign_attachment_token
    monkeypatch.setattr(s, "_sign_attachment_token",
                        lambda *a, **k: signed.append(a) or real_sign(*a, **k))

    await s.create_attachment_upload_url(**KWARGS)

    assert signed == [], "на фазе плана уже подписан ul-токен"


async def test_preview_is_readable_and_names_the_target(monkeypatch):
    """Обратная сторона: человек должен понимать, что одобряет."""
    _wire(monkeypatch)
    preview = await s.create_attachment_upload_url(**KWARGS)
    assert "scan.png" in preview and TASK_ID in preview
    assert "15 мин" in preview


# ===========================================================================
# 2. Вызов #2 с честным «да» — ссылка выдана, impl позван теми же аргументами
# ===========================================================================

async def test_call2_yes_runs_impl_with_planned_params(monkeypatch):
    spy = _wire(monkeypatch)
    preview = await s.create_attachment_upload_url(**KWARGS)

    result = await s.create_attachment_upload_url(
        **KWARGS, manifest_id=_mid(preview), user_reply="да")

    assert "🛑" not in result
    assert spy.calls == [EXPECTED_PARAMS], (
        f"исполнитель получил {spy.calls!r}, ожидалось [{EXPECTED_PARAMS!r}]")


async def test_call2_yes_really_produces_a_working_link(monkeypatch):
    """Без заглушки: настоящая ссылка появляется только на втором вызове, и
    её токен валиден для маршрута /ul."""
    _wire_clients(monkeypatch)
    preview = await s.create_attachment_upload_url(**KWARGS)
    assert "/ul/" not in preview

    result = await s.create_attachment_upload_url(
        **KWARGS, manifest_id=_mid(preview), user_reply="да")

    assert "https://tt.example.com/ul/" in result
    token = result.split("/ul/")[1].split()[0].strip('"')
    payload = s._verify_attachment_token(token, "ul")
    assert payload and payload["t"] == TASK_ID and payload["n"] == "scan.png"


async def test_call2_ignores_swapped_arguments(monkeypatch):
    """Подменить цель между планом и подтверждением нельзя — берутся данные
    манифеста (иначе одобрили бы ссылку на одну задачу, а получили на другую)."""
    spy = _wire(monkeypatch)
    preview = await s.create_attachment_upload_url(**KWARGS)

    await s.create_attachment_upload_url(
        **{**KWARGS, "task_id": "ЧУЖАЯ", "filename": "payload.exe",
           "ttl_minutes": 120},
        manifest_id=_mid(preview), user_reply="да")

    assert spy.calls == [EXPECTED_PARAMS]


# ===========================================================================
# 3. Отказ — ссылки нет, план аннулирован
# ===========================================================================

async def test_negative_reply_burns_the_plan(monkeypatch):
    spy = _wire(monkeypatch)
    preview = await s.create_attachment_upload_url(**KWARGS)
    mid = _mid(preview)

    refused = await s.create_attachment_upload_url(**KWARGS, manifest_id=mid,
                                                   user_reply="нет")

    assert "🛑" in refused and "/ul/" not in refused
    assert spy.calls == []
    assert s._MANIFESTS[mid]["consumed"] is True

    retry = await s.create_attachment_upload_url(**KWARGS, manifest_id=mid,
                                                 user_reply="да")
    assert "🛑" in retry
    assert spy.calls == []


async def test_call2_without_reply_is_refused(monkeypatch):
    spy = _wire(monkeypatch)
    preview = await s.create_attachment_upload_url(**KWARGS)

    refused = await s.create_attachment_upload_url(
        **KWARGS, manifest_id=_mid(preview), user_reply="")

    assert "🛑" in refused and "/ul/" not in refused
    assert spy.calls == []


# ===========================================================================
# 4. Чужой/протухший идентификатор манифеста — внятный отказ
# ===========================================================================

async def test_unknown_manifest_is_refused_clearly(monkeypatch):
    spy = _wire(monkeypatch)

    result = await s.create_attachment_upload_url(
        **KWARGS, manifest_id="deadbeefcafe", user_reply="да")

    assert spy.calls == []
    assert "🛑" in result and "deadbeefcafe" in result and "заново" in result
    assert "/ul/" not in result


async def test_manifest_of_another_tool_is_refused(monkeypatch):
    spy = _wire(monkeypatch)
    other = s.uuid.uuid4().hex[:12]
    s._MANIFESTS[other] = {"kind": "create_tag", "params": {"name": "x"},
                           "created": s.time.monotonic(),
                           "plan_shown_at": s.time.monotonic(),
                           "consumed": False}

    result = await s.create_attachment_upload_url(**KWARGS, manifest_id=other,
                                                  user_reply="да")

    assert spy.calls == []
    assert "🛑" in result and "/ul/" not in result


# ===========================================================================
# 5. Одноразовость: тем же id второй пропуск не выпросишь
# ===========================================================================

async def test_manifest_is_one_shot(monkeypatch):
    spy = _wire(monkeypatch)
    preview = await s.create_attachment_upload_url(**KWARGS)
    mid = _mid(preview)

    first = await s.create_attachment_upload_url(**KWARGS, manifest_id=mid,
                                                 user_reply="да")
    assert "🛑" not in first

    again = await s.create_attachment_upload_url(**KWARGS, manifest_id=mid,
                                                 user_reply="да")
    assert "🛑" in again and "/ul/" not in again
    assert spy.calls == [EXPECTED_PARAMS], "исполнитель отработал дважды"


# ===========================================================================
# 6. automation_key — headless-путь, как у соседних гейтованных тулов
# ===========================================================================

async def test_automation_key_bypasses_user_reply(monkeypatch):
    spy = _wire(monkeypatch)
    preview = await s.create_attachment_upload_url(**KWARGS)

    result = await s.create_attachment_upload_url(
        **KWARGS, manifest_id=_mid(preview), automation_key=s.SECRET)

    assert "🛑" not in result
    assert spy.calls == [EXPECTED_PARAMS]


async def test_wrong_automation_key_still_refused(monkeypatch):
    spy = _wire(monkeypatch)
    preview = await s.create_attachment_upload_url(**KWARGS)

    result = await s.create_attachment_upload_url(
        **KWARGS, manifest_id=_mid(preview),
        automation_key="not-the-real-secret")

    assert "🛑" in result and "/ul/" not in result
    assert spy.calls == []


# ===========================================================================
# 7. Контракт имён: поллер TG-кнопки ищет исполнителя через globals()
# ===========================================================================

async def test_impl_name_and_param_contract(monkeypatch):
    _wire_clients(monkeypatch)          # impl НЕ подменяем — он тут изучается
    seen = {}
    real_gate = s._gate_single

    def spy_gate(kind, tool_name, params, *a, **kw):
        seen.setdefault("kind", kind)
        seen.setdefault("tool_name", tool_name)
        if params is not None:
            seen["params"] = params
        return real_gate(kind, tool_name, params, *a, **kw)

    monkeypatch.setattr(s, "_gate_single", spy_gate)

    preview = await s.create_attachment_upload_url(**KWARGS)
    assert "manifest_id" in preview

    # (1) kind и tool_name одинаковы и равны имени python-функции тула.
    assert seen["kind"] == TOOL and seen["tool_name"] == TOOL

    # (2) исполнитель называется РОВНО `_create_attachment_upload_url_impl`.
    impl = getattr(s, f"_{TOOL}_impl", None)
    assert impl is not None, (
        f"нет функции _{TOOL}_impl — авто-исполнение по кнопке ищет её в "
        "globals() именно по этому имени")
    assert inspect.iscoroutinefunction(impl)

    # (3) ключи params дословно совпадают с параметрами исполнителя.
    sig = inspect.signature(impl)
    assert not any(p.kind is p.VAR_KEYWORD for p in sig.parameters.values()), (
        f"_{TOOL}_impl принимает **kwargs — опечатка в params пролезет молча")
    param_names = set(sig.parameters)
    keys = set(seen["params"])
    assert keys <= param_names, (
        f"_{TOOL}_impl не принимает ключи {sorted(keys - param_names)}")
    required = {n for n, p in sig.parameters.items() if p.default is p.empty}
    assert required <= keys, (
        f"params не покрывает обязательные параметры _{TOOL}_impl: "
        f"{sorted(required - keys)}")

    # (4) служебные аргументы гейта в манифест не попадают.
    for junk in ("manifest_id", "user_reply", "automation_key"):
        assert junk not in seen["params"]


def test_tool_accepts_gate_arguments():
    sig = inspect.signature(s.create_attachment_upload_url)
    for arg in ("manifest_id", "user_reply", "automation_key"):
        assert arg in sig.parameters, f"{TOOL} не принимает {arg}"
        assert sig.parameters[arg].default == ""


# ===========================================================================
# Локальные валидации остаются ПЕРЕД гейтом (незачем планировать заведомо
# невозможное — и незачем спрашивать человека про то, что всё равно не выйдет)
# ===========================================================================

async def test_oversized_file_refused_before_the_gate(monkeypatch):
    spy = _wire(monkeypatch)
    result = await s.create_attachment_upload_url(
        **KWARGS, size_bytes=s.ATTACHMENT_MAX_BYTES + 1)
    assert "МБ" in result and "manifest_id" not in result
    assert spy.calls == [] and s._MANIFESTS == {}


async def test_unknown_public_domain_refused_before_the_gate(monkeypatch):
    spy = _wire(monkeypatch)
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)

    result = await s.create_attachment_upload_url(**KWARGS)

    assert "PUBLIC_BASE_URL" in result and "manifest_id" not in result
    assert spy.calls == [] and s._MANIFESTS == {}
