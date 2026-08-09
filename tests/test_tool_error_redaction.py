"""П7 (2026-08-09): секрет доступа и подписанные ссылки вложений не должны
попадать в текст ошибки, который тул возвращает МОДЕЛИ.

До этой правки `except Exception as e: return f"Error ...: {str(e)}"` отдавал
текст исключения как есть — исключение HTTP-клиента (или ретрай к самому
серверу) вполне может содержать наш собственный `/mcp/<secret>` путь или
подписанную ссылку вложения `/dl/<token>` / `/ul/<token>`. `log_redaction`
уже чистит такие секреты в ЛОГАХ; здесь проверяется, что тот же редактор
теперь стоит и на пути, которым текст уходит модели — через `_tool_error` /
`_redact_for_user` в server.py.

Секреты здесь ВЫДУМАННЫЕ (как и в tests/test_log_redaction.py) — боевое
значение не должно появляться ни в тестах, ни в отчётах.

Проверено на пяти разных командах/путях отказа:
  * `get_projects` — исключение клиента (SECRET-путь, `_tool_error`);
  * `get_project`  — уже готовая строка ошибки в `{'error': ...}` (сигнатурная
    ссылка `/dl/<token>`, позиционный редактор без знания секрета);
  * `get_task`     — исключение клиента с сигнатурной ссылкой (`_tool_error`);
  * `_create_project_impl` — отдельная (не через `_tool_error`) точка правки,
    русский текст «TickTick отклонил» / «Ошибка», see server.py:7868;
  * `_maybe_tg_notify_plan` — КРИТИЧНЫЙ дефект, найденный независимым
    аудитом 2026-08-09 (доказан рабочим примером): при сбое отправки в
    Telegram `err` из `tg_approval.notify_plan` возвращался моделью как
    есть, а `tg_approval._tg_call` строит URL с ТОКЕНОМ БОТА буквально
    (`f"{TELEGRAM_API}/bot{cfg.bot_token}/{method}"`) — токен бота даёт
    полный контроль над вторым фактором подтверждения удалений.
"""
import time

import ticktick_mcp.src.server as s
import ticktick_mcp.src.tg_approval as tg_approval

# Выдуманные значения, похожие по форме на настоящие (длинные, url-safe) —
# как в tests/test_log_redaction.py.
FAKE_SECRET = "fake0secret0do0not0use0abcdef123456"
FAKE_LINK_TOKEN = "eyJwIjoiMSIsInQiOiIyIn0.ZmFrZXNpZ25hdHVyZQ"
FAKE_BOT_TOKEN = "123456789:AAHfake0bot0token0DO0NOT0USE"


class FakeOfficial:
    """Минимальный двойник TickTickClient (v1) — реальные методы с реальными
    сигнатурами (без **kwargs, см. tests/test_doubles_do_not_cheat.py),
    каждый настраивается независимо через конструктор."""

    def __init__(self, *, projects_error=None, project_result=None,
                 task_error=None, create_error=None):
        self._projects_error = projects_error
        self._project_result = project_result
        self._task_error = task_error
        self._create_error = create_error

    def get_projects(self) -> list:
        if self._projects_error is not None:
            raise self._projects_error
        return []

    def get_project(self, project_id: str) -> dict:
        return self._project_result

    def get_task(self, project_id: str, task_id: str) -> dict:
        if self._task_error is not None:
            raise self._task_error
        return {}

    def create_project(self, name: str, color: str = "#F18181",
                       view_mode: str = "list", kind: str = "TASK") -> dict:
        if self._create_error is not None:
            raise self._create_error
        return {}


# ---------------------------------------------------------------------------
# Юнит-уровень: сама функция-редактор.
# ---------------------------------------------------------------------------

def test_redact_for_user_strips_mcp_secret(monkeypatch):
    monkeypatch.setattr(s, "SECRET", FAKE_SECRET)
    exc = RuntimeError(f"connection refused for https://x.example/mcp/{FAKE_SECRET}")
    out = s._redact_for_user(exc)
    assert FAKE_SECRET not in out
    assert "<mcp-secret>" in out
    assert "connection refused" in out  # смысл сообщения не потерян


def test_redact_for_user_strips_signed_link_token():
    # Позиционное правило (/dl/, /ul/) срабатывает даже без знания секрета —
    # тут SECRET сознательно не задаётся. РЕАЛЬНЫЙ формат ссылки (не
    # придуманный для теста): `_public_base_url()` в server.py собирает
    # `f"{base}/dl/{token}"` с ПОЛНЫМ https-адресом перед "/dl/" — именно
    # так, а не голым путём после пробела, ссылка и попадает в текст
    # исключения при сетевом сбое (независимый аудит 2026-08-09, см.
    # tests/test_log_redaction.py::test_link_token_is_redacted_inside_a_full_url,
    # где под этот формат чинился сам редактор).
    exc = RuntimeError(
        f"upstream 502 while replaying https://ticktick-mcp.up.railway.app"
        f"/dl/{FAKE_LINK_TOKEN}")
    out = s._redact_for_user(exc)
    assert FAKE_LINK_TOKEN not in out
    assert "<link-token>" in out
    assert "upstream 502" in out


def test_tool_error_builds_uniform_message_and_redacts():
    exc = RuntimeError(
        f"boom https://ticktick-mcp.up.railway.app/dl/{FAKE_LINK_TOKEN}")
    out = s._tool_error("fetching widgets", exc)
    assert out.startswith("Error fetching widgets: ")
    assert FAKE_LINK_TOKEN not in out
    assert "<link-token>" in out


# ---------------------------------------------------------------------------
# Команда 1: get_projects — секрет уезжает через исключение клиента.
# ---------------------------------------------------------------------------

async def test_get_projects_exception_does_not_leak_secret(monkeypatch):
    monkeypatch.setattr(s, "SECRET", FAKE_SECRET)
    err = RuntimeError(
        f"HTTPSConnectionPool: Max retries exceeded with url: /mcp/{FAKE_SECRET}")
    monkeypatch.setattr(s, "ticktick", FakeOfficial(projects_error=err))

    out = await s.get_projects()

    assert FAKE_SECRET not in out
    assert "<mcp-secret>" in out
    # Смысл ошибки — какая операция сломалась — обязан остаться читаемым.
    assert "Error fetching projects" in out


# ---------------------------------------------------------------------------
# Команда 2: get_project — секрет уже лежит в готовой строке {'error': ...}
# (путь клиента ticktick_client.py:404, тоже str(e) изнутри), без SECRET.
# ---------------------------------------------------------------------------

async def test_get_project_error_dict_does_not_leak_signed_link(monkeypatch):
    monkeypatch.setattr(s, "ticktick_v2", None)  # выключает проверку Inbox
    err_text = (f"404 while fetching https://ticktick-mcp.up.railway.app"
                f"/dl/{FAKE_LINK_TOKEN} for retry-diagnostics")
    monkeypatch.setattr(s, "ticktick", FakeOfficial(project_result={"error": err_text}))

    out = await s.get_project(project_id="proj1")

    assert FAKE_LINK_TOKEN not in out
    assert "<link-token>" in out
    assert "Error fetching project" in out


# ---------------------------------------------------------------------------
# Команда 3: get_task — то же исключение, но с подписанной ссылкой вложения.
# ---------------------------------------------------------------------------

async def test_get_task_exception_does_not_leak_link_token(monkeypatch):
    err = RuntimeError(
        f"connection reset while streaming https://ticktick-mcp.up.railway.app"
        f"/ul/{FAKE_LINK_TOKEN}")
    monkeypatch.setattr(s, "ticktick", FakeOfficial(task_error=err))

    out = await s.get_task(project_id="proj1", task_id="task1")

    assert FAKE_LINK_TOKEN not in out
    assert "<link-token>" in out
    assert "Error fetching task" in out


# ---------------------------------------------------------------------------
# Команда 4: _create_project_impl — отдельная (не _tool_error) точка правки,
# см. server.py:7868 («### ❌ Проект ... НЕ создан\n\nОшибка: ...»).
# ---------------------------------------------------------------------------

async def test_create_project_impl_rejection_does_not_leak_secret(monkeypatch):
    monkeypatch.setattr(s, "SECRET", FAKE_SECRET)
    err = RuntimeError(f"POST /mcp/{FAKE_SECRET} timed out")
    monkeypatch.setattr(s, "ticktick", FakeOfficial(create_error=err))

    out = await s._create_project_impl(name="Проект Х")

    assert FAKE_SECRET not in out
    assert "<mcp-secret>" in out
    assert "НЕ создан" in out


# ---------------------------------------------------------------------------
# Команда 5: _maybe_tg_notify_plan — токен Telegram-бота не должен уезжать
# модели, когда отправка плана на подтверждение проваливается по сети.
# Живой пример из аудита: HTTPSConnectionPool(...): Max retries exceeded
# with url: /bot<токен>/sendMessage — весь URL целиком лежит в str(e)
# requests-исключения, которое tg_approval._tg_call кладёт в description.
# ---------------------------------------------------------------------------

def _plan_manifest_for_tg(mid: str) -> dict:
    """Минимальный манифест — только то, что читает _maybe_tg_notify_plan
    (пометка tg_notified/_tg_tool/_tg_manifest_id и попытка persist, которая
    молча пропускается без настроенного Postgres — см. manifest_store.store_ready)."""
    now = time.monotonic()
    s._MANIFESTS[mid] = {
        "kind": "delete", "items": [], "created": now,
        "plan_shown_at": now, "summary": "план", "consumed": False,
        "object_hash": s._manifest_object_hash("delete", []),
    }
    return s._MANIFESTS[mid]


async def test_maybe_tg_notify_plan_failure_does_not_leak_bot_token(monkeypatch):
    monkeypatch.setattr(s, "_TG_CFG", tg_approval.TgApprovalConfig(
        enabled=True, bot_token=FAKE_BOT_TOKEN, owner_chat_id="1",
        server="ticktick", tools_allowlist=None, ttl_s=3600))
    mid = "mid-tg-leak-test"
    _plan_manifest_for_tg(mid)

    # Ровно то, что кладёт tg_approval._tg_call в description при сетевом
    # сбое requests.post(f"{TELEGRAM_API}/bot{cfg.bot_token}/{method}", ...).
    leak_text = (
        "HTTPSConnectionPool(host='api.telegram.org', port=443): Max "
        f"retries exceeded with url: /bot{FAKE_BOT_TOKEN}/sendMessage")
    monkeypatch.setattr(tg_approval, "notify_plan",
                        lambda *a, **k: (False, leak_text))

    try:
        out = await s._maybe_tg_notify_plan("delete_tasks", mid, "### план")
    finally:
        s._MANIFESTS.pop(mid, None)

    assert FAKE_BOT_TOKEN not in out
    assert "/bot<tg-bot-token>" in out
    assert "Не смог отправить запрос подтверждения в Telegram" in out
