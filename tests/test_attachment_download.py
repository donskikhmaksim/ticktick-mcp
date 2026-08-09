"""2026-08-09 (П9 пакет ТЗ, пункт 5): download_task_attachment не имел ни
одного теста среди 108+ файлов в tests/, хотя на живых данных он оказался
практически непригоден — фотография чека вернула ответ такого размера, что
транспорт MCP отбрасывал его целиком (см. коммит, снизивший
DOWNLOAD_ATTACHMENT_MAX_BYTES с 15 МБ до 256 КБ). Минимум по ТЗ:
  1. маленький файл проходит целиком;
  2. файл за пределом отказывает И называет команду-альтернативу;
  3. несуществующее вложение отказывает внятно.

Двойник v2-клиента (FakeV2Attachments) намеренно устроен так же, как
настоящий TickTickV2Client.download_attachment: неизвестный attachment_id
роняет тот же ValueError с тем же текстом, что и реальный клиент на 404
(см. ticktick_v2_client.py:876+) — тест не имеет права быть добрее кода,
который он проверяет (tests/test_doubles_do_not_cheat.py)."""
import pytest

import ticktick_mcp.src.server as s

TASK_ID = "task1"
PROJECT_ID = "proj1"
SMALL_ATT_ID = "0ae67755c0e5411ab297476a"
BIG_ATT_ID = "1bf67755c0e5411ab297476b"


class FakeV2Attachments:
    """Двойник ровно тех методов ticktick_v2, которыми пользуются
    _merged_task_attachments/_resolve_attachment_ref/download_task_attachment."""

    def __init__(self, files):
        # files: {att_id: (fileName, bytes, mime)}
        self._files = dict(files)
        self.download_calls = []

    def get_task_attachments(self, task_id):
        return [{"id": att_id, "fileName": name}
                for att_id, (name, _data, _mime) in self._files.items()]

    def get_content_attachment_refs(self, task_id):
        return []

    def download_attachment(self, project_id, task_id, attachment_id, filename=None):
        self.download_calls.append((project_id, task_id, attachment_id, filename))
        if attachment_id not in self._files:
            # Тот же текст и тот же тип исключения, что и настоящий клиент
            # на HTTP 404 (ticktick_v2_client.py, download_attachment).
            raise ValueError(
                f"Attachment {attachment_id} not found on task {task_id} "
                "(wrong id, or the file was removed).")
        name, data, mime = self._files[attachment_id]
        return (filename or name, data, mime)


@pytest.fixture
def fake_v2(monkeypatch):
    fake = FakeV2Attachments({
        SMALL_ATT_ID: ("small.txt", b"hello world", "text/plain"),
        BIG_ATT_ID: ("receipt.jpg",
                     b"x" * (s.DOWNLOAD_ATTACHMENT_MAX_BYTES + 1024),
                     "image/jpeg"),
    })
    monkeypatch.setattr(s, "ticktick_v2", fake)
    return fake


# ===========================================================================
# 1. Маленький файл проходит целиком
# ===========================================================================

async def test_small_attachment_downloads_successfully(fake_v2):
    out = await s.download_task_attachment(
        task_id=TASK_ID, project_id=PROJECT_ID, attachment_id=SMALL_ATT_ID)

    assert "filename: small.txt" in out
    assert "mime: text/plain" in out
    assert "size_bytes: 11" in out
    import base64
    b64 = out.split("content_base64: ")[1].strip()
    assert base64.b64decode(b64) == b"hello world"


async def test_small_attachment_can_be_selected_by_index(fake_v2):
    """index/filename идут тем же путём резолвинга, что и attachment_id —
    happy path не завязан на один конкретный селектор."""
    out = await s.download_task_attachment(
        task_id=TASK_ID, project_id=PROJECT_ID, filename="small.txt")
    assert "filename: small.txt" in out
    assert "content_base64:" in out


# ===========================================================================
# 2. Файл за пределом отказывает и называет альтернативу
# ===========================================================================

async def test_oversized_attachment_refuses_and_names_the_alternative(fake_v2):
    out = await s.download_task_attachment(
        task_id=TASK_ID, project_id=PROJECT_ID, attachment_id=BIG_ATT_ID)

    assert "Not downloaded" in out
    assert "content_base64" not in out
    # Пункт 4 ТЗ: текст отказа СРАЗУ называет команду-альтернативу.
    assert "get_attachment_download_url" in out


async def test_oversized_attachment_reports_the_actual_and_max_size(fake_v2):
    out = await s.download_task_attachment(
        task_id=TASK_ID, project_id=PROJECT_ID, attachment_id=BIG_ATT_ID)

    max_kb = s.DOWNLOAD_ATTACHMENT_MAX_BYTES // 1024
    assert f"{max_kb} KB" in out


# ===========================================================================
# 3. Несуществующее вложение отказывает внятно
# ===========================================================================

async def test_unknown_attachment_id_refuses_clearly(fake_v2):
    """attachment_id, которого нет ни в списке вложений задачи, ни у
    TickTick — download_attachment() роняет ValueError (мимикрируя реальный
    404), и тул обязан вернуть человекочитаемую причину, а не выбросить
    исключение наружу или тихо соврать про успех."""
    out = await s.download_task_attachment(
        task_id=TASK_ID, project_id=PROJECT_ID, attachment_id="does-not-exist")

    assert "content_base64" not in out
    assert "not found" in out.lower()


async def test_unknown_filename_refuses_and_points_at_list_tool(fake_v2):
    out = await s.download_task_attachment(
        task_id=TASK_ID, project_id=PROJECT_ID, filename="ghost.pdf")

    assert "content_base64" not in out
    assert "No attachment named" in out
    assert "list_task_attachments" in out


async def test_task_with_no_attachments_refuses_clearly(monkeypatch):
    empty = FakeV2Attachments({})
    monkeypatch.setattr(s, "ticktick_v2", empty)

    out = await s.download_task_attachment(
        task_id=TASK_ID, project_id=PROJECT_ID, attachment_id="anything")

    assert "content_base64" not in out
    assert "No attachments found" in out
