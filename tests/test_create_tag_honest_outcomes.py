"""QA 2026-08-19, баги №4 и №5 (живая приёмка) — `create_tag` врало о том,
что произошло, в обе стороны.

№4: `create_tag(name="X")` дважды подряд — оба раза "✅ Тег «X» создан
(проверено)." Данные не задваиваются (TickTick второй вызов просто
игнорирует), но СООБЩЕНИЕ врёт: второго создания не было.

№5: `create_tag("")` и `create_tag(<280 символов>)` — оба раза
"⚠️ Тег «…» отправлен на создание, но не виден в свежем списке тегов — проверь
вручную." На деле TickTick тихо отказал ДО того, как тег мог бы появиться —
ответ был известен точно ("не создан"), а формулировка "проверь вручную"
изображала неопределённость там, где её не было.

Фикс — `_create_tag_impl` (ticktick_mcp/src/server.py): валидация пустого/
сверхдлинного имени и регистронезависимая проверка существования — ОБЕ ДО
обращения к API, а не после."""
import ticktick_mcp.src.server as s


class FakeV2:
    def __init__(self, tags=None):
        self.tags = list(tags or [])
        self.create_calls = []

    def get_state(self, force=True):
        return {}

    def get_tags(self):
        return [dict(t) for t in self.tags]

    def create_tag(self, name, color=None):
        self.create_calls.append((name, color))
        self.tags.append({"name": name.lower(), "label": name, "color": color})
        return {}


def _wire(monkeypatch, fake):
    monkeypatch.setattr(s, "ticktick_v2", fake)


# ─────────── бага №4: дубликат ───────────

async def test_second_create_of_the_same_name_does_not_claim_creation(
        monkeypatch):
    fake = FakeV2()
    _wire(monkeypatch, fake)

    first = await s._create_tag_impl("Работа")
    second = await s._create_tag_impl("Работа")

    assert "создан" in first.lower()
    assert "уже существует" in second.lower(), second
    assert "✅" not in second, second
    assert len(fake.create_calls) == 1, (
        f"второй вызов не имел права дёргать API: {fake.create_calls}")


async def test_duplicate_check_is_case_insensitive(monkeypatch):
    fake = FakeV2(tags=[{"name": "работа", "label": "Работа"}])
    _wire(monkeypatch, fake)

    result = await s._create_tag_impl("РАБОТА")

    assert "уже существует" in result.lower(), result
    assert fake.create_calls == []


# ─────────── бага №5: пустое / сверхдлинное имя ───────────

async def test_empty_name_is_refused_before_touching_the_api(monkeypatch):
    fake = FakeV2()
    _wire(monkeypatch, fake)

    result = await s._create_tag_impl("")

    assert "🛑" in result, result
    assert "проверь вручную" not in result.lower(), result
    assert fake.create_calls == [], "пустое имя не должно доходить до API"


async def test_whitespace_only_name_is_refused(monkeypatch):
    fake = FakeV2()
    _wire(monkeypatch, fake)

    result = await s._create_tag_impl("    ")

    assert "🛑" in result, result
    assert fake.create_calls == []


async def test_oversized_name_is_refused_before_touching_the_api(monkeypatch):
    fake = FakeV2()
    _wire(monkeypatch, fake)

    result = await s._create_tag_impl("ф" * 280)

    assert "🛑" in result, result
    assert "проверь вручную" not in result.lower(), result
    assert fake.create_calls == [], "сверхдлинное имя не должно доходить до API"


async def test_normal_name_still_creates_as_before(monkeypatch):
    """Контроль: обычное новое имя по-прежнему создаётся и подтверждается."""
    fake = FakeV2()
    _wire(monkeypatch, fake)

    result = await s._create_tag_impl("Дом", "#FF6161")

    assert "✅" in result and "создан" in result.lower(), result
    assert fake.create_calls == [("Дом", "#FF6161")]
