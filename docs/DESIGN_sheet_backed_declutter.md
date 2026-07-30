# DESIGN: Sheet-backed манифест для declutter («разбор помойки»)

Статус: спека для реализации (пишет Sonnet). Ничего в коде ещё не тронуто.
Автор дизайна: архитектурный проход по реальному коду ticktick-mcp + sheets-mcp.
Дата: 2026-07.

---

## 1. Проблема и цель

### Как сейчас
`plan_declutter(scope, …)` (`server.py:2846`) читает все открытые задачи в scope,
строит `actions` (delete / rename / group + flags obsolete/dupe/nonsmart) и кладёт
манифест в **RAM-словарь `_MANIFESTS`** (`server.py:1828`, `2940`):

```python
_MANIFESTS[mid] = {"kind": "declutter", "actions": actions,
                   "mutating_count": n_mut, "created": time.monotonic(),
                   "summary": ..., "consumed": False}
```

Свойства текущего хранилища:
- **RAM only** — живёт в процессе сервера. Рестарт Railway (деплой, OOM, крэш) → манифест исчез.
- **TTL 1 час** (`_MANIFEST_TTL = 3600`, prune в `_prune_manifests` `server.py:1876`).
- **One-shot** — `execute_declutter` ставит `m["consumed"] = True` (`server.py:3055`),
  следующий `_prune` его удаляет. Повторно исполнить/докрутить нельзя.
- `execute_declutter(manifest_id, confirm="DECLUTTER <N>")` (`server.py:3026`) применяет
  **весь** `actions` целиком (нет частичного одобрения на уровне пунктов), маршрутизируя
  каждую правку через уже-аудированные `execute_task_deletion` / `update_tasks` /
  `set_task_parent` (guard + журнал + пост-сверка).

### Что ломается
Когда разбор большого списка идёт «в контексте LLM», модель галлюцинирует: теряет задачи,
путает ID, предлагает удалить не то. Единственная durable-запись — RAM-манифест, который
человек **не видит** и который **не переживает рестарт**. Нет resume прерванного разбора,
нет живого аудита прогресса, approval — только «да» в чат по одному большому тексту.

### Цель (инвариант)
**Состояние (список задач, ID, решения, статусы) живёт во ВНЕШНЕМ durable-хранилище
(Google Sheet). LLM только выносит вердикт по пункту (дубликат? SMART-переформулировка?).
Код детерминированно читает/пишет таблицу и исполняет — без «курьерства» ID через контекст
модели.** Из этого: durability + resume + живой аудит (человек видит прогресс прямо в таблице,
правит колонку `decision`).

---

## 2. Рекомендованная архитектура

### Развилка 1 — где живёт логика записи в Sheet: **Вариант A (внутри ticktick-mcp)**

**Выбор: A — ticktick-mcp сам читает/пишет одну фиксированную Google-таблицу через тонкий
sheet-клиент.** Опционально, за флагом `persist="sheet"`.

Почему не B (оркестрация на уровне скилла, Claude курьерит между двумя MCP):

> Инвариант фичи требует, чтобы **исполнитель читал одобренные ID из durable-хранилища
> САМ, детерминированно**. В варианте B единственный мост «одобренные строки таблицы → аргумент
> `execute_declutter`» проходит через контекст LLM — то есть именно тот курьер-канал, ради
> устранения которого фича и делается. B чинит durability плана, но НЕ убирает
> галлюцинацию на самом опасном шаге (перенос ID из таблицы в вызов execute). A убирает.

Цена A — связность и лишний секрет. Но она минимизируется тем, что клиент повторяет
**модель безопасности triage.ts**: **одна захардкоженная spreadsheetId**, физически не может
писать в другую таблицу (см. `triage.ts:16` — тот же приём делает triage-инструменты
`always_allow`). Это не «полный доступ ticktick-mcp к Google», а один прибитый гвоздём лист.

**Реализация доступа без тяжёлой зависимости.** У ticktick-mcp сейчас в deps только
`requests` (см. `pyproject.toml`). Не тащить `google-api-python-client`. Вместо этого:
- **service account** (JSON-ключ в env `GSHEETS_SA_JSON` или путь), таблица расшарена на
  его email;
- минт access-token: JWT (RS256) → `https://oauth2.googleapis.com/token`, кэш на ~55 мин;
- вызовы Sheets REST v4 (`spreadsheets.values.get/append/batchUpdate`) обычным `requests`.
- Единственная новая рантайм-зависимость — подпись JWT: добавить **`PyJWT[crypto]`** (лёгкая),
  либо `google-auth` (тоже приемлемо). НЕ полный google client.

Service account предпочтительнее OAuth-токена пользователя: нет refresh-пляски, нет
per-user хранилища токенов, доступ строго к одной расшаренной таблице.

Если добавление Google-кредов в ticktick-mcp окажется неприемлемым (решение Максима) —
fallback на B описан в §8 «Открытые вопросы», но по умолчанию делаем A.

### Развилка 2 — replace vs mirror: **Sheet = источник истины, RAM = кэш горячего пути**

- **Google Sheet — durable источник истины.** Каждая строка = одна предложенная правка со
  своим `status`/`decision`.
- **RAM `_MANIFESTS` остаётся как кэш** для быстрого same-process пути (план только что
  построен → execute в том же процессе): избегаем лишнего чтения таблицы. Но RAM больше
  **не единственный носитель** и не обязателен для исполнения.
- **Resume после рестарта:** RAM пуст → `execute_declutter` / `resume_declutter`
  **реконструирует манифест из строк листа** по `manifest_id`. Плановые снапшоты задач
  (нужны движку удаления для журнала) хранятся в колонке `snapshot_json` (§3), поэтому resume
  не зависит от RAM.

### Развилка 6 — фиксированная таблица vs новая на разбор: **одна фиксированная, один лист-лог**

- **Одна захардкоженная spreadsheetId** (как triage-log) + **один растущий лист `Declutter Log`**.
- Строки разных разборов различаются по `manifest_id` + `run_ts`. Полная аудит-история
  копится в одном месте, как triage-log.
- Плюс модель безопасности «физически одна таблица» → инструменты можно держать
  `always_allow` без риска записи в произвольный документ.
- (Опционально, если один лист распухнет: helper-архивация старых разборов в лист
  `Declutter Archive`. Не в MVP.)

### Развилка 7 — обратная совместимость: **строго opt-in**

- `plan_declutter(..., persist="sheet")` — новый режим. По умолчанию `persist="ram"` →
  **текущее поведение не меняется вообще** (тот же RAM-манифест, те же тексты).
- `execute_declutter` определяет режим по флагу в RAM-манифесте (`persist`), либо, если
  манифеста в RAM нет, пытается прочитать sheet-манифест по `manifest_id`.
- Новый инструмент `resume_declutter(manifest_id, confirm)` — явный вход для продолжения
  после рестарта/паузы (тонкая обёртка над той же sheet-исполняющей веткой).

---

## 3. Схема Google-листа (`Declutter Log`)

Одна строка = одна **предложенная правка** (или флаг). Заголовок пишется при первом
`ensureHeader` (как triage.ts:21).

| Кол | Поле | Смысл |
|-----|------|-------|
| A | `row_id` | Стабильный ID строки (монотонный, как triage `lastId+1`). Ключ адресации при update. |
| B | `manifest_id` | ID разбора (`mid` из plan). Группирует строки одного плана. |
| C | `run_ts` | Время построения плана (America/Los_Angeles, ISO). |
| D | `task_id` | ID задачи TickTick. Для `group`-детей — ID ребёнка. |
| E | `title` | Текущее название задачи (на момент плана). Человеко-читаемый якорь. |
| F | `project` | Имя проекта (`names.get(projectId)`). |
| G | `column` | Имя kanban-колонки (если известно), иначе пусто. |
| H | `cluster_id` | ID кластера/группы (дубликатный кластер или umbrella-группа) — чтобы человек видел связанные строки вместе. |
| I | `action` | `delete` / `rename` / `group` / `flag_obsolete` / `flag_dupe` / `flag_nonsmart`. |
| J | `proposed_value` | Для `rename` — new_title; для `delete` — `keep→<keep_id>` (кого оставить); для `group` — `parent→<parentId>`; для flags — пусто. |
| K | `reason` | Обоснование (из shim/rule). То, что уже печатается в текущем плане. |
| L | `decision` | `pending` / `approved` / `rejected`. **Правится человеком** (в таблице или через инструмент). Источник истины «что применять». |
| M | `status` | `planned` / `applying` / `done` / `failed` / `skipped`. Пишет **код**. |
| N | `applied_ts` | Когда применено (LA time). Пишет код. |
| O | `error` | Текст ошибки при `failed`. Пишет код. |
| P | `snapshot_json` | Компактный `_snapshot_of(task)` (JSON) — для журнала удаления и resume без RAM. |

Обоснование ключевых колонок:
- **`row_id` (A)** — адресация update по стабильному ID, а не по номеру строки (triage.ts:133
  делает ровно так: `findIndex` по колонке A — переживает переупорядочивание).
- **`decision` (L) отдельно от `status` (M)** — это два разных владельца. `decision` пишет
  **человек** (одобрение), `status` пишет **код** (ход исполнения). Разделение убирает гонку
  «кто последний записал» в одну ячейку.
- **`cluster_id` (H)** — дубли/umbrella-группы — многострочные; человек должен видеть, что
  «удалить X, оставить Y» — это одна связка. Иначе одобрит удаление, не увидев кого оставляем.
- **`snapshot_json` (P)** — движок удаления (`execute_task_deletion`) кладёт snapshot в
  журнал восстановления; при resume после рестарта RAM пуст, поэтому snapshot должен жить в
  таблице. Плановый snapshot + live-сверка identity-guard при исполнении.
- **flags (`flag_*`) тоже строки** — но с `decision=pending` и по умолчанию НЕ применяются
  (см. §4). Они в таблице ради аудита/видимости; человек может перевести obsolete-строку в
  `approved`, только явно поменяв `action` — это осознанный отдельный шаг (в MVP flags
  остаются информационными, apply их не трогает).

**Дефолт `decision` на записи плана:** `delete`/`rename`/`group` → `approved`
(текущее поведение — execute применяет всё, что предложено); `flag_*` → `pending` (никогда не
применяется автоматически). Это сохраняет семантику «протухшие и спорные не входят»
(`server.py:3016`). Максим может **снять** одобрение (approved→rejected) прямо в таблице.

---

## 4. Изменения API / сигнатуры

### 4.1 `plan_declutter` — новый параметр `persist`

```python
async def plan_declutter(scope: str = "", dry_run: bool = True,
                         max_tasks: int = 0,
                         persist: str = "ram") -> str:   # NEW
```
- `persist="ram"` (default) — без изменений.
- `persist="sheet"`:
  1. Строит `actions` как сейчас (`_dc_analyze`).
  2. **Пишет строки в лист** `Declutter Log` через новый модуль (§4.4):
     по строке на каждый delete/rename/group-ребёнок/flag, с `manifest_id=mid`,
     `run_ts`, `decision` по правилу выше, `status=planned`, `snapshot_json`.
  3. Кладёт в RAM облегчённый манифест-указатель:
     `_MANIFESTS[mid] = {"kind":"declutter","persist":"sheet","spreadsheet":<id>,
      "mutating_count":n_mut,"created":...,"consumed":False, "actions":actions}`
     (`actions` оставляем в RAM как кэш; sheet — источник истины при resume).
  4. В печатаемый план добавляет строку: «📄 План записан в таблицу `<url>` — можешь править
     колонку `decision` прямо там; затем `execute_declutter(...)` применит одобренное».

### 4.2 `execute_declutter` — читает решения из листа (когда sheet-backed)

Сигнатура **не меняется** (`manifest_id`, `confirm`). Внутри:
- Если манифест RAM-only (`persist!="sheet"`) → **текущая ветка без изменений**.
- Если sheet-backed:
  1. **Свежо читает** строки листа по `manifest_id` (не берёт `decision` из RAM — человек мог
     править таблицу после плана).
  2. Отбирает `apply_rows = decision==approved AND status in (planned, failed) AND action in
     (delete, rename, group)`.
  3. `N_approved = len(apply_rows)`; **`confirm` должен быть `DECLUTTER <N_approved>`**
     (не старое n_mut из плана). Несовпадение → refuse + переспросить (см. §6 гонки).
  4. Помечает apply_rows `status=applying` (лок от двойного запуска).
  5. Применяет **через те же аудированные под-инструменты** (`execute_task_deletion` /
     `update_tasks` / `set_task_parent`) — реконструируя их вход из строк листа
     (`task_id`, `snapshot_json`, `proposed_value`).
  6. **Пишет назад по строкам** результат: `status=done|failed`, `applied_ts`, `error`.
     Реконсиляция per-row из operation_report/пост-сверки под-инструмента.
  7. НЕ ставит `consumed=True` жёстко: sheet-манифест может быть докручен (resume). RAM-запись
     можно `consumed`, но строки листа остаются как аудит; повторный execute применит только
     ещё-не-`done` approved-строки (идемпотентность §5).

### 4.3 Новый инструмент `resume_declutter(manifest_id, confirm)`

Тонкая обёртка: явный вход для продолжения после рестарта/паузы, когда RAM-манифеста уже нет.
Внутри — та же sheet-исполняющая ветка `execute_declutter` (общий приватный
`_execute_declutter_from_sheet(manifest_id, confirm)`), плюс дружелюбное сообщение, если
`manifest_id` в листе не найден.

Опционально `set_declutter_decision(manifest_id, row_ids, decision)` — программное одобрение/
отклонение (альтернатива ручной правке колонки L), чтобы Claude мог по «да/нет» из чата
проставить `decision` детерминированно, **не касаясь исполнения**. Разделяет approval и apply.

### 4.4 Новый модуль sheet-клиента: `ticktick_mcp/src/declutter_sheet.py`

Изолирует весь Google-I/O (по образцу triage.ts, но на Python/requests):
```python
SPREADSHEET_ID = os.environ["DECLUTTER_SHEET_ID"]   # одна фиксированная таблица
SHEET_NAME = "Declutter Log"

def _access_token() -> str: ...          # SA JWT → oauth token, кэш ~55 мин
def ensure_header() -> None: ...         # как triage.ts:21
def append_rows(rows: list[dict]) -> list[int]: ...   # вернуть присвоенные row_id
def read_manifest_rows(manifest_id: str) -> list[dict]: ...
def update_row(row_id: int, **fields) -> None: ...    # адресация по колонке A
def batch_update_rows(updates: list[dict]) -> None: ... # values.batchUpdate
def sheet_url() -> str: ...
```
`SPREADSHEET_ID` из env (не хардкод-строкой в коде — но одна на инстанс, эффект тот же:
инструмент физически не пишет в другую таблицу).

---

## 5. Идемпотентность и edge-cases (Развилки 4)

Гарантия «не удалить дважды / отличить done от failed / корректный resume»:

1. **`status` write-through per row.** Сразу после применения каждой строки (или каждого
   батча) её `status` пишется в лист. Прерывание (крэш/таймаут) → на диске уже отражено,
   что успело примениться.
2. **Resume пропускает `done`.** `apply_rows` фильтрует `status in (planned, failed)`.
   `done` никогда не переисполняется → **не удалит дважды**.
3. **`applying`-лок.** Перед применением строки помечаются `applying`. Параллельный второй
   запуск (или повторный execute) увидит `applying` и не возьмёт их (или возьмёт только после
   таймаута лока — см. ниже). После завершения → `done|failed`.
4. **Зависший `applying`.** Если процесс умер посередине (строка осталась `applying`, реально
   применена или нет — неизвестно): resume сверяет **живое состояние** через identity-guard
   под-инструментов. Для `delete`: если задача уже отсутствует в TickTick → строка `done`
   (эффект достигнут, идемпотентно). Для `rename`: если live-title уже == proposed → `done`.
   Иначе — переприменить. Т.е. решение о зависших строках принимается по **факту в TickTick**,
   а не по флагу в таблице.
5. **`done` vs `failed`.** Разные значения `status`; `failed` несёт `error` в колонке O.
   `failed` **можно ретраить** (входит в `apply_rows`), `done` — нет.
6. **Частично применённый батч.** Текущий execute батчит delete через под-манифест. Для
   per-row статуса: после возврата батча реконсиляция из operation_report/пост-сверки — каждая
   `task_id` из батча помечается `done` (подтверждено удалена) или `failed` (осталась). Если
   under-the-hood батч атомарен не полностью — именно пост-сверка (а не «батч вернул 200»)
   определяет per-row статус.
7. **Задача изменилась между plan и execute.** Identity-guard под-инструментов (уже
   существующий) отловит рассинхрон (напр. задача уже удалена/переименована/перемещена) и
   вернёт это в отчёте → строка `failed` с внятным `error`, остальные применяются. Плановый
   `snapshot_json` — для журнала и для сверки «то ли это, что мы видели».
8. **TTL.** Sheet-манифест **не истекает по TTL** (лист durable). RAM-указатель может
   истечь/очиститься — это норм, resume читает лист. `_prune_manifests` не трогает лист.
9. **Дубликат `keep`-задачи тоже одобрен на удаление?** Инвариант: `keep_id` строки delete
   не должен сам быть в approved-delete того же кластера. Проверка перед apply: если
   `keep_id` попадает в множество удаляемых `task_id` → refuse по кластеру (`cluster_id`),
   строки в `failed` с пояснением. (Защита от ручной ошибки в колонке `decision`.)

---

## 6. Approval: два пути без гонок (Развилка 5)

Два канала одобрения:
- **(a) Человек правит колонку `decision` прямо в таблице** (approved/rejected).
- **(b) Подтверждение в чате** («да») → Claude вызывает execute (или сначала
  `set_declutter_decision`).

Согласование:
1. **Источник истины «что применять» — колонка `decision` в листе, читаемая execute СВЕЖО**
   в момент запуска (не на момент плана). Поздние правки в таблице всегда учитываются.
2. **`confirm`-токен = консистентность-чек между каналами.** execute вычисляет
   `N_approved` из свежего чтения листа и требует `confirm == "DECLUTTER <N_approved>"`.
   Если человек поменял `decision` в таблице после того, как Claude напечатал план с
   N из плана — числа разойдутся → **refuse + переспросить** («в таблице сейчас одобрено M
   правок, не N; перечитай план и подтверди `DECLUTTER M`»). Тот же приём, что и сейчас
   (`server.py:3052`), но N берётся из durable-состояния, а не из RAM-плана.
3. **`applying`-лок** (§5.3) предотвращает гонку двух одновременных execute (второй видит
   `applying`/`done` и не дублирует).
4. **`set_declutter_decision`** (опц.) даёт детерминированный путь (b): Claude по «удали
   первые три, остальное нет» проставляет `decision` по `row_id` — **без** касания
   исполнения. Затем execute читает уже-записанные решения. Так «выносит вердикт» LLM, но
   «что реально применить» всё равно читается кодом из листа.

---

## 7. Поток данных plan → review → execute → resume (пошагово)

Легенда: **[код]** — детерминированная логика ticktick-mcp; **[LLM]** — Claude; **[human]** — Максим.

**PLAN**
1. [LLM] вызывает `plan_declutter(scope, persist="sheet")`.
2. [код] читает открытые задачи, `_dc_analyze` → `actions` (как сейчас).
3. [код] `ensure_header()`; `append_rows(...)` — строки delete/rename/group/flags в лист
   (`manifest_id`, `run_ts`, `snapshot_json`, `decision` по дефолту, `status=planned`).
4. [код] RAM-указатель + печатает план **и** ссылку на таблицу.
5. [LLM] печатает план вербатим пользователю.

**REVIEW**
6. [human] смотрит **живую таблицу**: связки дубликатов по `cluster_id`, кого оставляем.
   Правит `decision` (approved→rejected где не согласен) — прямо в Sheet, ИЛИ говорит Claude.
7. [LLM] (если через чат) `set_declutter_decision(...)` — [код] проставляет `decision`.

**EXECUTE**
8. [human] «да» → [LLM] `execute_declutter(manifest_id, confirm="DECLUTTER <N>")`.
9. [код] **свежо читает** approved-строки из листа; сверяет `N_approved` с `confirm`.
10. [код] `status=applying`; применяет через `execute_task_deletion`/`update_tasks`/
    `set_task_parent` (guard+журнал+пост-сверка); **write-through** `status=done|failed`,
    `applied_ts`, `error` per row.
11. [код] собирает независимые operation_report; возвращает сводку.
12. [LLM] печатает результат; человек видит финальные статусы **в таблице**.

**RESUME** (после рестарта/таймаута/частичного применения)
13. [LLM] `resume_declutter(manifest_id, confirm)` (RAM пуст).
14. [код] реконструирует из листа; `apply_rows = approved AND status in (planned, failed)`;
    зависшие `applying` разрешает по live-факту в TickTick (§5.4); применяет остаток
    идемпотентно; write-through.

Ключ: **между review и execute ID-шники и решения ни разу не проходят «через голову» LLM** —
код пишет их в лист и код же читает обратно. LLM только (1) выносит вердикт дубликат/SMART
(shim, как сейчас) и (2) реле человеческого «да».

---

## 8. Пошаговый план реализации для Sonnet

Трогаемые файлы: `ticktick_mcp/src/server.py` (declutter-блок ~2278–3108),
**новый** `ticktick_mcp/src/declutter_sheet.py`, `pyproject.toml` (+ dep), tests.

1. **Dep + креды.** Добавить в `pyproject.toml` `PyJWT[crypto]` (или `google-auth`). Env:
   `DECLUTTER_SHEET_ID`, `GSHEETS_SA_JSON` (service-account ключ). Задокументировать в README/
   `.env.example`. Расшарить таблицу на SA-email. **Не** тащить полный google client.

2. **`declutter_sheet.py`** — модуль-обёртка Sheets REST v4 на `requests`:
   `_access_token()` (JWT RS256 → oauth token, кэш ~55 мин), `ensure_header()`,
   `append_rows()` → присвоенные `row_id`, `read_manifest_rows(manifest_id)`,
   `update_row(row_id, **fields)` / `batch_update_rows()` (адресация по колонке A, как
   triage.ts:133), `sheet_url()`. Схема колонок из §3 как константа `HEADER`.
   Все функции gracefully-degrade: при отсутствии кредов/сети — понятная ошибка, НЕ крэш
   плана (см. §5 про degrade). Юнит-тесты с замоканным `requests`.

3. **`_dc_actions_to_rows(actions, manifest_id, run_ts, names) -> list[dict]`** в `server.py`
   — чистая функция: разворачивает `actions` (delete/rename/group-дети/flags) в плоский список
   строк листа по схеме §3 (включая `cluster_id`, `snapshot_json`, дефолтный `decision`).
   Юнит-тестируемо без I/O (как `_dc_analyze`).

4. **`_dc_rows_to_exec(rows) -> {delete, rename, group}`** — обратная чистая функция:
   из approved-строк листа реконструирует вход под-инструментов
   (`execute_task_deletion`-items со `snapshot`, `update_tasks`-changes, `set_task_parent`
   группы). Здесь же проверка инварианта `keep_id ∉ deletes` (§5.9). Юнит-тесты.

5. **`plan_declutter`**: добавить параметр `persist="ram"`; при `"sheet"` после `_dc_analyze`
   — `ensure_header` + `append_rows(_dc_actions_to_rows(...))`, RAM-указатель с `persist`,
   строка со ссылкой на таблицу в выводе. При недоступности листа — сообщить и (реши с
   Максимом, §9) либо фолбэк на ram, либо явный отказ. Ветка `persist="ram"` — байт-в-байт
   как сейчас.

6. **`_execute_declutter_from_sheet(manifest_id, confirm)`** — приватная sheet-ветка:
   свежее чтение (`read_manifest_rows`), фильтр `apply_rows` (§4.2/§5), сверка `N_approved` vs
   `confirm`, `applying`-лок, применение через существующие под-инструменты, per-row
   write-through `done|failed|applied_ts|error`, реконсиляция из operation_report. Разрешение
   зависших `applying` по live-факту (§5.4).

7. **`execute_declutter`**: диспетчер — RAM-only манифест → старая ветка (не менять);
   sheet-backed (или RAM отсутствует, но `manifest_id` есть в листе) → делегировать в (6).

8. **`resume_declutter(manifest_id, confirm)`** — новый `@mcp.tool()`; тонкая обёртка над (6)
   с дружелюбным «манифест `X` в таблице не найден», если пусто.

9. **`set_declutter_decision(manifest_id, row_ids, decision)`** (опц., но желательно) —
   `@mcp.tool()`: `batch_update_rows` колонки `decision`. Не касается исполнения.

10. **Тесты (обязательно):**
    - `_dc_actions_to_rows` / `_dc_rows_to_exec` round-trip (actions→rows→exec-вход эквивалентно
      текущему прямому пути).
    - Идемпотентность: два прогона execute над одним листом — второй ничего не применяет
      (все `done`), не удаляет дважды (мок под-инструментов).
    - Resume: строки `planned`+`failed` применяются, `done` пропускаются, `applying` при
      «задача уже удалена» → `done` без повторного вызова delete.
    - Approval-гонка: `decision` изменён после плана → `N_approved != confirm` → refuse.
    - `keep_id ∈ deletes` → refuse по кластеру.
    - Degrade: лист недоступен → плановый путь не крэшит (по решению §9).
    - Обратная совместимость: `persist="ram"` — существующие declutter-тесты зелёные без правок.
    - `declutter_sheet` — с замоканным `requests` (append→row_id, update по колонке A,
      read фильтрует по `manifest_id`).

11. **Документация** в докстрингах `plan_declutter`/`execute_declutter`/`resume_declutter`:
    новый режим, ссылка на эту спеку, инвариант «состояние в таблице, не в контексте».

---

## 9. Открытые вопросы к Максиму

1. **Google-креды в ticktick-mcp — ок?** Вариант A требует service-account ключа и одной
   расшаренной таблицы в ticktick-mcp (сейчас Google там нет). Если категорически нет —
   переключаемся на B (оркестрация; execute получает approved-ID как аргумент от Claude,
   ценой сохранения курьер-канала через LLM). **Дизайн по умолчанию — A.**
2. **Одна таблица или отдельная от triage-log?** Предлагаю **отдельную** фиксированную
   spreadsheet (новый `DECLUTTER_SHEET_ID`), не смешивать с email triage-log. Подтвердить.
3. **Лист недоступен на этапе plan** — фолбэк на `persist="ram"` (тихо продолжить в RAM) или
   явный отказ («не могу сохранить план durable, повтори позже»)? Для durability-инварианта
   логичнее **явный отказ**, но это грубее по UX.
4. **flags (obsolete/dupe/nonsmart) в таблице** — держать информационными (как сейчас, apply
   не трогает) или дать полноценный apply-путь (человек в таблице переводит obsolete→approved и
   код доводит/удаляет)? MVP: **информационные**. Подтвердить, нужен ли apply-путь для flags.
5. **Права на таблицу.** SA с writer-доступом к одной таблице; Максим — owner. Норм?
6. **`applied_ts` таймзона** — America/Los_Angeles (как всё у Максима), не UTC. Подтверждаю.

---

## Приложение: якоря в реальном коде

- RAM-манифест / TTL / prune: `server.py:1828–1880`.
- `_snapshot_of`: `server.py:1848`.
- `plan_declutter`: `server.py:2846–3022`; запись манифеста `2940`.
- `execute_declutter`: `server.py:3026–3107`; `consumed=True` `3055`; под-инструменты
  delete `3072` / rename `3078` / group `3087`; сверка confirm `3052`.
- `_dc_analyze` (структура actions): `server.py:2511–2713`.
- v2 batch-методы: `ticktick_v2_client.py` — `batch_delete_tasks:375`, `batch_update_tasks:385`,
  `batch_set_task_parent:337`, `batch_move_tasks:269`, `get_project_columns:618`.
- Образец Sheet-backed лога: `G-MCP/services/sheets-mcp/src/tools/triage.ts` —
  хардкод spreadsheetId `:16`, ensureHeader `:21`, add `:48`, update-по-колонке-A `:124–157`,
  get_pending-фильтр `:161`.
