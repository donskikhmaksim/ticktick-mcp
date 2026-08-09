"""2026-08-09 (П9 пакет ТЗ, пункт 1): CSRF-защита при OAuth-логине через
TickTick/Google не проверялась. `start_auth_flow` честно генерировал
случайный `state` (auth.py, было ~209) и клал его в authorization URL — но
`OAuthCallbackHandler.do_GET` (было ~33-41) читал только `code` из query и
никогда не сверял `state` с тем, что сам же сгенерировал. Практическое
следствие: злоумышленник, заранее получивший СВОЙ авторизационный код от
TickTick, мог заманить жертву перейти по ссылке
`http://localhost:8000/callback?code=<чужой_код>` (без state вообще, либо с
любым значением) — сервер принимал бы этот код как настоящий и обменивал бы
его на токен, который относится к аккаунту атакующего, а не жертвы (классическая
CSRF-подстава чужого code в OAuth callback).

Тесты ниже вызывают `do_GET` НАПРЯМУЮ на облегчённом двойнике хендлера — без
поднятия настоящего TCP-сервера — потому что единственное, что меняется в
этом дефекте, живёт целиком внутри одного метода: он читает query-параметры
`self.path` и пишет `OAuthCallbackHandler.auth_code` (либо не пишет). Двойник
не «упрощает» протокол: `send_response`/`send_header`/`end_headers`/`wfile`
— это ровно тот интерфейс, который `http.server.BaseHTTPRequestHandler`
предоставляет `do_GET`, и настоящий сервер эти же методы вызывает так же
(см. tests/test_doubles_do_not_cheat.py — двойник не имеет права быть добрее
настоящего кода; здесь он просто не открывает сокет, поведение do_GET не
меняет ни на йоту).

ДОБАВЛЕНО 2026-08-09 (доработка по замечанию ревью, вторая дыра в том же
пункте): первая версия сверки читала `got_state == OAuthCallbackHandler.
expected_state` напрямую. Если expected_state НЕ установлен (остался None) и
колбэк пришёл вовсе БЕЗ параметра state, то got_state тоже None — сравнение
`None == None` истинно, и сверка молча отключалась ровно в том состоянии,
которое выглядит как «защита на месте». Два теста в конце файла
(test_uninitialized_expected_state_*) ловят именно это; сверка вынесена в
`OAuthCallbackHandler._state_matches()`, которая считает пустой
expected_state явно невалидным (безусловный отказ) и сравнивает непустые
значения через hmac.compare_digest по sha256-дайджестам — тот же приём, что
`_automation_key_matches` в server.py:61."""
import io

from ticktick_mcp.src.auth import OAuthCallbackHandler


class _FakeHandler:
    """Минимальный стенд-ин под интерфейс BaseHTTPRequestHandler, которым
    пользуется do_GET: path на вход, статус-код и тело ответа на выход."""

    def __init__(self, path: str):
        self.path = path
        self.wfile = io.BytesIO()
        self.status_code = None
        self.headers = []

    def send_response(self, code):
        self.status_code = code

    def send_header(self, key, value):
        self.headers.append((key, value))

    def end_headers(self):
        pass


def _reset():
    OAuthCallbackHandler.auth_code = None
    OAuthCallbackHandler.expected_state = None


def test_matching_state_accepts_the_code():
    """Happy path: state в колбэке совпадает с тем, что сервер сам сгенерировал
    в start_auth_flow — код авторизации принимается."""
    _reset()
    OAuthCallbackHandler.expected_state = "real-state-abc123"
    fake = _FakeHandler("/callback?code=goodcode&state=real-state-abc123")

    OAuthCallbackHandler.do_GET(fake)

    assert OAuthCallbackHandler.auth_code == "goodcode"
    assert fake.status_code == 200


def test_mismatched_state_refuses_and_does_not_store_the_forged_code():
    """CSRF-случай: код пришёл, но state не совпадает (чужой/предугаданный
    code, состряпанная ссылка) — сервер обязан ОТКАЗАТЬ и не положить чужой
    code в OAuthCallbackHandler.auth_code, иначе exchange_code_for_token()
    обменяет его на токен чужого аккаунта."""
    _reset()
    OAuthCallbackHandler.expected_state = "real-state-abc123"
    fake = _FakeHandler("/callback?code=forgedcode&state=attacker-state")

    OAuthCallbackHandler.do_GET(fake)

    assert OAuthCallbackHandler.auth_code is None
    assert fake.status_code == 400


def test_missing_state_param_refuses():
    """Ссылка без state вообще (самый простой CSRF-вектор — атакующему не
    нужно ничего подделывать, просто не передавать параметр) — тоже отказ."""
    _reset()
    OAuthCallbackHandler.expected_state = "real-state-abc123"
    fake = _FakeHandler("/callback?code=forgedcode")

    OAuthCallbackHandler.do_GET(fake)

    assert OAuthCallbackHandler.auth_code is None
    assert fake.status_code == 400


def test_missing_code_still_refuses_as_before():
    """Не про CSRF — существовавшее поведение (нет code → отказ) не должно
    было сломаться при добавлении сверки state."""
    _reset()
    OAuthCallbackHandler.expected_state = "real-state-abc123"
    fake = _FakeHandler("/callback?state=real-state-abc123")

    OAuthCallbackHandler.do_GET(fake)

    assert OAuthCallbackHandler.auth_code is None
    assert fake.status_code == 400


def test_uninitialized_expected_state_refuses_even_without_a_state_param():
    """2026-08-09 (доработка по замечанию ревью): дыра, которую не ловили
    четыре теста выше. Если expected_state НЕ установлен (остался None —
    например, do_GET вызван до start_auth_flow, или что-то ещё пошло не так
    в инициализации) и колбэк пришёл БЕЗ параметра state вовсе, то
    got_state тоже None — наивное `None == None` было бы True, и код
    авторизации принимался бы РОВНО в том состоянии, которое выглядит как
    «защита на месте», хотя сверка полностью отключена. Пустой
    expected_state обязан считаться невалидным состоянием и отказывать
    БЕЗУСЛОВНО, а не полагаться на то, что вызывающий добросовестно передаст
    какой-нибудь state."""
    _reset()
    assert OAuthCallbackHandler.expected_state is None  # предпосылка теста
    fake = _FakeHandler("/callback?code=forgedcode")

    OAuthCallbackHandler.do_GET(fake)

    assert OAuthCallbackHandler.auth_code is None
    assert fake.status_code == 400


def test_uninitialized_expected_state_refuses_any_state_too():
    """Тот же провал инициализации, но колбэк пришёл С каким-то state —
    подделать None было бы легко (просто не передать параметр), поэтому
    отдельно проверяем, что и это не проходит: пустой expected_state
    отказывает ЛЮБОМУ state, а не только отсутствующему."""
    _reset()
    assert OAuthCallbackHandler.expected_state is None
    fake = _FakeHandler("/callback?code=forgedcode&state=whatever-attacker-sends")

    OAuthCallbackHandler.do_GET(fake)

    assert OAuthCallbackHandler.auth_code is None
    assert fake.status_code == 400
