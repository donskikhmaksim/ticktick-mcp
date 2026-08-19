"""QA 2026-08-19, бага №3 (живая приёмка): `delete_tags` молча ничего не
делал, когда `tags` приходил СТРОКОЙ вместо списка.

Живой прогон: вызов с непустым списком имён вернул "Пустой список — нечего
делать.", хотя имена были переданы — по диагностике список пришёл строкой, а
не массивом. Это ТИХИЙ ОТКАЗ: человек уверен, что теги удалены/обработаны, а
вызов не сделал вообще ничего, и ответ не даёт повода заподозрить проблему.
Одиночный `delete_tag` при этом отрабатывал штатно.

Фикс — `_coerce_str_list_arg` (ticktick_mcp/src/server.py): (а) строка,
которая парсится как JSON-массив, принимается как есть; (б) строка, которая
НЕ парсится (или парсится не в массив), и при этом непустая — ЯВНАЯ ошибка
формата, а не «нечего делать»; (в) настоящий пустой список/None — по-прежнему
законное «нечего делать», это не баг."""
import pytest

import ticktick_mcp.src.server as s


class _IsolateManifests:
    """Тот же приём, что tests/test_delete_tags.py — _MANIFESTS общий
    модульный словарь, снимок/восстановление того же объекта."""

    def __enter__(self):
        self.before = dict(s._MANIFESTS)
        s._MANIFESTS.clear()
        return self

    def __exit__(self, *exc):
        s._MANIFESTS.clear()
        s._MANIFESTS.update(self.before)


@pytest.fixture(autouse=True)
def _isolate_manifests():
    with _IsolateManifests():
        yield


class FakeV2:
    def __init__(self, tags=None):
        self.tags = tags if tags is not None else []
        self.calls = []

    def get_state(self, force=False):
        self.calls.append(("get_state", force))
        return {}

    def get_tags(self):
        return [dict(t) for t in self.tags]

    def get_open_tasks(self):
        return []

    def delete_tag(self, name):
        self.calls.append(("delete_tag", name))
        self.tags = [t for t in self.tags
                    if (t.get("name") or "").lower() != name.lower()]


def _wire(monkeypatch, fake):
    monkeypatch.setattr(s, "ticktick_v2", fake)


# ─────────── прямой юнит-тест хелпера ───────────

def test_coerce_passes_through_a_real_list():
    out, err = s._coerce_str_list_arg(["a", "b"], "tags")
    assert err is None
    assert out == ["a", "b"]


def test_coerce_parses_a_json_encoded_string():
    out, err = s._coerce_str_list_arg('["дом", "работа"]', "tags")
    assert err is None
    assert out == ["дом", "работа"]


def test_coerce_none_stays_none():
    out, err = s._coerce_str_list_arg(None, "tags")
    assert err is None
    assert out is None


def test_coerce_empty_string_is_a_legitimate_empty_list_not_an_error():
    out, err = s._coerce_str_list_arg("", "tags")
    assert err is None
    assert out == []


def test_coerce_non_json_non_empty_string_is_an_explicit_error():
    """Раньше именно этот случай (или его эквивалент выше по стеку) читался
    как «пустой список» — теперь явная ошибка формата."""
    out, err = s._coerce_str_list_arg("дом,работа", "tags")
    assert out is None
    assert err is not None
    assert "🛑" in err
    assert "tags" in err


def test_coerce_json_string_that_is_not_a_list_is_an_explicit_error():
    out, err = s._coerce_str_list_arg('"дом"', "tags")
    assert out is None
    assert err is not None


# ─────────── delete_tags целиком: ложная «пустота» на непустом вводе ───────

async def test_delete_tags_with_malformed_string_input_refuses_explicitly(
        monkeypatch):
    """Гвоздь бага: непустой, но не-JSON-строчный `tags` НЕ должен читаться
    как «нечего делать» — ни одна задача не должна выглядеть выполненной."""
    fake = FakeV2(tags=[{"name": "дом", "parent": None}])
    _wire(monkeypatch, fake)

    result = await s.delete_tags("Удаляю", tags="дом,работа")

    assert "Пустой список" not in result, result
    assert "🛑" in result, result
    assert not any(c[0] == "delete_tag" for c in fake.calls)
    assert len(s._MANIFESTS) == 0


async def test_delete_tags_with_json_encoded_string_still_works(monkeypatch):
    """(а)-половина фикса: JSON-строка списка — законный ввод, не ошибка."""
    fake = FakeV2(tags=[{"name": "дом", "parent": None}])
    _wire(monkeypatch, fake)

    result = await s.delete_tags("Удаляю", tags='["дом"]')

    assert "manifest_id" in result or "Манифест" in result, result
    assert len(s._MANIFESTS) == 1


async def test_delete_tags_genuinely_empty_list_is_still_the_old_message(
        monkeypatch):
    fake = FakeV2(tags=[])
    _wire(monkeypatch, fake)

    result = await s.delete_tags("Удаляю", tags=[])

    assert result == "Пустой список — нечего делать."


async def test_delete_tags_none_is_a_loud_refusal_not_nothing_to_do(monkeypatch):
    """ПЕРЕПИСАН (QA-2 2026-08-19, добор №7) — раньше закреплял старое
    поведение «None → "Пустой список — нечего делать"». Живой случай показал,
    что это тихий no-op на опечатку в ИМЕНИ параметра (delete_tags(names=[…])
    → tags=None → «нечего делать» → вызывающий уверен, что теги удалены).
    Новый контракт: параметр не передан вовсе — явный отказ с именем
    параметра; настоящий [] остаётся «нечего делать» (тест выше). Подробности
    — tests/test_required_list_param_missing.py."""
    fake = FakeV2(tags=[])
    _wire(monkeypatch, fake)

    result = await s.delete_tags("Удаляю", tags=None)

    assert "🛑" in result and "`tags` обязателен" in result, result
    assert "нечего делать" not in result.lower(), result
