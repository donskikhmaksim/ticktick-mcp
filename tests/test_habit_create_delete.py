"""Привычки стало можно ЗАВОДИТЬ и УДАЛЯТЬ (2026-08-06).

До этого клиент умел привычки только читать и отмечать: считалось, что
создания/удаления в API «нет». Живая проверка показала обратное — это тот же
батч-эндпоинт, что у задач/тегов/проектов:

    POST /api/v2/habits/batch   {"add": [habit], "update": [], "delete": [id]}

add   → HTTP 200, {"id2etag":{"<id>":"…"},"id2error":{}}, GET /habits 11→12;
delete→ HTTP 200, {"id2etag":{},"id2error":{}},           GET /habits 12→11.

Здесь закрепляется поведение ОБОИХ новых тулов, без сети (клиент v2 подменён
фейком в духе tests/test_slice9_habits.py::FakeHabitsV2):

  • гейт «план → исполнение»: вызов #1 ничего не мутирует, вызов #2 без
    подтверждения отказывает, манифест одноразовый, «нет» его гасит;
  • identity-guard удаления: id, указывающий на ДРУГУЮ привычку, — отказ,
    и ни одного удаления;
  • post-verify: успех объявляется только после ОТДЕЛЬНОГО свежего чтения
    списка привычек, а расхождение/пропажа дают ⚠️/❌, а не ✅;
  • защита от близнеца: одноимённая привычка второй раз не заводится.

def-116 (2026-08-07) — ВАЖНАЯ ПРАВКА ПОВЕДЕНИЯ, не просто новый тест.
До этой правки identity-guard delete_habit срабатывал ТОЛЬКО на исполнении
(call #2/_delete_habit_impl) — фаза ПЛАНА (call #1) печатала карточку с
именем ИЗ ЗАПРОСА, не сверяя его с habit_id вообще. Три теста ниже раньше
явно ЗАКРЕПЛЯЛИ это как ожидаемое поведение (все использовали общий
хелпер `_delete()`, который требует, чтобы call #1 НЕ отказывал):
  • test_delete_habit_identity_guard_blocks_wrong_name — раньше ждал, что
    `_delete(habit_name="Медитация", habit_id="h2")` (чужой id) пройдёт
    ЧЕРЕЗ фазу плана без единого 🛑 и откажет только на call #2;
  • test_delete_habit_unknown_id_is_refused — тот же паттерн для
    несуществующего id;
  • test_call1_previews_and_mutates_nothing[delete_habit] — явно
    утверждал `fake.get_habits_calls == 0` с комментарием «фаза плана в
    TickTick не ходит вовсе» — то есть план-фаза programmatically не имела
    ПРАВА проверить состояние, даже если бы захотела.
Тесты были зелёными НА БАГЕ: они не защищали от регрессии в эту сторону,
а фиксировали её как норму — найдено не аудитом кода, а живым прогоном
(Максим подал `habit_name` реальной привычки + `habit_id` другой,
получил карточку с чужим именем). Три теста ниже переписаны под новое,
ПРАВИЛЬНОЕ поведение (отказ уже на call #1); история — в def-116
коммите.
"""
import re

import pytest

import ticktick_mcp.src.server as s


def _extract_manifest_id(preview: str) -> str:
    m = re.search(r'manifest_id="([0-9a-f]+)"', preview)
    assert m, f"no manifest_id found in preview: {preview!r}"
    return m.group(1)


class FakeHabitsV2:
    """Заглушка v2-клиента: только то, чего касаются create_habit/delete_habit.

    write_effect управляет тем, что произойдёт на записи:
      "normal"   — создаёт/удаляет по-настоящему (post-verify должен пройти);
      "silent"   — вызов «успешен», но состояние не меняется (post-verify
                   обязан этого НЕ подтвердить);
      "renamed"  — создаёт привычку с ДРУГИМ именем (post-verify обязан
                   поймать расхождение);
      "rejected" — TickTick отклоняет элемент в id2error.
    raise_on_get — номер вызова get_habits (1-based), который упадёт с
    ошибкой: так отделяется предварительное чтение от пост-проверки.
    """

    def __init__(self, habits=None, write_effect="normal", raise_on_get=None,
                 checkins=None, raise_on_checkins=False):
        self._habits = [dict(h) for h in (habits or [])]
        self._write_effect = write_effect
        self._raise_on_get = raise_on_get
        self._checkins = checkins
        self._raise_on_checkins = raise_on_checkins
        self.get_habits_calls = 0
        self.get_habit_checkins_calls = 0
        self.create_calls = []
        self.delete_calls = []

    # --- чтение ---
    def get_habits(self):
        self.get_habits_calls += 1
        if self._raise_on_get == self.get_habits_calls:
            raise RuntimeError("network blip")
        return [dict(h) for h in self._habits]

    def get_habit_checkins(self, habit_ids, after_stamp):
        # Реальный счёт «успешных отметок», который delete_habit теперь читает
        # ЖИВЬЁМ (QA-раунд 2, 2026-08-19) вместо устаревшего totalCheckIns.
        # По умолчанию (без явного self._checkins) синтезируем ровно
        # totalCheckIns записей status=2 на привычку — так существующие тесты
        # этого файла (например «**7**» для «Медитация») продолжают работать
        # без изменений, а тесты, которым нужно ДОКАЗАТЬ расхождение с
        # totalCheckIns, передают checkins= явно.
        self.get_habit_checkins_calls += 1
        self.last_after_stamp = after_stamp
        if self._raise_on_checkins:
            raise RuntimeError("network blip")
        if self._checkins is not None:
            return {hid: [e for e in self._checkins.get(hid, [])
                          if e.get("checkinStamp", 0) > after_stamp]
                    for hid in habit_ids}
        out = {}
        for hid in habit_ids:
            habit = next((h for h in self._habits if h.get("id") == hid), None)
            n = int((habit or {}).get("totalCheckIns", 0))
            out[hid] = [{"checkinStamp": 20260101 + i, "status": 2,
                        "value": 1.0, "goal": 1.0} for i in range(n)
                        if 20260101 + i > after_stamp]
        return out

    # --- запись ---
    # Сигнатура — ДОСЛОВНАЯ копия TickTickV2Client.create_habit, а не
    # `**kwargs`. Разница принципиальная: `**kwargs` проглатывает что угодно,
    # поэтому двойник не может заметить ни опечатку в имени параметра, ни
    # пропущенное поле — а живой клиент на опечатке упал бы с TypeError, и
    # пропущенное поле уехало бы в TickTick значением по умолчанию.
    def create_habit(self, name, *, goal=1.0, step=0.0, unit="Count",
                     habit_type="Boolean",
                     repeat_rule="RRULE:FREQ=DAILY;INTERVAL=1",
                     section="morning", color="#97E38B",
                     icon="habit_daily_check_in", encouragement=""):
        self.create_calls.append({
            "name": name, "goal": goal, "step": step, "unit": unit,
            "habit_type": habit_type, "repeat_rule": repeat_rule,
            "section": section, "color": color, "icon": icon,
            "encouragement": encouragement})
        if self._write_effect == "rejected":
            raise RuntimeError("habit limit reached")
        hid = f"new{len(self.create_calls)}"
        if self._write_effect == "silent":
            return hid
        stored = name if self._write_effect != "renamed" else name + " (копия)"
        self._habits.append({
            "id": hid, "name": stored, "goal": goal, "unit": unit,
            "type": habit_type, "repeatRule": repeat_rule, "totalCheckIns": 0,
        })
        return hid

    def delete_habit(self, habit_id):
        self.delete_calls.append(habit_id)
        if self._write_effect == "rejected":
            return {"id2etag": {}, "id2error": {habit_id: "not allowed"}}
        if self._write_effect != "silent":
            self._habits = [h for h in self._habits if h.get("id") != habit_id]
        return {"id2etag": {}, "id2error": {}}


HABITS = [
    {"id": "h1", "name": "Медитация", "goal": 1.0, "unit": "Count",
     "type": "Boolean", "totalCheckIns": 7},
    {"id": "h2", "name": "Бег", "goal": 3.0, "unit": "km",
     "type": "Real", "totalCheckIns": 2},
]


def _wire(monkeypatch, fake):
    monkeypatch.setattr(s, "ticktick_v2", fake)
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)


@pytest.fixture(autouse=True)
def _isolate_manifests():
    before = dict(s._MANIFESTS)
    s._MANIFESTS.clear()
    yield
    s._MANIFESTS.clear()
    s._MANIFESTS.update(before)


@pytest.fixture(autouse=True)
def _journal_to_tmp(monkeypatch, tmp_path):
    """delete_habit пишет снимок в журнал мутаций — в тестах он идёт в tmp,
    а не в рабочий каталог."""
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))


async def _create(**kwargs):
    """Полный цикл плана и исполнения create_habit; отдаёт результат #2."""
    preview = await s.create_habit(**kwargs)
    assert "🛑" not in preview, f"фаза плана неожиданно отказала: {preview!r}"
    mid = _extract_manifest_id(preview)
    return await s.create_habit(**kwargs, manifest_id=mid, user_reply="да")


async def _delete(**kwargs):
    preview = await s.delete_habit(**kwargs)
    assert "🛑" not in preview, f"фаза плана неожиданно отказала: {preview!r}"
    mid = _extract_manifest_id(preview)
    return await s.delete_habit(**kwargs, manifest_id=mid, user_reply="да")


# ───────────────────────────── создание ─────────────────────────────

async def test_create_habit_success_is_post_verified(monkeypatch):
    fake = FakeHabitsV2(HABITS)
    _wire(monkeypatch, fake)

    result = await _create(name="Зарядка")

    assert result.startswith("### ✅ Привычка «Зарядка» создана")
    assert "🧾" in result
    assert len(fake.create_calls) == 1
    # одно чтение до записи (защита от близнеца) + ОТДЕЛЬНОЕ свежее после
    assert fake.get_habits_calls == 2


@pytest.mark.skip(reason="НЕ ПРОВЕРЕНО ЖИВЬЁМ: сервер не передаёт `step`, и "
                         "количественная привычка уезжает в TickTick с "
                         "step=0.0. Правильное значение и последствия нуля "
                         "(ломается ли шаг отметки в приложении) можно узнать "
                         "только запросом к настоящему TickTick — чинить "
                         "вслепую нельзя. Пропуск честнее зелёного теста.")
async def test_quantitative_habit_gets_a_usable_increment_step(monkeypatch):
    """Двойник больше не прячет этот пробел: с дословной сигнатурой видно, что
    `step` до клиента не доходит вовсе. Раньше `**kwargs` проглатывал факт."""
    fake = FakeHabitsV2(HABITS)
    _wire(monkeypatch, fake)

    await _create(name="Вода", goal=8, unit="ml", habit_type="quantitative")

    assert fake.create_calls[0]["step"] > 0


async def test_create_habit_passes_through_quantitative_params(monkeypatch):
    fake = FakeHabitsV2(HABITS)
    _wire(monkeypatch, fake)

    result = await _create(name="Вода", goal=8, unit="ml",
                           habit_type="quantitative", section="night")

    assert result.startswith("### ✅")
    call = fake.create_calls[0]
    assert call["habit_type"] == "Real"      # boolean|quantitative → API-тип
    assert call["goal"] == 8.0
    assert call["unit"] == "ml"
    assert call["section"] == "night"


async def test_create_habit_refuses_duplicate_name_without_writing(monkeypatch):
    fake = FakeHabitsV2(HABITS)
    _wire(monkeypatch, fake)

    result = await _create(name="  медитация ")   # тот же смысл, иной регистр

    assert result.startswith("### ↷")
    assert "уже есть" in result
    assert fake.create_calls == []


async def test_create_habit_silent_write_is_not_confirmed(monkeypatch):
    fake = FakeHabitsV2(HABITS, write_effect="silent")
    _wire(monkeypatch, fake)

    result = await _create(name="Зарядка")

    assert result.startswith("### ⚠️")
    assert "НЕ подтверждена" in result
    assert "### ✅" not in result


async def test_create_habit_name_mismatch_is_flagged(monkeypatch):
    fake = FakeHabitsV2(HABITS, write_effect="renamed")
    _wire(monkeypatch, fake)

    result = await _create(name="Зарядка")

    assert result.startswith("### ❌")
    assert "имя разошлось" in result


async def test_create_habit_api_rejection_is_reported(monkeypatch):
    fake = FakeHabitsV2(HABITS, write_effect="rejected")
    _wire(monkeypatch, fake)

    result = await _create(name="Зарядка")

    assert result.startswith("### ❌")
    assert "TickTick отклонил" in result


async def test_create_habit_postverify_read_error_is_unconfirmed(monkeypatch):
    # 1-е чтение — защита от близнеца (должно пройти), 2-е — пост-проверка
    fake = FakeHabitsV2(HABITS, raise_on_get=2)
    _wire(monkeypatch, fake)

    result = await _create(name="Зарядка")

    assert result.startswith("### ⚠️")
    assert "проверка не выполнена" in result
    assert len(fake.create_calls) == 1


@pytest.mark.parametrize("kwargs", [
    dict(name="   "),
    dict(name="Зарядка", habit_type="ежедневная"),
    dict(name="Зарядка", section="вечером"),
    dict(name="Зарядка", goal=0),
    dict(name="Зарядка", goal="много"),
])
async def test_create_habit_invalid_args_refused_before_the_gate(monkeypatch, kwargs):
    fake = FakeHabitsV2(HABITS)
    _wire(monkeypatch, fake)

    result = await s.create_habit(**kwargs)

    assert result.startswith("🛑")
    assert fake.create_calls == []
    assert fake.get_habits_calls == 0


# ───────────────────────────── удаление ─────────────────────────────

async def test_delete_habit_success_is_post_verified(monkeypatch):
    fake = FakeHabitsV2(HABITS)
    _wire(monkeypatch, fake)

    result = await _delete(habit_name="Медитация", habit_id="h1")

    assert result.startswith("### ✅ Привычка «Медитация» удалена")
    assert "**7**" in result            # радиус поражения: история отметок
    assert fake.delete_calls == ["h1"]
    # def-116: +1 к прежним двум (identity-guard на исполнении + отдельная
    # пост-проверка) — теперь ЕЩЁ одно чтение на этапе ПЛАНА (call #1).
    assert fake.get_habits_calls == 3


async def test_delete_habit_identity_guard_blocks_wrong_name(monkeypatch):
    """def-116 (2026-08-07): было — `_delete()` (план #1 → исполнение #2)
    отказывал только на #2, план #1 «Удаляю привычку «Медитация»…» уходил
    владельцу непроверенным (см. module docstring выше). Стало — отказ уже
    на #1, ДО того как карточка с (возможно) чужим именем вообще
    построена: план для чужого id не строится вовсе, ни одного вызова
    delete_calls."""
    fake = FakeHabitsV2(HABITS)
    _wire(monkeypatch, fake)

    # id чужой привычки под именем другой — ровно тот промах, ради которого
    # существует identity-guard
    result = await s.delete_habit(habit_name="Медитация", habit_id="h2")

    assert result.startswith("🛑")
    assert "«Бег»" in result
    assert "manifest_id" not in result, "план для несовпавшей пары строиться не должен"
    assert fake.delete_calls == []


async def test_delete_habit_unknown_id_is_refused(monkeypatch):
    """def-116: тот же перенос на call #1, что и в тесте выше — было:
    отказ только на исполнении; стало: план не строится вовсе."""
    fake = FakeHabitsV2(HABITS)
    _wire(monkeypatch, fake)

    result = await s.delete_habit(habit_name="Медитация", habit_id="h-нет-такой")

    assert result.startswith("🛑")
    assert "manifest_id" not in result
    assert fake.delete_calls == []


# ===========================================================================
# def-116 (2026-08-07): сверка habit_id↔habit_name перенесена с ИСПОЛНЕНИЯ
# (call #2, _delete_habit_impl — не тронуто, работает как раньше) на
# ПОСТРОЕНИЕ ПЛАНА (call #1) — по образцу _resolve_triage_ops/_names_agree
# в manual_triage. Живой пример: habit_name="Растяжка" (реальная привычка)
# + habit_id тестовой — карточка ушла бы с именем «Растяжка», хотя id вёл
# не туда.
# ===========================================================================

async def test_delete_habit_plan_read_failure_does_not_block_but_warns(
        monkeypatch):
    """Осторожность из ТЗ: временная недоступность live-чтения на этапе
    плана НЕ должна блокировать удаление наглухо (fail-open здесь дешевле,
    чем отказ ВСЕМ, у кого сейчас барахлит сеть) — но и не должна выдавать
    непроверенное имя за факт. План строится, честно предупреждает, а
    настоящая защита (call #2, identity guard на исполнении) остаётся
    активной и НЕ ослаблена этим путём."""
    fake = FakeHabitsV2(HABITS, raise_on_get=1)  # 1-е чтение (план) падает
    _wire(monkeypatch, fake)

    preview = await s.delete_habit(habit_name="Медитация", habit_id="h1")

    assert "🛑" not in preview, "временный сбой чтения не должен блокировать план"
    assert "НЕ удалось сверить" in preview
    mid = re.search(r'manifest_id="([0-9a-f]+)"', preview).group(1)

    # Исполнение (call #2) — 2-е и 3-е чтения (identity-guard + пост-
    # проверка), raise_on_get=1 их не касается: настоящая пара, удаление
    # проходит нормально.
    result = await s.delete_habit(habit_name="Медитация", habit_id="h1",
                                  manifest_id=mid, user_reply="да")
    assert result.startswith("### ✅ Привычка «Медитация» удалена")
    assert fake.delete_calls == ["h1"]


async def test_delete_habit_plan_read_failure_still_lets_execution_catch_a_real_mismatch(
        monkeypatch):
    """Тот же сбой чтения на плане, но пара В ДЕЙСТВИТЕЛЬНОСТИ не совпадает
    (habit_id="h2" = «Бег», а не «Медитация»): раз план-проверка не
    выполнилась, план строится с предупреждением, НО identity-guard на
    исполнении (не тронут этой правкой) обязан всё равно поймать
    расхождение — сеть, недоступная только на планировании, не должна
    ослаблять защиту при исполнении."""
    fake = FakeHabitsV2(HABITS, raise_on_get=1)
    _wire(monkeypatch, fake)

    preview = await s.delete_habit(habit_name="Медитация", habit_id="h2")
    assert "🛑" not in preview
    mid = re.search(r'manifest_id="([0-9a-f]+)"', preview).group(1)

    result = await s.delete_habit(habit_name="Медитация", habit_id="h2",
                                  manifest_id=mid, user_reply="да")
    assert result.startswith("🛑")
    assert "«Бег»" in result
    assert fake.delete_calls == []


async def test_delete_habit_automation_key_mismatch_is_refused_before_execution(
        monkeypatch):
    """def-116 специально для headless-пути: карточки с кнопками там нет
    ВООБЩЕ (см. docstring), поэтому если бы сверка стояла только внутри
    _gate_single/исполнения, ложная пара имя+id прошла бы молча по одному
    valid-ключу. Проверка стоит ДО _gate_single — значит применяется и
    здесь.

    ВАЖНО для мутационного тестирования: `_delete_habit_impl`'s СОБСТВЕННЫЙ
    identity-guard (не тронут этой правкой) тоже поймал бы это расхождение,
    если бы automation_key-путь дошёл до исполнения — поэтому одного
    `startswith("🛑")` мало, чтобы доказать, что именно ПЛАН-СТАДИЯ (не
    execution-стадия) отказала первой: обе ветки печатают разный текст
    («План НЕ построен» здесь vs «НЕ удалил» у _delete_habit_impl) и
    execution-путь вообще не должен быть достигнут (delete_calls пуст,
    ровно одно чтение — то самое, план-стадийное)."""
    fake = FakeHabitsV2(HABITS)
    _wire(monkeypatch, fake)
    monkeypatch.setattr(s, "SECRET", "test-secret")

    result = await s.delete_habit(habit_name="Медитация", habit_id="h2",
                                  automation_key="test-secret")

    assert result.startswith("🛑 План НЕ построен")
    assert "«Бег»" in result
    assert fake.delete_calls == []
    assert fake.get_habits_calls == 1
    assert fake.delete_calls == []


async def test_delete_habit_requires_both_name_and_id(monkeypatch):
    fake = FakeHabitsV2(HABITS)
    _wire(monkeypatch, fake)

    result = await s.delete_habit(habit_name="", habit_id="h1")

    assert result.startswith("🛑")
    assert fake.delete_calls == []
    assert fake.get_habits_calls == 0


async def test_delete_habit_silent_delete_is_not_confirmed(monkeypatch):
    fake = FakeHabitsV2(HABITS, write_effect="silent")
    _wire(monkeypatch, fake)

    result = await _delete(habit_name="Медитация", habit_id="h1")

    assert result.startswith("### ❌")
    assert "ВСЁ ЕЩЁ в списке" in result


async def test_delete_habit_api_rejection_is_reported(monkeypatch):
    fake = FakeHabitsV2(HABITS, write_effect="rejected")
    _wire(monkeypatch, fake)

    result = await _delete(habit_name="Медитация", habit_id="h1")

    assert result.startswith("### ❌")
    assert "TickTick отклонил" in result


async def test_delete_habit_reports_actual_checkins_not_stale_field(monkeypatch):
    """QA-раунд 2 (2026-08-19): delete_habit печатал «успешных было 3», хотя
    get_habit_checkins непосредственно перед этим показывал 4 done-записи —
    источником было устаревшее habit["totalCheckIns"]. Здесь totalCheckIns=3
    (как в живом баге), а живое чтение check-in'ов отдаёт 4 done-записи;
    отчёт обязан показать 4, а не 3."""
    habits = [dict(HABITS[0], totalCheckIns=3)]
    fake = FakeHabitsV2(habits, checkins={
        "h1": [{"checkinStamp": 20260101 + i, "status": 2, "value": 1.0,
                "goal": 1.0} for i in range(4)],
    })
    _wire(monkeypatch, fake)

    result = await _delete(habit_name="Медитация", habit_id="h1")

    assert result.startswith("### ✅ Привычка «Медитация» удалена")
    assert "**4**" in result
    assert "**3**" not in result


async def test_delete_habit_shows_unknown_count_when_checkins_read_fails(monkeypatch):
    """Пересчёт стоит лишнего запроса — если он падает, отчёт обязан
    показать «?», а НЕ откатиться на устаревшее totalCheckIns (и уж тем
    более не заблокировать подтверждённое необратимое удаление)."""
    fake = FakeHabitsV2(HABITS, raise_on_checkins=True)
    _wire(monkeypatch, fake)

    result = await _delete(habit_name="Медитация", habit_id="h1")

    assert result.startswith("### ✅ Привычка «Медитация» удалена")
    assert "**?**" in result
    assert fake.delete_calls == ["h1"], "неудачный подсчёт не должен блокировать удаление"


async def test_delete_habit_writes_presnapshot_to_journal(monkeypatch, tmp_path):
    fake = FakeHabitsV2(HABITS)
    _wire(monkeypatch, fake)

    await _delete(habit_name="Медитация", habit_id="h1")

    journal = tmp_path / "deletion_journal.jsonl"
    assert journal.exists(), "снимок перед необратимым удалением не записан"
    body = journal.read_text(encoding="utf-8")
    assert "delete_habit" in body and "Медитация" in body


async def test_delete_habit_says_so_when_the_journal_is_unwritable(monkeypatch):
    """Журнал недоступен (на проде это read-only том) — удаление всё равно
    исполняется, но ответ обязан сказать правду: снимка НЕТ. Обещание
    несуществующего снимка было бы ровно тем враньём в выводе, которое
    запрещено (references/output-format.md §7.4)."""
    fake = FakeHabitsV2(HABITS)
    _wire(monkeypatch, fake)
    monkeypatch.setattr(s, "_journal_write", lambda record: "")

    result = await _delete(habit_name="Медитация", habit_id="h1")

    assert result.startswith("### ✅")
    assert "снимок привычки записан" not in result
    assert "записать НЕ удалось" in result


# ────────────────────────────── гейт ──────────────────────────────

@pytest.mark.parametrize("tool,kwargs,expected_plan_reads", [
    ("create_habit", dict(name="Зарядка"), 0),
    # def-116: delete_habit's план-фаза ТЕПЕРЬ читает живой список привычек
    # ровно один раз (сверка habit_id↔habit_name ДО построения плана) — было
    # 0 («фаза плана в TickTick не ходит вовсе»), пока это и было багом
    # def-116 (см. module docstring). create_habit этой правкой не тронут.
    ("delete_habit", dict(habit_name="Медитация", habit_id="h1"), 1),
])
async def test_call1_previews_and_mutates_nothing(monkeypatch, tool, kwargs,
                                                   expected_plan_reads):
    fake = FakeHabitsV2(HABITS)
    _wire(monkeypatch, fake)

    preview = await getattr(s, tool)(**kwargs)

    assert "manifest_id" in preview
    assert fake.create_calls == [] and fake.delete_calls == []
    assert fake.get_habits_calls == expected_plan_reads


@pytest.mark.parametrize("tool,kwargs", [
    ("create_habit", dict(name="Зарядка")),
    ("delete_habit", dict(habit_name="Медитация", habit_id="h1")),
])
async def test_call2_without_reply_is_refused_and_retryable(monkeypatch, tool, kwargs):
    fake = FakeHabitsV2(HABITS)
    _wire(monkeypatch, fake)
    fn = getattr(s, tool)

    mid = _extract_manifest_id(await fn(**kwargs))
    refused = await fn(**kwargs, manifest_id=mid, user_reply="")
    assert "🛑" in refused
    assert fake.create_calls == [] and fake.delete_calls == []

    # пустой (не отрицательный) ответ манифест не сжигает — можно подтвердить
    ok = await fn(**kwargs, manifest_id=mid, user_reply="да, давай")
    assert "🛑" not in ok
    assert len(fake.create_calls) + len(fake.delete_calls) == 1


@pytest.mark.parametrize("tool,kwargs", [
    ("create_habit", dict(name="Зарядка")),
    ("delete_habit", dict(habit_name="Медитация", habit_id="h1")),
])
async def test_explicit_no_burns_the_manifest(monkeypatch, tool, kwargs):
    fake = FakeHabitsV2(HABITS)
    _wire(monkeypatch, fake)
    fn = getattr(s, tool)

    mid = _extract_manifest_id(await fn(**kwargs))
    refused = await fn(**kwargs, manifest_id=mid, user_reply="нет, погоди")
    assert "🛑" in refused

    dead = await fn(**kwargs, manifest_id=mid, user_reply="да")
    assert "🛑" in dead
    assert fake.create_calls == [] and fake.delete_calls == []


@pytest.mark.parametrize("tool,kwargs", [
    ("create_habit", dict(name="Зарядка")),
    ("delete_habit", dict(habit_name="Медитация", habit_id="h1")),
])
async def test_manifest_is_one_shot(monkeypatch, tool, kwargs):
    fake = FakeHabitsV2(HABITS)
    _wire(monkeypatch, fake)
    fn = getattr(s, tool)

    mid = _extract_manifest_id(await fn(**kwargs))
    await fn(**kwargs, manifest_id=mid, user_reply="да")
    assert len(fake.create_calls) + len(fake.delete_calls) == 1

    second = await fn(**kwargs, manifest_id=mid, user_reply="да")
    assert "🛑" in second
    assert len(fake.create_calls) + len(fake.delete_calls) == 1


@pytest.mark.parametrize("tool,kwargs", [
    ("create_habit", dict(name="Зарядка")),
    ("delete_habit", dict(habit_name="Медитация", habit_id="h1")),
])
async def test_unknown_manifest_is_refused(monkeypatch, tool, kwargs):
    fake = FakeHabitsV2(HABITS)
    _wire(monkeypatch, fake)

    result = await getattr(s, tool)(**kwargs, manifest_id="deadbeef",
                                    user_reply="да")

    assert result.startswith("🛑")
    assert fake.create_calls == [] and fake.delete_calls == []


@pytest.mark.parametrize("tool,kwargs", [
    ("create_habit", dict(name="Зарядка")),
    ("delete_habit", dict(habit_name="Медитация", habit_id="h1")),
])
async def test_automation_key_bypasses_user_reply(monkeypatch, tool, kwargs):
    """Headless-потребитель (бот/пайплайн) проходит гейт своим секретом —
    и он НЕ утекает в текст ответа."""
    fake = FakeHabitsV2(HABITS)
    _wire(monkeypatch, fake)
    monkeypatch.setattr(s, "SECRET", "test-secret")
    fn = getattr(s, tool)

    mid = _extract_manifest_id(await fn(**kwargs))
    result = await fn(**kwargs, manifest_id=mid, automation_key="test-secret")

    assert "🛑" not in result
    assert "test-secret" not in result
    assert len(fake.create_calls) + len(fake.delete_calls) == 1


@pytest.mark.parametrize("tool,kwargs", [
    ("create_habit", dict(name="Зарядка")),
    ("delete_habit", dict(habit_name="Медитация", habit_id="h1")),
])
async def test_wrong_automation_key_does_not_bypass(monkeypatch, tool, kwargs):
    fake = FakeHabitsV2(HABITS)
    _wire(monkeypatch, fake)
    monkeypatch.setattr(s, "SECRET", "test-secret")
    fn = getattr(s, tool)

    mid = _extract_manifest_id(await fn(**kwargs))
    # ключ намеренно ASCII: hmac.compare_digest падает TypeError на не-ASCII
    # строке — отдельный дефект общего гейта, не этих тулов
    result = await fn(**kwargs, manifest_id=mid, automation_key="wrong-key")

    assert "🛑" in result
    assert fake.create_calls == [] and fake.delete_calls == []
