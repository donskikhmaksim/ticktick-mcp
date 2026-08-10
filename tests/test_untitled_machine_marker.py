"""1.1.7 — Д14: машинный признак `Untitled: true` рядом с человеческим текстом.

Раньше на месте безымянного `Title:` печаталось машинное `No title`
(`task.get('title', 'No title')`, git-археология — см. коммит 79d95cb, где
строка появилась впервые). Это выражение уже тогда было ненадёжным: `.get()`
с default даёт «No title» ТОЛЬКО когда КЛЮЧА `title` в словаре нет вовсе —
задача с явным `title: ""` печаталась как `Title: ` (пустая строка), и
потребитель, искавший `Title: No title`, часть безымянных задач терял ещё до
всех последующих правок.

П15 (2026-08-09) заменила эту заглушку на человеческий заменитель
`_untitled_label` — «(без названия: 📎 1 файл)» / «есть текст» / «пусто» —
ради ровно одного: карточка задачи с фотографией чека перестала выглядеть
неотличимой от пустышки. Побочный эффект: потребитель, искавший пустые
задачи текстом, потерял и этот, уже честный, признак — три разных хвоста
плюс форма без хвоста, все по-русски, программно не ловятся одной подстрокой.

Эта правка (1.1.7, Д14) кладёт РЯДОМ со строкой `Title:` — которую трогать
нельзя, иначе возвращается то самое слепое пятно — отдельную МАШИННУЮ строку
`Untitled: true`, только для безымянных задач, по ТОМУ ЖЕ предикату
`_looks_untitled`, что уже решает, печатать ли заменитель в `Title:`.

Два сторожа:
  1. `test_card_of_a_nameless_task_carries_the_untitled_field` — признак есть
     у безымянной задачи и отсутствует у обычной, проверено на ТРЁХ разных
     командах (`get_task`, `get_project_tasks`, `search_all_tasks`) — правка
     только в одном месте вызова `format_task` такое не переживёт (подделка
     №2 из задания).
  2. `test_untitled_field_is_not_derived_from_russian_text` — задача, чьё
     НАСТОЯЩЕЕ название начинается ровно с того же текста, что и человеческий
     заменитель («(без названия…»), признака получать НЕ должна: он обязан
     идти от `_looks_untitled`, а не от разбора уже готовой строки на
     подстроку (подделка №1 из задания).

Плюс два прицельных пункта из "Готово, когда" (п.4 — невидимые символы,
п.5 — человеческая строка с вложением не деградировала), которые не требуют
асинхронного стенда и проверяются прямо на `format_task`.
"""
import ticktick_mcp.src.server as s
from tests.read_stand import P_ARCH, call, wire


# ═════════ 1. Признак виден во ВСЕХ местах печати карточки ═════════

async def test_card_of_a_nameless_task_carries_the_untitled_field(monkeypatch):
    marker = "МАРКЕР7Х3УНИК"
    untitled = {"id": "u1", "projectId": P_ARCH, "title": "", "status": 0,
                "content": f"{marker} документ без имени"}
    normal = {"id": "n1", "projectId": P_ARCH,
              "title": f"Обычная задача {marker}", "status": 0}
    wire(monkeypatch, v1_tasks=[untitled, normal])

    # Команда 1 — get_task
    card_untitled = await call("get_task", project_id=P_ARCH, task_id="u1")
    assert "Untitled: true" in card_untitled
    card_normal = await call("get_task", project_id=P_ARCH, task_id="n1")
    assert "Untitled:" not in card_normal

    # Команда 2 — get_project_tasks
    listing = await call("get_project_tasks", project_id=P_ARCH)
    assert listing.count("Untitled: true") == 1

    # Команда 3 — search_all_tasks. scope="closed" печатает найденное через
    # format_task (не через format_task_tree, как открытая ветка) — см.
    # server.py, ветка `if want_closed:` в search_all_tasks.
    found = await call("search_all_tasks", query=marker, scope="closed")
    assert found.count("Untitled: true") == 1


# ═════════ 2. Признак — от _looks_untitled, не от разбора текста ═════════

async def test_untitled_field_is_not_derived_from_russian_text(monkeypatch):
    # Настоящее имя, которое СЛУЧАЙНО начинается ровно с того текста, каким
    # _untitled_label подписывает пустышку. Наивная реализация вида
    # `"(без названия" in formatted` спутала бы это с признаком пустоты.
    real_title = "(без названия: реальная задача, не пустышка)"
    task = {"id": "n2", "projectId": P_ARCH, "title": real_title, "status": 0}
    wire(monkeypatch, v1_tasks=[task])

    # Предпосылка теста: с точки зрения предохранителя это НЕ безымянная
    # задача — у неё есть настоящее (пусть и совпадающее по тексту) имя.
    assert s._looks_untitled(real_title) is False

    out = await call("get_task", project_id=P_ARCH, task_id="n2")
    assert f"Title: {real_title}" in out
    assert "Untitled:" not in out


# ═════════ 3. Прицельные пункты "Готово, когда" — на голом format_task ═════

def test_invisible_only_title_still_gets_the_machine_marker():
    """П.4: название из одних невидимых символов (zero-width space) — тоже
    безымянная задача с точки зрения показа (`_looks_untitled`); признак
    обязан сработать так же, как на пустой строке."""
    task = {"id": "t1", "projectId": "p1", "title": "​", "status": 0}
    assert "Untitled: true" in s.format_task(task)


def test_human_placeholder_line_is_unchanged_for_attachment_case():
    """П.5: человеческая строка `Title: (без названия: 📎 1 файл)` не
    деградировала — правка добавляет ОТДЕЛЬНУЮ строку, а не переписывает
    `Title:`."""
    task = {"id": "t1", "projectId": "p1", "title": "",
            "attachments": [{"fileName": "receipt.jpg", "id": "a" * 24}]}
    out = s.format_task(task)
    assert "Title: (без названия: 📎 1 файл)\n" in out
    assert "Untitled: true" in out


def test_ordinary_task_gets_no_untitled_line_at_all():
    """Контроль: обычная задача с настоящим названием не несёт признак вовсе
    (не «Untitled: false», а полное отсутствие строки — задание требует
    искать по НАЛИЧИЮ строки, не по её значению)."""
    task = {"id": "t1", "projectId": "p1", "title": "Купить молоко", "status": 0}
    out = s.format_task(task)
    assert "Untitled:" not in out
