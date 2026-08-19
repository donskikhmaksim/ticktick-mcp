"""Два добора QA-2 (2026-08-19) по вложениям.

№4: `list_task_attachments` округлял размер в KB целочисленно — файл в 16
байт печатался как «0 KB» (при том что attach_file_to_task в своём отчёте
называет точное число байт). Теперь размер человеческий: байты — маленьким,
KB/MB — большим.

№5: `attach_file_to_task` на битый url отдавал сырой питоний стектрейс
(«HTTPSConnectionPool(host=…)… NameResolutionError…») вместо человеческого
сообщения; а HTTP-ошибка ФАЙЛОВОГО сервера, пройдя через общий
`_humanize_api_error`, выглядела бы как ошибка ТИКТИКА. Теперь скачивание
обёрнуто в клиенте (там известен контекст «не скачался файл, не API»), а
`_humanize_api_error` дополнительно расшифровывает DNS-ошибки для остальных
путей.
"""
import pytest
import requests

import ticktick_mcp.src.server as s
from ticktick_mcp.src import ticktick_v2_client as v2mod
from ticktick_mcp.src.ticktick_v2_client import TickTickV2Client


# ═══════ №4: человеческий размер ═══════

@pytest.mark.parametrize("size,expected", [
    (16, "16 байт"),
    (0, "0 байт"),
    (1023, "1023 байт"),
    (1024, "1 KB"),
    (1536, "1.5 KB"),
    (2 * 1024 * 1024, "2 MB"),
])
def test_human_file_size(size, expected):
    assert s._human_file_size(size) == expected


class _FakeV2Attachments:
    """Двойник на уровне КЛИЕНТА (не внутренних функций пути чтения — см.
    tests/test_doubles_do_not_cheat.py): `_merged_task_attachments` исполняется
    по-настоящему."""

    def get_task_attachments(self, task_id):
        return [{"fileName": "note.txt", "id": "a1", "size": 16},
                {"fileName": "big.pdf", "id": "a2", "size": 3 * 1024 * 1024}]

    def get_content_attachment_refs(self, task_id):
        return []


async def test_list_attachments_small_file_is_not_zero_kb(monkeypatch):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "ticktick_v2", _FakeV2Attachments())
    out = await s.list_task_attachments("t1")
    assert "16 байт" in out, out
    assert "0 KB" not in out, out
    assert "3 MB" in out, out


# ═══════ №5: битый url — человеческая ошибка, не стектрейс ═══════

_DNS_TRACE = ("HTTPSConnectionPool(host='no-such-host.invalid', port=443): "
              "Max retries exceeded with url: /f.png (Caused by "
              "NameResolutionError(\"<urllib3.connection.HTTPSConnection "
              "object>: Failed to resolve 'no-such-host.invalid'\"))")


@pytest.fixture
def client():
    return TickTickV2Client(token="tok")


def test_upload_attachment_dns_failure_is_human(client, monkeypatch):
    def _boom(url, timeout=60):
        raise requests.exceptions.ConnectionError(_DNS_TRACE)

    monkeypatch.setattr(v2mod.requests, "get", _boom)
    with pytest.raises(ValueError) as e:
        client.upload_attachment("p1", "t1", url="https://no-such-host.invalid/f.png")
    msg = str(e.value)
    assert "не удалось скачать файл по ссылке" in msg, msg
    assert "проверь адрес" in msg, msg
    assert "HTTPSConnectionPool" not in msg, "сырой стектрейс уехал человеку"


def test_upload_attachment_http_error_names_the_file_server_status(
        client, monkeypatch):
    class _Resp:
        status_code = 404
        content = b""

        def raise_for_status(self):
            err = requests.exceptions.HTTPError("404 Client Error")
            err.response = self
            raise err

    monkeypatch.setattr(v2mod.requests, "get", lambda url, timeout=60: _Resp())
    with pytest.raises(ValueError) as e:
        client.upload_attachment("p1", "t1", url="https://example.com/gone.png")
    msg = str(e.value)
    assert "не удалось скачать файл по ссылке" in msg, msg
    assert "404" in msg, msg
    # Текст НЕ содержит формат «404 Client Error», который общий
    # `_humanize_api_error` принял бы за ответ самого TickTick.
    assert "Client Error" not in msg, msg


def test_upload_attachment_timeout_is_human(client, monkeypatch):
    def _slow(url, timeout=60):
        raise requests.exceptions.ReadTimeout("Read timed out. (read timeout=60)")

    monkeypatch.setattr(v2mod.requests, "get", _slow)
    with pytest.raises(ValueError) as e:
        client.upload_attachment("p1", "t1", url="https://example.com/slow.bin")
    assert "не ответил" in str(e.value), str(e.value)


def test_humanize_api_error_translates_dns_failures():
    out = s._humanize_api_error(Exception(_DNS_TRACE))
    assert "имя хоста не разрешилось" in out, out
    # Исходный текст сохраняется в скобках для диагностики — как у HTTP-кодов.
    assert "NameResolutionError" in out
