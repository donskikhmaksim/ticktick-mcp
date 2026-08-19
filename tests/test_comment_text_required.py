"""Пустой текст комментария — отказ ДО гейта и ДО API (QA-2 2026-08-19,
добор №3).

Живой QA-кейс: `add_task_comment(text="")` реально создавал ПУСТОЙ
комментарий (TickTick принимает), а `update_task_comment(text="")` молча
СТИРАЛ существующий текст под видом правки — и оба отчитывались успехом.
Теперь оба отказывают внятно на call #1, ничего не планируя; call #2 не
трогается (аргументы там игнорируются — текст берётся из манифеста, уже
прошедшего эту проверку).
"""
import pytest

import ticktick_mcp.src.server as s


@pytest.fixture(autouse=True)
def _isolate_manifests():
    before = dict(s._MANIFESTS)
    s._MANIFESTS.clear()
    yield
    s._MANIFESTS.clear()
    s._MANIFESTS.update(before)


@pytest.fixture(autouse=True)
def _ready(monkeypatch):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)


class _ExplodingV2:
    """Любое обращение к API — провал теста: отказ обязан случиться раньше."""

    def __getattr__(self, name):
        raise AssertionError(f"ticktick_v2.{name} вызван — отказ по пустому "
                             "тексту обязан случиться ДО обращения к API")


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
async def test_add_task_comment_empty_text_refused_before_anything(
        monkeypatch, text):
    monkeypatch.setattr(s, "ticktick_v2", _ExplodingV2())
    out = await s.add_task_comment("Купить молоко", text, "p1", "t1")
    assert "🛑" in out and "Пустой текст" in out, out
    assert s._MANIFESTS == {}, "манифест не должен строиться на пустой текст"


@pytest.mark.parametrize("text", ["", "   "])
async def test_update_task_comment_empty_text_refused_and_points_to_delete(
        monkeypatch, text):
    monkeypatch.setattr(s, "ticktick_v2", _ExplodingV2())
    out = await s.update_task_comment("Купить молоко", text, "p1", "t1", "c1")
    assert "🛑" in out, out
    assert "delete_task_comment" in out, (
        "отказ обязан направить к настоящему удалению, а не молча стирать")
    assert s._MANIFESTS == {}


async def test_nonempty_text_still_builds_a_plan(monkeypatch):
    """Регресс: настоящий текст по-прежнему доходит до фазы плана."""
    monkeypatch.setattr(s, "ticktick_v2", object())
    monkeypatch.setattr(s, "_guard_task_incl_completed",
                        lambda *a, **k: object())
    monkeypatch.setattr(s, "_guard_or_refuse", lambda *a, **k: ("", ""))
    out = await s.add_task_comment("Купить молоко", "не забыть", "p1", "t1")
    assert "manifest_id" in out, out
    assert len(s._MANIFESTS) == 1
