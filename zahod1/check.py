"""Прогон сдачи захода 1.

    python -m zahod1.check --dsn "$TICKTICK_TEST_PG_DSN"

Прогон ВЫЧИСЛЯЕТ статус каждого пункта, а не читает его из реестра. Поле
«статус» в реестре на итог не влияет: единственное, что прогон принимает из
него на веру, — признание «остановлен и возвращён владельцу», потому что это
признание незакрытости и выиграть им нельзя.

Все выводы получены из git, из прогона тестов и из повторно выполненных команд
счетовода. У исполнителя прогон не спрашивает ничего.

Коды выхода:
    0 — ЗАХОД 1 СДАН;
    1 — ЗАХОД 1 НЕ СДАН;
    2 — прогон шёл без базы: таблица не печатается вовсе;
    3 — аварийная остановка (недопустимый статус в реестре, неразобранный
        реестр, невосстановленное рабочее дерево).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import yaml_min

KOREN = Path(__file__).resolve().parent.parent

NOMERA = [
    "1.1.1", "1.1.2", "1.1.3", "1.1.4", "1.1.5", "1.1.6", "1.1.7", "1.1.8",
    "1.2.1", "1.2.2", "1.2.3", "1.2.4",
    "1.3.1", "1.3.2", "1.3.3", "1.3.4", "1.3.5", "1.3.6",
]

ZAKRYT = "закрыт"
NE_ZAKRYT = "не закрыт"
OSTANOVLEN = "остановлен и возвращён владельцу"
DOPUSTIMYE_STATUSY = {ZAKRYT, NE_ZAKRYT, OSTANOVLEN}

# Отчёт скептика без конкретного входа или без перечня попыток считается
# отсутствующим (задание, ворота 3).
MARKERY_SKEPTIKA = ("Вход:", "Попытка")

KOD_SDAN = 0
KOD_NE_SDAN = 1
KOD_BEZ_BAZY = 2
KOD_AVARIYA = 3

TAYMAUT_TESTA = 900
TAYMAUT_KOMANDY = 600


class AvariynayaOstanovka(Exception):
    """Прогон не имеет права продолжаться и не имеет права печатать таблицу."""


@dataclass
class Rezultat:
    nomer: str
    status: str
    upavshiy_test: str = "—"
    kto_proveril: str = "—"
    chisla: str = "—"
    prichiny: list[str] = field(default_factory=list)
    stroki_padeniya: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- инструменты


def _vypolnit(argumenty: list[str], cwd: Path, taymaut: int = 120, okruzhenie: dict | None = None):
    return subprocess.run(
        argumenty,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=taymaut,
        env=okruzhenie,
    )


def _git(argumenty: list[str], cwd: Path, taymaut: int = 120):
    return _vypolnit(["git", *argumenty], cwd, taymaut)


def _okruzhenie_testov(dsn: str) -> dict:
    okruzhenie = dict(os.environ)
    okruzhenie["TICKTICK_TEST_PG_DSN"] = dsn
    # Чтобы прогон тестов не оставлял в рабочем дереве байт-код: иначе
    # проверка «дерево восстановлено» краснела бы на собственном мусоре.
    okruzhenie["PYTHONDONTWRITEBYTECODE"] = "1"
    return okruzhenie


def _prognat_test(nodeid: str, cwd: Path, dsn: str) -> tuple[bool, str]:
    """Прогнать один тест. Возвращает (зелёный, вывод)."""
    try:
        gotovo = _vypolnit(
            [sys.executable, "-m", "pytest", nodeid, "-q", "--no-header", "-p", "no:cacheprovider"],
            cwd,
            TAYMAUT_TESTA,
            _okruzhenie_testov(dsn),
        )
    except subprocess.TimeoutExpired:
        return False, f"прогон теста {nodeid} не уложился в {TAYMAUT_TESTA} с"
    vyvod = (gotovo.stdout or "") + (gotovo.stderr or "")
    return gotovo.returncode == 0, vyvod


def _stroka_padeniya(vyvod: str) -> str:
    """Строка вывода прогона, показывающая падение. Берётся ИЗ ВЫВОДА, не из реестра."""
    stroki = [s.strip() for s in vyvod.splitlines() if s.strip()]
    for stroka in stroki:
        if stroka.startswith("FAILED") or stroka.startswith("ERROR"):
            return stroka
    for stroka in reversed(stroki):
        if "failed" in stroka or "error" in stroka or "no tests ran" in stroka:
            return stroka
    return stroki[-1] if stroki else "(пустой вывод прогона)"


def _rol(uzel) -> str:
    if not isinstance(uzel, dict):
        return ""
    return str(uzel.get("роль", "") or "").strip()


def _klyuch_roli(rol: str) -> str:
    return " ".join(str(rol).split()).casefold()


def _kto(uzel) -> str:
    rol = _rol(uzel)
    model = str((uzel or {}).get("модель", "") or "").strip() if isinstance(uzel, dict) else ""
    if rol and model:
        return f"{rol} ({model})"
    return rol or "—"


# ---------------------------------------------------------------- откат


class Otkat:
    """Отдельное рабочее дерево, в котором коммиты пункта откатываются.

    Основной репозиторий не трогается: `git worktree add` создаёт отдельный
    каталог, а `git worktree remove` его убирает. Невозможность убрать —
    аварийная остановка всего прогона.
    """

    def __init__(self, repo: Path):
        self.repo = repo
        self.kornevoy_vremennyy = Path(tempfile.mkdtemp(prefix="zahod1-otkat-"))
        self.derevo = self.kornevoy_vremennyy / "wt"
        golova = _git(["rev-parse", "HEAD"], repo)
        if golova.returncode != 0:
            raise AvariynayaOstanovka(f"не удалось прочитать HEAD: {golova.stderr.strip()}")
        self.osnova = golova.stdout.strip()
        sozdanie = _git(["worktree", "add", "--detach", str(self.derevo), self.osnova], repo, 300)
        if sozdanie.returncode != 0:
            raise AvariynayaOstanovka(
                "не удалось создать отдельное рабочее дерево для отката: " + sozdanie.stderr.strip()
            )

    def _vershina(self, kommity: list[str]) -> tuple[str, str]:
        """Коммит пункта, содержащий все остальные его коммиты как предков.

        Откат ведётся от вершины САМОГО пункта, а не от общего HEAD. Иначе
        снятие раннего коммита упирается в конфликт с более поздней чужой
        работой в тех же строках, и «откат не применился» говорилось бы о
        соседях, а не о проверяемом пункте.
        """
        for kandidat in kommity:
            vse_predki = True
            for drugoy in kommity:
                if drugoy == kandidat:
                    continue
                proverka = _git(["merge-base", "--is-ancestor", drugoy, kandidat], self.derevo)
                if proverka.returncode != 0:
                    vse_predki = False
                    break
            if vse_predki:
                return kandidat, ""
        return "", "коммиты пункта не образуют одну цепочку: вершину определить нельзя"

    def _po_toplogii(self, kommity: list[str]) -> list[str]:
        """Коммиты пункта от позднего к раннему — в этом порядке они и снимаются."""
        gotovo = _git(["rev-list", "--topo-order", "--no-walk=sorted", *kommity], self.derevo)
        if gotovo.returncode != 0:
            return list(reversed(kommity))
        poryadok = [s.strip() for s in gotovo.stdout.splitlines() if s.strip()]
        polnye = {}
        for kommit in kommity:
            razvernutyy = _git(["rev-parse", kommit], self.derevo).stdout.strip()
            polnye[razvernutyy] = kommit
        return [polnye[h] for h in poryadok if h in polnye]

    def vernut_fayly_testov(self, vershina: str, nodeidy: list[str]) -> None:
        """Вернуть файлы названных тестов из вершины пункта поверх отката.

        Без этого пункт закрывался бы тестом, который вместе с правкой и
        исчезает: pytest сказал бы «файла нет», прогон засчитал бы красноту, а
        проверено бы ничего не было. Тест обязан выполниться на откаченном коде
        и упасть по существу — поэтому сам файл теста при откате остаётся.
        """
        for nodeid in nodeidy:
            fayl = nodeid.split("::", 1)[0]
            _git(["checkout", vershina, "--", fayl], self.derevo, 300)

    def otkatit(self, kommity: list[str]) -> tuple[bool, str, str]:
        """Снять коммиты пункта в отдельном дереве. Возвращает (получилось, причина, вершина)."""
        for kommit in kommity:
            proverka = _git(["cat-file", "-t", kommit], self.derevo)
            if proverka.returncode != 0 or proverka.stdout.strip() != "commit":
                return False, f"коммит {kommit} в репозитории не найден", ""
        vershina, beda = self._vershina(kommity)
        if beda:
            return False, beda, ""
        sbros = _git(["reset", "--hard", vershina], self.derevo, 300)
        if sbros.returncode != 0:
            return False, f"не удалось поставить отдельное дерево на коммит {vershina}", vershina
        _git(["clean", "-fdq"], self.derevo, 300)
        for kommit in self._po_toplogii(kommity):
            snyatie = _git(["revert", "--no-commit", "--no-edit", kommit], self.derevo, 300)
            if snyatie.returncode != 0:
                _git(["revert", "--abort"], self.derevo)
                pervaya = (snyatie.stderr.strip().splitlines() or ["без объяснения"])[0]
                return False, f"откат коммита {kommit} не применился: {pervaya}", vershina
        return True, "", vershina

    def zakryt(self) -> None:
        _git(["worktree", "remove", "--force", str(self.derevo)], self.repo, 300)
        if self.derevo.exists():
            shutil.rmtree(self.derevo, ignore_errors=True)
        _git(["worktree", "prune"], self.repo)
        shutil.rmtree(self.kornevoy_vremennyy, ignore_errors=True)
        if self.derevo.exists():
            raise AvariynayaOstanovka(
                f"откаченное рабочее дерево не удалось убрать: {self.derevo}. "
                "Прогон не имеет права оставить репозиторий откаченным."
            )


def _snimok_dereva(repo: Path) -> tuple[str, str]:
    """HEAD и состояние рабочего дерева — для проверки «дерево восстановлено».

    Из состояния выброшены только НЕОТСЛЕЖИВАЕМЫЕ кэши интерпретатора и pytest:
    они появляются от самого прогона тестов и порчей дерева не являются. Всё
    остальное — изменения, добавления, удаления, любые прочие неотслеживаемые
    файлы — остаётся и обязано совпасть до и после.
    """
    golova = _git(["rev-parse", "HEAD"], repo)
    sostoyanie = _git(["status", "--porcelain"], repo)
    znachimye = [
        stroka
        for stroka in sostoyanie.stdout.splitlines()
        if not (
            stroka.startswith("?? ")
            and ("__pycache__" in stroka or ".pytest_cache" in stroka or stroka.rstrip().endswith(".pyc"))
        )
    ]
    return golova.stdout.strip(), "\n".join(znachimye).strip()


# ---------------------------------------------------------------- реестр


def zagruzit_reestr(put: Path) -> tuple[dict, dict[str, dict]]:
    if not put.exists():
        raise AvariynayaOstanovka(f"реестра нет: {put}")
    try:
        dokument = yaml_min.zagruzit(put.read_text(encoding="utf-8"))
    except yaml_min.OshibkaRazbora as oshibka:
        raise AvariynayaOstanovka(f"реестр не разобран: {oshibka}") from oshibka
    if not isinstance(dokument, dict) or "пункты" not in dokument:
        raise AvariynayaOstanovka("в реестре нет раздела «пункты»")
    zapisi: dict[str, dict] = {}
    for zapis in dokument.get("пункты") or []:
        if not isinstance(zapis, dict):
            raise AvariynayaOstanovka("запись реестра не является набором полей")
        nomer = str(zapis.get("номер", "") or "").strip()
        if not nomer:
            raise AvariynayaOstanovka("в реестре есть запись без номера")
        if nomer in zapisi:
            raise AvariynayaOstanovka(f"номер {nomer} встречается в реестре дважды")
        status = str(zapis.get("статус", "") or "").strip()
        if status and status not in DOPUSTIMYE_STATUSY:
            raise AvariynayaOstanovka(
                f"пункт {nomer}: статус «{status}» вне трёх разрешённых значений "
                f"({', '.join(sorted(DOPUSTIMYE_STATUSY))})"
            )
        zapisi[nomer] = zapis
    meta = dokument.get("мета") if isinstance(dokument.get("мета"), dict) else {}
    return meta, zapisi


# ---------------------------------------------------------------- проверка пункта


def _pustye_polya(zapis: dict) -> list[str]:
    pusto: list[str] = []

    def prostoe(imya: str, znachenie) -> None:
        if not str(znachenie or "").strip():
            pusto.append(imya)

    prostoe("заголовок", zapis.get("заголовок"))
    prostoe("автор.роль", _rol(zapis.get("автор")))
    prostoe("автор.модель", (zapis.get("автор") or {}).get("модель") if isinstance(zapis.get("автор"), dict) else "")
    if not (zapis.get("коммиты") or []):
        pusto.append("коммиты")
    prostoe("тест_автора", zapis.get("тест_автора"))
    prostoe("тест_проверяющего", zapis.get("тест_проверяющего"))
    prostoe("проверяющий.роль", _rol(zapis.get("проверяющий")))
    prostoe(
        "проверяющий.модель",
        (zapis.get("проверяющий") or {}).get("модель") if isinstance(zapis.get("проверяющий"), dict) else "",
    )
    prostoe("счетовод.роль", _rol(zapis.get("счетовод")))
    prostoe(
        "счетовод.модель",
        (zapis.get("счетовод") or {}).get("модель") if isinstance(zapis.get("счетовод"), dict) else "",
    )
    skeptiki = zapis.get("скептики") or []
    if len(skeptiki) != 3:
        pusto.append("скептики (нужно три)")
    else:
        for skeptik in skeptiki:
            linza = str((skeptik or {}).get("линза", "?"))
            if not _rol(skeptik):
                pusto.append(f"скептик {linza}: роль")
            if not str((skeptik or {}).get("отчёт", "") or "").strip():
                pusto.append(f"скептик {linza}: отчёт")
    if not (zapis.get("числа") or []):
        pusto.append("числа")
    vorota = zapis.get("ворота") if isinstance(zapis.get("ворота"), dict) else {}
    for imya in ("ворота_1", "ворота_2", "ворота_3"):
        prostoe(f"ворота.{imya}", vorota.get(imya))
    prostoe("статус", zapis.get("статус"))
    return pusto


def _nesovmestimosti(zapis: dict, avtory: set[str]) -> list[str]:
    narusheniya: list[str] = []
    avtor = _klyuch_roli(_rol(zapis.get("автор")))
    proveryayushchiy = _klyuch_roli(_rol(zapis.get("проверяющий")))
    schetovod = _klyuch_roli(_rol(zapis.get("счетовод")))
    if avtor and proveryayushchiy and avtor == proveryayushchiy:
        narusheniya.append("автор и проверяющий — одно лицо")
    if proveryayushchiy and proveryayushchiy in avtory:
        narusheniya.append(f"проверяющий «{_rol(zapis.get('проверяющий'))}» значится автором пункта захода")
    if schetovod and schetovod in avtory:
        narusheniya.append(f"счетовод «{_rol(zapis.get('счетовод'))}» значится автором пункта захода")
    for skeptik in zapis.get("скептики") or []:
        klyuch = _klyuch_roli(_rol(skeptik))
        if not klyuch:
            continue
        linza = str((skeptik or {}).get("линза", "?"))
        if klyuch == avtor:
            narusheniya.append(f"скептик {linza} совпадает с автором")
        if klyuch == proveryayushchiy:
            narusheniya.append(f"скептик {linza} совпадает с проверяющим")
    return narusheniya


def _poryadok_vorot(zapis: dict) -> list[str]:
    vorota = zapis.get("ворота") if isinstance(zapis.get("ворота"), dict) else {}
    pervye = str(vorota.get("ворота_1", "") or "").strip()
    vtorye = str(vorota.get("ворота_2", "") or "").strip()
    tretyi = str(vorota.get("ворота_3", "") or "").strip()
    narusheniya: list[str] = []
    if vtorye and not pervye:
        narusheniya.append("ворота 2 заполнены при пустых воротах 1")
    if tretyi and not vtorye:
        narusheniya.append("ворота 3 заполнены при пустых воротах 2")
    return narusheniya


def _proverit_otchety_skeptikov(zapis: dict, repo: Path) -> list[str]:
    bedy: list[str] = []
    for skeptik in zapis.get("скептики") or []:
        linza = str((skeptik or {}).get("линза", "?"))
        put_stroka = str((skeptik or {}).get("отчёт", "") or "").strip()
        if not put_stroka:
            continue
        put = Path(put_stroka)
        if not put.is_absolute():
            put = repo / put
        if not put.exists():
            bedy.append(f"отчёт скептика {linza} не найден: {put_stroka}")
            continue
        soderzhimoe = put.read_text(encoding="utf-8", errors="replace").strip()
        if not soderzhimoe:
            bedy.append(f"отчёт скептика {linza} пуст: {put_stroka}")
            continue
        if not any(marker in soderzhimoe for marker in MARKERY_SKEPTIKA):
            bedy.append(
                f"отчёт скептика {linza} без конкретного входа и без перечня попыток "
                f"(нет ни строки «Вход:», ни строки «Попытка»)"
            )
    return bedy


def _proverit_chisla(zapis: dict, repo: Path) -> tuple[list[str], str]:
    bedy: list[str] = []
    kuski: list[str] = []
    for chislo in zapis.get("числа") or []:
        chto = str((chislo or {}).get("что", "") or "").strip() or "(без названия)"
        bylo = str((chislo or {}).get("было", "") or "").strip()
        stalo = str((chislo or {}).get("стало", "") or "").strip()
        komanda = str((chislo or {}).get("команда", "") or "").strip()
        kuski.append(f"{chto}: {bylo or '—'} → {stalo or '—'}")
        if not komanda:
            bedy.append(f"число «{chto}» без команды — считается неизмеренным")
            continue
        if not stalo:
            bedy.append(f"число «{chto}»: поле «стало» пусто")
            continue
        try:
            gotovo = subprocess.run(
                komanda,
                cwd=str(repo),
                shell=True,
                capture_output=True,
                text=True,
                timeout=TAYMAUT_KOMANDY,
            )
        except subprocess.TimeoutExpired:
            bedy.append(f"число «{chto}»: команда счетовода не уложилась в {TAYMAUT_KOMANDY} с")
            continue
        if gotovo.returncode != 0:
            bedy.append(f"число «{chto}»: команда счетовода не выполняется — считается неизмеренным")
            continue
        poluchennoe = gotovo.stdout.strip()
        if poluchennoe != stalo:
            bedy.append(f"число «{chto}»: записано {stalo}, команда даёт {poluchennoe or '(пусто)'}")
    return bedy, "; ".join(kuski) if kuski else "—"


def _proverit_test_zelenyy(nodeid: str, repo: Path, dsn: str, chey: str) -> list[str]:
    if "::" not in nodeid:
        return [f"тест {chey} записан не в виде «файл::имя»: {nodeid}"]
    fayl = nodeid.split("::", 1)[0]
    if not (repo / fayl).exists():
        return [f"файла теста {chey} нет: {fayl}"]
    zelenyy, vyvod = _prognat_test(nodeid, repo, dsn)
    if not zelenyy:
        return [f"тест {chey} на текущем состоянии не зелёный: {_stroka_padeniya(vyvod)}"]
    return []


def ocenit_punkt(nomer: str, zapis: dict | None, avtory: set[str], repo: Path, dsn: str, otkat: Otkat) -> Rezultat:
    if zapis is None:
        return Rezultat(nomer, NE_ZAKRYT, prichiny=["записи нет в реестре"])

    zayavlennyy_status = str(zapis.get("статус", "") or "").strip()
    kto_proveril = _kto(zapis.get("проверяющий"))

    if zayavlennyy_status == OSTANOVLEN:
        return Rezultat(
            nomer,
            OSTANOVLEN,
            kto_proveril=kto_proveril,
            prichiny=["остановлен по процедуре «упёрся», ждёт ответа владельца"],
        )

    prichiny: list[str] = []

    pusto = _pustye_polya(zapis)
    prichiny.extend(f"поле «{imya}» пусто" for imya in pusto)
    prichiny.extend(_nesovmestimosti(zapis, avtory))
    prichiny.extend(_poryadok_vorot(zapis))

    if pusto:
        # Дальше идти некуда: нет ни коммитов, ни имён тестов, ни отчётов.
        return Rezultat(nomer, NE_ZAKRYT, kto_proveril=kto_proveril, prichiny=prichiny)

    prichiny.extend(_proverit_otchety_skeptikov(zapis, repo))
    bedy_chisel, chisla_stroka = _proverit_chisla(zapis, repo)
    prichiny.extend(bedy_chisel)

    test_avtora = str(zapis.get("тест_автора", "")).strip()
    test_proveryayushchego = str(zapis.get("тест_проверяющего", "")).strip()

    prichiny.extend(_proverit_test_zelenyy(test_avtora, repo, dsn, "автора"))
    prichiny.extend(_proverit_test_zelenyy(test_proveryayushchego, repo, dsn, "проверяющего"))

    upavshie: list[str] = []
    stroki_padeniya: list[str] = []
    kommity = [str(k).strip() for k in (zapis.get("коммиты") or []) if str(k).strip()]
    poluchilos, prichina_otkata, vershina = otkat.otkatit(kommity)
    if not poluchilos:
        prichiny.append(prichina_otkata)
    else:
        otkat.vernut_fayly_testov(vershina, [test_avtora, test_proveryayushchego])
        for nodeid, chey in ((test_avtora, "автора"), (test_proveryayushchego, "проверяющего")):
            zelenyy, vyvod = _prognat_test(nodeid, otkat.derevo, dsn)
            if zelenyy:
                prichiny.append(f"при откате коммитов пункта тест {chey} ({nodeid}) не покраснел")
            else:
                upavshie.append(nodeid)
                stroki_padeniya.append(f"{nodeid} → {_stroka_padeniya(vyvod)}")

    status = NE_ZAKRYT if prichiny else ZAKRYT
    return Rezultat(
        nomer,
        status,
        " ;; ".join(upavshie) if upavshie else "—",
        kto_proveril,
        chisla_stroka,
        prichiny,
        stroki_padeniya,
    )


# ---------------------------------------------------------------- условия 2.4


def _hvost_pytest(vyvod: str) -> str:
    stroki = [s.strip() for s in vyvod.splitlines() if s.strip()]
    return stroki[-1] if stroki else "(пустой вывод)"


def proverit_usloviya_2_4(meta: dict, repo: Path, dsn: str) -> tuple[list[str], list[str]]:
    """Четыре условия приёмки раздела 2.4. Возвращает (беды, строки отчёта)."""
    bedy: list[str] = []
    otchet: list[str] = []

    # 1. Полный прогон набора тестов на настоящей базе.
    try:
        gotovo = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-q", "-rs", "-p", "no:cacheprovider"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=3600,
            env=_okruzhenie_testov(dsn),
        )
        vyvod = (gotovo.stdout or "") + (gotovo.stderr or "")
        hvost = _hvost_pytest(vyvod)
        otchet.append(f"2.4/1 полный прогон: {hvost}")
        if gotovo.returncode != 0:
            bedy.append(f"полный прогон тестов не зелёный: {hvost}")
        proshlo = _vydernut_chislo(hvost, "passed")
        propushcheno_bez_bazy = [
            s.strip()
            for s in vyvod.splitlines()
            if s.strip().startswith("SKIPPED")
            and any(slovo in s.lower() for slovo in ("dsn", "postgres", "база", "базы"))
        ]
        if propushcheno_bez_bazy:
            bedy.append(
                f"тестов, пропущенных по причине «нет базы»: {len(propushcheno_bez_bazy)} (обязано быть 0)"
            )
        bylo = meta.get("тестов_до_захода")
        if isinstance(bylo, int) and proshlo is not None and proshlo < bylo:
            bedy.append(f"выполненных тестов стало меньше: было {bylo}, стало {proshlo}")
    except subprocess.TimeoutExpired:
        bedy.append("полный прогон тестов не уложился в час")

    # 2. Ни одного пуша в удалённый репозиторий.
    osnova = str(meta.get("основа", "") or "").strip()
    if not osnova:
        bedy.append("в реестре не записана основа (коммит origin/main до начала захода)")
    else:
        udalennyy = _git(["ls-remote", "origin", "refs/heads/main"], repo, 120)
        if udalennyy.returncode != 0:
            bedy.append("не удалось прочитать origin: проверить «ни одного пуша» нечем")
        else:
            hesh = (udalennyy.stdout.split() or [""])[0]
            otchet.append(f"2.4/4 origin/main: {hesh}")
            if not hesh.startswith(osnova):
                bedy.append(f"origin/main сдвинулся: было {osnova}, стало {hesh} — это пуш")

    # 3. Отчёт стража формата.
    strazh = str(meta.get("отчёт_стража_формата", "") or "").strip()
    if not strazh:
        bedy.append("в реестре не указан отчёт стража формата")
    else:
        put = repo / strazh if not Path(strazh).is_absolute() else Path(strazh)
        if not put.exists() or not put.read_text(encoding="utf-8", errors="replace").strip():
            bedy.append(f"отчёта стража формата нет или он пуст: {strazh}")
        elif "не заявлено" in put.read_text(encoding="utf-8", errors="replace"):
            bedy.append("в отчёте стража формата есть незаявленное расхождение")
        else:
            otchet.append(f"2.4/2 страж формата: {strazh}")

    # 4. Семь слепых тестов.
    slepye = str(meta.get("подтверждение_семи_слепых", "") or "").strip()
    if not slepye:
        bedy.append("в реестре не указано подтверждение по семи слепым тестам")
    else:
        put = repo / slepye if not Path(slepye).is_absolute() else Path(slepye)
        if not put.exists():
            bedy.append(f"подтверждения по семи слепым тестам нет: {slepye}")
        else:
            text = put.read_text(encoding="utf-8", errors="replace")
            nedostayushchie = [n for n in range(1, 8) if f"слепой {n}:" not in text]
            if nedostayushchie:
                bedy.append(
                    "в подтверждении по слепым тестам нет строк «слепой N:» для номеров: "
                    + ", ".join(str(n) for n in nedostayushchie)
                )
            else:
                otchet.append(f"2.4/3 семь слепых тестов: {slepye}")

    return bedy, otchet


def _vydernut_chislo(stroka: str, slovo: str) -> int | None:
    chasti = stroka.replace(",", " ").split()
    for indeks, kusok in enumerate(chasti):
        if kusok.startswith(slovo) and indeks > 0:
            try:
                return int(chasti[indeks - 1])
            except ValueError:
                return None
    return None


# ---------------------------------------------------------------- печать


def napechatat_tablicu(rezultaty: list[Rezultat]) -> None:
    zagolovki = ["Пункт", "Статус", "Тест, упавший при откате (файл::имя)", "Кто проверил", "Числа до → после"]
    stroki = [
        [r.nomer, r.status, r.upavshiy_test, r.kto_proveril, r.chisla]
        for r in rezultaty
    ]
    shiriny = [
        max(len(zagolovki[i]), *(len(s[i]) for s in stroki)) if stroki else len(zagolovki[i])
        for i in range(len(zagolovki))
    ]
    def sobrat(yacheyki: list[str]) -> str:
        return " | ".join(yacheyka.ljust(shiriny[i]) for i, yacheyka in enumerate(yacheyki)).rstrip()

    print(sobrat(zagolovki))
    print("-+-".join("-" * sh for sh in shiriny))
    for stroka in stroki:
        print(sobrat(stroka))


# ---------------------------------------------------------------- вход


def prognat(dsn: str | None, repo: Path, put_reestra: Path) -> int:
    if not dsn:
        print(
            "ПРОГОН НЕ ВЫПОЛНЕН: не задан --dsn. Прогон запускается только на настоящей базе; "
            "таблица не печатается, иначе неполный прогон выглядел бы как сдача.",
            file=sys.stderr,
        )
        return KOD_BEZ_BAZY
    try:
        import psycopg2  # noqa: PLC0415 — импорт по месту: без базы прогон вообще не идёт
    except ImportError:
        print("ПРОГОН НЕ ВЫПОЛНЕН: нет psycopg2, подключиться к базе нечем. Таблица не печатается.", file=sys.stderr)
        return KOD_BEZ_BAZY
    try:
        soedinenie = psycopg2.connect(dsn)
        with soedinenie.cursor() as kursor:
            kursor.execute("SELECT 1")
            kursor.fetchone()
        soedinenie.close()
    except Exception as oshibka:  # noqa: BLE001 — любая беда с базой означает одно и то же
        print(
            f"ПРОГОН НЕ ВЫПОЛНЕН: к базе подключиться не удалось ({oshibka.__class__.__name__}: {oshibka}). "
            "Таблица не печатается.",
            file=sys.stderr,
        )
        return KOD_BEZ_BAZY

    meta, zapisi = zagruzit_reestr(put_reestra)

    avtory = {
        _klyuch_roli(_rol(zapis.get("автор")))
        for zapis in zapisi.values()
        if _klyuch_roli(_rol(zapis.get("автор")))
    }

    do_progona = _snimok_dereva(repo)

    otkat = Otkat(repo)
    try:
        rezultaty = [
            ocenit_punkt(nomer, zapisi.get(nomer), avtory, repo, dsn, otkat)
            for nomer in NOMERA
        ]
    finally:
        otkat.zakryt()

    posle_progona = _snimok_dereva(repo)
    if posle_progona != do_progona:
        raise AvariynayaOstanovka(
            "рабочее дерево после прогона не совпадает с исходным "
            f"(было {do_progona}, стало {posle_progona})"
        )

    napechatat_tablicu(rezultaty)
    print()

    # Строки вывода прогона тестов при откате — взяты из вывода, а не из реестра.
    s_padeniyami = [r for r in rezultaty if r.stroki_padeniya]
    if s_padeniyami:
        print("Строки вывода прогона при откате:")
        for rezultat in s_padeniyami:
            for stroka in rezultat.stroki_padeniya:
                print(f"  {rezultat.nomer}: {stroka}")
        print()

    ne_zakryty = [r for r in rezultaty if r.status == NE_ZAKRYT]
    ostanovleny = [r for r in rezultaty if r.status == OSTANOVLEN]

    if ne_zakryty or ostanovleny:
        print("ЗАХОД 1 НЕ СДАН")
        print("Не закрыты: " + (", ".join(r.nomer for r in ne_zakryty) if ne_zakryty else "—"))
        print(
            "Остановлены и возвращены владельцу: "
            + (", ".join(r.nomer for r in ostanovleny) if ostanovleny else "—")
        )
        for rezultat in ne_zakryty + ostanovleny:
            prichina = rezultat.prichiny[0] if rezultat.prichiny else "причина не установлена"
            if len(rezultat.prichiny) > 1:
                prichina += f" (и ещё {len(rezultat.prichiny) - 1})"
            print(f"{rezultat.nomer}: {prichina}")
        return KOD_NE_SDAN

    bedy, otchet = proverit_usloviya_2_4(meta, repo, dsn)
    for stroka in otchet:
        print(stroka)
    if bedy:
        print("ЗАХОД 1 НЕ СДАН")
        print("Не закрыты: —")
        print("Остановлены и возвращены владельцу: —")
        for beda in bedy:
            print(f"условия 2.4: {beda}")
        return KOD_NE_SDAN

    print("ЗАХОД 1 СДАН")
    return KOD_SDAN


def main(argv: list[str] | None = None) -> int:
    razbor = argparse.ArgumentParser(
        prog="python -m zahod1.check",
        description="Прогон сдачи захода 1: статус каждого пункта вычисляется, а не читается.",
    )
    razbor.add_argument("--dsn", default=None, help="строка подключения к настоящей базе Postgres")
    razbor.add_argument("--repo", default=str(KOREN), help="корень репозитория (по умолчанию — этот)")
    razbor.add_argument("--reestr", default=None, help="путь к реестру (по умолчанию zahod1/reestr.yaml)")
    argumenty = razbor.parse_args(argv)

    repo = Path(argumenty.repo).resolve()
    put_reestra = Path(argumenty.reestr).resolve() if argumenty.reestr else repo / "zahod1" / "reestr.yaml"

    try:
        return prognat(argumenty.dsn, repo, put_reestra)
    except AvariynayaOstanovka as oshibka:
        print(f"АВАРИЙНАЯ ОСТАНОВКА ПРОГОНА: {oshibka}", file=sys.stderr)
        return KOD_AVARIYA


if __name__ == "__main__":
    sys.exit(main())
