"""Минимальный разборщик и записыватель подмножества YAML.

Зачем свой, а не PyYAML: прогон обязан воспроизводиться владельцем ОДНОЙ
командой на его машине, а PyYAML в окружении сервера не установлен и ставить
его ради сдачи — значит менять зависимости продукта. Поэтому разбирается
строго то подмножество, в котором написан реестр, а всё остальное — ошибка
разбора, а не тихое игнорирование.

Поддерживается ровно:
  ключ: значение
  ключ:            (вложенный блок ниже, с отступом)
  ключ: []         (пустой список)
  ключ: {}         (пустая карта)
  - "скаляр"       (элемент списка — обязательно в кавычках)
  - ключ: значение (элемент списка — карта; продолжение с тем же отступом)
  # комментарий    (только целой строкой)

Отступы — только пробелы. Табуляция — ошибка.
"""

from __future__ import annotations


class OshibkaRazbora(Exception):
    """Реестр написан не на том подмножестве, которое разбирается."""


def _podgotovit(text: str) -> list[tuple[int, int, str]]:
    """Строки в виде (номер строки, отступ, содержимое) без пустых и комментариев."""
    gotovo: list[tuple[int, int, str]] = []
    for nomer, syraya in enumerate(text.splitlines(), 1):
        if "\t" in syraya:
            raise OshibkaRazbora(f"строка {nomer}: табуляция в отступе запрещена")
        bez_otstupa = syraya.lstrip(" ")
        if not bez_otstupa.strip() or bez_otstupa.startswith("#"):
            continue
        gotovo.append((nomer, len(syraya) - len(bez_otstupa), bez_otstupa.rstrip()))
    return gotovo


def _skalyar(syroy: str, nomer: int):
    """Значение справа от двоеточия или элемент списка."""
    znachenie = syroy.strip()
    if znachenie == "":
        return ""
    if znachenie == "[]":
        return []
    if znachenie == "{}":
        return {}
    if znachenie.startswith('"'):
        if not znachenie.endswith('"') or len(znachenie) < 2:
            raise OshibkaRazbora(f"строка {nomer}: незакрытая кавычка")
        vnutri = znachenie[1:-1]
        return vnutri.replace('\\"', '"').replace("\\\\", "\\")
    if znachenie.lstrip("-").isdigit():
        return int(znachenie)
    return znachenie


def _razobrat_blok(stroki: list[tuple[int, int, str]], poz: int, otstup: int):
    """Разобрать блок с данным отступом. Возвращает (значение, новая позиция)."""
    if poz >= len(stroki):
        return "", poz
    if stroki[poz][2].startswith("- "):
        return _razobrat_spisok(stroki, poz, otstup)
    return _razobrat_kartu(stroki, poz, otstup)


def _razobrat_spisok(stroki: list[tuple[int, int, str]], poz: int, otstup: int):
    spisok: list = []
    while poz < len(stroki):
        nomer, tek_otstup, soderzhimoe = stroki[poz]
        if tek_otstup < otstup:
            break
        if tek_otstup > otstup:
            raise OshibkaRazbora(f"строка {nomer}: неожиданный отступ внутри списка")
        if not soderzhimoe.startswith("- "):
            break
        hvost = soderzhimoe[2:]
        if hvost.startswith('"') or ":" not in hvost:
            spisok.append(_skalyar(hvost, nomer))
            poz += 1
            continue
        # Элемент-карта: первая пара живёт на строке с дефисом, остальные — ниже
        # с отступом на два больше.
        virtualnye = [(nomer, otstup + 2, hvost)]
        poz += 1
        while poz < len(stroki) and stroki[poz][1] > otstup:
            virtualnye.append(stroki[poz])
            poz += 1
        znachenie, konec = _razobrat_kartu(virtualnye, 0, otstup + 2)
        if konec != len(virtualnye):
            raise OshibkaRazbora(f"строка {nomer}: элемент списка разобран не целиком")
        spisok.append(znachenie)
    return spisok, poz


def _razobrat_kartu(stroki: list[tuple[int, int, str]], poz: int, otstup: int):
    karta: dict = {}
    while poz < len(stroki):
        nomer, tek_otstup, soderzhimoe = stroki[poz]
        if tek_otstup < otstup:
            break
        if tek_otstup > otstup:
            raise OshibkaRazbora(f"строка {nomer}: неожиданный отступ внутри карты")
        if soderzhimoe.startswith("- "):
            break
        if ":" not in soderzhimoe:
            raise OshibkaRazbora(f"строка {nomer}: ожидалась пара «ключ: значение»")
        klyuch, _, hvost = soderzhimoe.partition(":")
        klyuch = klyuch.strip()
        if not klyuch:
            raise OshibkaRazbora(f"строка {nomer}: пустой ключ")
        if klyuch in karta:
            raise OshibkaRazbora(f"строка {nomer}: ключ «{klyuch}» повторяется")
        poz += 1
        if hvost.strip():
            karta[klyuch] = _skalyar(hvost, nomer)
            continue
        # Значение — блок ниже (или пусто, если ниже ничего нет с бо́льшим отступом).
        if poz < len(stroki) and stroki[poz][1] > otstup:
            karta[klyuch], poz = _razobrat_blok(stroki, poz, stroki[poz][1])
        else:
            karta[klyuch] = ""
    return karta, poz


def zagruzit(text: str):
    """Разобрать текст в питоновские словари/списки/строки."""
    stroki = _podgotovit(text)
    if not stroki:
        return {}
    znachenie, poz = _razobrat_blok(stroki, 0, stroki[0][1])
    if poz != len(stroki):
        nomer = stroki[poz][0]
        raise OshibkaRazbora(f"строка {nomer}: документ разобран не целиком")
    return znachenie


def _v_skalyar(znachenie) -> str:
    if isinstance(znachenie, bool):
        raise OshibkaRazbora("логические значения в реестре не используются")
    if isinstance(znachenie, int):
        return str(znachenie)
    text = "" if znachenie is None else str(znachenie)
    ekranirovannoe = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{ekranirovannoe}"'


def sohranit(znachenie, otstup: int = 0) -> str:
    """Записать структуру обратно в то же подмножество YAML."""
    probely = " " * otstup
    kuski: list[str] = []
    if isinstance(znachenie, dict):
        if not znachenie:
            return probely + "{}\n"
        for klyuch, vnutri in znachenie.items():
            if isinstance(vnutri, (dict, list)) and vnutri:
                kuski.append(f"{probely}{klyuch}:\n")
                kuski.append(sohranit(vnutri, otstup + 2))
            elif isinstance(vnutri, list):
                kuski.append(f"{probely}{klyuch}: []\n")
            elif isinstance(vnutri, dict):
                kuski.append(f"{probely}{klyuch}: {{}}\n")
            else:
                kuski.append(f"{probely}{klyuch}: {_v_skalyar(vnutri)}\n")
        return "".join(kuski)
    if isinstance(znachenie, list):
        if not znachenie:
            return probely + "[]\n"
        for element in znachenie:
            if isinstance(element, dict):
                if not element:
                    raise OshibkaRazbora("пустая карта элементом списка не записывается")
                telo = sohranit(element, otstup + 2)
                stroki_tela = telo.splitlines(keepends=True)
                pervaya = stroki_tela[0]
                kuski.append(probely + "- " + pervaya.lstrip(" "))
                kuski.extend(stroki_tela[1:])
            elif isinstance(element, list):
                raise OshibkaRazbora("список внутри списка не записывается")
            else:
                kuski.append(f"{probely}- {_v_skalyar(element)}\n")
        return "".join(kuski)
    return f"{probely}{_v_skalyar(znachenie)}\n"
