"""Наблюдаемость папок (project groups) в читающих инструментах.

Дыра, которую закрывают эти тесты: проект можно было переместить в папку
(move_project_to_group), но НИ ОДИН read-инструмент не показывал, в какой
папке проект лежит — format_project() печатал Name/Color/View Mode/Closed/
Kind/id и молча выбрасывал `groupId`, хотя поле приходит от обоих клиентов
(официальный v1 GET /project и v2 projectProfiles) и сервер сам им
пользуется в post-verify перемещения. list_project_groups() при этом
печатал только имя и id папки — состав и размер были не видны, пустая
папка не отличалась от заполненной.

Сети здесь нет: оба клиента подменяются фейками.
"""
import ticktick_mcp.src.server as s


class FakeV2Groups:
    """Минимальный стенд-ин под TickTickV2Client: только то, что трогают
    _v2_group_names() и list_project_groups(). Считает обращения к состоянию,
    чтобы проверить «состав папок не стоит лишнего сетевого вызова»."""

    def __init__(self, groups=None, projects=None, state_error=None,
                 projects_error=None):
        self._groups = groups or []
        self._projects = projects or []
        self._state_error = state_error
        self._projects_error = projects_error
        self.state_calls = 0

    def get_state(self, force=False):
        self.state_calls += 1
        if self._state_error:
            raise RuntimeError(self._state_error)
        return {"projectGroups": list(self._groups),
                "projectProfiles": list(self._projects)}

    def list_project_groups(self):
        return self.get_state().get("projectGroups", [])

    def list_projects(self):
        if self._projects_error:
            raise RuntimeError(self._projects_error)
        return self.get_state().get("projectProfiles", [])


# ---------------------------------------------------------------------------
# format_project: строка Folder во всех четырёх состояниях
# ---------------------------------------------------------------------------

def test_format_project_shows_folder_name_and_id():
    project = {"id": "p1", "name": "Работа", "groupId": "g1"}
    out = s.format_project(project, {"g1": "Личное"})
    assert "Folder: Личное (id: g1)" in out
    # id проекта по-прежнему последней строкой — старый контракт не сломан
    assert out.rstrip().endswith("(id: p1)")


def test_format_project_without_folder_says_none_explicitly():
    out = s.format_project({"id": "p1", "name": "Работа"}, {"g1": "Личное"})
    assert "Folder: (none)" in out
    # «поля нет» и «папки нет» обязаны различаться: строка есть всегда
    assert out.count("Folder:") == 1


def test_format_project_treats_literal_NONE_as_no_folder():
    # "NONE" — то, чем move_project_to_group разгруппировывает проект;
    # сервис может вернуть эту строку обратно вместо null.
    out = s.format_project({"id": "p1", "name": "Работа", "groupId": "NONE"},
                           {"g1": "Личное"})
    assert "Folder: (none)" in out


def test_format_project_unknown_group_id_is_flagged_not_silently_dropped():
    out = s.format_project({"id": "p1", "name": "Работа", "groupId": "gZZZ"},
                           {"g1": "Личное"})
    assert "Folder: (unknown group) (id: gZZZ)" in out


def test_format_project_folder_name_unavailable_differs_from_no_folder(monkeypatch):
    """v2 недоступен → имя папки узнать НЕЧЕМ (в официальном API списка
    папок нет). Это не «без папки»: id обязан быть виден."""
    monkeypatch.setattr(s, "ticktick_v2", None)
    out = s.format_project({"id": "p1", "name": "Работа", "groupId": "g1"})
    assert "Folder: (name unavailable) (id: g1)" in out
    assert "(none)" not in out


def test_format_project_resolves_folder_itself_when_map_not_passed(monkeypatch):
    monkeypatch.setattr(s, "ticktick_v2",
                        FakeV2Groups(groups=[{"id": "g1", "name": "Личное"}]))
    out = s.format_project({"id": "p1", "name": "Работа", "groupId": "g1"})
    assert "Folder: Личное (id: g1)" in out


# ---------------------------------------------------------------------------
# _v2_group_names
# ---------------------------------------------------------------------------

def test_v2_group_names_drops_deleted_groups(monkeypatch):
    monkeypatch.setattr(s, "ticktick_v2", FakeV2Groups(groups=[
        {"id": "g1", "name": "Личное"},
        {"id": "g2", "name": "Старое", "deleted": 1},
    ]))
    assert s._v2_group_names() == {"g1": "Личное"}


def test_v2_group_names_returns_none_when_state_unreadable(monkeypatch):
    """None («не смог проверить») обязано отличаться от {} («папок нет») —
    иначе проект в реальной папке напечатался бы как «unknown group»."""
    monkeypatch.setattr(s, "ticktick_v2", FakeV2Groups(state_error="v2 down"))
    assert s._v2_group_names() is None
    monkeypatch.setattr(s, "ticktick_v2", FakeV2Groups(groups=[]))
    assert s._v2_group_names() == {}


# ---------------------------------------------------------------------------
# get_projects / get_project — сквозь инструменты
# ---------------------------------------------------------------------------

class FakeOfficialProjects:
    def __init__(self, projects):
        self._projects = projects

    def get_projects(self):
        return [dict(p) for p in self._projects]

    def get_project(self, project_id):
        return next((dict(p) for p in self._projects if p["id"] == project_id),
                    {"error": "not found"})


async def test_get_projects_lists_folder_per_project(monkeypatch):
    monkeypatch.setattr(s, "ticktick", FakeOfficialProjects([
        {"id": "p1", "name": "Работа", "groupId": "g1"},
        {"id": "p2", "name": "Инбокс"},
    ]))
    fake_v2 = FakeV2Groups(groups=[{"id": "g1", "name": "Личное"}])
    monkeypatch.setattr(s, "ticktick_v2", fake_v2)

    out = await s.get_projects()

    assert "Folder: Личное (id: g1)" in out
    assert "Folder: (none)" in out
    # карта папок резолвится ОДИН раз на весь список, а не на каждый проект
    assert fake_v2.state_calls == 1


async def test_get_project_shows_folder(monkeypatch):
    monkeypatch.setattr(s, "ticktick", FakeOfficialProjects([
        {"id": "p1", "name": "Работа", "groupId": "g1"},
    ]))
    monkeypatch.setattr(s, "ticktick_v2",
                        FakeV2Groups(groups=[{"id": "g1", "name": "Личное"}]))

    out = await s.get_project("p1")

    assert "Folder: Личное (id: g1)" in out


# ---------------------------------------------------------------------------
# list_project_groups — состав и размер папок
# ---------------------------------------------------------------------------

def _wire_groups(monkeypatch, fake_v2):
    monkeypatch.setattr(s, "ticktick_v2", fake_v2)
    monkeypatch.setattr(s, "ticktick", object())  # _ensure_ready: клиент есть


async def test_list_project_groups_shows_members_and_empty_folders(monkeypatch):
    fake = FakeV2Groups(
        groups=[{"id": "g1", "name": "Личное"}, {"id": "g2", "name": "Работа"}],
        projects=[
            {"id": "p1", "name": "Дом", "groupId": "g1"},
            {"id": "p2", "name": "Финансы", "groupId": "g1"},
            {"id": "p3", "name": "Инбокс"},
            {"id": "p4", "name": "Удалённый", "groupId": "g1", "deleted": 1},
        ])
    _wire_groups(monkeypatch, fake)

    out = await s.list_project_groups()

    assert "- Личное  (id: g1) — 2 projects: Дом, Финансы" in out
    assert "- Работа  (id: g2) — empty" in out
    assert "1 project(s) in no folder." in out
    assert "Удалённый" not in out


async def test_list_project_groups_flags_orphaned_projects(monkeypatch):
    fake = FakeV2Groups(
        groups=[{"id": "g1", "name": "Личное"}],
        projects=[{"id": "p1", "name": "Осиротевший", "groupId": "gGONE"}])
    _wire_groups(monkeypatch, fake)

    out = await s.list_project_groups()

    assert "- Личное  (id: g1) — empty" in out
    assert "1 project(s) point at a folder that no longer exists." in out


async def test_list_project_groups_membership_costs_no_extra_fetch(monkeypatch):
    """Состав берётся из того же кэшированного снапшота — если бы он стоил
    отдельного запроса, правку не стоило бы делать."""
    fake = FakeV2Groups(groups=[{"id": "g1", "name": "Личное"}],
                        projects=[{"id": "p1", "name": "Дом", "groupId": "g1"}])
    _wire_groups(monkeypatch, fake)

    await s.list_project_groups()

    # два обращения к клиенту (группы + проекты), оба через один и тот же
    # кэш get_state() — реальный клиент делает по нему максимум один HTTP
    assert fake.state_calls == 2


async def test_list_project_groups_degrades_when_membership_unreadable(monkeypatch):
    """Наблюдаемость не должна ломать базовую функцию: если список проектов
    не читается, папки всё равно печатаются (без состава)."""
    fake = FakeV2Groups(groups=[{"id": "g1", "name": "Личное"}],
                        projects_error="boom")
    _wire_groups(monkeypatch, fake)

    out = await s.list_project_groups()

    assert "- Личное  (id: g1)" in out
    assert "in no folder" not in out
    assert "Error fetching project groups" not in out


async def test_list_project_groups_caps_long_member_lists(monkeypatch):
    projects = [{"id": f"p{i}", "name": f"Проект{i}", "groupId": "g1"}
                for i in range(s._GROUP_MEMBERS_CAP + 3)]
    fake = FakeV2Groups(groups=[{"id": "g1", "name": "Личное"}],
                        projects=projects)
    _wire_groups(monkeypatch, fake)

    out = await s.list_project_groups()

    assert f"— {s._GROUP_MEMBERS_CAP + 3} projects:" in out
    assert "+3 more" in out
