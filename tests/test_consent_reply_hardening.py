"""Ужесточение классификатора ответа согласия (аудит 2026-08-06).

Три дыры В САМОМ классификаторе — они опаснее дыры в отдельном туле, потому
что гейт согласия расширяется с 2 тулов на ~25 и дефект тиражируется на все:

  A. частичное согласие («ок, кроме последней») исполнялось как полное —
     манифест применяется ТОЛЬКО целиком, поэтому исполнялось и то, что
     человек явно исключил;
  B. отрицание искалось лишь в первых 4 токенах, хотя докстринг обещал, что
     отрицание где угодно во фразе перевешивает («да, всё верно, но подожди
     с третьей» → считалось согласием);
  C. пересказ модели («Пользователь: да», «он сказал да») проходил как
     дословная реплика человека.

Отдельный файл — чтобы не конфликтовать с параллельной работой в
tests/test_consent_gate.py.
"""
import time

import pytest

import ticktick_mcp.src.server as s


# ===========================================================================
# Дыра A — частичное согласие / оговорка
# ===========================================================================

CAVEAT_REPLIES = [
    "удали первые три, а последнюю не надо",
    "ок, кроме последней",
    "confirm, but skip the last one",
    "давай, только вторую оставь",
    "ок, только первые две",
    "да, но не третью",
    "да, все кроме созвона",
    "delete all except the last",
    "ок, исключая последнюю",
    "удали, без последней",
    "ok, all but the last one",
    "да, только молоко и хлеб",
    "ага, пропусти вторую",
]


@pytest.mark.parametrize("reply", CAVEAT_REPLIES)
def test_caveat_reply_is_never_affirmative(reply):
    assert s._is_affirmative_reply(reply) is False, reply


@pytest.mark.parametrize("reply", CAVEAT_REPLIES)
def test_caveat_reply_burns_the_plan(reply):
    """Оговорка ⇒ набор объектов другой, показанный план больше не годится:
    он должен быть аннулирован, а не «оставлен до следующей попытки»."""
    assert s._is_negative_reply(reply) is True, reply


@pytest.mark.parametrize("reply", CAVEAT_REPLIES)
def test_caveat_refusal_explains_what_to_do_next(reply):
    """Отказ обязан быть обучающим: сказать, что частичное исполнение
    невозможно и что нужно перепланировать, — а не просто «не подтверждено»."""
    reason = s._consent_refusal_reason(reply)
    assert reason, reply
    assert "🛑" in reason
    assert "заново" in reason.lower(), reason
    # Именно caveat-объяснение (а не общий отказ) для явных оговорок.
    if s._classify_consent_reply(reply).kind == "caveat":
        assert "частич" in reason.lower(), reason


def test_caveat_refusal_never_executes_a_partial_plan():
    """Сквозная проверка через сам гейт: оговорка не проходит и манифест
    сгорает — сервер не пытается угадать подмножество."""
    m = {"kind": "delete", "items": [{"taskId": "t1"}, {"taskId": "t2"}],
         "created": time.monotonic(), "plan_shown_at": time.monotonic() - 10,
         "consumed": False}
    r = s._require_consent(action="delete", tier=2, manifest=m,
                           user_reply="ок, кроме последней", min_gap=0)
    assert r.ok is False
    assert m["consumed"] is True


# ===========================================================================
# Дыра B — отрицание дальше окна tokens[:4]
# ===========================================================================

LATE_NEGATION_REPLIES = [
    "да, всё верно, но подожди с третьей",
    "ок, всё правильно, но нет",
    "да, всё так, но стоп",
    "конечно, всё верно, отмена",
    "yes, everything is right, but wait",
    "да, я посмотрел план, нельзя",
    "ок, я всё проверил, отбой",
]


@pytest.mark.parametrize("reply", LATE_NEGATION_REPLIES)
def test_negation_anywhere_in_the_sentence_outweighs_a_yes(reply):
    assert s._is_affirmative_reply(reply) is False, reply
    assert s._is_negative_reply(reply) is True, reply


@pytest.mark.parametrize("reply", LATE_NEGATION_REPLIES)
def test_late_negation_refusal_has_a_reason(reply):
    reason = s._consent_refusal_reason(reply)
    assert reason and "🛑" in reason, reply
    assert "заново" in reason.lower(), reason


def test_da_net_navernoe_still_refused():
    """Классика неоднозначности — обязана оставаться отказом."""
    assert s._is_affirmative_reply("да нет наверное") is False
    assert s._is_negative_reply("да нет наверное") is True


# ===========================================================================
# Дыра C — пересказ вместо дословной реплики
# ===========================================================================

PARAPHRASE_REPLIES = [
    "Пользователь: да",
    "юзер: ок",
    "он сказал да",
    "она сказала ок",
    "он ответил да",
    "yes (по словам пользователя)",
    "user: yes",
    "the user said yes",
    "пользователь подтвердил",
    "he confirmed",
]


@pytest.mark.parametrize("reply", PARAPHRASE_REPLIES)
def test_paraphrase_is_never_affirmative(reply):
    assert s._is_affirmative_reply(reply) is False, reply


@pytest.mark.parametrize("reply", PARAPHRASE_REPLIES)
def test_paraphrase_refusal_demands_the_literal_words(reply):
    reason = s._consent_refusal_reason(reply)
    assert reason and "🛑" in reason, reply
    assert "дословн" in reason.lower(), reason


@pytest.mark.parametrize("reply", PARAPHRASE_REPLIES)
def test_paraphrase_does_not_burn_the_plan(reply):
    """Пересказ — это промах ОФОРМЛЕНИЯ: реплики человека тут нет вовсе,
    поэтому план остаётся валидным и вызов можно повторить с дословным
    ответом (в отличие от оговорки, где набор объектов реально другой)."""
    assert s._is_negative_reply(reply) is False, reply


def test_paraphrase_through_the_gate_is_refused_but_retryable():
    m = {"kind": "delete", "items": [{"taskId": "t1"}],
         "created": time.monotonic(), "plan_shown_at": time.monotonic() - 10,
         "consumed": False}
    r = s._require_consent(action="delete", tier=2, manifest=m,
                           user_reply="Пользователь: да", min_gap=0)
    assert r.ok is False
    assert m["consumed"] is False


# ===========================================================================
# Регресс: нормальные человеческие «да» обязаны продолжать работать.
# Ужесточение классификатора, из-за которого владелец не может ничего
# подтвердить, хуже самой дыры.
# ===========================================================================

GENUINE_YES = [
    "да", "ок", "окей", "давай", "подтверждаю", "да, удаляй", "ага", "го",
    "yes", "confirm", "+", "да, только быстрее", "ок, давай",
    "да, сливай", "да, создавай", "ну давай", "да, без проблем",
    "ок, только аккуратно",
    # добавлены этим фиксом — однозначные согласия, раньше молча отказные
    "хорошо", "договорились", "принято", "валяй",
]


@pytest.mark.parametrize("reply", GENUINE_YES)
def test_genuine_yes_still_affirmative(reply):
    assert s._is_affirmative_reply(reply) is True, reply
    assert s._is_negative_reply(reply) is False, reply
    assert s._consent_refusal_reason(reply) == ""


@pytest.mark.parametrize("reply", ["ДА", "Да.", "ОК!", "  да  ", "Да, Удаляй",
                                   "ХОРОШО", "Ага!"])
def test_case_and_whitespace_do_not_break_a_yes(reply):
    assert s._is_affirmative_reply(reply) is True, reply


@pytest.mark.parametrize("reply", ["нет", "отмена", "стоп", "не надо", "no",
                                   "cancel", "нет, отмена", "погоди"])
def test_plain_no_still_refused(reply):
    assert s._is_affirmative_reply(reply) is False, reply
    assert s._is_negative_reply(reply) is True, reply


@pytest.mark.parametrize("reply", ["ладно", "ну ладно", "делай что хочешь",
                                   "мне всё равно", "как скажешь",
                                   "наверное да", "думаю да", "может быть да",
                                   "да, наверное", "whatever, go"])
def test_hesitant_replies_stay_refused(reply):
    """Неуверенность/безразличие — не согласие. «ладно»/«ну ладно» осознанно
    не добавлены в словарь; «наверное да»/«делай что хочешь» отсекаются
    отдельным hedge-маркером (в них есть утвердительное слово)."""
    assert s._is_affirmative_reply(reply) is False, reply
    # План при этом не сжигается — человека можно просто переспросить.
    assert s._is_negative_reply(reply) is False, reply


@pytest.mark.parametrize("reply", ["наверное да", "думаю да",
                                   "делай что хочешь", "мне всё равно"])
def test_hedge_refusal_asks_for_an_unambiguous_answer(reply):
    reason = s._consent_refusal_reason(reply)
    assert reason and "🛑" in reason, reply
    assert "переспроси" in reason.lower(), reason


# ===========================================================================
# Граничные случаи
# ===========================================================================

@pytest.mark.parametrize("reply", ["", None, "   ", "\n\t "])
def test_empty_replies_are_neither(reply):
    assert s._is_affirmative_reply(reply) is False, repr(reply)
    assert s._is_negative_reply(reply) is False, repr(reply)
    assert s._classify_consent_reply(reply).kind == "empty"


ECHO_ARTIFACTS = [
    "DELETE 5",
    "delete 3",
    "CREATE 2",
    "DECLUTTER 1",
    'execute_task_deletion(manifest_id="abc")',
    'plan_task_deletion(summary="x")',
    'манифест manifest_id=abc123',
    '{"decision": "approved", "user_reply": "да"}',
]


@pytest.mark.parametrize("reply", ECHO_ARTIFACTS)
def test_echo_artifacts_are_refused_with_an_explanation(reply):
    assert s._is_affirmative_reply(reply) is False, reply
    reason = s._consent_refusal_reason(reply)
    assert reason and "🛑" in reason, reply
    assert "дословн" in reason.lower(), reason


def test_unrecognized_reply_is_ambiguous_with_its_own_explanation():
    """ИЗМЕНЕНО 2026-08-06 (смена принципа на fail-closed). Раньше нераспознанный
    ответ давал kind="unrecognized" и ПУСТОЕ объяснение — вызывающий подставлял
    общий _NO_REPLY_INSTRUCTION («передай дословную реплику»), который для этого
    случая просто неверен: реплику-то передали, она непонятна. Теперь такой
    ответ — kind="ambiguous" со своим текстом, который просит человека ответить
    однозначно. Отказ по-прежнему отказ, план по-прежнему жив."""
    v = s._classify_consent_reply("что там по погоде")
    assert v.kind == "ambiguous"
    reason = s._consent_refusal_reason("что там по погоде")
    assert reason and "🛑" in reason
    assert "однозначн" in reason.lower(), reason
    assert s._is_affirmative_reply("что там по погоде") is False
    assert s._is_negative_reply("что там по погоде") is False
