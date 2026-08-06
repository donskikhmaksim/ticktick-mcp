"""Тесты для честной фразы о сроке жизни манифеста (_manifest_ttl_phrase /
_duration_ru / _ru_plural, ticktick_mcp/src/server.py).

Контекст: раньше во всех текстах плана была намертво зашита строка
«действует 1 час», которая враньё, если TTL сконфигурирован иначе (найдено
на живом прогоне при TG_APPROVAL_TTL_S=6ч — сообщение всё равно утверждало
«1 час»). Эти тесты закрывают три вещи: (1) правильные русские склонения,
(2) что берётся именно MIN() из двух независимых TTL, когда включён
Telegram-слой, (3) что при выключенном слое TTL строки в Postgres вообще не
учитывается — используется только RAM-манифест.
"""
import ticktick_mcp.src.server as s
import ticktick_mcp.src.tg_approval as tg


# ===========================================================================
# _ru_plural — голая проверка склонений (без привязки к секундам/часам)
# ===========================================================================

def test_ru_plural_one_form_for_1_21_31():
    for n in (1, 21, 31, 101):
        assert s._ru_plural(n, "час", "часа", "часов") == "час", n


def test_ru_plural_few_form_for_2_3_4():
    for n in (2, 3, 4, 22, 23, 24):
        assert s._ru_plural(n, "час", "часа", "часов") == "часа", n


def test_ru_plural_many_form_for_5_to_20():
    for n in (0, 5, 6, 7, 8, 9, 10, 15, 20):
        assert s._ru_plural(n, "час", "часа", "часов") == "часов", n


def test_ru_plural_11_to_14_is_exception_always_many():
    # Ловушка: по последней цифре 11/12/13/14 выглядят как "one/few", но
    # правильно — «11 часов», а не «11 час»/«12 часа».
    for n in (11, 12, 13, 14, 111, 112, 113, 114):
        assert s._ru_plural(n, "час", "часа", "часов") == "часов", n


# ===========================================================================
# _duration_ru — форматирование секунд в русскую фразу целиком
# ===========================================================================

def test_duration_ru_hours_various_declensions():
    assert s._duration_ru(3600) == "1 час"
    assert s._duration_ru(2 * 3600) == "2 часа"
    assert s._duration_ru(4 * 3600) == "4 часа"
    assert s._duration_ru(5 * 3600) == "5 часов"
    assert s._duration_ru(11 * 3600) == "11 часов"
    assert s._duration_ru(21 * 3600) == "21 час"
    assert s._duration_ru(100 * 3600) == "100 часов"


def test_duration_ru_minutes_below_an_hour():
    assert s._duration_ru(30 * 60) == "30 минут"
    assert s._duration_ru(1 * 60) == "1 минута"
    assert s._duration_ru(2 * 60) == "2 минуты"


def test_duration_ru_seconds_below_a_minute():
    assert s._duration_ru(45) == "45 секунд"
    assert s._duration_ru(1) == "1 секунда"
    assert s._duration_ru(3) == "3 секунды"


def test_duration_ru_ninety_seconds_is_one_minute_thirty_not_seconds():
    # 90 c >= 60 c, значит это уже минуты (округление вниз до целой минуты).
    assert s._duration_ru(90) == "1 минута"


# ===========================================================================
# _manifest_ttl_phrase — реальная логика выбора минимума из двух TTL
# ===========================================================================

def _tg_cfg(enabled: bool, ttl_s: int) -> tg.TgApprovalConfig:
    return tg.TgApprovalConfig(
        enabled=enabled, bot_token="x", owner_chat_id="1", server="ticktick",
        tools_allowlist=None, ttl_s=ttl_s)


def test_ttl_phrase_uses_manifest_ttl_when_tg_layer_disabled(monkeypatch):
    """ТГ-слой выключен — TG_APPROVAL_TTL_S не имеет значения (строка в
    Postgres даже не заводится), фраза должна опираться ТОЛЬКО на
    _MANIFEST_TTL, даже если бы cfg.ttl_s был меньше."""
    monkeypatch.setattr(s, "_MANIFEST_TTL", 3600.0)
    monkeypatch.setattr(s, "_TG_CFG", _tg_cfg(enabled=False, ttl_s=60))
    assert s._manifest_ttl_phrase() == "1 час"


def test_ttl_phrase_takes_minimum_when_tg_ttl_is_shorter(monkeypatch):
    """ТГ-слой включён, его TTL короче RAM-манифеста (1 час) — план
    перестаёт быть исполнимым раньше по Telegram-строке, значит честная
    фраза должна называть именно её, а не RAM-TTL."""
    monkeypatch.setattr(s, "_MANIFEST_TTL", 3600.0)
    monkeypatch.setattr(s, "_TG_CFG", _tg_cfg(enabled=True, ttl_s=30 * 60))
    assert s._manifest_ttl_phrase() == "30 минут"


def test_ttl_phrase_takes_minimum_when_manifest_ttl_is_shorter(monkeypatch):
    """Обратный случай: ТГ-TTL сконфигурирован ДЛИННЕЕ (например, 6 часов —
    ровно баг, найденный на живом прогоне), но RAM-манифест живёт всего
    час — исполнимость всё равно ограничена более коротким RAM-сроком."""
    monkeypatch.setattr(s, "_MANIFEST_TTL", 3600.0)
    monkeypatch.setattr(s, "_TG_CFG", _tg_cfg(enabled=True, ttl_s=6 * 3600))
    assert s._manifest_ttl_phrase() == "1 час"


def test_ttl_phrase_reflects_reconfigured_manifest_ttl_when_tg_disabled(monkeypatch):
    """Регрессия к самому найденному багу: TTL сконфигурирован НЕ на час
    (здесь — 6 часов) и ТГ-слой выключен — фраза обязана сказать «6 часов»,
    а не молчаливо соврать «1 час»."""
    monkeypatch.setattr(s, "_MANIFEST_TTL", 6 * 3600.0)
    monkeypatch.setattr(s, "_TG_CFG", _tg_cfg(enabled=False, ttl_s=3600))
    assert s._manifest_ttl_phrase() == "6 часов"


def test_ttl_phrase_equal_ttls_still_resolve_to_the_shared_value(monkeypatch):
    monkeypatch.setattr(s, "_MANIFEST_TTL", 3600.0)
    monkeypatch.setattr(s, "_TG_CFG", _tg_cfg(enabled=True, ttl_s=3600))
    assert s._manifest_ttl_phrase() == "1 час"
