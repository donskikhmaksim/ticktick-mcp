"""Строгий протокол подтверждения (смена принципа, 2026-08-06).

Подход «перечислим слова-ограничители» проигрывал гонку: «ок, но только те,
что просрочены» ловилось, а «делай, я передумал насчёт третьей» — нет, потому
что «делай» есть в словаре согласий, а «передумал насчёт третьей» не подходит
ни под один маркер. Добавлять новые слова можно бесконечно.

Новый принцип — fail-closed: согласием считается ТОЛЬКО ответ, целиком
собранный из понятных элементов (слово согласия + безобидные наполнители,
короткая фраза). Всё остальное — условие, уточнение, перечисление,
рассуждение, незнакомое слово — НЕ согласие. Нельзя ошибиться в разборе того,
что не разбираешь.

ГЛАВНЫЙ РИСК этого фикса — не дыра, а ложные отказы: если владелец не сможет
подтвердить обычной человеческой фразой, это ХУЖЕ закрываемой дыры. Поэтому
регресс-набор нормальных «да» здесь идёт ПЕРВЫМ и проверяется целиком.
"""
import ast
import builtins
import inspect
import textwrap
import time

import pytest

import ticktick_mcp.src.server as s


# ===========================================================================
# 1. ГЛАВНОЕ: нормальные человеческие подтверждения обязаны проходить
# ===========================================================================

GENUINE_YES = [
    "да", "Да.", "ДА", "ок", "окей", "ok", "okay", "давай", "подтверждаю",
    "подтверждено", "ага", "угу", "го", "погнали", "yes", "yep", "sure",
    "confirm", "approve", "+", "+1", "да, удаляй", "ок, давай",
    "да, только быстрее", "давай, пожалуйста", "хорошо", "договорились",
    "принято", "валяй", "да, всё верно", "да, правильно", "согласен",
    "подтверждаю, действуй",
]


@pytest.mark.parametrize("reply", GENUINE_YES)
def test_ordinary_human_yes_is_still_accepted(reply):
    """Ужесточение НЕ должно превращать обычные подтверждения в отказы."""
    assert s._classify_consent_reply(reply).kind == "affirmative", reply
    assert s._is_affirmative_reply(reply) is True, reply
    assert s._is_negative_reply(reply) is False, reply
    assert s._consent_refusal_reason(reply) == "", reply


@pytest.mark.parametrize("reply", GENUINE_YES)
def test_ordinary_human_yes_passes_the_real_gate(reply):
    """Сквозная проверка: согласие проходит гейт и сжигает манифест штатно
    (one-shot), а не спотыкается о классификатор."""
    m = {"kind": "delete", "items": [{"taskId": "t1"}],
         "created": time.monotonic(), "plan_shown_at": time.monotonic() - 10,
         "consumed": False}
    r = s._require_consent(action="delete", tier=2, manifest=m,
                           user_reply=reply, min_gap=0)
    assert r.ok is True, (reply, r.reason)


# ===========================================================================
# 2. Всё сложное — отказ
# ===========================================================================

# (фраза, ожидаемый вид, должен ли план быть аннулирован)
REFUSED = [
    # то, ради чего менялся принцип: «делай» в словаре согласий, а остальное
    # не подходит ни под один маркер-ограничитель
    ("делай, я передумал насчёт третьей", "ambiguous", False),
    # оговорки/исключения — набор объектов другой, план перестраивать
    ("ок, кроме последней", "caveat", True),
    ("удали первые три, а последнюю не надо", "caveat", True),
    ("confirm, but skip the last one", "caveat", True),
    ("давай, только вторую оставь", "caveat", True),
    # отрицание в любом месте фразы
    ("да, всё верно, но подожди с третьей", "negative", True),
    ("нет", "negative", True),
    ("отмена", "negative", True),
    ("стоп", "negative", True),
    # реплики человека тут нет — пересказ модели
    ("Пользователь: да", "paraphrase", False),
    ("он сказал да", "paraphrase", False),
    # неуверенность/самоустранение
    ("наверное да", "hedge", False),
    ("думаю да", "hedge", False),
    ("делай что хочешь", "hedge", False),
    # сложная структура: отсрочка, условие, РАСШИРЕНИЕ плана
    ("да, но сначала покажи ещё раз", "ambiguous", False),
    ("ок, если ты уверен", "ambiguous", False),
    ("да, и заодно удали ещё вон ту", "ambiguous", False),
]


@pytest.mark.parametrize("reply,kind,burns", REFUSED)
def test_complex_reply_is_never_affirmative(reply, kind, burns):
    assert s._is_affirmative_reply(reply) is False, reply


@pytest.mark.parametrize("reply,kind,burns", REFUSED)
def test_refused_reply_has_the_expected_kind(reply, kind, burns):
    assert s._classify_consent_reply(reply).kind == kind, reply


@pytest.mark.parametrize("reply,kind,burns", REFUSED)
def test_refusal_is_explained(reply, kind, burns):
    reason = s._consent_refusal_reason(reply)
    assert reason and "🛑" in reason, reply


def test_plan_extension_is_refused_not_executed():
    """«да, и заодно удали ещё вон ту» — не отказ и не оговорка, а РАСШИРЕНИЕ
    плана. Исполнять показанный план в ответ на это нельзя: набор объектов у
    человека в голове уже другой, чем в манифесте."""
    reply = "да, и заодно удали ещё вон ту"
    assert s._is_affirmative_reply(reply) is False
    m = {"kind": "delete", "items": [{"taskId": "t1"}],
         "created": time.monotonic(), "plan_shown_at": time.monotonic() - 10,
         "consumed": False}
    r = s._require_consent(action="delete", tier=2, manifest=m,
                           user_reply=reply, min_gap=0)
    assert r.ok is False


# ===========================================================================
# 3. Два разных вида отказа: «ответ был, но не согласие» vs «внятного ответа
#    нет». В первом случае план аннулируется, во втором остаётся живым.
# ===========================================================================

def _gate_with(reply):
    m = {"kind": "delete", "items": [{"taskId": "t1"}, {"taskId": "t2"}],
         "created": time.monotonic(), "plan_shown_at": time.monotonic() - 10,
         "consumed": False}
    r = s._require_consent(action="delete", tier=2, manifest=m,
                           user_reply=reply, min_gap=0)
    return r, m


@pytest.mark.parametrize("reply,kind,burns", REFUSED)
def test_plan_survival_matches_the_kind_of_refusal(reply, kind, burns):
    r, m = _gate_with(reply)
    assert r.ok is False, reply
    assert m["consumed"] is burns, (reply, kind)
    assert s._is_negative_reply(reply) is burns, (reply, kind)


def test_ambiguous_keeps_the_plan_alive_and_asks_for_one_word():
    """Новый вид отказа: ответа по существу нет — губить план незачем, надо
    просто переспросить, и текст отказа обязан сказать, чего именно ждём."""
    reply = "делай, я передумал насчёт третьей"
    assert s._classify_consent_reply(reply).kind == "ambiguous"
    assert s._is_negative_reply(reply) is False
    r, m = _gate_with(reply)
    assert r.ok is False
    assert m["consumed"] is False
    reason = s._consent_refusal_reason(reply)
    assert "однозначн" in reason.lower(), reason
    assert "да" in reason.lower() and "нет" in reason.lower(), reason


def test_caveat_and_negation_still_burn_the_plan():
    """Разделение, существовавшее до этого фикса, не сломано."""
    for reply in ("ок, кроме последней", "нет, отмена"):
        r, m = _gate_with(reply)
        assert r.ok is False, reply
        assert m["consumed"] is True, reply


# ===========================================================================
# 4. Переносимость блока классификатора
# ===========================================================================
#
# Этот же блок живёт в шести репозиториях (ticktick-mcp + пять Google-MCP).
# Его ценность — в том, что он копируется целиком и не тянет за собой
# состояние сервера. Тест обязан покраснеть, если кто-то в будущем привяжет
# классификатор к манифестам, клиенту, настройкам или Telegram.

_BEGIN = "# === CONSENT-REPLY CLASSIFIER — BEGIN"
_END = "# === CONSENT-REPLY CLASSIFIER — END"

# Единственное, чем блоку разрешено пользоваться извне: стандартный `re` и
# typing-алиасы в аннотациях. Больше НИЧЕГО (никаких _MANIFESTS, _TG_CFG, os,
# клиента, settings).
_ALLOWED_EXTERNAL_NAMES = {"re", "Optional", "List"}


def _classifier_block_source() -> str:
    """Исходник блока между маркерами — РОВНО то, что скопируют в другой репо."""
    lines = inspect.getsource(s).splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.startswith(_BEGIN)]
    ends = [i for i, ln in enumerate(lines) if ln.startswith(_END)]
    assert len(starts) == 1, "маркер BEGIN должен быть ровно один"
    assert len(ends) == 1, "маркер END должен быть ровно один"
    assert starts[0] < ends[0], "END раньше BEGIN"
    return textwrap.dedent("\n".join(lines[starts[0] + 1:ends[0]])) + "\n"


def _collect_bound_names(tree: ast.AST) -> set:
    bound = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.Lambda)):
            if getattr(node, "name", None):
                bound.add(node.name)
            args = getattr(node, "args", None)
            if isinstance(args, ast.arguments):
                for a in (list(args.args) + list(args.posonlyargs)
                          + list(args.kwonlyargs)):
                    bound.add(a.arg)
                if args.vararg:
                    bound.add(args.vararg.arg)
                if args.kwarg:
                    bound.add(args.kwarg.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx,
                                                       (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add(alias.asname or alias.name.split(".")[0])
    return bound


def test_classifier_block_has_no_server_dependencies():
    """Блок должен оставаться переносимым ЦЕЛИКОМ: любое свободное имя, кроме
    `re`/typing, означает привязку к этому серверу — и ломает перенос на
    остальные пять."""
    block = _classifier_block_source()
    tree = ast.parse(block)
    bound = _collect_bound_names(tree)
    used = {n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    external = used - bound - set(dir(builtins)) - _ALLOWED_EXTERNAL_NAMES
    assert not external, (
        "классификатор перестал быть переносимым — он тянет внешние имена: "
        f"{sorted(external)}. Либо убери зависимость, либо (если она реально "
        "неизбежна) осознанно расширь _ALLOWED_EXTERNAL_NAMES и синхронизируй "
        "все шесть копий блока."
    )


def test_classifier_block_imports_nothing_of_its_own():
    """Импорт внутри блока — тоже зависимость, просто не видимая как имя."""
    tree = ast.parse(_classifier_block_source())
    imports = [n for n in ast.walk(tree)
               if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert not imports, "внутри переносимого блока не должно быть импортов"


def test_classifier_block_documents_its_six_copies_and_its_limits():
    """Комментарий-шапка блока — часть контракта переноса: он предупреждает о
    шести копиях и честно называет границу применимости (сервер не отличает
    сочинённое моделью «да» от настоящего; настоящий внеполосный фактор —
    только кнопка в Telegram)."""
    block = _classifier_block_source()
    low = block.lower()
    assert "шести местах" in low or "шесть коп" in low or "шести копи" in low
    assert "синхронизируй" in low
    assert "кнопкой в telegram" in low or "кнопк" in low
    assert "уменьшение поверхности" in low


# ===========================================================================
# 5. Мелочи, которые легко потерять при переносе
# ===========================================================================

@pytest.mark.parametrize("reply", ["", None, "   ", "\n\t "])
def test_empty_is_neither_and_keeps_the_plan(reply):
    assert s._classify_consent_reply(reply).kind == "empty"
    assert s._is_affirmative_reply(reply) is False
    assert s._is_negative_reply(reply) is False


def test_long_all_known_words_reply_is_still_refused():
    """Страховка от абсурдно длинной фразы, собранной из «понятных» слов:
    протокол подтверждения короткий по смыслу."""
    reply = " ".join(["да"] * (s._CONSENT_MAX_TOKENS + 1))
    assert s._is_affirmative_reply(reply) is False, reply


def test_filler_alone_is_not_a_yes():
    """Наполнители — не согласие: нужно хотя бы одно слово согласия."""
    for reply in ("пожалуйста", "только быстрее", "ну"):
        assert s._is_affirmative_reply(reply) is False, reply
