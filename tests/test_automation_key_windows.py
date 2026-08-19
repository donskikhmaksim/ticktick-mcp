"""automation_key.py — временные окна.

Базовый контракт — docs/TZ/TZ_temp_automation_key.md §3.1/§3.6, тестовый
план §6, пункты 1-3, 7. Контракт «несколько окон одновременно» — docs/TZ/
TZ_multi_automation_windows.md, тестовый план (аддендум), пункты 1-5.

Настоящий Postgres (см. tests/pg_helper.py) — та же дисциплина, что у
tests/test_manifest_restart.py/test_automation_key_first_call.py: атомарность
INSERT'а и видимость `revoked_at`/`expires_at` между процессами — поведение
самого движка, не воспроизводимое честно двойником в памяти.

АДАПТАЦИЯ ПОД НОВЫЙ КОНТРАКТ (не подгонка — смена самого поведения). Первая
версия хранила РОВНО ОДНО окно на сервер (`_WINDOW_ID` фиксирован),
`generate_window` делал UPSERT, и повторная генерация СОЗНАТЕЛЬНО
инвалидировала предыдущий токен — под это был тест
`test_regenerating_an_active_window_invalidates_the_old_token`. Аддендум
переворачивает это требование ровно наоборот (владелец использует ключи в
нескольких чатах одновременно, повторная генерация НЕ должна гасить более
раннюю) — тот тест удалён и заменён на его логическую противоположность
(`test_two_windows_are_both_valid_at_the_same_time` и соседние ниже), а не
обойдён/закомментирован. `window_status` (алиас «последнего» окна) в новом
контракте не осмыслена и убрана из модуля — все тесты, читавшие её, переведены
на `find_window`/`list_windows`.

Пункты тестового плана, покрытые ЭТИМ файлом:
  Основной ТЗ §6: 1 (окно работает), 2 (истекает без отзыва), 3 (отзыв до
  истечения), 7 (погашенная/истёкшая строка не удаляется).
  Аддендум: 1 (два окна одновременно валидны), 2 (revoke гасит только одно),
  3 (revoke_all гасит оба), 4 (list_windows — только активные), 5 (у каждого
  окна своё время истечения).
"""
import time

from tests.pg_helper import fresh_automation_key_store, requires_pg

pytestmark = requires_pg


def _rows():
    """Все строки tg_automation_windows этого сервера — читаем НАПРЯМУЮ, а
    не через find_window()/list_windows(), чтобы проверка "запись осталась
    в базе" не зависела от той же функции, что мы тестируем."""
    from ticktick_mcp.src import automation_key as ak

    with ak._conn() as cur:
        cur.execute(
            "SELECT window_id, token_hash, label, created_at, expires_at, "
            "revoked_at, created_by_chat FROM tg_automation_windows "
            "WHERE server = %s ORDER BY created_at ASC",
            (ak.SERVER,),
        )
        return cur.fetchall()


# ═══════════ 1. Токен работает в течение объявленного окна ═══════════

def test_token_works_within_the_window(monkeypatch):
    ak = fresh_automation_key_store()
    monkeypatch.setattr(ak, "AUTOMATION_WINDOW_HOURS", 24.0)

    token = ak.generate_window("4242")

    assert token, "generate_window вернул пустую строку при поднятом хранилище"
    assert ak.check_window(token) is True
    assert ak.check_window("не-тот-токен") is False


def test_generate_window_returns_empty_when_store_not_ready():
    """store_ready()=False (TG_APPROVAL_ENABLED выключен/CONSENT_DATABASE_URL
    не задан) — вежливый пустой результат, не исключение."""
    from ticktick_mcp.src import automation_key as ak

    ak.close_store()
    try:
        assert ak.generate_window("4242") == ""
        assert ak.check_window("что-угодно") is False
        assert ak.find_window("что-угодно") is None
        assert ak.revoke_window("some-id") is False
        assert ak.revoke_all_windows() == 0
        assert ak.list_windows() == []
    finally:
        ak.close_store()


# ═══════════ 2. Токен НЕ работает после истечения (без отзыва) ═══════════

def test_token_stops_working_after_expiry_without_revoke(monkeypatch):
    ak = fresh_automation_key_store()
    # Часы дробные специально: секунды, а не сутки, чтобы тест не спал долго.
    monkeypatch.setattr(ak, "AUTOMATION_WINDOW_HOURS", 0.2 / 3600)  # 0.2 c

    token = ak.generate_window("4242")
    assert ak.check_window(token) is True

    time.sleep(0.35)

    assert ak.check_window(token) is False, "истёкший токен всё ещё проходит"
    assert ak.list_windows() == [], "истёкшее окно не должно быть в списке активных"


# ═══════════ 3. Токен НЕ работает после явного отзыва (до истечения) ═══════

def test_token_stops_working_after_explicit_revoke(monkeypatch):
    ak = fresh_automation_key_store()
    monkeypatch.setattr(ak, "AUTOMATION_WINDOW_HOURS", 24.0)

    token = ak.generate_window("4242")
    win = ak.find_window(token)
    assert win is not None

    revoked = ak.revoke_window(win["window_id"], "4242")

    assert revoked is True, "revoke_window сказал, что гасить было нечего"
    assert ak.check_window(token) is False, "отозванный токен всё ещё проходит"
    assert ak.list_windows() == [], "отозванное окно не должно быть в списке активных"


def test_revoke_without_any_window_reports_nothing_to_revoke():
    ak = fresh_automation_key_store()
    assert ak.revoke_window("no-such-id") is False
    assert ak.list_windows() == []


# ═══════ 7. Просроченная/отозванная запись остаётся в базе (не удаляется) ═══

def test_revoked_row_is_not_deleted_it_stays_in_the_table(monkeypatch):
    ak = fresh_automation_key_store()
    monkeypatch.setattr(ak, "AUTOMATION_WINDOW_HOURS", 24.0)
    token = ak.generate_window("4242")
    assert len(_rows()) == 1
    win = ak.find_window(token)

    ak.revoke_window(win["window_id"], "4242")

    rows = _rows()
    assert len(rows) == 1, "отозванная строка исчезла из базы — должна остаться"
    assert rows[0][5] is not None, "revoked_at не проставлен"


def test_expired_row_is_not_deleted_it_stays_in_the_table(monkeypatch):
    ak = fresh_automation_key_store()
    monkeypatch.setattr(ak, "AUTOMATION_WINDOW_HOURS", 0.2 / 3600)
    ak.generate_window("4242")
    time.sleep(0.35)

    assert ak.check_window("что-угодно") is False  # окно уже истекло
    rows = _rows()
    assert len(rows) == 1, "просроченная строка исчезла из базы — должна остаться"
    assert rows[0][4] < ak._now_ms(), "expires_at не в прошлом — тест не про то"
    assert rows[0][5] is None, "revoked_at не должен быть проставлен истечением"


# ═══ Аддендум 1. Два окна валидны ОДНОВРЕМЕННО — генерация Б не гасит А ═══

def test_two_windows_are_both_valid_at_the_same_time(monkeypatch):
    ak = fresh_automation_key_store()
    monkeypatch.setattr(ak, "AUTOMATION_WINDOW_HOURS", 24.0)

    token_a = ak.generate_window("chat-a")
    token_b = ak.generate_window("chat-b")

    assert token_a != token_b
    assert ak.check_window(token_a) is True, "окно А погашено генерацией Б"
    assert ak.check_window(token_b) is True
    assert len(_rows()) == 2, "должны быть ДВЕ отдельные строки, не UPSERT одной"


def test_regenerating_uses_a_fresh_random_window_id_each_time(monkeypatch):
    ak = fresh_automation_key_store()
    monkeypatch.setattr(ak, "AUTOMATION_WINDOW_HOURS", 24.0)

    ak.generate_window("4242")
    ak.generate_window("4242")

    ids = [r[0] for r in _rows()]
    assert len(ids) == 2
    assert ids[0] != ids[1], "window_id обязан быть новым/случайным на каждую генерацию"


# ═══ Аддендум 2. revoke_window(id_А) гасит только А, Б продолжает работать ═══

def test_revoke_window_by_id_only_kills_that_one_window(monkeypatch):
    ak = fresh_automation_key_store()
    monkeypatch.setattr(ak, "AUTOMATION_WINDOW_HOURS", 24.0)

    token_a = ak.generate_window("chat-a")
    token_b = ak.generate_window("chat-b")
    win_a = ak.find_window(token_a)

    revoked = ak.revoke_window(win_a["window_id"], "chat-a")

    assert revoked is True
    assert ak.check_window(token_a) is False, "окно А должно быть погашено"
    assert ak.check_window(token_b) is True, "окно Б не должно было пострадать"


# ═══════ Аддендум 3. revoke_all_windows гасит оба разом ═══════

def test_revoke_all_windows_kills_every_active_window(monkeypatch):
    ak = fresh_automation_key_store()
    monkeypatch.setattr(ak, "AUTOMATION_WINDOW_HOURS", 24.0)

    token_a = ak.generate_window("chat-a")
    token_b = ak.generate_window("chat-b")

    n = ak.revoke_all_windows("chat-a")

    assert n == 2
    assert ak.check_window(token_a) is False
    assert ak.check_window(token_b) is False


def test_revoke_all_windows_returns_zero_when_nothing_active():
    ak = fresh_automation_key_store()
    assert ak.revoke_all_windows() == 0


# ═══════ Аддендум 4. list_windows — только активные, не погашенные/истёкшие ══

def test_list_windows_shows_both_active_windows(monkeypatch):
    ak = fresh_automation_key_store()
    monkeypatch.setattr(ak, "AUTOMATION_WINDOW_HOURS", 24.0)

    ak.generate_window("chat-a", label="чат А")
    ak.generate_window("chat-b")

    windows = ak.list_windows()

    assert len(windows) == 2
    labels = {w["label"] for w in windows}
    assert "чат А" in labels
    assert all("window_id" in w and "remaining_s" in w for w in windows)


def test_list_windows_excludes_revoked_and_expired(monkeypatch):
    ak = fresh_automation_key_store()
    monkeypatch.setattr(ak, "AUTOMATION_WINDOW_HOURS", 24.0)
    token_a = ak.generate_window("chat-a")
    ak.generate_window("chat-b")
    monkeypatch.setattr(ak, "AUTOMATION_WINDOW_HOURS", 0.2 / 3600)
    ak.generate_window("chat-c")  # истечёт через мгновение
    time.sleep(0.35)

    win_a = ak.find_window(token_a)
    ak.revoke_window(win_a["window_id"], "chat-a")

    windows = ak.list_windows()

    assert len(windows) == 1, "остаться должно только окно Б (не отозванное А, не истёкшее В)"
    assert windows[0]["created_by_chat"] == "chat-b"


# ═══ Аддендум 5. Каждое окно истекает по СВОЕМУ собственному времени ═══

def test_each_window_expires_on_its_own_schedule(monkeypatch):
    ak = fresh_automation_key_store()
    monkeypatch.setattr(ak, "AUTOMATION_WINDOW_HOURS", 0.2 / 3600)  # ~0.2с
    token_a = ak.generate_window("chat-a")  # истекает раньше

    monkeypatch.setattr(ak, "AUTOMATION_WINDOW_HOURS", 24.0)
    token_b = ak.generate_window("chat-b")  # живёт долго

    time.sleep(0.35)

    assert ak.check_window(token_a) is False, "А должно было истечь"
    assert ak.check_window(token_b) is True, "Б не должно было истечь заодно с А"


# ═ TZ_automation_key_duration_labels.md §1 — expires_at=NULL = бессрочно ═══
#
# ticktick-mcp сама больше не генерирует окна с NULL-сроком (генерация вся
# переехала в gmail-mcp) — но обязана честно ЧИТАТЬ такую строку, если её
# создал gmail-mcp. Вставляем напрямую SQL'ем, эмулируя чужую запись, а не
# через generate_window (у неё до сих пор нет параметра "бессрочно" — она
# вызывается только из легаси-пути/тестов, TZ §7 явно не расширяет её).

def _insert_null_expiry_window(ak, window_id, token, scope="ticktick"):
    with ak._conn() as cur:
        cur.execute(
            """
            INSERT INTO tg_automation_windows
              (server, window_id, token_hash, label, created_at, expires_at,
               revoked_at, created_by_chat, scope)
            VALUES (%s, %s, %s, NULL, %s, NULL, NULL, %s, %s)
            """,
            (ak.SERVER, window_id, ak._hash(token), ak._now_ms(), "gmail-mcp-chat", scope),
        )


def test_null_expiry_window_never_expires(monkeypatch):
    ak = fresh_automation_key_store()
    _insert_null_expiry_window(ak, "forever1", "forever-token")

    assert ak.check_window("forever-token") is True
    win = ak.find_window("forever-token")
    assert win is not None and win["expires_at"] is None

    # "Истечение" по времени не должно наступить никогда — не просто "ещё не
    # наступило", а структурно невозможно (нет числа, с которым сравнивать).
    # Мысленный эксперимент: если бы код сравнивал `expires_at > now` без
    # NULL-проверки, отсутствующее число дало бы False на любом now — тест
    # ловит именно это.
    assert ak.check_window("forever-token") is True


def test_null_expiry_window_appears_in_list_with_remaining_s_none(monkeypatch):
    ak = fresh_automation_key_store()
    _insert_null_expiry_window(ak, "forever2", "forever-token-2")

    rows = ak.list_windows()

    assert len(rows) == 1
    assert rows[0]["window_id"] == "forever2"
    assert rows[0]["expires_at"] is None
    assert rows[0]["remaining_s"] is None, (
        "бессрочное окно не должно получать 0 (0 читается как 'вот-вот "
        "истечёт' — противоположность правде)")


def test_null_expiry_window_can_still_be_revoked(monkeypatch):
    ak = fresh_automation_key_store()
    _insert_null_expiry_window(ak, "forever3", "forever-token-3")

    revoked = ak.revoke_window("forever3", "4242")

    assert revoked is True
    assert ak.check_window("forever-token-3") is False, "отозванное бессрочное окно всё ещё проходит"


def test_null_expiry_window_out_of_scope_is_rejected(monkeypatch):
    """Бессрочность не освобождает от проверки scope — окно на другой
    сервис остаётся отклонено для ticktick, даже если оно никогда не
    истекает по времени."""
    ak = fresh_automation_key_store()
    _insert_null_expiry_window(ak, "forever4", "forever-token-4", scope="gmail,calendar")

    assert ak.check_window("forever-token-4") is False


# ═══════════════════════ matches_static — без базы вовсе ═══════════════════

def test_matches_static_is_constant_time_and_does_not_need_a_store(monkeypatch):
    """matches_static не трогает Postgres вообще — работает даже когда
    хранилище окон не поднято."""
    from ticktick_mcp.src import automation_key as ak

    ak.close_store()
    try:
        monkeypatch.setattr(ak, "AUTOMATION_KEY", "test-static-key")
        assert ak.matches_static("test-static-key") is True
        assert ak.matches_static("wrong") is False
        assert ak.matches_static("") is False
        monkeypatch.setattr(ak, "AUTOMATION_KEY", "")
        assert ak.matches_static("test-static-key") is False, (
            "пустой AUTOMATION_KEY не должен совпадать ни с чем")
    finally:
        ak.close_store()


# ═══ QA-2 2026-08-19 (добор №9): revoke/list — по SCOPE, как find_window ═══
#
# После переезда генерации ключей в gmail-mcp боевые окна создаются со
# server='gmail' (и scope, покрывающим несколько сервисов). `find_window` —
# то, что реально открывает гейт, — принимает окно ЛЮБОГО создателя, чей
# scope покрывает ticktick; а `revoke_all_windows`/`list_windows`/
# `revoke_window` фильтровали `WHERE server='ticktick'` и таких окон НЕ
# ВИДЕЛИ: кнопка «Выключить всё» отвечала «активных окон не было», а окно
# продолжало открывать гейт. Теперь отзыв/список ходят той же scope-логикой,
# что и допуск: гасится/показывается ровно то, что может открыть гейт этому
# серверу; чужое (scope без ticktick) — не видно и не трогается.

def _insert_foreign_server_window(ak, window_id, token, scope="all",
                                  server="gmail"):
    """Эмуляция строки, созданной gmail-mcp: чужой `server`, свой срок."""
    now = ak._now_ms()
    with ak._conn() as cur:
        cur.execute(
            """
            INSERT INTO tg_automation_windows
              (server, window_id, token_hash, label, created_at, expires_at,
               revoked_at, created_by_chat, scope)
            VALUES (%s, %s, %s, NULL, %s, %s, NULL, %s, %s)
            """,
            (server, window_id, ak._hash(token), now, now + 3600_000,
             "gmail-mcp-chat", scope),
        )


def test_list_windows_sees_gmail_created_window_that_opens_this_gate():
    ak = fresh_automation_key_store()
    _insert_foreign_server_window(ak, "gm1", "gmail-token-1", scope="all")
    # Контроль честности стенда: это окно РЕАЛЬНО открывает гейт ticktick.
    assert ak.check_window("gmail-token-1") is True

    rows = ak.list_windows()

    assert [r["window_id"] for r in rows] == ["gm1"], (
        "окно, способное открыть гейт, невидимо для «Списка»")


def test_list_windows_hides_foreign_scope_window():
    ak = fresh_automation_key_store()
    _insert_foreign_server_window(ak, "gm2", "gmail-token-2",
                                  scope="gmail,calendar")
    assert ak.check_window("gmail-token-2") is False  # гейт оно не открывает

    assert ak.list_windows() == []


def test_revoke_all_kills_gmail_created_window():
    """Кнопка «Выключить всё» обязана гасить ВСЁ, что открывает гейт этому
    серверу, — раньше она отвечала «0» и окно продолжало действовать."""
    ak = fresh_automation_key_store()
    _insert_foreign_server_window(ak, "gm3", "gmail-token-3", scope="all")
    assert ak.check_window("gmail-token-3") is True

    assert ak.revoke_all_windows() == 1
    assert ak.check_window("gmail-token-3") is False, (
        "«Выключить всё» отчиталось, а окно продолжает открывать гейт")


def test_revoke_all_does_not_touch_foreign_scope_windows():
    """Чужое окно (scope без ticktick) этот сервер не гасит: оно не открывает
    ЕГО гейт, а решать за gmail-mcp судьбу его окон — не наше дело."""
    ak = fresh_automation_key_store()
    _insert_foreign_server_window(ak, "gm4", "gmail-token-4",
                                  scope="gmail,calendar")

    assert ak.revoke_all_windows() == 0
    with ak._conn() as cur:
        cur.execute("SELECT revoked_at FROM tg_automation_windows "
                    "WHERE window_id = %s", ("gm4",))
        assert cur.fetchone()[0] is None, "чужое окно погашено без права"


def test_revoke_window_by_id_works_on_gmail_created_window():
    ak = fresh_automation_key_store()
    _insert_foreign_server_window(ak, "gm5", "gmail-token-5", scope="all")

    assert ak.revoke_window("gm5", "4242") is True
    assert ak.check_window("gmail-token-5") is False


def test_revoke_window_by_id_refuses_foreign_scope_window():
    ak = fresh_automation_key_store()
    _insert_foreign_server_window(ak, "gm6", "gmail-token-6",
                                  scope="gmail,calendar")

    assert ak.revoke_window("gm6", "4242") is False
    with ak._conn() as cur:
        cur.execute("SELECT revoked_at FROM tg_automation_windows "
                    "WHERE window_id = %s", ("gm6",))
        assert cur.fetchone()[0] is None
