# 1.1.3 — П6 пункты 1-3: инвентаризация непогашенных планов автоуборки

Прогон: 2026-08-10 18:58 UTC, боевая база (`CONSENT_DATABASE_URL`, Railway,
проект `ticktick-mcp`, окружение `production`). Только чтение
(`conn.set_session(readonly=True)`), ничего не менялось. Скрипт:
`/private/tmp/claude-501/-Users-maksim-Code-ticktick-mcp/a46f0aeb-cc1b-4d0f-9ebb-21dbb31b261e/scratchpad/inventory_1_1_3.py`,
DSN получен через `railway variables --service ticktick-mcp --kv`, нигде не
печатался в открытом виде.

## Пункт 1 — непогашенные планы автоуборки (`mcp_manifests`)

```sql
SELECT manifest_id, tool, payload->>'kind' AS kind,
       to_timestamp(created_at/1000) AS created,
       to_timestamp(expires_at/1000) AS expires
  FROM mcp_manifests
 WHERE server = 'ticktick'
   AND consumed_at IS NULL
   AND (tool IN ('plan_declutter','execute_declutter',
                 'resume_declutter','set_declutter_decision')
        OR payload->>'kind' = 'declutter');
```

**Результат: 0 строк.** Непогашенных планов автоуборки в базе нет —
гашение (пункт 2 задания, `manifest_store.mark_consumed`) не требуется,
списка идентификаторов нет.

Сверка `count(*)` (для честности, что база вообще не пуста и запрос не
провалился молча):

```sql
SELECT count(*) FROM mcp_manifests WHERE server = 'ticktick';
```

Результат: **11** строк всего для `server='ticktick'` (ни одна не относится
к declutter — что и подтверждает нулевой результат выше не как сбой
запроса, а как реальное отсутствие таких строк).

## Пункт 3 — зависшие подтверждения (`tg_approvals`)

Целевой запрос (по идентификаторам из пункта 1 — список пуст, поэтому не
выполнялся, честно так и записано):

```sql
SELECT manifest_id, status, to_timestamp(expires_at/1000) AS expires
  FROM tg_approvals
 WHERE server = 'ticktick'
   AND status IN ('PENDING','APPROVED')
   AND manifest_id = ANY(<пустой список>);
```

Результат: **0 строк** (запрос тривиально пуст — не выполнялся, id сверять
не с чем).

Дополнительно (шире, не входит в критерий, но для полноты картины): всего
`PENDING`/`APPROVED` строк для `server='ticktick'` — **10**, все со сроком
действия в пределах ближайшего часа от момента прогона (18:58–19:44 UTC
10.08.2026) — это текущие, недавние подтверждения обычной работы сервера,
не зависшие остатки автоуборки. Отдельно не расследовалось — вне периметра
этого пункта.

## Пункт "Третий резервуар" (Google Sheet, колонка `decision`)

Не проверялось в этом прогоне — по заданию это "уборка вглубь", а не
закрытие живой дыры (строки там сегодня недосягаемы, т.к. декоратор снят и
`_resolve_auto_executor` отказывает независимо от их значения). Число в
отчёт не внесено; если понадобится — отдельный проход с доступом к Sheets
MCP.

## Итог

Пункты 1-2-3 задания 1.1.3 (найти и погасить непогашенные планы автоуборки
и зависшие подтверждения) закрываются как **"инвентаризация проведена,
находок ноль, гашение не требовалось"** — не потому что уборка не делалась,
а потому что убирать было нечего на момент проверки. Классовая защита
(пункт 4 пакета П6 — `_resolve_auto_executor` проверяет
`_tool_registration_status`) отдельно подтверждена независимым аудитом ранее
и покрыта тестом `tests/test_executor_registration_guard.py` (10 тестов,
зелёные) — само название файла в `ZAHOD1.md` (`test_auto_executor_registry_guard.py`)
устарело относительно фактического имени, это не дефект защиты.
