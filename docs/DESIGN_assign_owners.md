# DESIGN: `assign_owners` — назначение владельцев бесхозным задачам в shared-проектах

Статус: спека для реализации (пишет Sonnet). Ничего в коде не тронуто.
Автор дизайна: архитектурный проход по реальному коду ticktick-mcp + declutter-спеке.
Дата: 2026-07.
Связанные документы: `docs/DESIGN_sheet_backed_declutter.md` (Sheet-манифест — переиспользуем его целиком).

> **ГЛАВНОЕ, сразу:** назначить исполнителя технически **МОЖНО**. Это подтверждено кодом
> (`ticktick_v2_client.batch_update_tasks` мёржит поле `assignee` на свежий объект задачи и
> POST-ит в `/batch/task`; ровно этот путь уже работает в `create_tasks`/`update_tasks`,
> `server.py:1418-1422`, `1502-1503`). Никакого фолбэка «API не умеет» не требуется. Единственные
> ограничения: задача должна быть в **shared-проекте**, а `assignee` — это **userId участника**
> этого проекта (из `get_project_members`).

---

## 1. Проблема и цель

### Проблема
В общих (shared) проектах TickTick масса задач **без назначенного исполнителя**
(`assignee` пуст). Никто не знает, чья задача → она провисает. Расставлять владельцев вручную по
одной — долго; делать это «в контексте LLM» на большом списке — модель путает userId, теряет
задачи, галлюцинирует (та же болезнь, ради которой declutter переехал на Sheet-манифест).

### Цель
Отдельный инструмент (НЕ часть declutter — решение Максима), который:
1. Находит **бесхозные** задачи в заданном scope (только shared-проекты, `assignee` пуст).
2. По каждой **предлагает** владельца из числа участников проекта (правило + shim-судья).
3. Человек **одобряет/правит** предложения (в durable-таблице или через чат).
4. Код детерминированно проставляет `assignee` одобренным задачам через уже-аудированный путь.

Инвариант — тот же, что у declutter: **состояние (список задач, ID, предложенные владельцы,
решения, статусы) живёт во внешнем durable-хранилище (Google Sheet), а не в контексте модели.**
LLM только выносит вердикт «кому это» по спорным пунктам; код читает таблицу и исполняет.

Поток: **`plan_assign` → review (Sheet/чат) → `execute_assign`** (+ `resume_assign`,
`set_assign_decision`) — калька declutter `plan/execute/resume/set_decision`.

---

## 2. Как ставится assignee (подтверждено из кода)

Механизм — **готовый, переиспользуем существующий аудированный путь `update_tasks`**:

- `update_tasks(summary, [{"taskId", "projectId", "title", "assignee": <userId>}])`
  - `title` передаём **текущий** — для identity-guard (`_guard_task`, `server.py:1374`), чтобы не
    назначить исполнителя не на ту задачу при устаревшем ID.
  - `new_title` НЕ передаём → название не трогается; основной `update_task` делает no-op по title,
    а под-шаг assignee срабатывает: `ticktick_v2.batch_update_tasks([{"taskId": tid,
    "assignee": uid}])` (`server.py:1418-1422`). Сбой под-шага уходит в текст результата, не
    прячется.
  - Работает и на одиночном пути (`len==1`/advanced), и на батч-пути (`server.py:1502-1503`
    кладёт `ch["assignee"]`). Оба — с guard'ом.
- Требования API: `assignee` = **userId** из `get_project_members(projectId)`; проект **shared**;
  нужен **v2** (`ticktick_v2` доступен). Если v2 недоступен — `update_tasks` сам вернёт понятное
  «исполнитель требует v2 API» (`server.py:1426`).
- Пост-проверка «применилось ли»: перечитать живое состояние (`_open_by_id(fresh=True)`) и
  сравнить `task.get("assignee") == uid` (поле `assignee` реально присутствует на задачах —
  `server.py:6102`, `6377`). Ровно как declutter проверяет rename через `_names_agree`.

Вывод: **execute_assign НЕ вызывает `batch_update_tasks` напрямую** — он строит `changes` и
зовёт существующий `update_tasks`, так же как `_execute_declutter_from_sheet` зовёт `update_tasks`
для переименований (`server.py:3131-3137`). Ноль нового опасного I/O, весь guard/журнал бесплатно.

---

## 3. Механизм предложения владельца

TickTick **не имеет** встроенного понятия «ответственный за колонку»: kanban-колонка
(`get_project_columns`) несёт только `{id, name, sortOrder, userId=создатель}` — не владельца
задач (`ticktick_v2_client.py:618`). Значит «по колонке → её ответственный» реализуемо только
через **явную карту от Максима**, не автоматически. Отсюда — многоуровневая схема, дешёвые
детерминированные правила сначала, shim-судья только для спорного (калька declutter: правило →
shim с bias «не уверен — не гадать»).

**Кандидаты** (для каждого проекта в scope): `get_project_members(projectId)` →
`{userId: displayName}` только по **принятым** участникам (`accepted != False`). Владельца проекта
включаем как валидного кандидата (иногда владелец и есть исполнитель), но правило «единственный»
считает по не-владельцам (см. ниже).

**Уровни предложения** (первый сработавший побеждает):

1. **Единственный кандидат.** Если в проекте ровно один принятый не-владелец участник →
   предложить его. `confidence=high`.
2. **Имя участника в задаче.** display-name участника (или его алиас из `hints`) встречается в
   `title`/`content`/тегах задачи (case-insensitive, по границе слова) → предложить его.
   `confidence=high`. (Пример: «Аня: позвонить в банк» → Аня.)
3. **Карта колонок (`hints`).** Параметр `hints` (см. §5) может задавать
   `{column_name → member}`: задачи в этой колонке получают этого владельца. Так реализуется
   «ответственный за колонку», которого в API нет. `confidence=high`.
4. **Shim-судья** (для оставшихся неоднозначных). `_asg_judge_fn` (калька `_dc_judge_fn`,
   `server.py:2717`) шлёт в `_dc_shim_json` батч: по каждой задаче — `title`+`content`(кратко)+имя
   колонки + список кандидатов `{i: name}`; ответ строго JSON:
   `[{"i":0,"uid":"<userId|null>","confidence":"low|med|high","reason":"<кратко>"}]`.
   **Bias безопасности:** не уверен → `uid=null` (лучше оставить человеку, чем назначить не того) —
   зеркало declutter «сомневаешься → keep-both».
5. **Нет уверенного предложения** (shim выключен/деградировал/вернул null): строка всё равно
   создаётся, но `proposed_value` пуст, `reason="уверенного кандидата нет — назначь вручную"`.
   Человек впишет владельца в таблице или через `set_assign_decision`.

**Дефолт `decision` для assign — `pending` ВСЕГДА** (в отличие от declutter, где
delete/rename/group по умолчанию `approved`). Причина: каждый владелец — это догадка, а неверный
исполнитель — реальная неприятность. Человек обязан подтвердить/поправить каждую строку. Это
единственное содержательное отличие маппинга от declutter.

---

## 4. Переиспользование Sheet-манифеста (точная привязка к схеме)

Переиспользуем модуль **`ticktick_mcp/src/declutter_sheet.py` как есть** — та же таблица
(`DECLUTTER_SHEET_ID`), та же 16-колоночная схема `HEADER` (A..P), те же
`ensure_header/append_rows/read_manifest_rows/batch_update_rows/update_row` с адресацией по
`row_id` (колонка A). Никакого нового хранилища.

**Отдельная вкладка, а не общий лог.** Рекомендация: assign-строки писать в **отдельный лист
`Assign Log`** в той же книге, чтобы не мешать их с `Declutter Log`. Реализация — **чисто
аддитивная, declutter не трогается**: в `declutter_sheet.py` добавить всем публичным функциям
необязательный параметр `sheet_name: str = SHEET_NAME` (диапазоны `A:P` строятся из него). При
дефолте — байт-в-байт текущее поведение declutter. assign зовёт те же функции с
`sheet_name="Assign Log"`.
*Фолбэк с нулевым касанием declutter_sheet.py* (если параметризацию решим не делать): писать
assign-строки в тот же `Declutter Log`, различая по `action="assign"` + своему `manifest_id` — это
полностью в духе declutter-развилки 6 «один растущий лист, строки различаются по manifest_id».
**По умолчанию — отдельная вкладка `Assign Log`.**

### Маппинг assign на колонки A..P (схема §3 declutter-спеки, без изменений схемы)

| Кол | Поле | Что кладёт assign |
|-----|------|-------------------|
| A | `row_id` | авто (`append_rows`, `lastId+1`) |
| B | `manifest_id` | id прогона assign (`mid`) — группирует строки одного плана |
| C | `run_ts` | время плана, America/Los_Angeles (`_dc_now_la_iso`) |
| D | `task_id` | ID задачи TickTick |
| E | `title` | текущее название задачи (человеко-якорь + для identity-guard при execute) |
| F | `project` | имя проекта (`names.get(projectId)`) |
| G | `column` | имя kanban-колонки задачи (контекст ревью + сигнал уровня 3) |
| H | `cluster_id` | пусто в MVP (опц.: id-группа по колонке, чтобы видеть задачи одного владельца рядом) |
| I | `action` | **`assign`** |
| J | `proposed_value` | **предложенный владелец** — `"<displayName>→<userId>"` (человек видит имя, код парсит uid). Пусто, если уверенного кандидата нет |
| K | `reason` | обоснование правила/шима + confidence (напр. «имя в заголовке (high)») |
| L | `decision` | **`pending`** (дефолт) → человек ставит `approved`/`rejected` или правит `proposed_value` |
| M | `status` | `planned`/`applying`/`done`/`failed`/`skipped` — пишет код |
| N | `applied_ts` | когда назначено (LA), пишет код |
| O | `error` | текст ошибки при `failed`, пишет код |
| P | `snapshot_json` | `_snapshot_of(task)` (projectId, title, columnId, assignee=пусто) — для resume/сверки без RAM |

**Парсинг `proposed_value` при execute — прощающий к ручной правке:** взять подстроку после
последнего `→` (если есть, иначе всю строку); `.isdigit()` → это userId; иначе — имя, резолвить в
uid по **свежему** `get_project_members` (case-insensitive). Так Максим может вписать в колонку J
хоть `Аня`, хоть `588f…`, хоть `Аня→588f…`.

**Идентично declutter (не переизобретаем):** durability, resume из строк листа, `decision`(человек)
vs `status`(код) — разные владельцы ячеек, свежее чтение `decision` при execute, `applying`-лок,
write-through per row, отсутствие TTL у листа.

---

## 5. API / сигнатуры

Новые `@mcp.tool()` — рядом с declutter-блоком, полностью изолированы (§8).

```python
async def plan_assign(scope: str = "", max_tasks: int = 0,
                      persist: str = "sheet", hints: str = "") -> str
async def execute_assign(manifest_id: str, confirm: str = "") -> str
async def resume_assign(manifest_id: str, confirm: str = "") -> str
async def set_assign_decision(manifest_id: str, row_ids: List[int],
                              decision: str, owner: str = "") -> str
```

- **`plan_assign`**
  - `scope`: как declutter — `""`(все) / `"Project"` / `"Project/Column"` / `"inbox"`.
    Переиспользовать `_dc_scope_filter` + `_dc_column_names_for_scope` **без изменений**.
  - Фильтр после scope: оставить только задачи, где **`not t.get("assignee")`** (бесхозные)
    **и** проект **shared** (есть участники). Не-shared и уже-назначенные отсеиваются здесь.
  - `max_tasks`: кап (дефолт `_DC_MAX_TASKS=200`), как declutter.
  - `persist`: **дефолт `"sheet"`** (ценность фичи — durable ревью многих предложений). `"ram"` —
    быстрый inline-путь (печатает предложения, одобрение «да» из чата, применяет всё предложенное;
    без таблицы). При `persist="sheet"` и ненастроенной таблице — как в declutter §9.3: **явный
    отказ без тихого фолбэка** (durability — смысл режима). *Открытый вопрос §9.1.*
  - `hints`: опц. строка/JSON с картой `{column_name: member}` и/или алиасами `{alias: member}`
    для уровней 2-3. Пусто — работают только уровни 1/4/5.
  - Делает: собрать участников по проектам scope → `_asg_analyze(...)` (правила + shim) →
    `_asg_proposals_to_rows(...)` → `ensure_header` + `append_rows` в `Assign Log` → RAM-указатель
    `_MANIFESTS[mid]={"kind":"assign","persist":"sheet",...}` → печать плана + ссылка на таблицу
    и подсказка «правь колонку `decision`/`proposed_value`, затем execute_assign(...)».

- **`execute_assign(manifest_id, confirm)`** — диспетчер (калька `execute_declutter`,
  `server.py:3474`): RAM-указатель `persist="sheet"` **или** RAM пуст, но manifest есть в листе →
  делегирует в `_execute_assign_from_sheet`. `confirm` = **`"ASSIGN <N>"`**, N = свежий счётчик
  approved-ещё-не-done строк (race-guard, как declutter §6).

- **`resume_assign(manifest_id, confirm)`** — тонкая обёртка над `_execute_assign_from_sheet` для
  продолжения после рестарта/паузы (RAM пуст). Дружелюбное «манифест X в `Assign Log` не найден».

- **`set_assign_decision(manifest_id, row_ids, decision, owner="")`** — важнее, чем у declutter:
  главный вход человека — это **выбор владельца**. Программно: `batch_update_rows` ставит колонку
  `decision`, и если `owner` задан — ещё и `proposed_value` (резолвит `owner`→`name→uid` по свежим
  участникам). Позволяет Claude по чату «задачу 3 — на Аню, одобрить» детерминированно записать
  решение, **не касаясь исполнения**. Разделяет approval и apply.

### Приватные чистые хелперы (unit-тестируемы без сети, калька `_dc_*`)

- `_asg_analyze(tasks, members_by_project, col_names, hints, judge_fn) -> List[proposal]`
  — правила 1-3 + shim (4) + фолбэк (5). `proposal = {taskId,title,projectId,project,column,
  snapshot, suggested_uid, suggested_name, confidence, reason}`. Ноль I/O (judge_fn инъектится).
- `_asg_proposals_to_rows(proposals, manifest_id, run_ts) -> List[dict]` — разворот в строки
  листа по таблице §4 (`action="assign"`, `proposed_value="name→uid"|""`, `decision="pending"`,
  `snapshot_json`). Ноль I/O.
- `_asg_rows_to_exec(approved_rows, members_now) -> {"changes":[...], "refused":[...]}` — из
  approved-строк строит вход `update_tasks` (`{taskId, projectId:"" , title, assignee:uid}`;
  projectId пуст — guard/`_resolve_project_id` перерешает по живому, как в declutter
  `_dc_rows_to_exec`). Парсит `proposed_value`; строки без валидного/резолвимого владельца или с
  uid ∉ `members_now` → в `refused` с внятной причиной (не гадать, не звать API с мусорным uid).
- `_asg_judge_fn(tasks, members, fail_tracker) -> per-task suggestion` — обёртка над
  `_dc_shim_json` (калька `_dc_judge_fn`).
- `_execute_assign_from_sheet(manifest_id, confirm)` — движок execute/resume (калька
  `_execute_declutter_from_sheet`, `server.py:3009`).

---

## 6. Поток plan → review → execute → resume (пошагово)

Легенда: **[код]** детерминированная логика; **[LLM]** Claude; **[human]** Максим.

**PLAN**
1. [LLM] `plan_assign(scope, persist="sheet"[, hints])`.
2. [код] читает открытые задачи → `_dc_scope_filter`/`_dc_column_names_for_scope` → оставляет
   бесхозные в shared-проектах.
3. [код] по каждому проекту `get_project_members` → кандидаты; `_asg_analyze` (правила + shim) →
   предложения.
4. [код] `ensure_header("Assign Log")`; `append_rows(_asg_proposals_to_rows(...))` — строки с
   `decision="pending"`, `snapshot_json`.
5. [код] RAM-указатель; печатает план **и** ссылку на таблицу.
6. [LLM] печатает предложения вербатим (задача → предложенный владелец + причина + confidence).

**REVIEW**
7. [human] в таблице (или в чате): для каждой строки ставит `approved`/`rejected`, при желании
   правит `proposed_value` (вписывает другого владельца). ИЛИ говорит Claude →
8. [LLM] `set_assign_decision(mid, row_ids, decision, owner=...)` — [код] пишет `decision`
   (и `proposed_value`).

**EXECUTE**
9. [human] «назначай» → [LLM] `execute_assign(mid, confirm="ASSIGN <N>")`.
10. [код] **свежо читает** approved-ещё-не-done строки; сверяет `N` с `confirm` (race-guard);
    свежий `get_project_members` для валидации uid.
11. [код] пропускает задачи, у которых **в живом состоянии уже есть assignee** (кто-то назначил
    после плана) → `status=skipped`. Ставит `applying`-лок; строит `changes`; зовёт **`update_tasks`**
    (guard+журнал+под-шаг assignee).
12. [код] пост-сверка из свежего состояния: `live.assignee == uid` → `done`+`applied_ts`, иначе
    `failed`+`error`. Write-through per row.
13. [LLM] печатает сводку; финальные статусы видны в таблице.

**RESUME** (после рестарта/таймаута)
14. [LLM] `resume_assign(mid, confirm)` → та же ветка (12); зависший `applying` разрешается по
    live-факту (`live.assignee == uid` → `done`, иначе переприменить).

Ключ: между review и execute userId и решения **ни разу не проходят через контекст LLM** — код
пишет их в лист и код же читает обратно.

---

## 7. Edge-cases

1. **Проект не shared / нет участников.** `get_project_members` → `[]`. Задачи такого проекта
   в assign не попадают (фильтр на этапе plan). В сводке plan: «проект X не расшарен — пропущен».
   Никакого крэша.
2. **Только владелец в проекте (нет коллабораторов).** Назначать некому осмысленно → пропустить
   с пометкой (правило 1 не срабатывает, кандидатов-не-владельцев нет).
3. **Задача уже с владельцем.** На plan — отсечена фильтром `not assignee`. На execute — повторная
   live-проверка (шаг 11): если assignee появился после плана → `skipped`, чужой владелец не
   перезаписывается.
4. **Участник ушёл из проекта между plan и execute.** Свежий `get_project_members` на execute:
   `uid ∉ members_now` → строка в `refused`/`failed` с «участник вышел — назначь другого», API с
   невалидным uid не зовём. Если проскочило и API отклонил — пост-сверка `live.assignee != uid`
   → `failed`.
5. **Задача ушла из shared-проекта (перемещена в личный).** Назначение невозможно → под-шаг
   assignee/пост-сверка дадут `failed` с понятным текстом; identity-guard отловит рассинхрон.
6. **v2 недоступен.** `update_tasks` вернёт «исполнитель требует v2 API» → строки `failed`,
   план/остальное не рушится.
7. **Shim выключен/деградировал.** Работают только детерминированные уровни 1-3; спорные →
   `proposed_value` пуст, `decision=pending` — человек назначит вручную. Плановый путь не падает
   (fail_tracker отмечает деградацию для сообщения).
8. **Ручная ошибка в `proposed_value`.** Нераспознанный/нерезолвимый владелец → `refused`, а не
   слепой вызов API.
9. **Идемпотентность.** `done` не переисполняется; если `live.assignee` уже == предложенному → сразу
   `done` без вызова (как declutter `_names_agree`). Повторный execute применяет только
   approved-`planned/failed`.
10. **Таблица недоступна.** `DeclutterSheetError` → чистый отказ (`str(e)`), ничего не тронуто —
    механизм declutter_sheet уже это гарантирует.

---

## 8. Обратная совместимость / изоляция

- **declutter не трогается вообще.** assign — отдельные `@mcp.tool()`, отдельные `_asg_*` хелперы,
  отдельная вкладка `Assign Log`. RAM-указатели различаются `kind` (`"assign"` vs `"declutter"`),
  `_prune_manifests` работает как есть.
- Единственная правка общего кода — **аддитивный** необязательный `sheet_name=` в
  `declutter_sheet.py` (дефолт = текущее поведение). Все declutter-тесты остаются зелёными без
  правок. Если и это нежелательно — фолбэк «общий лист, action='assign'» вообще ничего в
  declutter_sheet не меняет (§4).
- `update_tasks`, `get_project_members`, `_dc_scope_filter`, `_dc_shim_json`, `_snapshot_of`,
  `_open_by_id`, `_guard_task` используются как есть — новых опасных путей записи нет.

---

## 9. Пошаговый план для Sonnet

Трогаемые файлы: `ticktick_mcp/src/server.py` (новый assign-блок рядом с declutter),
`ticktick_mcp/src/declutter_sheet.py` (аддитивный `sheet_name=`), tests. Новых зависимостей нет.

1. **`declutter_sheet.py`** — добавить публичным функциям (`ensure_header`, `append_rows`,
   `read_manifest_rows`, `update_row`, `batch_update_rows`, `sheet_url`) необязательный
   `sheet_name: str = SHEET_NAME`; диапазоны считать из него. Дефолт = declutter без изменений.
   Тест: assign в `Assign Log` и declutter в `Declutter Log` не пересекаются.
2. **`_asg_analyze`** (чистая) — правила 1-3 + shim(4, judge_fn инъектится) + фолбэк(5). Юнит-тесты
   на каждый уровень и на bias «не уверен → null».
3. **`_asg_proposals_to_rows`** (чистая) — разворот в строки по §4. Юнит-тест схемы (`action=assign`,
   `decision=pending`, `proposed_value` формат, snapshot).
4. **`_asg_rows_to_exec`** (чистая) — парсинг `proposed_value` (uid/имя/`name→uid`), валидация
   против `members_now`, `refused` для мусора. Юнит-тесты парсинга и refuse.
5. **`_asg_judge_fn`** — обёртка `_dc_shim_json` (по образцу `_dc_judge_fn`).
6. **`plan_assign`** — scope-фильтр (`_dc_*`) + фильтр бесхозных-в-shared, сбор участников,
   `_asg_analyze` → `append_rows(sheet_name="Assign Log")`, RAM-указатель, печать + ссылка. Ветка
   `persist="ram"` — inline (печать предложений, confirm-токен, применить всё предложенное).
7. **`_execute_assign_from_sheet`** — свежее чтение, apply-фильтр (`approved` ∧
   `status∈{planned,failed}` ∧ `action=assign`), live-skip уже-назначенных, `ASSIGN <N>`-сверка,
   `applying`-лок, вызов `update_tasks`, per-row write-through из свежего `live.assignee`,
   разрешение зависшего `applying` по live-факту.
8. **`execute_assign`** — диспетчер (RAM `persist=sheet` или manifest в листе → (7)).
9. **`resume_assign`** — тонкая обёртка над (7).
10. **`set_assign_decision`** — `batch_update_rows` колонок `decision` (+`proposed_value` при
    `owner`); не касается исполнения.
11. **Тесты (обяз.):** round-trip `proposals→rows→exec`; идемпотентность (2-й execute ничего не
    назначает, `done` пропущены); resume (`planned/failed` применяются, `done` нет, зависший
    `applying` при `live.assignee==uid` → `done` без вызова); race-guard (`decision` изменён →
    `N != confirm` → refuse); edge: не-shared/уже-назначено/участник-ушёл → корректные skip/refuse/
    fail; изоляция declutter (его тесты зелёные без правок).
12. **Докстринги** `plan_assign`/`execute_assign`/`resume_assign` со ссылкой на эту спеку и
    инвариантом «состояние в таблице, не в контексте».

---

## 10. Открытые вопросы к Максиму

1. **`persist="sheet"` по умолчанию + отказ при ненастроенной таблице** — ок? (Логика: смысл
   assign — durable ревью многих предложений; тихий фолбэк на RAM противоречит инварианту, как и в
   declutter §9.3.) Либо дефолт `"ram"`, чтобы работало без Google-кредов?
2. **Отдельная вкладка `Assign Log`** (рекоменд.) или всё в общий `Declutter Log` с
   `action="assign"`? Первое чище, второе — ноль правок в declutter_sheet.
3. **Карта «колонка → ответственный» (`hints`)** — нужна ли, и в каком виде удобнее задавать
   (JSON-строка? отдельный tool `set_column_owners`, хранящий карту в таблице?). Без неё уровень 3
   не работает, но уровни 1/2/4 остаются.
4. **Дефолт `decision="pending"` для всех assign-строк** (человек одобряет каждого) — подтверждаешь?
   (Осознанно строже declutter, где предложенное по умолчанию approved.)
5. **Владельца проекта считать валидным кандидатом** (можно назначить на себя) или исключать из
   предложений? Сейчас: включаем как кандидата, но правило «единственный» смотрит на не-владельцев.
6. **Обратное действие** (снять/сменить уже стоящего владельца) — вне scope этого инструмента
   (только заполняем пустые). Подтверждаешь, что переназначение не нужно в MVP?
```
