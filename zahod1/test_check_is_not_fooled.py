"""Собственная проверка прогона на порчу (задание, раздел 2.3, «Чем прогон проверяем»).

Прогон, который не краснеет на трёх порчах ниже, сам держится на
добросовестности и потому проверкой не считается. Поэтому порчи вносятся
машинно, во ВРЕМЕННУЮ копию реестра, и по каждой требуется «не закрыт»:

  (а) статус «не закрыт» заменён на «закрыт» — прогон обязан всё равно дать
      «не закрыт», потому что статус он вычисляет, а не читает;
  (б) стёрто имя теста — «не закрыт»;
  (в) подставлен коммит из соседнего пункта — тест при откате не краснеет,
      «не закрыт».

Проверка идёт через тот же публичный вход, которым пользуется владелец:
`python -m zahod1.check --dsn ...`. Ни одна внутренняя функция напрямую не
зовётся — иначе проверялось бы не то, что запускает владелец.

Запуск:
    TICKTICK_TEST_PG_DSN="postgresql://…" python -m pytest zahod1/test_check_is_not_fooled.py -q

Тестовое поле — СВОЙ временный git-репозиторий с двумя пунктами и двумя
коммитами. Репозиторий сервера при этом не трогается вовсе.
"""

from __future__ import annotations

import copy
import os
import subprocess
import sys
from pathlib import Path

import pytest

from . import yaml_min
from .check import KOD_NE_SDAN, NOMERA

KOREN = Path(__file__).resolve().parent.parent

MOD_ISHODNYY = "def obshchee():\n    return 'база'\n"

FICHA_A = """

def a():
    return 'A'
"""

TEST_A = """
from mod import a


def test_a_avtor():
    assert a() == 'A'


def test_a_proveryayushchiy():
    assert a().islower() is False
"""

FICHA_B = """

def b():
    return 'B'
"""

TEST_B = """
from mod import b


def test_b_avtor():
    assert b() == 'B'


def test_b_proveryayushchiy():
    assert len(b()) == 1
"""


def _dsn() -> str:
    dsn = os.environ.get("TICKTICK_TEST_PG_DSN", "").strip()
    if not dsn:
        pytest.fail(
            "нужен TICKTICK_TEST_PG_DSN: прогон запускается только на настоящей базе, "
            "и проверка прогона на порчу — тоже"
        )
    return dsn


def _git(argumenty: list[str], cwd: Path) -> None:
    gotovo = subprocess.run(
        ["git", "-c", "user.name=zahod1", "-c", "user.email=zahod1@local", *argumenty],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert gotovo.returncode == 0, f"git {' '.join(argumenty)}: {gotovo.stderr}"


def _hesh(cwd: Path) -> str:
    gotovo = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(cwd), capture_output=True, text=True)
    return gotovo.stdout.strip()


def _pustaya_zapis(nomer: str) -> dict:
    return {
        "номер": nomer,
        "заголовок": f"пункт {nomer}",
        "автор": {"роль": "", "модель": ""},
        "коммиты": [],
        "тест_автора": "",
        "тест_проверяющего": "",
        "проверяющий": {"роль": "", "модель": ""},
        "скептики": [
            {"линза": "С-Д", "роль": "", "модель": "", "отчёт": ""},
            {"линза": "С-Л", "роль": "", "модель": "", "отчёт": ""},
            {"линза": "С-Г", "роль": "", "модель": "", "отчёт": ""},
        ],
        "счетовод": {"роль": "", "модель": ""},
        "числа": [],
        "ворота": {"ворота_1": "", "ворота_2": "", "ворота_3": ""},
        "статус": "",
    }


def _polnaya_zapis(nomer: str, avtor: str, proveryayushchiy: str, kommit: str, prefiks: str) -> dict:
    fayl_testa = f"tests/test_{prefiks}.py"
    return {
        "номер": nomer,
        "заголовок": f"пункт {nomer}",
        "автор": {"роль": avtor, "модель": "Opus"},
        "коммиты": [kommit],
        "тест_автора": f"{fayl_testa}::test_{prefiks}_avtor",
        "тест_проверяющего": f"{fayl_testa}::test_{prefiks}_proveryayushchiy",
        "проверяющий": {"роль": proveryayushchiy, "модель": "Sonnet"},
        "скептики": [
            {"линза": "С-Д", "роль": "С-Д", "модель": "Opus", "отчёт": "otchety/sd.md"},
            {"линза": "С-Л", "роль": "С-Л", "модель": "Opus", "отчёт": "otchety/sl.md"},
            {"линза": "С-Г", "роль": "С-Г", "модель": "Opus", "отчёт": "otchety/sg.md"},
        ],
        "счетовод": {"роль": "С-1", "модель": "Haiku"},
        "числа": [
            {"что": "контрольное число", "было": "0", "стало": "42", "команда": "echo 42"},
        ],
        "ворота": {
            "ворота_1": "2026-08-08 10:00",
            "ворота_2": "2026-08-08 11:00",
            "ворота_3": "2026-08-08 12:00",
        },
        "статус": "закрыт",
    }


@pytest.fixture(scope="module")
def pole(tmp_path_factory) -> dict:
    """Временный репозиторий: два пункта, два коммита, по два теста на пункт."""
    repo = tmp_path_factory.mktemp("zahod1-pole")
    _git(["init", "-b", "main"], repo)
    (repo / "mod.py").write_text(MOD_ISHODNYY, encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "__init__.py").write_text("", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-m", "основа"], repo)

    with (repo / "mod.py").open("a", encoding="utf-8") as f:
        f.write(FICHA_A)
    (repo / "tests" / "test_a.py").write_text(TEST_A, encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-m", "пункт 1.1.1"], repo)
    kommit_a = _hesh(repo)

    with (repo / "mod.py").open("a", encoding="utf-8") as f:
        f.write(FICHA_B)
    (repo / "tests" / "test_b.py").write_text(TEST_B, encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-m", "пункт 1.1.2"], repo)
    kommit_b = _hesh(repo)

    (repo / "otchety").mkdir()
    for imya in ("sd.md", "sl.md", "sg.md"):
        (repo / "otchety" / imya).write_text(
            "Вход: пустое название задачи\nНаблюдаемый результат: отказ до записи\n",
            encoding="utf-8",
        )

    punkty = []
    for nomer in NOMERA:
        if nomer == "1.1.1":
            punkty.append(_polnaya_zapis("1.1.1", "Ремонтник сверки", "П-1", kommit_a, "a"))
        elif nomer == "1.1.2":
            punkty.append(_polnaya_zapis("1.1.2", "Ремонтник сверки", "П-2", kommit_b, "b"))
        else:
            zapis = _pustaya_zapis(nomer)
            if nomer == "1.1.3":
                zapis["статус"] = "не закрыт"
            punkty.append(zapis)

    reestr = {"мета": {"основа": "0000000", "тестов_до_захода": 0}, "пункты": punkty}
    return {"repo": repo, "reestr": reestr, "kommit_a": kommit_a, "kommit_b": kommit_b}


def _prognat(repo: Path, reestr: dict, imya: str) -> tuple[int, str]:
    put = repo / f"reestr-{imya}.yaml"
    put.write_text(yaml_min.sohranit(reestr), encoding="utf-8")
    gotovo = subprocess.run(
        [
            sys.executable,
            "-m",
            "zahod1.check",
            "--dsn",
            _dsn(),
            "--repo",
            str(repo),
            "--reestr",
            str(put),
        ],
        cwd=str(KOREN),
        capture_output=True,
        text=True,
        timeout=1800,
    )
    vyvod = (gotovo.stdout or "") + (gotovo.stderr or "")
    # Задание требует приложить вывод по каждой порче. Печать видна при
    # запуске с -s и попадает в отчёт pytest при падении.
    print(f"\n===== прогон «{imya}», код выхода {gotovo.returncode} =====\n{vyvod}")
    return gotovo.returncode, vyvod


def _status(vyvod: str, nomer: str) -> str:
    for stroka in vyvod.splitlines():
        if stroka.startswith(nomer + " "):
            yacheyki = [y.strip() for y in stroka.split("|")]
            return yacheyki[1] if len(yacheyki) > 1 else ""
    raise AssertionError(f"в таблице нет строки пункта {nomer}:\n{vyvod}")


def test_bez_porchi_punkt_zakryt(pole):
    """Опора всей проверки: без порчи пункт 1.1.1 обязан быть «закрыт».

    Без этой опоры три проверки ниже прошли бы и на прогоне, который всегда
    отвечает «не закрыт».
    """
    kod, vyvod = _prognat(pole["repo"], pole["reestr"], "chisto")
    assert _status(vyvod, "1.1.1") == "закрыт", vyvod
    assert _status(vyvod, "1.1.2") == "закрыт", vyvod
    assert kod == KOD_NE_SDAN, "остальные шестнадцать пунктов пусты — заход не сдан"


def test_porcha_a_status_zakryt_ne_pomogaet(pole):
    """(а) Статус «не закрыт» заменён на «закрыт» — прогон вычисляет, а не читает."""
    isporchennyy = copy.deepcopy(pole["reestr"])
    for zapis in isporchennyy["пункты"]:
        if zapis["номер"] == "1.1.3":
            assert zapis["статус"] == "не закрыт"
            zapis["статус"] = "закрыт"
    kod, vyvod = _prognat(pole["repo"], isporchennyy, "porcha-a")
    assert _status(vyvod, "1.1.3") == "не закрыт", vyvod
    assert kod == KOD_NE_SDAN
    assert "ЗАХОД 1 НЕ СДАН" in vyvod


def test_porcha_b_stertoe_imya_testa(pole):
    """(б) Стёрто имя теста — «не закрыт»."""
    isporchennyy = copy.deepcopy(pole["reestr"])
    for zapis in isporchennyy["пункты"]:
        if zapis["номер"] == "1.1.1":
            zapis["тест_автора"] = ""
    kod, vyvod = _prognat(pole["repo"], isporchennyy, "porcha-b")
    assert _status(vyvod, "1.1.1") == "не закрыт", vyvod
    assert kod == KOD_NE_SDAN
    assert "тест_автора" in vyvod


def test_porcha_v_chuzhoy_kommit(pole):
    """(в) Подставлен коммит соседнего пункта — при откате тест не краснеет."""
    isporchennyy = copy.deepcopy(pole["reestr"])
    for zapis in isporchennyy["пункты"]:
        if zapis["номер"] == "1.1.1":
            zapis["коммиты"] = [pole["kommit_b"]]
    kod, vyvod = _prognat(pole["repo"], isporchennyy, "porcha-v")
    assert _status(vyvod, "1.1.1") == "не закрыт", vyvod
    assert kod == KOD_NE_SDAN
    assert "не покраснел" in vyvod
