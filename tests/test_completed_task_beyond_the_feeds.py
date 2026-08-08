"""Завершённая задача, которая СТАРШЕ лент завершённых и корзины: её знает
только ТОЧЕЧНОЕ чтение официального API.

Зачем отдельный файл. Фикс №1 (живая приёмка 2026-08-07) разделил две
функции, слитые в одну: `_official_task_read` — «как эта задача называется»
(любой статус) и `_official_task_snapshot` — «можно ли её трогать» (только
открытые, guard-политика). Соседний фикс из той же ветки добавил
`_live_task_title` третий шаг — ленты завершённых/корзины
(`find_task_any_state`), и на обычном стенде он ПЕРЕКРЫВАЕТ первый: задача
находится по ленте, даже если снять с точечного чтения фильтр открытости.
Мутационная проверка при слиянии (2026-08-07) это и показала — вернуть
фильтр внутрь `_official_task_read` можно было, не покраснев ни одним из
2318 тестов.

Перекрытие держится ровно до того места, где кончаются ленты. Они — СТРАНИЦЫ
с потолком (100 завершённых / 500 корзины, см. `find_task_any_state`), а не
индекс: задача, завершённая достаточно давно, не попадает ни в одну, и
единственный источник, который её знает, — точечный `GET
/project/{pid}/task/{tid}`. Здесь воспроизведён именно этот случай: ленты
ПУСТЫ, задача есть только в официальном API.

Что проверяется:
  * отображение (карточка выдачи ссылки на загрузку) называет её по имени —
    ради этого фикс и делался;
  * guard класса «операции над завершённой» (`_guard_task_incl_completed`)
    её находит и пропускает операцию;
  * защита от «не той задачи» при этом НЕ ослабла;
  * настоящая неизвестность (задачи нет нигде) по-прежнему произносится
    вслух.

Мутация-доказательство: вернуть `if t.get("status", 0) != 0: return None`
внутрь `_official_task_read` — первые два теста обязаны покраснеть.

Стенд `tests/read_stand.py`: настоящие клиенты, подменён только транспорт.
Ни `_live_task_title`, ни `_official_task_read`, ни guard не подменяются —
иначе тест проверял бы мок вместо фикса. Календарных привязок нет: ленты
пусты, а точечное чтение от дат не зависит.
"""
import pytest

import ticktick_mcp.src.server as s
from tests import read_stand as rs

NO_NAME_MARK = "НАЗВАНИЕ ЗАДАЧИ УСТАНОВИТЬ НЕ УДАЛОСЬ"
DONE_TITLE = "Купить бумагу"          # rs.COMPLETED[0], status=2, лежит в P_WORK
GHOST_TASK = "deadbeefdeadbeefdeadbeef"


@pytest.fixture(autouse=True)
def public_base(monkeypatch):
    """Сервер знает свой публичный адрес — иначе выдача ссылки отказывает ДО
    гейта, и тест проверял бы не то."""
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://tt.example.com")
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)


@pytest.fixture(autouse=True)
def isolate_manifests():
    before, tombs = dict(s._MANIFESTS), dict(s._MANIFEST_TOMBSTONES)
    s._MANIFESTS.clear()
    s._MANIFEST_TOMBSTONES.clear()
    yield
    s._MANIFESTS.clear()
    s._MANIFESTS.update(before)
    s._MANIFEST_TOMBSTONES.clear()
    s._MANIFEST_TOMBSTONES.update(tombs)


@pytest.fixture(autouse=True)
def stand(monkeypatch):
    """Задача ЗА пределами обеих лент: v2 отдаёт пустые ленты завершённых и
    корзины (как для задачи, вывалившейся за потолок 100/500), в снапшоте
    открытых её нет, и знает её ТОЛЬКО точечное чтение официального API."""
    return rs.wire(monkeypatch,
                   v2_kwargs={"completed": [], "trash": []},
                   v1_tasks=list(rs.TASKS) + list(rs.COMPLETED))


async def test_card_names_a_task_older_than_the_feeds():
    """Отображение: имя берётся точечным чтением, без оглядки на статус.

    С фильтром открытости внутри `_official_task_read` источников не
    остаётся вовсе — и карточка, ВРУЧАЮЩАЯ право записи в аккаунт, снова
    показывает голый id."""
    card = await rs.call("create_attachment_upload_url",
                         task_id=rs.TASK_COMPLETED, project_id=rs.P_WORK,
                         filename="чек.pdf")

    assert DONE_TITLE in card, card
    assert NO_NAME_MARK not in card, card


async def test_guard_finds_a_task_older_than_the_feeds():
    """Guard класса «операции над завершённой»: `_closed_task_snapshot` при
    известном project_id начинается с того же точечного чтения. Ленты пусты,
    полный скан аккаунта не запускается (project_id передан) — значит
    операция проходит РОВНО потому, что у точечного чтения нет фильтра
    открытости."""
    plan = await rs.call("add_task_comment", task_id=rs.TASK_COMPLETED,
                         task_title=DONE_TITLE, project_id=rs.P_WORK,
                         text="готово, чек приложен")

    assert "🛑" not in plan, plan
    assert "manifest_id" in plan, plan
    assert "завершена" in plan.lower(), (
        f"подтверждающему не сказали, что задача завершена:\n{plan}")


async def test_wrong_title_still_refused_beyond_the_feeds():
    """Снятый фильтр открытости — не поблажка: имя сверяется с живой записью,
    и подлог ловится так же, как у открытой задачи."""
    plan = await rs.call("add_task_comment", task_id=rs.TASK_COMPLETED,
                         task_title="Совсем другая задача", project_id=rs.P_WORK,
                         text="правка")

    assert "🛑" in plan, plan
    assert "manifest_id" not in plan, plan


async def test_a_task_no_source_knows_is_still_refused():
    """Контроль честности: когда задачи нет НИ В ОДНОМ источнике, ответ
    обязан остаться отказом, а не превратиться в «нашли что-нибудь»."""
    plan = await rs.call("add_task_comment", task_id=GHOST_TASK,
                         task_title="Призрак", project_id=rs.P_WORK,
                         text="привет")

    assert "🛑" in plan, plan
    assert "manifest_id" not in plan, plan


async def test_a_task_no_source_knows_has_no_name_to_show():
    """И та же неизвестность на отображающем пути: «не знаю» произносится
    вслух, а не заминается голым id."""
    card = await rs.call("create_attachment_upload_url",
                         task_id=GHOST_TASK, project_id=rs.P_WORK,
                         filename="чек.pdf")

    assert NO_NAME_MARK in card, card
