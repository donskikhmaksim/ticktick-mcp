"""Shared fixtures. Env is set BEFORE importing the server module so its
module-level config (SECRET, _USER_TZ) reads deterministic values."""
import os

os.environ.setdefault("MCP_SECRET", "test-secret")
os.environ.setdefault("USER_TIMEZONE", "UTC")
os.environ.setdefault("TICKTICK_CLIENT_ID", "cid")
os.environ.setdefault("TICKTICK_CLIENT_SECRET", "csecret")
os.environ.setdefault("TICKTICK_ACCESS_TOKEN", "atoken")
# The consent gate (docs/DESIGN_approval_gate.md §4.3.4) requires a minimum
# wall-clock gap between plan_* and execute_*(user_reply=...) to catch a
# model firing both in the same turn. Unit tests build a manifest and consume
# it within microseconds on purpose — default that gap to 0 here so ordinary
# tests aren't fighting a timer; a dedicated consent test overrides this back
# to a positive value with monkeypatch to exercise the gap itself.
os.environ.setdefault("MIN_CONSENT_GAP", "0")
# ТЗ_consent_web_hub.md, часть 1 (2026-08-12): гибридное ожидание внутри
# _gate_batch/_gate_single. Дефолт в проде ненулевой (25000мс) — тесты,
# которые ХОТЯТ проверить именно эту ветку, override'ят monkeypatch'ем
# consent.CONSENT_SYNC_WAIT_MS/CONSENT_SYNC_POLL_MS на маленькие значения.
# Обычные тесты (которых десятки — любой тест, планирующий манифест через
# _gate_batch/_gate_single) НЕ должны платить 25с ожидания на каждый план —
# ноль здесь даёт побайтовую совместимость со старым поведением (ТЗ, тест 1).
os.environ.setdefault("CONSENT_SYNC_WAIT_MS", "0")

import json  # noqa: E402


def pytest_configure(config):
    """Маркер `triage_e2e("<тип>")` — «этот тест совершает через агрегатор то
    же действие, что совершал закрытый прямой метод, и судит по живому
    состоянию» (2026-08-10, §1.3.4 Б).

    Объявляется здесь, чтобы pytest не считал его опечаткой и чтобы его смысл
    был записан в одном месте, а не только в тесте покрытия."""
    config.addinivalue_line(
        "markers",
        "triage_e2e(op): сквозной тест типа операции агрегатора "
        "apply_task_changes — доказательство, что у закрытого прямого метода "
        "есть ПРОВЕРЕННАЯ замена, а не строка в таблице")


def pytest_collection_modifyitems(session, config, items):
    """Выгрузка «какие типы покрыты помеченными тестами» для теста покрытия.

    Пишется только когда задана `TRIAGE_E2E_DUMP` — то есть в подпроцессе,
    который запускает tests/test_hidden_tools_coverage.py. Обычный прогон
    ничего не пишет и ничего не платит."""
    dump = os.environ.get("TRIAGE_E2E_DUMP")
    if not dump:
        return
    found = {}
    for item in items:
        for mark in item.iter_markers("triage_e2e"):
            for op in mark.args:
                found.setdefault(op, []).append(item.nodeid)
    with open(dump, "w", encoding="utf-8") as fh:
        json.dump(found, fh, ensure_ascii=False)
