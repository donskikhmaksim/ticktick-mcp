"""СТРУКТУРНАЯ ЛОВУШКА на сами тесты: двойник не имеет права быть добрее
настоящего сервера, а тест — знать больше настоящего клиента.

Повод (три независимо найденных случая, все с зелёными тестами):
  1. клиент слал в TickTick строку "NONE" там, где нужен пустой groupId, —
     разгруппировка проекта не работала НИКОГДА, потому что перевод "NONE" в
     пустое значение делал сам ТЕСТОВЫЙ ДВОЙНИК;
  2. тест обходил известный дефект сверки ключа, подставляя удобные данные, и
     фиксировал это комментарием вместо починки;
  3. тесты доставали идентификатор плана из реестра планов В ПАМЯТИ ПРОЦЕССА
     — путь, которого у клиента по HTTP нет, — и поэтому не видели, что
     идентификатор наружу вообще не печатается.

Точечные проверки такие вещи не ловят: они ловят ровно тот случай, который в
них записали. Ловушки ниже смотрят на СТРУКТУРУ тестов и поэтому краснеют на
следующем случае того же вида, а не на повторении прошлого.
"""
import ast
import pathlib

import ticktick_mcp.src.ticktick_client as v1_mod
import ticktick_mcp.src.ticktick_v2_client as v2_mod

_TESTS_DIR = pathlib.Path(__file__).resolve().parent


def _test_modules():
    return sorted(p for p in _TESTS_DIR.glob("*.py") if p.name != "__init__.py")


def _parse(path):
    return ast.parse(path.read_text(encoding="utf-8"))


# ═════════ 1. Двойник клиента не проглатывает что попало ═════════

def _real_client_methods():
    out = {}
    for module in (v2_mod, v1_mod):
        src = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        for cls in ast.walk(ast.parse(src)):
            if not isinstance(cls, ast.ClassDef):
                continue
            for fn in cls.body:
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.setdefault(fn.name, fn)
    return out


def _double_classes(tree):
    """Классы-двойники: те, что подставляются вместо клиента, плюс всё, что
    названо Fake/Stub/Spy. Имя — эвристика, а не контракт, поэтому проверка
    ниже применяется только к методам, СОВПАДАЮЩИМ по имени с методами
    настоящего клиента: случайный класс так не зацепить."""
    for cls in ast.walk(tree):
        if isinstance(cls, ast.ClassDef):
            yield cls


def test_no_client_double_swallows_arbitrary_arguments():
    """Метод двойника, названный как метод настоящего клиента, обязан
    объявлять параметры ЯВНО.

    `**kwargs` — это «принимаю что угодно»: двойник не заметит ни опечатки в
    имени параметра, ни потерянного поля, тогда как живой клиент на опечатке
    упал бы с TypeError, а потерянное поле уехало бы в TickTick значением по
    умолчанию. Именно так пряталось то, что сервер не передаёт `step` при
    создании количественной привычки."""
    real = _real_client_methods()
    offenders = []
    for path in _test_modules():
        for cls in _double_classes(_parse(path)):
            for fn in cls.body:
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if fn.name.startswith("_") or fn.name not in real:
                    continue
                if fn.args.kwarg is not None:
                    offenders.append(f"{path.name}::{cls.name}.{fn.name}")
    assert not offenders, (
        "двойники принимают **kwargs вместо явных параметров настоящего "
        f"клиента (пропустят опечатку и потерянное поле): {offenders}")


def test_no_client_double_answers_a_method_the_real_client_lacks():
    """Двойник, отвечающий на метод, которого у настоящего клиента нет, даёт
    тесту «покрытие» пути, которого в бою не существует (обычно — след от
    переименования)."""
    real = set(_real_client_methods())
    # Классы, которые ПОДСТАВЛЯЮТСЯ вместо клиентов: только их и проверяем —
    # остальные Fake* моделируют Telegram, Postgres, Google Sheets и т.п.
    substituted = set()
    for path in _test_modules():
        tree = _parse(path)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "setattr" and len(node.args) >= 3):
                target = node.args[1]
                value = node.args[2]
                if (isinstance(target, ast.Constant)
                        and target.value in ("ticktick", "ticktick_v2")
                        and isinstance(value, ast.Call)
                        and isinstance(value.func, ast.Name)):
                    substituted.add((path.name, value.func.id))

    offenders = []
    for path in _test_modules():
        names = {c for f, c in substituted if f == path.name}
        if not names:
            continue
        for cls in _double_classes(_parse(path)):
            if cls.name not in names:
                continue
            for fn in cls.body:
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if fn.name.startswith("_") or fn.name in real:
                    continue
                offenders.append(f"{path.name}::{cls.name}.{fn.name}")
    assert not offenders, (
        "двойники клиента отвечают на методы, которых у настоящего клиента "
        f"нет — тесты «покрывают» несуществующий путь: {offenders}")


# ═════════ 2. monkeypatch не выдумывает то, чего нет ═════════

def test_monkeypatch_setattr_never_creates_a_missing_attribute():
    """`raising=False` у `setattr` не ПРОВЕРЯЕТ атрибут, а СОЗДАЁТ его.

    Тест с такой подменой остаётся зелёным даже если функции в боевом модуле
    больше нет вовсе — и продолжает утверждать, что она отработала. Здесь так
    и было: три канала публикации отчёта подменялись с `raising=False`, и
    удаление любого из них пакет не заметил бы.

    `delenv(..., raising=False)` — законен: переменной окружения может не
    быть, это не утверждение о существовании кода."""
    offenders = []
    for path in _test_modules():
        for node in ast.walk(_parse(path)):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("setattr", "setitem", "delattr")):
                continue
            for kw in node.keywords:
                if kw.arg == "raising" and isinstance(kw.value, ast.Constant) \
                        and kw.value.value is False:
                    offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "monkeypatch.setattr(..., raising=False) создаёт отсутствующий "
        f"атрибут вместо проверки: {offenders}")


# ═════════ 3. Тест не знает больше сетевого клиента ═════════

# Реестр планов живёт в памяти ОДНОГО процесса. У MCP-клиента его нет: он на
# другом конце HTTP и видит РОВНО текст ответа. Тест, который берёт оттуда
# идентификатор плана, проверяет путь, которого в бою не существует.
_MEMORY_REGISTRIES = ("_MANIFESTS", "_MANIFEST_TOMBSTONES")

# Единственное законное основание попасть сюда: тесту нужен id, которого
# снаружи НЕТ вовсе, и проверяется как раз отказ по нему.
_ALLOWED_ID_HARVEST = {
    "test_tg_create_and_direct.py": (
        "план не отправлен и погашен — id наружу не печатается; проверяется, "
        "что даже узнавший его клиент ничего не исполнит"),
}


def _harvests_id_from_registry(tree):
    """Ищет выражения, которые ВЫТАСКИВАЮТ ключ из реестра планов:
    next(iter(...)), next(генератор по .items()), list(...)[i], sorted(...)[i].

    Заведение фикстуры (`_MANIFESTS["m1"] = {...}`) и проверка состояния
    (`_MANIFESTS[mid]["consumed"]`) сюда НЕ попадают — они законны: первое
    готовит стенд, второе проверяет инвариант сервера, а не подменяет канал
    связи с клиентом."""
    hits = []

    def mentions_registry(node):
        return any(isinstance(n, ast.Attribute) and n.attr in _MEMORY_REGISTRIES
                   for n in ast.walk(node))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "next" and node.args and mentions_registry(node.args[0]):
                hits.append(node.lineno)
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Call) \
                and isinstance(node.value.func, ast.Name) \
                and node.value.func.id in ("list", "sorted") \
                and node.value.args and mentions_registry(node.value.args[0]):
            hits.append(node.lineno)
    return hits


def test_tests_do_not_take_the_plan_id_out_of_process_memory():
    """Идентификатор плана обязан браться из ТЕКСТА ответа — единственного,
    что доходит до клиента. Пока тесты брали его из памяти, дыра «id снаружи
    не печатается» была невидима, хотя формально «покрыта»."""
    offenders = {}
    for path in _test_modules():
        lines = _harvests_id_from_registry(_parse(path))
        if lines and path.name not in _ALLOWED_ID_HARVEST:
            offenders[path.name] = lines
    assert not offenders, (
        "тесты добывают id плана из реестра в памяти процесса вместо текста "
        f"ответа: {offenders} — так дыра «id не печатается» снова станет "
        "невидимой")


def test_the_id_harvest_allowlist_has_not_gone_stale():
    """Исключение без причины — это дыра. Если файл перестал брать id из
    памяти, он обязан выйти из списка, иначе список тихо разрешит следующему
    тесту то же самое."""
    stale = []
    for name in _ALLOWED_ID_HARVEST:
        path = _TESTS_DIR / name
        if not path.exists() or not _harvests_id_from_registry(_parse(path)):
            stale.append(name)
    assert not stale, (
        f"в списке исключений файлы, которые уже не берут id из памяти: {stale}")
