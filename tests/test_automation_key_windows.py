"""automation_key.py — временные окна (docs/TZ/TZ_temp_automation_key.md
§3.1/§3.6, тестовый план §6, пункты 1-3, 7, 10).

Настоящий Postgres (см. tests/pg_helper.py) — та же дисциплина, что у
tests/test_manifest_restart.py/test_automation_key_first_call.py: атомарность
UPSERT'а (`generate_window`'s `ON CONFLICT ... DO UPDATE`) и видимость
`revoked_at`/`expires_at` между процессами — поведение самого движка, не
воспроизводимое честно двойником в памяти.

Пункты теста, покрытые ЭТИМ файлом (нумерация — TZ §6):
  1. Токен работает в течение объявленного окна.
  2. Токен НЕ работает после истечения — даже без явного отзыва.
  3. Токен НЕ работает после явного отзыва, до истечения срока.
  7. Просроченная/отозванная запись остаётся в базе (не удаляется).
 10. Повторная генерация при активном окне продлевает/заменяет — старый
     токен перестаёт работать, новый работает, окно продлено.
"""
import time

from tests.pg_helper import fresh_automation_key_store, requires_pg

pytestmark = requires_pg


def _rows():
    """Все строки tg_automation_windows этого сервера — читаем НАПРЯМУЮ, а
    не через window_status(), чтобы проверка "запись осталась в базе" не
    зависела от той же функции, что мы тестируем."""
    from ticktick_mcp.src import automation_key as ak

    with ak._conn() as cur:
        cur.execute(
            "SELECT window_id, token_hash, created_at, expires_at, revoked_at, "
            "created_by_chat FROM tg_automation_windows WHERE server = %s",
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
        assert ak.revoke_window("4242") is False
        assert ak.window_status("4242") is None
    finally:
        ak._pg_pool = None


# ═══════════ 2. Токен НЕ работает после истечения (без отзыва) ═══════════

def test_token_stops_working_after_expiry_without_revoke(monkeypatch):
    ak = fresh_automation_key_store()
    # Часы дробные специально: секунды, а не сутки, чтобы тест не спал долго.
    monkeypatch.setattr(ak, "AUTOMATION_WINDOW_HOURS", 0.2 / 3600)  # 0.2 c

    token = ak.generate_window("4242")
    assert ak.check_window(token) is True

    time.sleep(0.35)

    assert ak.check_window(token) is False, "истёкший токен всё ещё проходит"
    status = ak.window_status("4242")
    assert status["expired"] is True
    assert status["revoked"] is False
    assert status["active"] is False


# ═══════════ 3. Токен НЕ работает после явного отзыва (до истечения) ═══════

def test_token_stops_working_after_explicit_revoke(monkeypatch):
    ak = fresh_automation_key_store()
    monkeypatch.setattr(ak, "AUTOMATION_WINDOW_HOURS", 24.0)

    token = ak.generate_window("4242")
    assert ak.check_window(token) is True

    revoked = ak.revoke_window("4242")

    assert revoked is True, "revoke_window сказал, что гасить было нечего"
    assert ak.check_window(token) is False, "отозванный токен всё ещё проходит"
    status = ak.window_status("4242")
    assert status["revoked"] is True
    assert status["expired"] is False
    assert status["active"] is False


def test_revoke_without_any_window_reports_nothing_to_revoke():
    ak = fresh_automation_key_store()
    assert ak.revoke_window("4242") is False
    assert ak.window_status("4242") is None


# ═══════ 7. Просроченная/отозванная запись остаётся в базе (не удаляется) ═══

def test_revoked_row_is_not_deleted_it_stays_in_the_table(monkeypatch):
    ak = fresh_automation_key_store()
    monkeypatch.setattr(ak, "AUTOMATION_WINDOW_HOURS", 24.0)
    ak.generate_window("4242")
    assert len(_rows()) == 1

    ak.revoke_window("4242")

    rows = _rows()
    assert len(rows) == 1, "отозванная строка исчезла из базы — должна остаться"
    assert rows[0][4] is not None, "revoked_at не проставлен"


def test_expired_row_is_not_deleted_it_stays_in_the_table(monkeypatch):
    ak = fresh_automation_key_store()
    monkeypatch.setattr(ak, "AUTOMATION_WINDOW_HOURS", 0.2 / 3600)
    ak.generate_window("4242")
    time.sleep(0.35)

    assert ak.check_window("что-угодно") is False  # окно уже истекло
    rows = _rows()
    assert len(rows) == 1, "просроченная строка исчезла из базы — должна остаться"
    assert rows[0][3] < ak._now_ms(), "expires_at не в прошлом — тест не про то"
    assert rows[0][4] is None, "revoked_at не должен быть проставлен истечением"


# ═══ 10. Повторная генерация при активном окне продлевает/заменяет ═══

def test_regenerating_an_active_window_invalidates_the_old_token(monkeypatch):
    ak = fresh_automation_key_store()
    monkeypatch.setattr(ak, "AUTOMATION_WINDOW_HOURS", 24.0)

    old_token = ak.generate_window("4242")
    assert ak.check_window(old_token) is True

    new_token = ak.generate_window("4242")

    assert new_token != old_token
    assert ak.check_window(old_token) is False, "старый токен должен перестать работать"
    assert ak.check_window(new_token) is True, "новый токен должен работать"


def test_regenerating_extends_expiry_and_reuses_the_same_row(monkeypatch):
    """«Продлевает» — не только новый токен, но и новый expires_at, и это
    ЗАМЕНА строки (UPSERT), а не вторая строка рядом (см. TZ §3.6.4: «старый
    хэш инвалидируется заменой строки»)."""
    ak = fresh_automation_key_store()
    monkeypatch.setattr(ak, "AUTOMATION_WINDOW_HOURS", 24.0)

    ak.generate_window("4242")
    first_expiry = _rows()[0][3]

    time.sleep(0.05)
    ak.generate_window("4242")

    rows = _rows()
    assert len(rows) == 1, "повторная генерация завела ВТОРУЮ строку вместо замены"
    assert rows[0][3] > first_expiry, "expires_at не продлился"


def test_regenerating_after_revoke_reactivates_the_window(monkeypatch):
    """Повторное нажатие «Показать ключ» после «Выключить» — новое окно
    обязано снова быть активным (revoked_at сбрасывается UPSERT'ом), а не
    навсегда мёртвым из-за старой отметки об отзыве."""
    ak = fresh_automation_key_store()
    monkeypatch.setattr(ak, "AUTOMATION_WINDOW_HOURS", 24.0)

    ak.generate_window("4242")
    ak.revoke_window("4242")
    assert ak.window_status("4242")["revoked"] is True

    new_token = ak.generate_window("4242")

    assert ak.check_window(new_token) is True
    assert ak.window_status("4242")["active"] is True
    assert ak.window_status("4242")["revoked"] is False


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
        ak._pg_pool = None
