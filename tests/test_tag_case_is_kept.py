"""Регистр имени тега: set_task_tags обязан сохранять его так же, как create_tag.

Дефект (живая приёмка 2026-08-07). TickTick держит у тега ДВА поля: `name` —
ключ, всегда в нижнем регистре, по нему тег ищется и сопоставляется, и
`label` — то, что человек видит в списке тегов и в приложении. Настоящий
`TickTickV2Client.create_tag` это уважает: `name=name.lower()`, `label=name`
как передали.

А `set_task_tags`, когда сам регистрирует ещё не существующий тег, отдавал в
create_tag УЖЕ пониженную строку (`x.lstrip("#").lower()` — нормализация,
нужная для записи на задачу, утекала и в регистрацию). Итог: «Работа»,
поставленная через set_task_tags, появлялась в аккаунте как «работа», а та
же «Работа» через create_tag — как «Работа». Один и тот же тег, два разных
написания, и какое достанется — зависит от того, каким тулом его завели
первым.

Проверяется через `tests/read_stand.py` — настоящие клиенты, подменён только
HTTP: регистрация реально уходит в /batch/tag боевым `create_tag`, тег
реально оседает в снимке, и читается он обратно публичным `list_tags`. Ни
`_set_task_tags_impl` (где сидел дефект), ни `create_tag` клиента не
подменяются — иначе тест проверял бы двойника.

Календарных привязок нет вовсе: теги от часов не зависят.
"""
import re

import pytest

import ticktick_mcp.src.server as s
from tests.read_stand import P_HOME, TASK_TAGGED, call, wire


@pytest.fixture(autouse=True)
def stand(monkeypatch, tmp_path):
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    return wire(monkeypatch)


@pytest.fixture(autouse=True)
def _isolate_manifests():
    before, tombs = dict(s._MANIFESTS), dict(s._MANIFEST_TOMBSTONES)
    s._MANIFESTS.clear()
    s._MANIFEST_TOMBSTONES.clear()
    yield
    s._MANIFESTS.clear()
    s._MANIFESTS.update(before)
    s._MANIFEST_TOMBSTONES.clear()
    s._MANIFEST_TOMBSTONES.update(tombs)


def _mid(preview: str) -> str:
    m = re.search(r"Манифест `([0-9a-f]+)`", preview)
    assert m, f"в превью нет id манифеста:\n{preview}"
    return m.group(1)


async def _approve(tool: str, **args) -> str:
    """План → «да» → исполнение, тем же тулом, как того требует гейт."""
    preview = await call(tool, **args)
    return await call(tool, **{**args, "manifest_id": _mid(preview),
                               "user_reply": "да, давай"})


# ─────────── эталон: create_tag ───────────

async def test_create_tag_keeps_the_case_the_user_typed():
    """Опорная точка, с которой сравнивается второй путь: этот тул регистр
    сохраняет — так тег и выглядит в приложении."""
    await _approve("create_tag", name="Работа")

    assert "Работа" in await call("list_tags")


# ─────────── тот же тег, но заведённый постановкой на задачу ───────────

async def test_set_task_tags_registers_a_new_tag_with_the_same_case():
    await _approve("set_task_tags", summary="Ставлю тег «Работа»",
                   tasks=[{"title": "Полить цветы", "taskId": TASK_TAGGED,
                           "tags": ["Работа"]}])

    tags = await call("list_tags")
    assert "Работа" in tags, (
        "тег, заведённый через set_task_tags, потерял регистр — в списке "
        f"тегов он выглядит иначе, чем его напечатал человек:\n{tags}")


async def test_both_paths_produce_the_same_single_tag():
    """Регистр — не косметика: «Работа» и «работа» это ОДИН тег (ключ у него
    общий), и рядом в списке они появиться не должны ни при каком порядке
    вызовов."""
    await _approve("create_tag", name="Работа")
    await _approve("set_task_tags", summary="Ставлю тег «Работа»",
                   tasks=[{"title": "Полить цветы", "taskId": TASK_TAGGED,
                           "tags": ["Работа"]}])

    tags = await call("list_tags")
    assert tags.count("Работа") == 1, f"тег задвоился написанием:\n{tags}"
    assert "- работа" not in tags, f"рядом появился двойник в нижнем регистре:\n{tags}"


# ─────────── связанный вопрос: поиск по тегу в другом регистре ───────────

async def test_lookup_by_tag_is_case_insensitive():
    """Записывается на задачу по-прежнему ключ (нижний регистр) — и поиск
    обязан находить задачу, как бы её ни спросили. Здесь это закреплено,
    чтобы «сохранение регистра» никто не понял как «писать на задачу как
    напечатано»: тогда «Работа» и «работа» разъехались бы в разные теги."""
    await _approve("set_task_tags", summary="Ставлю тег «Работа»",
                   tasks=[{"title": "Полить цветы", "taskId": TASK_TAGGED,
                           "tags": ["Работа"]}])

    for asked in ("Работа", "работа", "РАБОТА", "#Работа"):
        found = await call("get_tasks_by_tag", tag=asked)
        assert "Полить цветы" in found, f"поиск по «{asked}» не нашёл задачу:\n{found}"


async def test_the_tag_written_on_the_task_stays_the_lowercase_key(stand):
    """Прямое подтверждение того же на уровне снимка: на задаче лежит ключ, а
    не отображаемое написание."""
    v2, _v1, _transport = stand
    await _approve("set_task_tags", summary="Ставлю тег «Работа»",
                   tasks=[{"title": "Полить цветы", "taskId": TASK_TAGGED,
                           "tags": ["Работа"]}])

    task = next(t for t in v2.get_open_tasks() if t["id"] == TASK_TAGGED)
    assert task["tags"] == ["работа"], task["tags"]
    assert task["projectId"] == P_HOME
