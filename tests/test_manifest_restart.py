"""ГЛАВНЫЙ ТЕСТ ПРИЁМКИ задачи #91: план подтверждения переживает НАСТОЯЩИЙ
перезапуск процесса, и нажатие кнопки ПОСЛЕ рестарта действительно ИСПОЛНЯЕТ
операцию, а не сообщает о потере.

Сценарий воспроизводит ровно ту ситуацию, из-за которой задача и появилась:
владелец получил план с кнопками → сервис перезапустился (авто-деплой из
`main` делает это в произвольный момент) → владелец нажал ✅.

Процессы здесь настоящие (`subprocess`), не «очищенный словарь»: см. шапку
tests/restart_child.py. Postgres тоже настоящий: см. tests/pg_helper.py.
Наружу — ни одного сетевого вызова: Telegram замокан на уровне отправки,
мутация TickTick заменена шпионом.
"""
import json
import os
import subprocess
import sys
import time

import pytest

from tests.pg_helper import fresh_stores, pg_dsn, requires_pg

pytestmark = requires_pg

_CHILD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "restart_child.py")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(phase, dsn, calls_file, tag="тег-из-плана", data_dir="/tmp", mid=""):
    env = dict(os.environ)
    env["TEST_CALLS_FILE"] = calls_file
    env["TEST_DATA_DIR"] = data_dir
    env["PYTHONPATH"] = _ROOT
    argv = [sys.executable, _CHILD, phase, dsn, tag] + ([mid] if mid else [])
    proc = subprocess.run(argv,
                          capture_output=True, text=True, timeout=180, env=env,
                          cwd=_ROOT)
    assert proc.returncode == 0, (
        f"фаза {phase} упала:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _press_the_button(store, manifest_id):
    """То, что делает вебхук соседнего сервиса при нажатии ✅: строка
    подтверждения становится APPROVED. Никакого отношения к нашей памяти это
    не имеет — в том и была проблема."""
    with store._conn() as cur:
        cur.execute(
            "UPDATE tg_approvals SET status = 'APPROVED', decided_at = %s "
            "WHERE manifest_id = %s",
            (int(time.time() * 1000) - 600_000, manifest_id),
        )
        assert cur.rowcount == 1, "строки подтверждения нет — план не был отправлен"


@pytest.fixture
def clean_db():
    store = fresh_stores()
    # Пул этого (родительского) процесса дочерним не наследуется — они поднимают
    # свой. Закрываем, чтобы соединения не копились между тестами.
    yield store
    store.close_store()


def test_button_after_a_real_restart_executes_the_plan(clean_db, tmp_path):
    """ЭТО И ЕСТЬ СМЫСЛ ЗАДАЧИ. Без долговечных планов второй процесс не нашёл
    бы ЧТО исполнять и в лучшем случае извинился бы за потерю."""
    dsn, calls = pg_dsn(), str(tmp_path / "calls.jsonl")

    # ── Процесс №1: построили план, он ушёл «в Telegram» и лёг в базу.
    planned = _run("plan", dsn, calls, tag="тег-после-рестарта",
                   data_dir=str(tmp_path))
    assert len(planned["manifest_ids"]) == 1
    mid = planned["manifest_ids"][0]
    assert planned["in_db"] == [mid], "план не сохранился в базе"
    assert not os.path.exists(calls), "на фазе плана ничего исполнять нельзя"

    # ── Владелец нажимает ✅ (процесс №1 к этому моменту уже мёртв).
    _press_the_button(clean_db, mid)

    # ── Процесс №2 — ЧИСТАЯ ПАМЯТЬ. Один тик поллера.
    pressed = _run("press", dsn, calls, data_dir=str(tmp_path))

    lines = [json.loads(x) for x in open(calls, encoding="utf-8")]
    assert len(lines) == 1, f"ожидалось ровно одно исполнение, вышло {lines}"
    assert lines[0] == {"tool": "create_tag", "name": "тег-после-рестарта"}
    # План поднят из базы в память нового процесса и погашен там же.
    assert pressed["tombstones"].get(mid) is not None
    # ...и в базе он тоже израсходован — второй раз его не займёт никто.
    assert clean_db.load(mid)["consumed_at"] is not None


def test_a_second_restart_does_not_execute_the_plan_again(clean_db, tmp_path):
    """Одноразовость обязана пережить перезапуск ТОЖЕ. Иначе каждая выкатка
    заново исполняла бы все подтверждённые планы, до которых дотянется."""
    dsn, calls = pg_dsn(), str(tmp_path / "calls.jsonl")
    planned = _run("plan", dsn, calls, tag="один-раз", data_dir=str(tmp_path))
    mid = planned["manifest_ids"][0]
    _press_the_button(clean_db, mid)

    _run("press", dsn, calls, data_dir=str(tmp_path))
    _run("press", dsn, calls, data_dir=str(tmp_path))  # ещё один «рестарт»

    lines = [json.loads(x) for x in open(calls, encoding="utf-8")]
    assert len(lines) == 1, f"план исполнился {len(lines)} раз(а) вместо одного"


def test_two_simultaneous_presses_after_restart_execute_exactly_once(
        clean_db, tmp_path):
    """Два одновременных нажатия (два тика в один момент) → РОВНО ОДНО
    исполнение. Здесь конкуренция настоящая: захват уходит в разные потоки и
    разруливается блокировкой строки в Postgres."""
    dsn, calls = pg_dsn(), str(tmp_path / "calls.jsonl")
    planned = _run("plan", dsn, calls, tag="только-раз", data_dir=str(tmp_path))
    mid = planned["manifest_ids"][0]
    _press_the_button(clean_db, mid)

    _run("press2", dsn, calls, data_dir=str(tmp_path))

    lines = [json.loads(x) for x in open(calls, encoding="utf-8")]
    assert len(lines) == 1, f"двойное исполнение: {lines}"


def test_a_plan_refused_in_chat_stays_dead_across_a_restart(clean_db, tmp_path):
    """Обратная сторона того же: гашение плана обязано доезжать до базы.

    Иначе «нет» в чате отменяло бы операцию только до ближайшей выкатки, а
    висящая в Telegram кнопка ✅ исполнила бы её после.

    Отказ идёт НАСТОЯЩИМ путём — вторым вызовом того же инструмента в НОВОМ
    процессе, — а не прямым `mark_consumed` из теста. Первая редакция звала
    хранилище напрямую, и мутационная проверка показала цену такой экономии:
    сломанная проводка «гейт → база» оставляла тест зелёным."""
    dsn, calls = pg_dsn(), str(tmp_path / "calls.jsonl")
    planned = _run("plan", dsn, calls, tag="отменённый", data_dir=str(tmp_path))
    mid = planned["manifest_ids"][0]

    refused = _run("refuse", dsn, calls, tag="отменённый",
                   data_dir=str(tmp_path), mid=mid)
    assert "НЕ подтвердил" in refused["answer"], refused["answer"]
    assert refused["known_after"], "план не был поднят из базы — гасить было нечего"
    assert clean_db.load(mid)["consumed_at"] is not None, \
        "отказ не доехал до базы: после рестарта план воскреснет"

    # Владелец всё же жмёт ✅ на старом сообщении — исполнять уже нечего.
    _press_the_button(clean_db, mid)
    _run("press", dsn, calls, data_dir=str(tmp_path))
    assert not os.path.exists(calls), "исполнили операцию, от которой отказались"


def test_chat_path_recognises_the_plan_after_a_restart(clean_db, tmp_path):
    """Модель зовёт инструмент второй раз уже после перезапуска. Сервер обязан
    УЗНАТЬ план (и ответить про кнопку, раз текстовый путь для него закрыт), а
    не выдать «манифест не найден/истёк/уже исполнен» на совершенно живой
    план."""
    dsn, calls = pg_dsn(), str(tmp_path / "calls.jsonl")
    planned = _run("plan", dsn, calls, tag="живой-план", data_dir=str(tmp_path))
    mid = planned["manifest_ids"][0]

    again = _run("retry_yes", dsn, calls, tag="живой-план",
                 data_dir=str(tmp_path), mid=mid)
    assert again["known_after"], "план не подняли из базы"
    assert "не найден" not in again["answer"], again["answer"]
    assert "кнопк" in again["answer"].lower(), again["answer"]
    # Ответ «ждём нажатия» НЕ гасит план: кнопка обязана остаться рабочей.
    assert clean_db.load(mid)["consumed_at"] is None
    assert not os.path.exists(calls), "чат-«да» не должен ничего исполнять"
