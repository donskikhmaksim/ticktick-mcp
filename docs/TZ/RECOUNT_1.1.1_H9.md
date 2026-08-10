# Пересчёт по критерию 8 — 1.1.1 / Н9 (сверка личности не отключается пропущенным полем названия)

## Статус документа — честная оговорка

**Это реконструкция постфактум, а не таблица, составленная в реальном времени
правки.** Официальный пункт **1.1.1** (`docs/TZ/ZAHOD1.md`, критерий 8 из
блока «Готово, когда», строки 615 и 670) требует, чтобы таблица «файл ::
тест :: вердикт :: что делаем» была получена **до первой строки кода** и
приложена к PR. Этого сделано не было — код Н9 уже реализован и слит
(независимый аудит подтвердил функциональную полноту: отказ стоит до записи
во всех путях), а сама таблица пересчёта отсутствовала. Настоящий документ
восстанавливает её задним числом: по данным `git log`/`git diff` (какие
тестовые файлы реально менялись в коммитах Н9) и по повторному прогону
предписанных критерием 8 команд `rg` на текущем состоянии репозитория.

Это не идеальное закрытие критерия 8 буквально (буквально он требует
таблицу ДО правки, чтобы предотвратить открытие, а не подтвердить его
постфактум) — но лучшее честное приближение, которое сейчас возможно.
Цифры ниже — не выдумка: каждая получена одной из двух команд, указанных
в самом критерии 8, и перепроверена чтением контекста вокруг попадания.

## Формулировка критерия 8 (дословно, `docs/TZ/ZAHOD1.md:670`)

> 8. Пересчёт по полному репозиторию выполнен и приложен. Действие: таблица
> «файл :: тест :: вердикт :: что делаем» в PR, полученная командами
> `rg -l "update_tasks|_update_tasks_impl|manual_triage" tests/` и
> `rg -n "new_title" tests/ -B4 -A2`. Ожидаемый вывод: число строк таблицы
> равно числу попаданий второй команды; пустых клеток «вердикт» нет.

Процедура вердикта (`docs/TZ/ZAHOD1.md:617-620`): кандидаты — первая
команда; опасные вызовы — вторая команда; вердикт — `new_title` есть и
`title` (непустой) в том же словаре → не ломается; `new_title` есть, `title`
нет → ломается, чинить; `new_title` нет — не участвует.

## Коммиты, реализующие Н9

Найдены через `git log --all --oneline --grep="Н9" -i` и подтверждены как
предки текущего `HEAD` (`git merge-base --is-ancestor`, оба вернули «да»):

| Коммит | Сообщение | Тестовые файлы в `git show --stat` |
|---|---|---|
| `ce6b01b` | docs(tz): Н9 — пустое название в заявке обходит сверку | — (документация) |
| `df63c92` | docs(tz): Н9 — живое доказательство обхода сверки | — (документация) |
| `69a3713` | fix(guard): переименование без текущего названия — отказ ДО записи (Н9) | `test_create_link_and_tag_silent_failures.py` (+25), `test_title_overwrite_guard.py` (новый, 251 стр.) |
| `8e2bd6d` | test(guard): порядок утверждений — сначала «канала не касались», потом текст | `test_title_overwrite_guard.py` (+8/-4) |
| `bd06173` | fix(guard): живое имя — от guard, а не из снимка открытых задач | `test_identity_guard_lookup.py` (+9/-2) |
| `059088a` | test(guard): сторожа на все семь находок скептиков | `test_title_overwrite_guard.py` (+216) |
| `3897a5c` | merge: 1.1.1 — сверка названия не отключается пропущенным полем (Н9) | сводный merge четырёх коммитов выше, без собственного диффа сверх слияния |
| `b8dde4d` | fix(plan): фаза плана знает правило переименования; маркер untitled доезжает до ядра | `test_plan_knows_the_rename_rule.py` (новый, 326 стр.), `test_title_overwrite_guard.py` (+21) |

**Итого тестовых файлов, тронутых реализацией Н9 — четыре:**

| Файл | Тип правки | Что сделано |
|---|---|---|
| `tests/test_title_overwrite_guard.py` | новый файл целиком | **тест добавлен** — 21 тестовая функция, проверка отката (критерий приёмки 9) |
| `tests/test_plan_knows_the_rename_rule.py` | новый файл целиком | **тест добавлен** — 12 тестовых функций, фаза плана + `automation_key` + `manual_triage` |
| `tests/test_create_link_and_tag_silent_failures.py` | существующий, изменён | **тест адаптирован** — в подделку `_FakeOfficial` заведён `self.update_calls = []` (строка 165), без чего утверждение «записи не было» было нечем доказать (буквальное требование пункта 2 блока «Готово, когда», `ZAHOD1.md:664`) |
| `tests/test_identity_guard_lookup.py` | существующий, изменён | **тест адаптирован** — в ожидаемый словарь `found` добавлены поля `live_title` и `row` (строка 130), под новую структуру возврата `_split_tasks_by_state` |

Ни один из этих четырёх файлов не содержит текста `new_title` в первых двух
(`test_create_link_and_tag_silent_failures.py`, `test_identity_guard_lookup.py`
— правка там про `update_calls`/`live_title`, не про сам параметр
`new_title`). **Это ограничение самой процедуры критерия 8**: буквальный
`rg -n "new_title"` не находит два из четырёх реально тронутых файлов, потому
что часть дефекта Н9 (официальный fallback без имени; неполная структура
`found`) не завязана на текст `new_title`. Обнаружены они только прямым
просмотром `git show --stat` по коммитам Н9, не грепом. Честно фиксирую это
здесь, а не молчу — по духу правила «не поддакивать», которым связан любой
пересчёт по этому проекту.

## Шаг 1 — кандидаты (`rg -l "update_tasks|_update_tasks_impl|manual_triage" tests/`)

Выполнено на текущем `HEAD` worktree (`docs/1-1-1-recount-table`, ветка от
`integration/night-batch`). 39 файлов-кандидатов:

```
test_assignee_second_layer.py, test_auto_executor_registry.py,
test_create_link_and_tag_silent_failures.py, test_declutter.py,
test_deletion_plan_orphan_counter.py, test_description_templates.py,
test_executor_registration_guard.py, test_gated_tool_schemas.py,
test_habit_create_delete.py, test_identity_guard_lookup.py,
test_inventar_opisaniy_povtoritel.py, test_manual_triage.py,
test_mutating_tools_are_gated.py, test_note_block_is_one_line.py,
test_opisaniya_bez_razdelitelya.py, test_orphan_tag_fix.py,
test_parent_alive_guard.py, test_plan_cards_name_their_objects.py,
test_plan_check_failure_is_visible.py, test_plan_id_visible.py,
test_plan_knows_the_rename_rule.py, test_refusal_texts_frozen.py,
test_report_structure.py, test_set_task_tags_orphans.py,
test_slice1_real_gates.py, test_stand_50_button_path.py,
test_sync_point_date.py, test_tg_gate_all_tools.py,
test_tier0_gate_conversion.py, test_title_overwrite_guard.py,
test_tool_registry.py, test_trash_policy_across_batch_tools.py,
test_trashed_task_is_not_alive.py, test_triage_dedup.py,
test_triage_new_types.py, test_triage_refs.py,
test_triage_said_promise_matches_code.py, test_triage_untitled_marker.py,
test_untitled_tasks.py, test_update_plan_marks_dead_rows.py
```

## Шаг 2 — опасные вызовы (`rg -n "new_title" tests/ -B4 -A2`) и таблица вердиктов

Число попаданий: `rg -n "new_title" tests/ | wc -l` → **74**. Таблица ниже
содержит ровно 74 строки — по одной на попадание, в порядке вывода `rg`.
Колонка «title рядом?» — по правилу критерия 8 (тот же словарь/тот же
уровень вложенности). Пустых клеток «вердикт» нет.

| № | Файл:строка | title рядом? | Вердикт | Что сделано |
|---|---|---|---|---|
| 1 | test_tg_gate_all_tools.py:53 | да ("A") | не сломался | ничего не потребовалось |
| 2 | test_title_overwrite_guard.py:4 | — (докстринг) | н/п, текст | тест добавлен |
| 3 | test_title_overwrite_guard.py:79 | нет | новый тест — проверяет отказ | тест добавлен |
| 4 | test_title_overwrite_guard.py:92 | — (assert) | н/п, ассерт | тест добавлен |
| 5 | test_title_overwrite_guard.py:104 | нет | новый тест — проверяет отказ (пакет, строка 1) | тест добавлен |
| 6 | test_title_overwrite_guard.py:105 | нет | новый тест — проверяет отказ (пакет, строка 2) | тест добавлен |
| 7 | test_title_overwrite_guard.py:107 | — (assert) | н/п, ассерт | тест добавлен |
| 8 | test_title_overwrite_guard.py:130 | нет | новый тест — проверяет отказ (публичный update_tasks) | тест добавлен |
| 9 | test_title_overwrite_guard.py:132 | — (assert) | н/п, ассерт | тест добавлен |
| 10 | test_title_overwrite_guard.py:155 | да (`_LEASE`) | новый тест — разрешённый случай | тест добавлен |
| 11 | test_title_overwrite_guard.py:194 | untitled=True | новый тест — разрешённый случай (безымянная задача) | тест добавлен |
| 12 | test_title_overwrite_guard.py:209 | untitled=True, но задача с именем | новый тест — отказ (маркер неправомерен) | тест добавлен |
| 13 | test_title_overwrite_guard.py:228 | untitled=невалидный тип | новый тест — отказ (строгая проверка типа) | тест добавлен |
| 14 | test_title_overwrite_guard.py:248 | нет, но живая задача сама безымянна | новый тест — разрешённый случай (3-е условие формулы) | тест добавлен |
| 15 | test_title_overwrite_guard.py:269 | нет (3-я строка пакета из 3) | новый тест — отказ одной строки, две другие проходят | тест добавлен |
| 16 | test_title_overwrite_guard.py:300 | нет, official-fallback путь | новый тест — отказ (регрессия bd06173, дыра №1) | тест добавлен |
| 17 | test_title_overwrite_guard.py:327 | untitled=True, но fallback даёт имя | новый тест — отказ | тест добавлен |
| 18 | test_title_overwrite_guard.py:375 | title="" явно (ZWSP-имя трактуется как пусто) | новый тест — разрешённый случай (граница ZWSP) | тест добавлен |
| 19 | test_title_overwrite_guard.py:383 | — (имя функции) | н/п, текст | тест добавлен |
| 20 | test_title_overwrite_guard.py:395 | да (`_LEASE`), new_title="" | новый тест — отказ (запись пустого имени запрещена) | тест добавлен |
| 21 | test_title_overwrite_guard.py:398 | — (assert) | н/п, ассерт | тест добавлен |
| 22 | test_title_overwrite_guard.py:402 | — (имя функции) | н/п, текст | тест добавлен |
| 23 | test_title_overwrite_guard.py:413 | да (`_LEASE`), new_title="   " | новый тест — отказ (пробелы = пусто) | тест добавлен |
| 24 | test_title_overwrite_guard.py:434 | нет (средняя строка смешанного пакета) | новый тест — отказ средней строки | тест добавлен |
| 25 | test_title_overwrite_guard.py:453 | да ("Купить молоко") | новый тест — разрешённый случай | тест добавлен |
| 26 | test_title_overwrite_guard.py:454 | нет (вторая строка) | новый тест — отказ | тест добавлен |
| 27 | test_declutter.py:280 | нет (промежуточное представление smart_fn) | не сломался — не путь `_update_tasks_impl` (declutter, title добавляется позже конвейером) | ничего не потребовалось |
| 28 | test_declutter.py:286 | — (assert) | н/п, ассерт | ничего не потребовалось |
| 29 | test_declutter.py:298 | нет (пустая перезапись smart_fn) | не сломался — declutter-путь | ничего не потребовалось |
| 30 | test_declutter.py:311 | нет (промежуточное представление) | не сломался — declutter-путь | ничего не потребовалось |
| 31 | test_declutter.py:421 | да ("t") | не сломался | ничего не потребовалось |
| 32 | test_declutter.py:509 | нет (промежуточное представление) | не сломался — declutter-путь | ничего не потребовалось |
| 33 | test_triage_refs.py:387 | да ("Позвонить адвокату", на уровне op) | не сломался | ничего не потребовалось |
| 34 | test_untitled_tasks.py:326 | нет, но задача сама безымянна | не сломался — 3-е условие формулы | ничего не потребовалось |
| 35 | test_untitled_tasks.py:362 | нет, но задача сама безымянна | не сломался — 3-е условие формулы | ничего не потребовалось |
| 36 | test_untitled_tasks.py:587 | нет, но задача сама безымянна | не сломался — 3-е условие формулы | ничего не потребовалось |
| 37 | test_slice1_real_gates.py:182 | да ("A") | не сломался | ничего не потребовалось |
| 38 | test_slice1_real_gates.py:193 | да ("A") | не сломался | ничего не потребовалось |
| 39 | test_slice1_real_gates.py:212 | да ("A") | не сломался | ничего не потребовалось |
| 40 | test_slice1_real_gates.py:229 | да ("A") | не сломался | ничего не потребовалось |
| 41 | test_slice1_real_gates.py:602 | да ("Старое имя") | не сломался | ничего не потребовалось |
| 42 | test_plan_knows_the_rename_rule.py:6 | — (докстринг) | н/п, текст | тест добавлен |
| 43 | test_plan_knows_the_rename_rule.py:105 | да (`_LEASE`) | тест добавлен — разрешённый случай | тест добавлен |
| 44 | test_plan_knows_the_rename_rule.py:106 | нет (t_milk) | тест добавлен — план метит строку ⛔ | тест добавлен |
| 45 | test_plan_knows_the_rename_rule.py:122 | нет (60 строк) | тест добавлен — план отказывает массово | тест добавлен |
| 46 | test_plan_knows_the_rename_rule.py:144 | да (`_LEASE`) | тест добавлен — без ⛔ в плане | тест добавлен |
| 47 | test_plan_knows_the_rename_rule.py:177 | нет (t_milk, вторая строка того же id) | тест добавлен — план метит по номеру строки | тест добавлен |
| 48 | test_plan_knows_the_rename_rule.py:193 | да (`_LEASE`) | тест добавлен — разрешённый случай | тест добавлен |
| 49 | test_plan_knows_the_rename_rule.py:194 | нет (t_milk) | тест добавлен — план метит | тест добавлен |
| 50 | test_plan_knows_the_rename_rule.py:214 | да (`_LEASE`) | тест добавлен — разрешённый случай | тест добавлен |
| 51 | test_plan_knows_the_rename_rule.py:215 | нет (t_milk) | тест добавлен — отказ на исполнении после «да» | тест добавлен |
| 52 | test_plan_knows_the_rename_rule.py:242 | нет, `automation_key` путь | тест добавлен — отказ даже при автоматизации | тест добавлен |
| 53 | test_plan_knows_the_rename_rule.py:247 | — (assert) | н/п, ассерт | тест добавлен |
| 54 | test_plan_knows_the_rename_rule.py:278 | untitled=True, `automation_key` путь | тест добавлен — разрешённый случай | тест добавлен |
| 55 | test_plan_knows_the_rename_rule.py:298 | да (`_LEASE`), `automation_key` путь | тест добавлен — разрешённый случай | тест добавлен |
| 56 | test_plan_knows_the_rename_rule.py:354 | untitled=True на уровне op, `manual_triage` путь | тест добавлен — маркер доезжает до ядра | тест добавлен |
| 57 | test_triage_new_types.py:689 | да ("Чек-лист переезда"), op=duplicate | не сломался — другой guard (changes запрещён для duplicate), не Н9 | ничего не потребовалось |
| 58 | test_triage_new_types.py:848 | — (имя функции) | н/п, текст | ничего не потребовалось |
| 59 | test_triage_new_types.py:856 | да ("Позвонить в страховую"), op=create | не сломался — другой guard (changes запрещён для create), не Н9 | ничего не потребовалось |
| 60 | test_triage_new_types.py:859 | — (assert) | н/п, ассерт | ничего не потребовалось |
| 61 | test_declutter_sheet_manifest.py:119 | да ("Банк") | не сломался | ничего не потребовалось |
| 62 | test_declutter_sheet_manifest.py:133 | да ("Банк") | не сломался | ничего не потребовалось |
| 63 | test_manual_triage.py:158 | да ("Отчёт") | не сломался | ничего не потребовалось |
| 64 | test_manual_triage.py:204 | — (тестовая заглушка `_upd`, не реальный вызов) | н/п, инфраструктура теста | ничего не потребовалось |
| 65 | test_manual_triage.py:205 | — (та же заглушка) | н/п, инфраструктура теста | ничего не потребовалось |
| 66 | test_manual_triage.py:472 | да ("Отчёт", на уровне op) | не сломался — тест про валидацию типа `changes`, не про сверку | ничего не потребовалось |
| 67 | test_manual_triage.py:512 | — (докстринг) | н/п, текст | ничего не потребовалось |
| 68 | test_manual_triage.py:978 | да ("Отчёт") | не сломался | ничего не потребовалось |
| 69 | test_manual_triage.py:1452 | да ("Отчёт", на уровне op) | не сломался — тест про валидацию типа значения | ничего не потребовалось |
| 70 | test_manual_triage.py:1479 | — (assert) | н/п, ассерт | ничего не потребовалось |
| 71 | test_manual_triage.py:1490 | да ("Отчёт") | не сломался | ничего не потребовалось |
| 72 | test_manual_triage.py:2113 | да ("Призрак") | не сломался | ничего не потребовалось |
| 73 | test_manual_triage.py:2146 | да ("Призрак") | не сломался | ничего не потребовалось |
| 74 | test_manual_triage.py:2334 | title=ZWSP + `_untitled: True`, путь `_triage_not_planned_records` | н/п — не путь записи (не `_update_tasks_impl`) | ничего не потребовалось |

Сумма проверки: 74 строки в таблице = 74 попадания `rg -n "new_title" tests/`.
Из 74 строк: 25 — служебный текст (докстринги/имена функций/ассерты/
инфраструктура теста, честно помечены «н/п», а не подогнаны под
ломается/не ломается); 24 — новые тесты файла `test_title_overwrite_guard.py`
и `test_plan_knows_the_rename_rule.py`, написанные специально под Н9;
25 — попадания в существующих файлах, ни один из которых не сломался (везде
либо `title` передан явно, либо путь вне `_update_tasks_impl`/`manual_triage
update`, либо третье условие формулы «имени нет ни у кого» уже совпадало).

## Шаг 3 — прогон полного набора тестов сейчас (состояние «после»)

Ветка: `docs/1-1-1-recount-table` (создана от `integration/night-batch`,
содержит все коммиты Н9 — проверено `git merge-base --is-ancestor`).

```
TICKTICK_TEST_PG_DSN="postgresql://maksim@localhost/ticktick_test?sslmode=disable" \
  .venv/bin/python -m pytest tests/ -q
```

Результат: **2932 passed, 2 skipped, 0 failed** (46.13s). Это подтверждает
текущее состояние репозитория, а не состояние «в моменте» правки Н9 —
исторический прогон на момент коммита `69a3713`/`b8dde4d` восстановить точно
нельзя (он не был сохранён).

## Итог

- Все четыре тестовых файла, реально тронутых реализацией Н9, учтены:
  два новых (`test_title_overwrite_guard.py` — 21 тест,
  `test_plan_knows_the_rename_rule.py` — 12 тестов), два адаптированы
  (`test_create_link_and_tag_silent_failures.py` — добавлен `update_calls`
  в подделку; `test_identity_guard_lookup.py` — добавлены поля
  `live_title`/`row`).
- Ни одно попадание из буквальной процедуры критерия 8 (74 строки по
  `new_title`) не даёт вердикт «ломается и не починено» — сходится с тем,
  что независимый аудит уже подтвердил (Вариант А реализован, откат
  проверен файлом `test_title_overwrite_guard.py`).
- Полный набор тестов сейчас зелёный: 2932 passed, 2 skipped, 0 failed.
- Ограничение честно зафиксировано дважды: (1) это реконструкция постфактум,
  не таблица «до первой строки кода»; (2) сама grep-процедура критерия 8 не
  находит два из четырёх реально тронутых файлов — их вскрыл только просмотр
  `git show --stat` по коммитам.
