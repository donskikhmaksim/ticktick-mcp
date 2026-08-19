"""Живой QA прода 2026-08-19 — пять дефектов инструментов проектов/групп/
колонок (ветка fix/qa2-projects). По пунктам:

№1 (критично): delete_project_group рапортовал «проекты остались, просто без
папки», а независимый get_project показывал «Folder: (unknown group)
(id: <удалённая группа>)» — TickTick при удалении группы САМ groupId у
входивших проектов НЕ чистит, они остаются с висячей ссылкой. Фикс: после
подтверждённого удаления группы сервер сам разгруппировывает осиротевшие
проекты (batch через set_projects_group — тот же хелпер, что у
move_project_to_group) и пост-верифицирует ФАКТИЧЕСКОЕ состояние проектов;
текст ответа соответствует реальности во всех исходах.

№2: move_project_to_group(group_id="NONE") не разгруппировывал (клиент слал
`groupId: null`, прод его молча игнорирует) — починено в v2-клиенте (шлётся
буквальный "NONE", который TickTick сам использует как «без папки»), сквозные
тесты — в test_silent_failures.py (#100, переписаны). Здесь — батч-контракт
нового общего хелпера set_projects_group.

№3: сырые HTTP-ошибки («500 Server Error:  for url: …») уходили человеку без
объяснения. Фикс: _humanize_api_error (429/401/403/5xx → русская фраза +
исходник в скобках) в _tool_error и в ветках «TickTick отклонил»; плюс
валидация color (hex) и непустого name ДО запроса — по образцу существующей
проверки view_mode.

№4: create_project_group("") молча создавал безымянную группу, хотя
create_project такое имя отклоняет. Фикс: понятный отказ до API.

№5: create_project_column не предупреждал человека, что в проекте с
view_mode=list колонка не видна (знание жило только в докстринге). Фикс:
предупреждение в самом ответе, из того же get_project_with_data, которым
пост-верификация уже подтверждает колонку.

Сети нет: клиенты подменяются фейками (стиль test_slice6_projects.py)."""
import re

import pytest

import ticktick_mcp.src.server as s
from ticktick_mcp.src.ticktick_v2_client import TickTickV2Client


def _extract_manifest_id(preview: str) -> str:
    m = re.search(r'manifest_id="([0-9a-f]+)"', preview)
    assert m, f"no manifest_id found in preview: {preview!r}"
    return m.group(1)


async def _gated_call(fn, *args, **kwargs):
    preview = await fn(*args, **kwargs)
    assert "🛑" not in preview, f"plan phase unexpectedly refused: {preview!r}"
    mid = _extract_manifest_id(preview)
    return await fn(*args, manifest_id=mid, user_reply="да", **kwargs)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Пост-верификация ретраит через time.sleep — в тестах не спим."""
    monkeypatch.setattr(s.time, "sleep", lambda *a, **k: None)


# ===========================================================================
# №1 — delete_project_group: разгруппировка осиротевших проектов + честный
# отчёт по фактическому состоянию
# ===========================================================================

class FakeV2GroupDelete:
    """Ведёт себя как НАСТОЯЩИЙ TickTick (наблюдено живым QA 2026-08-19):
    удаление группы НЕ чистит groupId у входивших в неё проектов — они
    остаются с висячей ссылкой, пока их не разгруппируют отдельным запросом
    (set_projects_group). Разгруппированный проект хранит groupId="NONE" —
    как на проде."""

    def __init__(self, groups=None, projects=None, ungroup_error=None,
                 ungroup_noop=False):
        self.groups = groups if groups is not None else []
        self.projects = projects if projects is not None else []
        self._ungroup_error = ungroup_error
        self._ungroup_noop = ungroup_noop
        self.calls = []

    def get_state(self, force=False):
        return {}

    def list_project_groups(self):
        return [dict(g) for g in self.groups]

    def list_projects(self):
        return [dict(p) for p in self.projects]

    def delete_project_group(self, group_id):
        self.calls.append(("delete_group", group_id))
        self.groups = [g for g in self.groups if g.get("id") != group_id]
        # groupId проектов НАМЕРЕННО не трогаем — так делает настоящий сервер
        return {}

    def set_projects_group(self, project_ids, group_id):
        self.calls.append(("set_group", tuple(sorted(project_ids)), group_id))
        if self._ungroup_error:
            raise RuntimeError(self._ungroup_error)
        if not self._ungroup_noop:
            for p in self.projects:
                if p.get("id") in project_ids:
                    p["groupId"] = "NONE"
        return {}


def _wire_delete(monkeypatch, fake):
    monkeypatch.setattr(s, "ticktick_v2", fake)


async def test_delete_group_ungroups_the_orphaned_projects(monkeypatch):
    """Главный тест критичного пункта: после удаления группы входившие в неё
    проекты реально разгруппированы (одним batch-вызовом), и ответ называет
    их поимённо с пометкой «проверено» — а не постулирует «без папки» на
    основании одного лишь исчезновения группы."""
    fake = FakeV2GroupDelete(
        groups=[{"id": "g1", "name": "Личное"}],
        projects=[{"id": "p1", "name": "Дом", "groupId": "g1"},
                  {"id": "p2", "name": "Финансы", "groupId": "g1"},
                  {"id": "p3", "name": "Чужой", "groupId": "g2"}])
    _wire_delete(monkeypatch, fake)

    out = await s._delete_project_group_impl("Личное", "g1")

    assert ("set_group", ("p1", "p2"), "NONE") in fake.calls, (
        f"осиротевшие проекты не разгруппированы одним batch-вызовом: "
        f"{fake.calls}")
    assert all(p["groupId"] == "NONE" for p in fake.projects
               if p["id"] in ("p1", "p2")), "проекты остались с висячей ссылкой"
    assert fake.projects[2]["groupId"] == "g2", "тронут проект из ДРУГОЙ папки"
    assert out.startswith("✅"), out
    assert "«Дом»" in out and "«Финансы»" in out, (
        f"разгруппированные проекты не названы:\n{out}")
    assert "без папки" in out and "проверено" in out, out
    assert "«Чужой»" not in out


async def test_delete_group_does_not_claim_ungrouped_when_the_call_fails(monkeypatch):
    """Дословный сценарий QA-репорта: группа удалена, но проекты внутри
    хранят ссылку на несуществующую папку. Раньше тул отвечал «проекты
    остались, просто без папки» — состояние, которого не возникло. Теперь:
    группа — ✅ (это правда), проекты — ⚠️ поимённо, со ссылкой на
    удалённую папку и способом починить; фразы «просто без папки» в ответе
    быть не должно."""
    fake = FakeV2GroupDelete(
        groups=[{"id": "g1", "name": "Личное"}],
        projects=[{"id": "p1", "name": "Дом", "groupId": "g1"}],
        ungroup_error="429 Client Error:  for url: https://api.ticktick.com/api/v2/batch/project")
    _wire_delete(monkeypatch, fake)

    out = await s._delete_project_group_impl("Личное", "g1")

    assert fake.projects[0]["groupId"] == "g1", "фейк не должен был разгруппировать"
    assert out.startswith("✅"), "удаление группы подтверждено — это правда"
    assert "⚠️" in out and "«Дом»" in out, out
    assert "ОСТАЛИСЬ со ссылкой на удалённую папку" in out, out
    assert "move_project_to_group" in out, "не сказано, как починить"
    assert "просто без папки" not in out, (
        f"тул снова рапортует состояние, которого не возникло:\n{out}")
    # №3 заодно: сырой 429 переведён в русскую фразу
    assert "слишком много запросов" in out, out


async def test_delete_group_postverify_catches_a_silently_stuck_ungroup(monkeypatch):
    """set_projects_group «успешен», но перечитывание показывает, что ссылка
    осталась (тихий отказ сервера) — ответ обязан сказать ⚠️, а не
    «проверено, без папки»: пост-верификация смотрит на фактическое
    состояние проектов, не на код ответа."""
    fake = FakeV2GroupDelete(
        groups=[{"id": "g1", "name": "Личное"}],
        projects=[{"id": "p1", "name": "Дом", "groupId": "g1"}],
        ungroup_noop=True)
    _wire_delete(monkeypatch, fake)

    out = await s._delete_project_group_impl("Личное", "g1")

    assert out.startswith("✅"), out
    assert "ОСТАЛИСЬ со ссылкой на удалённую папку" in out, out
    assert "«Дом»" in out
    assert "просто без папки" not in out and "теперь без папки" not in out, out


async def test_delete_group_empty_folder_needs_no_ungrouping(monkeypatch):
    """Пустая папка: разгруппировывать нечего — и set_projects_group не
    зовётся вовсе (лишний batch-запрос при живом rate-limit — сам дефект)."""
    fake = FakeV2GroupDelete(
        groups=[{"id": "g1", "name": "Личное"}],
        projects=[{"id": "p2", "name": "Чужой", "groupId": "g2"},
                  {"id": "p3", "name": "Вольный", "groupId": None}])
    _wire_delete(monkeypatch, fake)

    out = await s._delete_project_group_impl("Личное", "g1")

    assert out.startswith("✅") and "проверено" in out, out
    assert "разгруппировывать нечего" in out, out
    assert not any(c[0] == "set_group" for c in fake.calls), fake.calls


async def test_delete_group_counts_the_literal_NONE_marker_as_ungrouped(monkeypatch):
    """Проект с groupId="NONE" (так прод помечает «без папки») — НЕ сирота
    удаляемой группы и разгруппировке не подлежит."""
    fake = FakeV2GroupDelete(
        groups=[{"id": "g1", "name": "Личное"}],
        projects=[{"id": "p1", "name": "Вольный", "groupId": "NONE"}])
    _wire_delete(monkeypatch, fake)

    out = await s._delete_project_group_impl("Личное", "g1")

    assert "разгруппировывать нечего" in out, out
    assert not any(c[0] == "set_group" for c in fake.calls)


async def test_delete_group_full_gate_cycle_reports_the_ungrouped_projects(monkeypatch):
    """Сквозной прогон через гейт (план → «да» → исполнение): итоговый отчёт
    несёт и удаление группы, и фактическую разгруппировку."""
    fake = FakeV2GroupDelete(
        groups=[{"id": "g1", "name": "Личное"}],
        projects=[{"id": "p1", "name": "Дом", "groupId": "g1"}])
    _wire_delete(monkeypatch, fake)

    out = await _gated_call(s.delete_project_group, "Личное", "g1")

    assert out.startswith("✅"), out
    assert "«Дом»" in out and "теперь без папки (проверено)" in out, out
    assert fake.projects[0]["groupId"] == "NONE"


# ===========================================================================
# №2 — общий хелпер set_projects_group (батч-контракт; сквозная
# разгруппировка одного проекта — в test_silent_failures.py, #100)
# ===========================================================================

def test_set_projects_group_sends_one_batch_with_the_NONE_sentinel(monkeypatch):
    c = TickTickV2Client(token="t")
    monkeypatch.setattr(c, "get_state", lambda force=False: {})
    monkeypatch.setattr(c, "list_projects", lambda: [
        {"id": "p1", "name": "A", "groupId": "g1", "color": "#111111"},
        {"id": "p2", "name": "B", "groupId": "g1"}])
    posted = []
    c._request = lambda m, p, **kw: (posted.append((m, p, kw.get("json"))), {})[1]

    c.set_projects_group(["p1", "p2"], "NONE")

    assert len(posted) == 1, "разгруппировка N проектов обязана быть ОДНИМ запросом"
    method, path, body = posted[0]
    assert (method, path) == ("POST", "/batch/project")
    upds = body["update"]
    assert [u["id"] for u in upds] == ["p1", "p2"]
    assert all(u["groupId"] == "NONE" for u in upds), (
        "ушло не 'NONE' — прод такой запрос молча игнорирует")
    # полный живой объект, а не огрызок: другие поля не потеряны
    assert upds[0]["color"] == "#111111"


def test_set_projects_group_reports_per_item_failures(monkeypatch):
    c = TickTickV2Client(token="t")
    monkeypatch.setattr(c, "get_state", lambda force=False: {})
    monkeypatch.setattr(c, "list_projects", lambda: [
        {"id": "p1", "name": "A", "groupId": "g1"},
        {"id": "p2", "name": "B", "groupId": "g1"}])
    c._request = lambda m, p, **kw: {"id2error": {"p2": "boom"}}

    with pytest.raises(RuntimeError) as ei:
        c.set_projects_group(["p1", "p2"], "NONE")
    assert "p2" in str(ei.value) and "boom" in str(ei.value)


def test_move_project_to_group_is_a_thin_wrapper_over_the_shared_helper(monkeypatch):
    """Фикс №2 и фикс №1 обязаны идти через ОДНУ логику — иначе следующая
    правка семантики разгруппировки снова разъедется на два куска."""
    c = TickTickV2Client(token="t")
    seen = {}

    def fake_set(ids, gid):
        seen["args"] = (list(ids), gid)
        return {"ok": True}
    monkeypatch.setattr(c, "set_projects_group", fake_set)

    resp = c.move_project_to_group("p1", "NONE")

    assert seen["args"] == (["p1"], "NONE")
    assert resp == {"ok": True}


# ===========================================================================
# №3 — человекочитаемые HTTP-ошибки + валидация color/name до API
# ===========================================================================

def test_humanize_500_names_the_problem_in_russian():
    raw = "500 Server Error:  for url: https://api.ticktick.com/open/v1/project"
    out = s._humanize_api_error(raw)
    assert "временно недоступен" in out or "отклонил запрос" in out
    assert "500" in out, "исходный код ошибки потерян — диагностика ослепла"


def test_humanize_429_says_too_many_requests():
    raw = "429 Client Error:  for url: https://api.ticktick.com/api/v2/batch/check/0"
    out = s._humanize_api_error(raw)
    assert "слишком много запросов" in out and "повтори" in out
    assert "429" in out


def test_humanize_auth_codes_and_passthrough():
    assert "авторизаци" in s._humanize_api_error("401 Client Error:  for url: x")
    # не-HTTP текст проходит без изменений (расшифровка, не переписывание)
    assert s._humanize_api_error("просто текст") == "просто текст"
    # 404 намеренно не трогаем — по нему выше по стеку уже есть точные ответы
    raw404 = "404 Client Error:  for url: x"
    assert s._humanize_api_error(raw404) == raw404


def test_tool_error_funnel_translates_http_codes():
    """Дословный QA-случай: «Error deleting project group: 429 …» теперь
    несёт русское объяснение."""
    out = s._tool_error(
        "deleting project group",
        RuntimeError("429 Client Error:  for url: "
                     "https://api.ticktick.com/api/v2/batch/check/0"))
    assert out.startswith("Error deleting project group:")
    assert "слишком много запросов" in out


class TrapOfficial:
    """Официальный клиент-ловушка: любое обращение к API фиксируется —
    валидация обязана отказать ДО единого сетевого вызова. Сигнатуры — явные,
    как у настоящего клиента (см. tests/test_doubles_do_not_cheat.py)."""

    def __init__(self):
        self.calls = []

    def create_project(self, name, color="#F18181", view_mode="list"):
        self.calls.append("create_project")
        return {"id": "p1", "name": "X"}

    def update_project(self, project_id, name=None, color=None, view_mode=None):
        self.calls.append("update_project")
        return {}

    def get_project(self, project_id):
        self.calls.append("get_project")
        return {"id": "p1"}


async def test_create_project_rejects_empty_name_before_api(monkeypatch):
    trap = TrapOfficial()
    monkeypatch.setattr(s, "ticktick", trap)
    for bad in ("", "   "):
        out = await s.create_project(bad)
        assert out.startswith("🛑"), out
        assert "Пустое имя" in out
    assert trap.calls == []


async def test_create_project_rejects_non_hex_color_before_api(monkeypatch):
    """QA-репро: color «красный» и «#ZZZZZZ» раньше доезжали до API и
    возвращались голым «500 Server Error» — теперь понятный отказ ДО сети."""
    trap = TrapOfficial()
    monkeypatch.setattr(s, "ticktick", trap)
    for bad in ("красный", "#ZZZZZZ", "F18181", "#12"):
        out = await s.create_project("Работа", color=bad)
        assert out.startswith("🛑"), f"{bad!r}: {out}"
        assert "hex" in out and "#RRGGBB" in out, out
    assert trap.calls == []


async def test_create_project_valid_color_still_reaches_the_plan(monkeypatch):
    trap = TrapOfficial()
    monkeypatch.setattr(s, "ticktick", trap)
    preview = await s.create_project("Работа", color="#F18181")
    assert "🛑" not in preview and "manifest_id" in preview
    assert trap.calls == [], "план не должен ничего создавать"


async def test_update_project_rejects_non_hex_color_before_api(monkeypatch):
    trap = TrapOfficial()
    monkeypatch.setattr(s, "ticktick", trap)
    out = await s.update_project("Работа", "p1", color="красный")
    assert out.startswith("🛑"), out
    assert "hex" in out
    assert trap.calls == []


def test_color_refusal_accepts_the_hex_family_only():
    assert s._color_refusal(None) is None
    assert s._color_refusal("#F18181") is None
    assert s._color_refusal("#abc") is None
    for bad in ("", "red", "#ZZZZZZ", "F18181", "#12345", "#1234567"):
        refusal = s._color_refusal(bad)
        assert refusal and refusal.startswith("🛑"), f"{bad!r} прошёл"


# ===========================================================================
# №4 — create_project_group: пустое имя — внятный отказ до API
# ===========================================================================

class TrapV2Group:
    def __init__(self):
        self.calls = []

    def create_project_group(self, name):
        self.calls.append(("create_group", name))
        return "g-new"

    def get_state(self, force=False):
        return {}

    def list_project_groups(self):
        return [{"id": "g-new", "name": ""}]


async def test_create_project_group_rejects_empty_name_before_api(monkeypatch):
    """QA-репро: name="" раньше молча создавал безымянную группу («✅ Группа
    проектов «» создана»), при том что create_project такое имя отклоняет."""
    trap = TrapV2Group()
    monkeypatch.setattr(s, "ticktick_v2", trap)
    for bad in ("", "   "):
        out = await s.create_project_group(bad)
        assert out.startswith("🛑"), out
        assert "Пустое имя группы" in out
        assert "manifest_id" not in out, "план для пустого имени строиться не должен"
    assert trap.calls == []


async def test_create_project_group_valid_name_still_works(monkeypatch):
    trap = TrapV2Group()
    monkeypatch.setattr(s, "ticktick_v2", trap)
    preview = await s.create_project_group("Личное")
    assert "🛑" not in preview and "manifest_id" in preview
    assert trap.calls == []


# ===========================================================================
# №5 — create_project_column предупреждает про view_mode≠kanban В ОТВЕТЕ
# ===========================================================================

class FakeV2Column:
    def create_column(self, project_id, name):
        return "col1"


class FakeOfficialColumns:
    def __init__(self, view_mode=None):
        self._view_mode = view_mode

    def get_project_with_data(self, project_id):
        data = {"columns": [{"id": "col1", "name": "В работе"}]}
        if self._view_mode is not None:
            data["project"] = {"id": project_id, "viewMode": self._view_mode}
        return data


def _wire_column(monkeypatch, view_mode):
    monkeypatch.setattr(s, "ticktick_v2", FakeV2Column())
    monkeypatch.setattr(s, "ticktick", FakeOfficialColumns(view_mode))
    monkeypatch.setattr(s, "_guard_project", lambda *a, **k: None)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Работа"})


async def test_column_in_a_list_project_warns_the_human_in_the_reply(monkeypatch):
    """Знание «колонки видны только в kanban» жило в докстринге, который
    человек не видит. Теперь оно в самом ответе — вместе с тем, как
    переключить вид."""
    _wire_column(monkeypatch, view_mode="list")

    out = await _gated_call(s.create_project_column, "p1", "В работе",
                            project_name="Работа")

    assert out.startswith("### ✅"), out
    assert "⚠️" in out and "kanban" in out, f"предупреждения нет:\n{out}"
    assert "НЕ виден" in out, out
    assert 'view_mode="kanban"' in out, "не сказано, как переключить"


async def test_column_in_a_kanban_project_gets_no_warning(monkeypatch):
    _wire_column(monkeypatch, view_mode="kanban")

    out = await _gated_call(s.create_project_column, "p1", "В работе",
                            project_name="Работа")

    assert out.startswith("### ✅"), out
    assert "⚠️" not in out, f"ложное предупреждение в kanban-проекте:\n{out}"


async def test_column_warning_stays_silent_when_view_mode_is_unknown(monkeypatch):
    """Вид проекта неизвестен (в ответе нет блока project) — не пугаем
    предупреждением на догадке."""
    _wire_column(monkeypatch, view_mode=None)

    out = await _gated_call(s.create_project_column, "p1", "В работе",
                            project_name="Работа")

    assert out.startswith("### ✅"), out
    assert "kanban-виде" not in out
