"""ТАБЛИЦА ПОКРЫТИЯ — правило приёмки, а не обещание (2026-08-10, §1.3.4 Б).

Правило дословно: метод не закрывается, пока в агрегаторе нет ПРОВЕРЕННОЙ
замены. Проверенная — это одновременно (а) тип присутствует в `_TRIAGE_OPS`;
(б) у типа есть обработчики в реестре типов; (в) существует ЗЕЛЁНЫЙ сквозной
тест, который тем же типом через агрегатор совершает то же действие, что
совершал закрываемый метод, и судит по живому состоянию, а не по тексту.

Зачем условие (в) отдельно от (а). Это второй способ подделки из ТЗ: тип
добавлен в список, таблица заполнена, тест на членство зелёный — а через
агрегатор действие ни разу не совершалось по-настоящему. Строка в таблице
такое пропускает; помеченный и РЕАЛЬНО ЗАПУЩЕННЫЙ тест — нет.

Почему подпроцесс. «Тест собран в прогоне» нельзя честно проверить изнутри
своего же прогона: при выборочном запуске (`-k`, один файл) остальные тесты
не собраны, и проверка либо врала бы, либо ломалась на ровном месте. Поэтому
запускается отдельный pytest ровно по маркеру `-m triage_e2e`: он и собирает
(через хук в conftest пишет карту «тип → тесты»), и ИСПОЛНЯЕТ их. Пропущенный
(skip) или красный тест замены не доказывает — поэтому проверяется код
возврата, а не только факт существования файла.
"""
import json
import os
import re
import subprocess
import sys
import tempfile

import pytest

import ticktick_mcp.src.server as s

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def e2e_run():
    """Один подпроцесс на весь файл: собрать карту типов и прогнать их тесты."""
    with tempfile.TemporaryDirectory() as tmp:
        dump = os.path.join(tmp, "triage_e2e.json")
        env = dict(os.environ)
        env["TRIAGE_E2E_DUMP"] = dump
        env["PYTHONPATH"] = _ROOT
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "tests", "-m", "triage_e2e",
             "-q", "-p", "no:randomly"],
            cwd=_ROOT, env=env, capture_output=True, text=True, timeout=600)
        covered = {}
        if os.path.exists(dump):
            with open(dump, encoding="utf-8") as fh:
                covered = json.load(fh)
        return proc, covered


def test_every_hidden_tool_has_verified_triage_replacement(e2e_run):
    proc, covered = e2e_run

    # ── (0) таблица покрывает ровно то, что закрыто ──────────────────────
    hidden = s._hidden_tool_names() - {"manual_triage"}
    table = set(s._HIDDEN_TOOL_REPLACEMENT)
    assert hidden <= table, (
        "закрыто имя, которого нет в таблице покрытия: "
        f"{sorted(hidden - table)} — закрытие без объявленной замены значит "
        "отнятое действие, а не защиту")

    # ── (а) каждый объявленный тип существует в реестре агрегатора ───────
    ops_of_table = {op for r in s._HIDDEN_TOOL_REPLACEMENT.values() for op in r.ops}
    unknown = sorted(ops_of_table - set(s._TRIAGE_OPS))
    assert not unknown, (
        f"таблица ссылается на типы, которых нет в _TRIAGE_OPS: {unknown}")

    # Имя без типов допустимо ровно тогда, когда названа замена ВНЕ агрегатора
    # и она сама жива в реестре инструментов (иначе это отсылка в пустоту).
    for name in sorted(table):
        repl = s._HIDDEN_TOOL_REPLACEMENT[name]
        if repl.ops:
            continue
        assert repl.note, f"{name}: ни типа агрегатора, ни замены — это отнятое действие"
        named = set(re.findall(r"[a-z_][a-z_0-9]*", repl.note))
        alive = sorted(named & set(s.mcp._tool_manager._tools))
        assert alive, (
            f"{name}: замена «{repl.note}» не называет ни одного живого "
            "инструмента сервера")

    # ── (в) у каждого типа есть ПОМЕЧЕННЫЙ и РЕАЛЬНО ПРОГНАННЫЙ тест ─────
    assert covered, (
        "подпроцесс не собрал ни одного помеченного теста — маркер "
        f"triage_e2e потерян целиком.\n{proc.stdout[-2000:]}")
    missing = sorted(op for op in ops_of_table if not covered.get(op))
    assert not missing, (
        f"типы без помеченного сквозного теста: {missing}. Строка в таблице "
        "не доказывает, что действие через агрегатор вообще совершалось")
    assert proc.returncode == 0, (
        "помеченные сквозные тесты не зелёные — замена не проверена:\n"
        f"{proc.stdout[-4000:]}")


def test_every_triage_type_is_covered_end_to_end(e2e_run):
    """Шире таблицы: помеченный тест обязан быть у КАЖДОГО из типов реестра, а
    не только у тех, что закрывают прямой метод.

    Причина — тот же способ подделки, с другой стороны. Тип, который никто не
    закрывает напрямую, всё равно едет в план владельцу и исполняется; если
    сквозного теста у него нет, «двенадцать типов» — это двенадцать строк
    реестра, а не двенадцать проверенных действий."""
    _proc, covered = e2e_run
    missing = sorted(op for op in s._TRIAGE_OPS if not covered.get(op))
    assert not missing, f"типы реестра без сквозного теста: {missing}"


def test_marked_tests_are_not_skipped(e2e_run):
    """Skip — не доказательство. Тест, помеченный как сквозной, но
    пропущенный (нет Postgres, xfail, условие окружения), оставляет тип
    непроверенным ровно так же, как его отсутствие."""
    proc, _covered = e2e_run
    tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    assert "skipped" not in tail, (
        f"часть помеченных сквозных тестов пропущена: {tail}")
