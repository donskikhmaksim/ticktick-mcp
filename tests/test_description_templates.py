"""Сторож описаний (docstring) MCP-команд — 2026-08-09, ZAHOD1.md 1.2.1.

Абзацы TG_NOTE / AUTOMATION_NOTE и строки-аргументы ARG_USER_REPLY /
ARG_AUTOMATION_KEY скопированы вручную в докстринги десятков команд
(plan_* / execute_* с двухшаговым подтверждением). Копипаста уже разъехалась
в двух местах — это измеренный факт, не гипотеза: `create_tasks` потеряла
абзац про запрет подставлять `automation_key`, `manual_triage` описывает
Telegram-кнопку своими словами. Пока копий больше одной, следующее
расхождение появится тем же путём: кто-то поправит текст в N местах и
забудет в N+1-м, и заметить это нечем — до этого файла.

Дизайн важен для того, чтобы сторож не подделывался по-тихому (см.
ZAHOD1.md, раздел «Чем это можно подделать» для 1.2.1):

- список проверяемых команд берётся из РЕЕСТРА сервера
  (`server.mcp.list_tools()`), а не из зашитого вручную перечня имён —
  иначе можно «забыть» внести расходящуюся команду в список;
- команда попадает под проверку по НАЛИЧИЮ якорной подстроки в самом её
  описании — то же самое правило исключает пропуск;
- эталон — текст, встречающийся у БОЛЬШИНСТВА команд (мода), а не у первой
  попавшейся — иначе можно набрать эталон тем же обходом, каким собирается
  список кандидатов, и получить тест, зелёный сам с собой;
- сравнение — на РАВЕНСТВО блока целиком, а не на присутствие подстроки
  (проверка вида «якорь входит в текст описания» пропустит оба известных
  расхождения: якорная фраза есть в тексте обеих разъехавшихся команд,
  разъехалось только продолжение).
"""
import asyncio
import collections

from ticktick_mcp.src import server


# Абзац про Telegram-approval-слой (когда он включён на сервере).
TG_NOTE_ANCHOR = "Telegram approval layer (when it is enabled"

# Абзац про то, что automation_key — только для headless-автоматизации.
AUTOMATION_NOTE_ANCHOR = "automation_key is ONLY for headless"

# Строка описания аргумента user_reply в Args:.
USER_REPLY_ARG_ANCHOR = "user_reply: the user's literal reply approving the plan"

# Строка описания аргумента automation_key в Args:.
AUTOMATION_KEY_ARG_ANCHOR = "automation_key: headless-automation only"


def _tools():
    """Список зарегистрированных MCP-команд из живого реестра сервера —
    тот же источник, на который ссылается tests/test_tool_registry.py."""
    return asyncio.run(server.mcp.list_tools())


def _extract_paragraph(description, anchor):
    """Абзац целиком: от начала строки, содержащей anchor, до пустой строки
    (двойной перевод строки) или конца описания. Хвостовые пробелы/переводы
    строк не значимы (в докстринге абзац иногда упирается прямо в закрывающие
    тройные кавычки — это не отличие по содержанию, поэтому .rstrip())."""
    idx = description.find(anchor)
    if idx == -1:
        return None
    line_start = description.rfind("\n", 0, idx) + 1
    end = description.find("\n\n", idx)
    block = description[line_start:] if end == -1 else description[line_start:end]
    return block.rstrip()


def _extract_line(description, anchor):
    """Одна строка, содержащая anchor (для однострочных описаний аргументов
    в Args: — там абзацем считать нечего, следующая строка — уже другой
    аргумент)."""
    idx = description.find(anchor)
    if idx == -1:
        return None
    line_start = description.rfind("\n", 0, idx) + 1
    line_end = description.find("\n", idx)
    line = description[line_start:] if line_end == -1 else description[line_start:line_end]
    return line.rstrip()


def _collect(tools, anchor, extractor):
    """Имя команды -> извлечённый блок/строка, только для команд, где anchor
    вообще встретился (это и есть «отбор из данных» — см. докстринг модуля)."""
    found = {}
    for t in tools:
        piece = extractor(t.description or "", anchor)
        if piece is not None:
            found[t.name] = piece
    return found


def _majority_reference(pieces_by_tool):
    """Эталон = вариант текста, встречающийся у большинства команд (мода).

    Намеренно НЕ «текст первой команды из того же обхода» — так эталон был
    бы собран тем же способом, каким собирается список кандидатов, и тест
    сравнивал бы текст сам с собой (зелёный, но слепой к обоим известным
    расхождениям). Ловится это требованием «первый прогон обязан быть
    красным» — если эталон собран неверно, эти тесты не найдут ничего."""
    counts = collections.Counter(pieces_by_tool.values())
    (reference, _n), = counts.most_common(1)
    return reference


def _mismatches(pieces_by_tool, reference):
    return {name: text for name, text in pieces_by_tool.items() if text != reference}


def test_tg_note_is_identical_everywhere():
    tools = _tools()
    blocks = _collect(tools, TG_NOTE_ANCHOR, _extract_paragraph)
    # Якорь обязан находиться хоть где-то — иначе отбор молча выродился
    # в пустое множество и тест ничего бы не проверял.
    assert len(blocks) >= 2, (
        "якорь TG-абзаца не встретился ни в одной команде реестра — "
        "отбор сломан или текст абзаца изменился"
    )
    reference = _majority_reference(blocks)
    bad = _mismatches(blocks, reference)
    assert not bad, (
        f"TG_NOTE разошёлся в {len(bad)} из {len(blocks)} команд, где встретился "
        f"якорь: {sorted(bad)}.\nЭталон (текст большинства):\n{reference!r}\n"
        + "\n".join(f"{name}:\n{text!r}" for name, text in sorted(bad.items()))
    )


def test_automation_note_is_identical_everywhere():
    tools = _tools()
    blocks = _collect(tools, AUTOMATION_NOTE_ANCHOR, _extract_paragraph)
    assert len(blocks) >= 2, (
        "якорь абзаца про automation_key не встретился ни в одной команде "
        "реестра — отбор сломан или текст абзаца изменился"
    )
    reference = _majority_reference(blocks)
    bad = _mismatches(blocks, reference)
    assert not bad, (
        f"AUTOMATION_NOTE разошёлся в {len(bad)} из {len(blocks)} команд, где "
        f"встретился якорь: {sorted(bad)}.\nЭталон (текст большинства):\n{reference!r}\n"
        + "\n".join(f"{name}:\n{text!r}" for name, text in sorted(bad.items()))
    )


def test_user_reply_arg_line_is_identical_everywhere():
    tools = _tools()
    lines = _collect(tools, USER_REPLY_ARG_ANCHOR, _extract_line)
    assert len(lines) >= 2, (
        "якорь строки user_reply не встретился ни в одной команде реестра — "
        "отбор сломан или текст строки изменился"
    )
    reference = _majority_reference(lines)
    bad = _mismatches(lines, reference)
    assert not bad, (
        f"строка ARG_USER_REPLY разошлась в {len(bad)} из {len(lines)} команд, "
        f"где встретился якорь: {sorted(bad)}.\nЭталон (текст большинства):\n"
        f"{reference!r}\n" + "\n".join(f"{name}: {text!r}" for name, text in sorted(bad.items()))
    )


def test_automation_key_arg_line_is_identical_everywhere():
    tools = _tools()
    lines = _collect(tools, AUTOMATION_KEY_ARG_ANCHOR, _extract_line)
    assert len(lines) >= 2, (
        "якорь строки automation_key не встретился ни в одной команде "
        "реестра — отбор сломан или текст строки изменился"
    )
    reference = _majority_reference(lines)
    bad = _mismatches(lines, reference)
    assert not bad, (
        f"строка ARG_AUTOMATION_KEY разошлась в {len(bad)} из {len(lines)} команд, "
        f"где встретился якорь: {sorted(bad)}.\nЭталон (текст большинства):\n"
        f"{reference!r}\n" + "\n".join(f"{name}: {text!r}" for name, text in sorted(bad.items()))
    )


def test_tg_note_anchor_is_not_duplicated_within_a_description():
    """Нужно для будущего пункта 1.2.2: если абзац завернут в общую функцию,
    но исходный текст не вырезан, якорь задвоится внутри ОДНОГО описания —
    это отдельная поломка от несовпадения текста МЕЖДУ командами, и её надо
    ловить отдельно."""
    tools = _tools()
    doubled = sorted(
        t.name for t in tools if (t.description or "").count(TG_NOTE_ANCHOR) > 1
    )
    assert not doubled, f"TG-якорь встречается более одного раза в описании: {doubled}"


def test_automation_note_anchor_is_not_duplicated_within_a_description():
    tools = _tools()
    doubled = sorted(
        t.name for t in tools if (t.description or "").count(AUTOMATION_NOTE_ANCHOR) > 1
    )
    assert not doubled, f"якорь automation_key встречается более одного раза в описании: {doubled}"
