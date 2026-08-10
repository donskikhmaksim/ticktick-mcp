"""Состояние манифестов после разноса — ОДИН объект, а не две копии.

Пункт 1.2.4 захода 1 увёл гейт согласия из `server.py` в `consent.py`.
`_MANIFESTS` — обычный словарь на уровне модуля, в котором живут ВСЕ выданные
планы: что именно удалить, чей это план, до какого момента он действителен.
Если при переносе он скопируется в два объекта, поломка выйдет тихой и
страшной одновременно: план, записанный одним путём, второму пути не виден —
владелец жмёт «подтвердить», а сервер отвечает «манифест не найден»; либо
наоборот, погашенный манифест остаётся живым во второй копии и его можно
предъявить повторно.

Обычные тесты такую поломку могут не заметить: они работают в одном процессе
и часто ходят одним и тем же путём. Поэтому проверка явная и двусторонняя.
"""
import time

import ticktick_mcp.src.consent as c
import ticktick_mcp.src.server as s
import ticktick_mcp.src.tg_auto_execute as t


def test_write_through_consent_is_visible_through_server():
    """Записали манифест через `consent._MANIFESTS` — читаем через путь
    `server`, и обратно. Оба пути обязаны видеть одно и то же."""
    assert s._MANIFESTS is c._MANIFESTS, (
        "server и consent держат РАЗНЫЕ словари манифестов — план, выданный "
        "одним путём, второму не виден")

    mid = "delete 999999"
    payload = {"kind": "delete", "created_ms": int(time.time() * 1000)}
    c._MANIFESTS[mid] = payload
    try:
        assert s._MANIFESTS.get(mid) is payload
        # и в обратную сторону: гашение через server видно в consent
        s._MANIFESTS[mid] = {"kind": "delete", "consumed": True}
        assert c._MANIFESTS[mid]["consumed"] is True
    finally:
        c._MANIFESTS.pop(mid, None)
    assert mid not in s._MANIFESTS


def test_tombstones_and_journal_are_shared_too():
    """Тот же вопрос для соседнего состояния гейта: надгробия манифестов и
    каталог журнала операций. Их читают все три модуля."""
    assert s._MANIFEST_TOMBSTONES is c._MANIFEST_TOMBSTONES
    assert s._MANIFESTS is t._MANIFESTS, (
        "модуль кнопки держит свою копию манифестов — нажатие на кнопку "
        "не найдёт план, выданный через чат")


def test_patching_the_journal_dir_on_server_reaches_the_gate(tmp_path,
                                                             monkeypatch):
    """Подмена атрибута на модуле `server` доезжает до кода, который этим
    именем пользуется. До разноса это было само собой (одно пространство
    имён); после — держится на явном пробросе в конце `server.py`. Если
    проброс убрать, тест краснеет здесь, а не в двух сотнях чужих проверок."""
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    assert c._JOURNAL_DIR == str(tmp_path), (
        "подмена server._JOURNAL_DIR не дошла до consent — журнал операций "
        "пишется мимо того места, куда смотрит проверка")
    assert t._JOURNAL_DIR == str(tmp_path)
