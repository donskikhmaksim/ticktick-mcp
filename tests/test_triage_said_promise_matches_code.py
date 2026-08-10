"""Что докстринг apply_task_changes обещает про подставное `said` — то код и делает.

Дефект (живая приёмка 2026-08-07): расхождение обещанного и фактического.
Поле `said` обязано нести дословные слова человека про КОНКРЕТНУЮ задачу.
Докстринг про фразу-заглушку, размноженную на все строки, писал «is a
protocol violation» — читается как «сервер такое отвергнет». Код же
отвергает только ПУСТОЕ `said`, а на повтор отвечает предупреждением
(`_triage_plan_notes`) и строит план.

Тест написан ДВУСТОРОННИМ намеренно: он сверяет обещание докстринга с
поведением, а не закрепляет одну из сторон. Усилить код до отказа —
проходит; смягчить докстринг до предупреждения — тоже. Красным он остаётся
ровно в одном случае: когда текст и код говорят разное.

Факт берётся живым вызовом через `tests/read_stand.py` (настоящие клиенты,
подменён только транспорт, тул зовётся по имени), а не чтением
`_triage_plan_notes`: подменять или читать ту самую функцию, вокруг которой
идёт спор, — значит проверять её же саму.

Календарных привязок нет: обоснования и задачи от часов не зависят.
"""
import re

import pytest

import ticktick_mcp.src.server as s
from tests.read_stand import TASK_MID, TASK_ROOT, call, wire

# Маркеры в тексте докстринга. «refuse/refused/rejected/violation» — обещание
# отказа; «warning/warns/flagged» — обещание предупреждения.
_PROMISES_REFUSAL = re.compile(r"refus|reject|violation", re.I)
_PROMISES_WARNING = re.compile(r"warn|flag", re.I)


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


def _sentences_about_repeated_said() -> list:
    """Куски докстринга, говорящие про ОДНУ И ТУ ЖЕ фразу на нескольких
    строках. Ищутся по смыслу («blanket», «reused», «same … said»), а не по
    одной заученной формулировке, чтобы переписанный текст всё равно попал
    под проверку."""
    doc = " ".join((s.apply_task_changes.__doc__ or "").split())
    hits = []
    for sentence in re.split(r"(?<=[.!?])\s+", doc):
        low = sentence.lower()
        if ("blanket" in low or "reused on every row" in low
                or ("same" in low and "said" in low and "row" in low)):
            hits.append(sentence)
    return hits


def _doc_promises_refusal_for_repeated_said() -> bool:
    text = " ".join(_sentences_about_repeated_said())
    return bool(_PROMISES_REFUSAL.search(text))


async def _plan_with_one_phrase_for_two_tasks() -> str:
    return await call(
        "apply_task_changes", summary="Закрываю по одной фразе",
        operations=[
            {"op": "complete", "task_id": TASK_ROOT, "title": "Собрать отчёт",
             "said": "эти две уже сделаны"},
            {"op": "complete", "task_id": TASK_MID, "title": "Записаться к врачу",
             "said": "эти две уже сделаны"}])


def test_the_docstring_still_talks_about_the_repeated_phrase_at_all():
    """Страж разбора: если абзац про фразу-заглушку исчезнет из докстринга,
    сравнение ниже пойдёт по пустой строке и станет проходить впустую."""
    assert _sentences_about_repeated_said(), (
        "в докстринге apply_task_changes больше нет ни слова про одну и ту же "
        "фразу на нескольких строках — сравнение обещания с кодом обесценено")


async def test_promise_about_a_placeholder_phrase_matches_what_the_code_does():
    preview = await _plan_with_one_phrase_for_two_tasks()

    refused = "🛑" in preview and "Манифест" not in preview
    promised = _doc_promises_refusal_for_repeated_said()

    assert promised == refused, (
        "докстринг и код говорят разное про подставную фразу в `said`.\n"
        f"докстринг обещает ОТКАЗ: {promised} "
        f"(«{' '.join(_sentences_about_repeated_said())}»)\n"
        f"код отказывает: {refused}\n"
        f"ответ сервера:\n{preview}")


def test_the_docstring_names_the_measure_it_actually_takes():
    """Мало не обещать лишнего — надо сказать, что БУДЕТ. Иначе поле выглядит
    непроверяемым вовсе, и следующий читатель снова додумает отказ."""
    text = " ".join(_sentences_about_repeated_said())
    assert _PROMISES_WARNING.search(text), (
        "докстринг не называет меру, которую сервер реально принимает "
        f"(предупреждение в превью): «{text}»")


async def test_the_warning_the_docstring_promises_is_really_printed():
    """Вторая половина того же согласия: обещанное предупреждение обязано
    реально попасть в текст плана."""
    preview = await _plan_with_one_phrase_for_two_tasks()

    assert "⚠️" in preview and "обоснование" in preview, preview
    assert re.search(r"Манифест `[0-9a-f]+`", preview), (
        f"план не построен, хотя это предупреждение, а не отказ:\n{preview}")


async def test_empty_said_is_still_refused_outright():
    """Контроль границы: то, что докстринг обещает отвергать, отвергается.
    Без этой стороны «согласие» можно было бы получить, разрешив всё."""
    out = await call(
        "apply_task_changes", summary="Закрываю",
        operations=[{"op": "complete", "task_id": TASK_ROOT,
                     "title": "Собрать отчёт", "said": "  "}])

    assert "🛑" in out and "said" in out, out
    assert "Манифест" not in out, "на пустом said манифест создаваться не должен"
