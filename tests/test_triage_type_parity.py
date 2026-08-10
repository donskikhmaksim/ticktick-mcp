"""Реестр типов операций агрегатора: полнота набора обработчиков (1.3.3/изм-1).

Что здесь закреплено:
  1. у КАЖДОГО типа из `_TRIAGE_OPS` в реестре есть все пять обработчиков —
     валидатор входа, сверка плана, сверка исполнения, независимая
     перепроверка, исполнитель; ни одного None;
  2. списки имён/эмодзи/глаголов ВЫВЕДЕНЫ из реестра, а не написаны рядом
     руками: второй список был бы вторым источником правды, и тип, попавший
     только в него, проехал бы мимо инварианта;
  3. инвариант полноты стоит НА ИМПОРТЕ, а не только в этом файле: тип без
     обработчика роняет сервер на старте, а сообщение называет и тип, и
     недостающий обработчик поимённо.

Пункт 3 проверяется двумя разными способами намеренно. Прямой вызов
`_triage_registry_check` доказывает, что проверка работает; разбор исходника
доказывает, что она ВЫЗВАНА на уровне модуля — то есть при импорте, а не
«когда-нибудь в тесте». Убери вызов из модуля — и первый тест останется
зелёным, а сервер начнёт молча подниматься с недоделанным типом.
"""
import ast
import inspect

import pytest

import ticktick_mcp.src.server as s


def test_every_type_has_five_handlers():
    """ТЗ 1.3.3, пункт 2 приёмки: для каждого элемента `_TRIAGE_OPS` в реестре
    есть валидатор, сверка плана, сверка исполнения, исполнитель и
    перепроверка; ни одного None."""
    assert set(s._TRIAGE_BY_OP) == set(s._TRIAGE_OPS)
    for op in s._TRIAGE_OPS:
        entry = s._TRIAGE_BY_OP[op]
        for field, human in s._TRIAGE_HANDLER_FIELDS:
            handler = getattr(entry, field, None)
            assert callable(handler), (
                f"тип {op!r}: обработчик «{human}» ({field}) = {handler!r}")


def test_lists_are_derived_from_the_registry_not_hand_written():
    """Имена, эмодзи, глаголы и ранг — производные реестра. Расхождение между
    ними и реестром означало бы тип, который где-то есть, а где-то нет."""
    assert s._TRIAGE_OPS == tuple(t.op for t in s._TRIAGE_REGISTRY)
    assert s._TRIAGE_ORDER == {t.op: i for i, t in enumerate(s._TRIAGE_REGISTRY)}
    assert s._TRIAGE_EMOJI == {t.op: t.emoji for t in s._TRIAGE_REGISTRY}
    assert s._TRIAGE_VERB == {t.op: t.verb for t in s._TRIAGE_REGISTRY}
    assert all(t.emoji and t.verb for t in s._TRIAGE_REGISTRY)


@pytest.mark.parametrize("field,human", list(s._TRIAGE_HANDLER_FIELDS))
def test_type_without_a_handler_refuses_to_start(field, human):
    """Проверка откатом из ТЗ: убери из реестра ЛЮБОЙ из пяти обработчиков —
    и сервер не поднимается, а сообщение называет тип и что именно потеряно."""
    crippled = s._TRIAGE_REGISTRY[0]._replace(**{field: None})
    with pytest.raises(RuntimeError) as err:
        s._triage_registry_check((crippled,))
    assert crippled.op in str(err.value)
    assert human in str(err.value)


def test_duplicate_type_in_the_registry_refuses_to_start():
    """Две строки на один тип — это вопрос «какая из них сверяет», ответ на
    который давал бы порядок объявления, а не решение человека."""
    with pytest.raises(RuntimeError) as err:
        s._triage_registry_check(
            (s._TRIAGE_REGISTRY[0], s._TRIAGE_REGISTRY[0]))
    assert s._TRIAGE_REGISTRY[0].op in str(err.value)


def test_invariant_is_called_at_import_time():
    """ТЗ 1.3.3, пункт 3: инвариант — НА ИМПОРТЕ, а не в тесте.

    Разбирается сам исходник модуля: на верхнем уровне обязан стоять вызов
    `_triage_registry_check(_TRIAGE_REGISTRY)`. Внутри функции или в тесте он
    выполнялся бы только когда его позовут, — а недоделанный тип обязан
    ронять сервер на старте."""
    tree = ast.parse(inspect.getsource(s))
    calls = [n for n in tree.body
             if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
             and isinstance(n.value.func, ast.Name)
             and n.value.func.id == "_triage_registry_check"]
    assert len(calls) == 1, (
        "на уровне модуля должен стоять ровно один вызов "
        "_triage_registry_check — иначе инвариант не на импорте")
    assert [a.id for a in calls[0].value.args if isinstance(a, ast.Name)] \
        == ["_TRIAGE_REGISTRY"]
