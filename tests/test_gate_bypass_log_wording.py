"""Формулировка аудит-строки обхода гейта (QA-2 2026-08-19, добор №8).

`_log_gate_bypass` пишется ДО исполнения — в `delete_tasks`/
`plan_task_creation`/`plan_task_deletion` она стоит перед вызовом
исполнителя, а дальше по пути ещё возможен отказ (протухший манифест, пустой
список, identity guard). Старый текст «выполнено БЕЗ подтверждения» в этих
случаях оставался в логе ЛОЖЬЮ о выполнении, которого не было. Новый —
«допущено к исполнению»: честное утверждение о том, что РЕАЛЬНО произошло к
моменту записи; сам итог исполнения фиксирует журнал мутаций.
"""
import logging

import ticktick_mcp.src.consent as consent


def test_bypass_log_says_admitted_not_done(caplog):
    with caplog.at_level(logging.WARNING):
        consent._log_gate_bypass("delete", "delete_tasks")
    lines = [r.message for r in caplog.records if "ГЕЙТ ВЫКЛЮЧЕН" in r.message]
    assert lines, "строка обхода не записалась вовсе"
    line = lines[0]
    assert "допущено к исполнению" in line, line
    assert "выполнено" not in line, (
        "лог снова утверждает исполнение ДО исполнения: " + line)
    # Контекст на месте: и действие, и инструмент, и имя переключателя.
    assert "delete" in line and "delete_tasks" in line
    assert consent._GATE_DISABLED_ENV in line
