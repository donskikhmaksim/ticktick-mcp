"""Параметр читающего инструмента обязан ДОЕХАТЬ и что-то изменить.

Каждый тест здесь написан под конкретную мутацию, которую весь набор из 2072
тестов НЕ ЗАМЕЧАЛ (мутационный прогон 2026-08-07): фильтр по Inbox снят в
клиенте, тег игнорируется, `include_completed` не применяется, `afterStamp`
не сдвинут, `project_id`/`task_id` переставлены местами, правило фильтра не
печатается, подзадачи не выводятся, вложение по индексу всегда первое.

Общий приём — `tests/read_stand.py`: НАСТОЯЩИЕ клиенты, подменён только
транспорт, инструмент вызывается по имени через реестр (`mcp.call_tool`), а
проверяется только ТЕКСТ ответа — ровно то, что видит MCP-клиент по сети.
Ни одна внутренняя функция сервера не подменяется: именно подмена
`_merged_task_attachments` в своё время и оставила дефект выбора вложения
невидимым для семи «покрывающих» тестов.
"""
import json
from datetime import date, timedelta

import pytest

import ticktick_mcp.src.server as s
from tests.read_stand import (
    ATT_1, ATT_2, ATT_3, ATT_CONTENT_ONLY, ATT_NO_ID, COL_A, COL_B, COL_C,
    COMMENT_ID, FILTER_ID, HABIT_ID, INBOX_ID, MEMBER_ME, MEMBER_OTHER,
    COMPLETED, P_ARCH, P_HOME, P_WORK, TASK_ARCHIVED, TASK_ASSIGNED_DONE,
    TASK_ASSIGNED_OPEN, TASK_ATT, TASK_ATT_CONTENT, TASK_ATT_NOID,
    TASK_GRANDKID, TASK_KID, TASK_ROOT, TODAY, build_state, call, wire)


@pytest.fixture(autouse=True)
def stand(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://tt.example.com")
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)
    v2, v1, transport = wire(monkeypatch)
    return v2, v1, transport


# ─────────── страж самой фикстуры ───────────

def test_stand_today_is_safely_in_the_past():
    """`_is_task_overdue` сравнивает ТАЙМИРОВАННЫЙ срок с реальными часами
    (`datetime.now`), подменить которые стенд не может. Пока «сегодня» стенда
    заведомо в прошлом, эта ветка отвечает одинаково всегда; вынеси TODAY в
    будущее — и часть тестов ниже начнёт зависеть от дня запуска. Страж
    падает раньше, чем это станет загадкой."""
    assert TODAY < date.today() - timedelta(days=1), (
        "TODAY стенда должен оставаться в прошлом — иначе результаты "
        "инструментов «просрочено»/«скоро» поедут вместе с календарём")


# ─────────── Inbox: фильтр живёт в клиенте, а не в двойнике ───────────

async def test_inbox_returns_only_inbox_tasks(stand):
    """Мутация B9: снять фильтр `projectId == inboxId` в НАСТОЯЩЕМ клиенте —
    и «Входящие» показывают весь аккаунт (живьём это выглядело как «во
    входящих 1477 задач»). Двойник, повторяющий фильтр за клиента, такого не
    замечает — здесь фильтрует боевой код."""
    out = await call("get_inbox_tasks")

    assert "Разобрать входящее" in out and "Позвонить в банк" in out
    assert "Собрать отчёт" not in out, out          # чужой проект
    assert "Полить цветы" not in out, out
    assert "Inbox tasks (2)" in out, out


def test_client_inbox_filter_is_not_the_doubles_job(stand):
    v2 = stand[0]
    ids = {t["id"] for t in v2.get_inbox_tasks()}
    assert all(t.get("projectId") == INBOX_ID for t in v2.get_inbox_tasks())
    assert len(ids) == 2, ids


# ─────────── тег ───────────

async def test_tag_selection_returns_only_tagged_tasks(monkeypatch):
    """Мутация B10: клиентский фильтр по тегу снят — выборка «по тегу»
    становится всем подряд."""
    tasks = [
        {"id": "t1", "projectId": P_HOME, "title": "Полить цветы", "tags": ["дом"]},
        {"id": "t2", "projectId": P_HOME, "title": "Помыть окна", "tags": ["дом", "весна"]},
        {"id": "t3", "projectId": P_WORK, "title": "Отчёт", "tags": ["работа"]},
        {"id": "t4", "projectId": P_WORK, "title": "Созвон"},
        {"id": "t5", "projectId": INBOX_ID, "title": "Разобрать"},
    ]
    wire(monkeypatch, state=build_state(tasks=tasks))

    out = await call("get_tasks_by_tag", tag="дом")

    assert "Tasks tagged 'дом' (2)" in out, out
    assert "Полить цветы" in out and "Помыть окна" in out
    assert "Отчёт" not in out and "Созвон" not in out and "Разобрать" not in out


async def test_tag_query_tolerates_the_hash_but_not_a_foreign_tag(monkeypatch):
    tasks = [{"id": "t1", "projectId": P_HOME, "title": "Полить цветы", "tags": ["дом"]},
             {"id": "t2", "projectId": P_WORK, "title": "Отчёт", "tags": ["работа"]}]
    wire(monkeypatch, state=build_state(tasks=tasks))

    with_hash = await call("get_tasks_by_tag", tag="#дом")
    assert "Полить цветы" in with_hash and "Отчёт" not in with_hash

    missing = await call("get_tasks_by_tag", tag="дача")
    assert "No open tasks found with tag 'дача'." in missing


# ─────────── сохранённый фильтр ───────────

async def test_saved_filter_applies_its_priority_condition(stand):
    """Мутация B16: условие `priority` в `_leaf_matches` заменено на `True` —
    фильтр молча становится шире, чем заявлен, и отдаёт весь пул."""
    out = await call("run_filter", filter="Только срочное")

    # ровно три задачи с приоритетом 5 из тринадцати в пуле
    assert "3 task(s)" in out, out
    for high in ("Оплатить страховку", "Продлить домен", "Забрать посылку"):
        assert high in out, out
    for foreign in ("Собрать отчёт", "Записаться к врачу", "Полить цветы",
                    "Разобрать входящее"):
        assert foreign not in out, out


async def test_filter_listing_prints_the_rule_not_just_the_name(stand):
    """Мутация B13: правило не печатается — по списку фильтров нечем понять,
    что фильтр вообще делает, а `run_filter` придётся проверять вслепую."""
    out = await call("list_filters")

    assert "Только срочное" in out and FILTER_ID in out
    assert "rule:" in out, out
    rule_line = next(ln for ln in out.splitlines() if "rule:" in ln)
    assert "priority" in rule_line, out
    # правило печатается целиком и остаётся разбираемым JSON'ом
    payload = json.loads(rule_line.split("rule:", 1)[1].strip())
    assert payload == {"and": [{"conditionName": "priority", "or": [5]}]}


# ─────────── комментарии: порядок аргументов и id ───────────

async def test_comments_are_fetched_with_project_then_task(stand):
    """Мутация B3: `get_task_comments(task_id, project_id)` — аргументы
    переставлены. Живой API на такой путь отвечает пустотой, то есть дефект
    выглядит как «комментариев нет»; двойник, принимающий любой порядок, его
    не показывает. Транспорт стенда знает ровно ОДИН правильный путь."""
    transport = stand[2]

    out = await call("get_task_comments", task_title="Собрать отчёт",
                     project_id=P_WORK, task_id=TASK_ROOT)

    assert "Цифры будут в пятницу" in out, out
    assert "No comments" not in out
    paths = [p for _m, p, _k in transport.calls if p.endswith("/comments")]
    assert paths == [f"/project/{P_WORK}/task/{TASK_ROOT}/comments"], paths


async def test_comment_id_is_printed_so_it_can_be_edited_or_deleted(stand):
    """Мутация C2: id комментария не печатается — `update_task_comment` и
    `delete_task_comment` требуют его, а взять его человеку больше неоткуда."""
    out = await call("get_task_comments", task_title="Собрать отчёт",
                     project_id=P_WORK, task_id=TASK_ROOT)

    assert f"(id:{COMMENT_ID})" in out, out


# ─────────── участники проекта ───────────

async def test_members_are_printed_with_their_user_ids(stand):
    """Мутация B6: `userId` пропал из строки — назначить задачу этому человеку
    становится нечем (assignee принимает именно id)."""
    out = await call("get_project_members", project_id=P_WORK)

    assert "Максим" in out and "Ирина" in out
    assert MEMBER_ME in out and MEMBER_OTHER in out, out
    for line in out.splitlines():
        if line.startswith("- "):
            assert "userId:" in line, line


# ─────────── задачи на человеке ───────────

async def test_assignee_listing_hides_completed_by_default(stand):
    """Мутация B7: `include_completed` игнорируется — завершённая задача
    остаётся висеть в списке «на человеке»."""
    out = await call("get_tasks_by_assignee", assignee="Ирина")

    assert "Согласовать смету" in out, out
    assert "Отправить договор" not in out, out
    assert TASK_ASSIGNED_DONE not in out, out


async def test_assignee_listing_shows_completed_when_asked(stand):
    out = await call("get_tasks_by_assignee", assignee="Ирина",
                     include_completed=True)

    assert "Согласовать смету" in out and "Отправить договор" in out, out
    assert TASK_ASSIGNED_OPEN in out and TASK_ASSIGNED_DONE in out


# ─────────── теги: печатаются ВСЕ ───────────

async def test_every_tag_is_printed_and_the_counter_agrees(stand):
    """Мутация B8: печатается только первый тег — со стороны это читается как
    «теги пропали», а счётчик в заголовке продолжает говорить правду."""
    out = await call("list_tags")

    lines = [ln for ln in out.splitlines() if ln.startswith("- ")]
    assert len(lines) == 30, out
    assert "Tags (30)" in out
    assert "- тег30" in out and "- тег01" in out


# ─────────── колонки канбана ───────────

async def test_columns_come_back_in_board_order(stand):
    """Мутация B5: порядок колонок перевёрнут — «Готово» оказывается первой
    полосой доски, и любой перенос задачи «в первую колонку» бьёт мимо."""
    out = await call("list_project_columns", project_id=P_WORK)

    assert out.index("К работе") < out.index("В работе") < out.index("Готово"), out


async def test_column_ids_are_printed(stand):
    """Мутация C4: id колонки не печатается — `column_id` для create_task
    взять неоткуда."""
    out = await call("list_project_columns", project_id=P_WORK)

    for cid in (COL_A, COL_B, COL_C):
        assert f"(id: {cid})" in out, out


# ─────────── чек-ины привычки ───────────

async def test_checkin_window_includes_the_requested_day(stand):
    """Мутация B11: сдвиг `afterStamp` на день выброшен. API отдаёт записи
    СТРОГО позже отметки, поэтому без сдвига запрошенный день исчезает из
    истории — тихая потеря первого дня."""
    transport = stand[2]
    after = TODAY - timedelta(days=2)

    out = await call("get_habit_checkins", habit_name="Зарядка",
                     habit_id=HABIT_ID, after_date=after.isoformat())

    assert after.isoformat() in out, out          # запрошенный день на месте
    sent = [kw.get("json", {}).get("afterStamp")
            for _m, p, kw in transport.calls if p == "/habitCheckins/query"]
    assert sent == [int(after.strftime("%Y%m%d")) - 1], sent


async def test_checkin_statuses_are_words_not_raw_codes(stand):
    """Мутация C6: печатается сырой `status` (0/1/2) вместо метки — «2»
    рядом с датой читается как значение, а не как «сделано»."""
    out = await call("get_habit_checkins", habit_name="Зарядка",
                     habit_id=HABIT_ID,
                     after_date=(TODAY - timedelta(days=2)).isoformat())

    body = [ln for ln in out.splitlines() if ln.startswith("- ")]
    assert body, out
    for line in body:
        head = line.split("(value")[0]
        assert any(word in head for word in ("done", "failed", "not done")), line


# ─────────── карточка задачи ───────────

async def test_task_info_lists_subtasks(stand):
    """Мутация B12: подзадачи не выводятся — карточка отвечает «у задачи нет
    подзадач», хотя они есть."""
    out = await call("get_task_info", task_id=TASK_ROOT)

    assert "Subtasks (1)" in out, out
    assert "Взять цифры у бухгалтерии" in out
    assert "no checklist items, subtasks, or attachments" not in out


async def test_task_info_subtask_id_is_usable_for_the_next_call(stand):
    """Мутация C3: id подзадачи не печатается. Инвариант самосогласованности:
    напечатанный идентификатор обязан приниматься следующим шагом — id
    вырезается ИЗ ТЕКСТА ответа, как это сделал бы человек."""
    out = await call("get_task_info", task_id=TASK_ROOT)

    line = next(ln for ln in out.splitlines() if "Взять цифры" in ln)
    assert "(id:" in line, line
    kid_id = line.split("(id:", 1)[1].split(")")[0].strip()
    assert kid_id == TASK_KID

    deeper = await call("get_task_info", task_id=kid_id)
    assert "Взять цифры у бухгалтерии" in deeper
    assert "Уточнить курс" in deeper, deeper      # внучатая подзадача на месте
    assert TASK_GRANDKID in deeper


# ─────────── вложения: индекс ───────────

async def test_download_url_picks_the_attachment_at_the_given_index(stand):
    """Мутация B2: `atts[index - 1]` → `atts[0]` — скачивается не тот файл, и
    молча. Прежний тест «по индексу» этого не видел: в его фикстуре было
    ОДНО вложение, где «нашёл по индексу» и «взял первое» неразличимы."""
    out = await call("get_attachment_download_url", task_id=TASK_ATT,
                     project_id=P_WORK, index=2)

    assert "квитанция.pdf" in out, out
    token = out.split("/dl/")[1].split()[0]
    payload = s._verify_attachment_token(token, "dl")
    assert payload["a"] == ATT_2, payload
    assert payload["n"] == "квитанция.pdf", payload


async def test_download_url_index_walks_the_whole_list(stand):
    """Три вложения — три разных id: инвариант «i-я ссылка ведёт к i-му
    файлу», а не «ссылка вообще строится»."""
    got = []
    for idx in (1, 2, 3):
        out = await call("get_attachment_download_url", task_id=TASK_ATT,
                         project_id=P_WORK, index=idx)
        token = out.split("/dl/")[1].split()[0]
        got.append(s._verify_attachment_token(token, "dl")["a"])
    assert got == [ATT_1, ATT_2, ATT_3], got


async def test_attachment_known_only_from_the_task_text_is_still_reachable(stand):
    """Мутация: выбросить второй источник ссылок (разбор `![file](id/имя)` в
    тексте задачи). У части аккаунтов структурный массив `attachments` пуст —
    тогда файл виден ТОЛЬКО из текста, и без слияния он молча недостижим."""
    listing = await call("list_task_attachments", task_id=TASK_ATT_CONTENT,
                         project_id=P_WORK)
    assert "паспорт.pdf" in listing, listing
    assert ATT_CONTENT_ONLY in listing, listing

    link = await call("get_attachment_download_url", task_id=TASK_ATT_CONTENT,
                      project_id=P_WORK, index=1)
    token = link.split("/dl/")[1].split()[0]
    assert s._verify_attachment_token(token, "dl")["a"] == ATT_CONTENT_ONLY


async def test_attachment_without_id_borrows_it_from_the_task_text(stand):
    """Обратная сторона того же слияния: структурный массив есть, но без `id`
    (наблюдалось живьём). Тогда id берётся из текста по совпадению имени —
    иначе ссылку не подписать и файл снова недостижим."""
    link = await call("get_attachment_download_url", task_id=TASK_ATT_NOID,
                      project_id=P_WORK, filename="полис.pdf")

    assert "/dl/" in link, link
    token = link.split("/dl/")[1].split()[0]
    payload = s._verify_attachment_token(token, "dl")
    assert payload["a"] == ATT_NO_ID, payload
    assert payload["n"] == "полис.pdf", payload


async def test_download_url_out_of_range_index_is_refused_not_clamped(stand):
    out = await call("get_attachment_download_url", task_id=TASK_ATT,
                     project_id=P_WORK, index=9)
    assert "/dl/" not in out, out
    assert "out of range" in out


# ─────────── поиск ───────────

async def test_search_looks_inside_the_note_body(stand):
    """Мутация B15: поиск по content выключен — «ничего не найдено» при
    задаче, где искомое слово написано в заметке."""
    out = await call("search_tasks", search_term="квитанция")

    assert "Оплатить счёт" in out, out
    assert "No tasks found" not in out


async def test_search_scope_closed_reaches_archived_projects(stand):
    """Мутация B4: `scope` игнорируется. Архивные проекты не входят в
    sync-пул, поэтому это единственный способ их прочитать — и наоборот,
    `scope='open'` не должен выдавать архив за открытое."""
    closed = await call("search_all_tasks", query="отчёт", scope="closed")
    assert "Отчёт за прошлый год" in closed, closed
    assert TASK_ARCHIVED in closed
    assert "Собрать отчёт" not in closed, closed

    open_only = await call("search_all_tasks", query="отчёт", scope="open")
    assert "Собрать отчёт" in open_only, open_only
    assert "Отчёт за прошлый год" not in open_only, open_only
    assert P_ARCH not in open_only


async def test_comment_search_declares_its_own_ceiling(monkeypatch):
    """Поиск по комментариям ходит в сеть по одной задаче и обрывается на
    сотне. Молчание об обрыве читается как «искали везде, не нашли» — то есть
    как ОТВЕТ, которого не было. Ответ обязан назвать и сколько задач
    просмотрено, и что упёрлись в потолок."""
    many = [{"id": f"c{i:03d}", "projectId": P_WORK, "title": f"Задача {i}",
             "commentCount": 1} for i in range(120)]
    wire(monkeypatch, state=build_state(tasks=many))

    out = await call("search_all_tasks", query="ничего-такого", scope="open",
                     search_comments=True)

    assert "CAP HIT" in out, out
    assert "scanned 100 task(s)" in out, out


async def test_comment_search_declares_the_ceiling_even_when_it_found_something(monkeypatch):
    """Найденное совпадение не отменяет обрыва: список «нашлось столько-то»
    без пометки о потолке читается как исчерпывающий."""
    tasks = [{"id": TASK_ROOT, "projectId": P_WORK, "title": "Собрать отчёт",
              "commentCount": 1}]
    tasks += [{"id": f"c{i:03d}", "projectId": P_WORK, "title": f"Задача {i}",
               "commentCount": 1} for i in range(120)]
    wire(monkeypatch, state=build_state(tasks=tasks))

    out = await call("search_all_tasks", query="пятницу", scope="open",
                     search_comments=True)

    assert "Собрать отчёт" in out, out          # совпадение по комментарию
    assert "Comment matches (1" in out, out
    assert "CAP HIT" in out, out


async def test_comment_search_without_the_ceiling_says_how_much_it_scanned(monkeypatch):
    """Обратная сторона: потолок не задет — пометки быть не должно, но число
    просмотренных задач называется всё равно (иначе «не найдено» непонятно
    насколько полное)."""
    few = [{"id": f"c{i:03d}", "projectId": P_WORK, "title": f"Задача {i}",
            "commentCount": 1} for i in range(5)]
    wire(monkeypatch, state=build_state(tasks=few))

    out = await call("search_all_tasks", query="ничего-такого", scope="open",
                     search_comments=True)

    assert "CAP HIT" not in out, out
    scanned = len(few) + len(COMPLETED)   # завершённые тоже попадают в пул
    assert f"scanned {scanned} task(s)" in out, out


async def test_search_match_word_does_not_hit_inside_a_longer_word(monkeypatch):
    """`match='word'` обязан отсекать попадания внутрь слова, иначе он не
    отличается от подстроки и заявлен зря."""
    tasks = [{"id": "t1", "projectId": P_WORK, "title": "Купить board"},
             {"id": "t2", "projectId": P_WORK, "title": "Накормить boa"}]
    wire(monkeypatch, state=build_state(tasks=tasks))

    word = await call("search_all_tasks", query="boa", scope="open", match="word")
    assert "Накормить boa" in word, word
    assert "Купить board" not in word, word

    substring = await call("search_all_tasks", query="boa", scope="open")
    assert "Купить board" in substring, substring


async def test_search_fields_title_ignores_the_note_body(monkeypatch):
    tasks = [{"id": "t1", "projectId": P_WORK, "title": "Оплатить счёт",
              "content": "приложена квитанция"},
             {"id": "t2", "projectId": P_WORK, "title": "Квитанция для бухгалтерии"}]
    wire(monkeypatch, state=build_state(tasks=tasks))

    only_title = await call("search_all_tasks", query="квитанция", scope="open",
                            fields="title")
    assert "Квитанция для бухгалтерии" in only_title, only_title
    assert "Оплатить счёт" not in only_title, only_title

    everywhere = await call("search_all_tasks", query="квитанция", scope="open",
                            fields="all")
    assert "Оплатить счёт" in everywhere, everywhere
