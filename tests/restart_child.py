"""Один «прогон сервера» для теста перезапуска (#91) — запускается КАК
ОТДЕЛЬНЫЙ ПРОЦЕСС из tests/test_manifest_restart.py.

Почему отдельный процесс, а не `_MANIFESTS.clear()` в том же интерпретаторе:
проверяется ровно то, что план переживает СМЕРТЬ ПРОЦЕССА. Очистка словаря
имитирует лишь одно из следствий (пустая память) и молча сохраняет всё
остальное — импортированный модуль, живой пул соединений, счётчик
`time.monotonic()`. Настоящий `subprocess` не оставляет от прошлого прогона
ничего, кроме того, что действительно легло в Postgres.

Фазы (аргумент командной строки):
  plan     — построить план гейтованным инструментом; он уходит «в Telegram»
             (сеть замокана) и обязан осесть в базе;
  press    — НОВЫЙ процесс: прогнать один тик поллера, как будто владелец
             только что нажал ✅. Успех = исполнитель инструмента реально
             вызван;
  press2   — то же, но ДВА тика одновременно, чтобы проверить, что исполнение
             всё равно ровно одно.

Наружу отдаётся одна строка JSON в stdout.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PHASE = sys.argv[1]
DSN = sys.argv[2]
TAG_NAME = sys.argv[3] if len(sys.argv) > 3 else "тег-из-плана"

# Конфиг слоя читается ПРИ ИМПОРТЕ server, поэтому env выставляется до него.
os.environ["TG_APPROVAL_ENABLED"] = "true"
os.environ["TG_BOT_TOKEN"] = "test-token"
os.environ["TG_OWNER_CHAT_ID"] = "4242"
os.environ["TG_APPROVAL_TOOLS"] = "create_tag"
os.environ.setdefault("MCP_SECRET", "test-secret")
os.environ.setdefault("USER_TIMEZONE", "UTC")
os.environ.setdefault("TICKTICK_CLIENT_ID", "cid")
os.environ.setdefault("TICKTICK_CLIENT_SECRET", "csecret")
os.environ.setdefault("TICKTICK_ACCESS_TOKEN", "atoken")
os.environ.setdefault("MIN_CONSENT_GAP", "0")
# Журнал мутаций — во временную папку теста, чтобы ничего не писать в /data.
os.environ.setdefault("TICKTICK_DATA_DIR", os.environ.get("TEST_DATA_DIR", "/tmp"))

from ticktick_mcp.src import manifest_store, tg_approval  # noqa: E402
import ticktick_mcp.src.server as s  # noqa: E402

manifest_store.init_store(DSN)
tg_approval.init_store(DSN)

_sent = []


def _fake_send(cfg, chat_id, md_text, reply_markup_on_last=None, **kw):
    """Telegram замокан на уровне отправки: ни одного сетевого вызова."""
    _sent.append(md_text)
    return tg_approval.SendResult(ok=True, message_ids=[1001], error="",
                                  total_chunks=1)


tg_approval.send_message_chunked = _fake_send
tg_approval.delete_message = lambda *a, **k: True
tg_approval.clear_inline_keyboard = lambda *a, **k: True
tg_approval._tg_call = lambda *a, **k: {"ok": True, "result": {"message_id": 1}}

# TickTick не трогаем вовсе: инструмент считается готовым, а его мутация
# заменена шпионом, который лишь отмечается в файле (файл, а не переменная —
# результат нужен родительскому тесту, переживая смерть этого процесса).
s._ensure_ready = lambda: ""
s._ensure_official = lambda: ""
CALLS_FILE = os.environ["TEST_CALLS_FILE"]


async def _spy_create_tag_impl(name: str, color: str = None) -> str:
    with open(CALLS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({"tool": "create_tag", "name": name}) + "\n")
    return f"✅ Тег «{name}» создан."


s._create_tag_impl = _spy_create_tag_impl
# Независимая перепроверка и публикация отчёта ходят в TickTick и в Telegram —
# в тесте перезапуска они не проверяются, поэтому обе заглушены.
s._verified_auto_execute_report = lambda mid, tool, report: (report, "ok")
s._publish_auto_execute_outcome = lambda *a, **k: None


async def phase_plan():
    out = await s.create_tag(name=TAG_NAME)
    mids = [m for m in s._MANIFESTS]
    return {"answer": out, "manifest_ids": mids,
            "in_db": [m for m in mids if manifest_store.load(m)]}


async def phase_press():
    # Память ПУСТА — это новый процесс, он про план ничего не знает.
    assert not s._MANIFESTS, "новый процесс не должен помнить планы"
    await s._tg_auto_execute_tick()
    return {"manifests_after": list(s._MANIFESTS),
            "tombstones": {k: v.get("reason")
                           for k, v in s._MANIFEST_TOMBSTONES.items()}}


async def phase_refuse():
    """Человек говорит «нет» в ЧАТЕ — уже В НОВОМ процессе, который про план
    ничего не знает. Идём НАСТОЯЩИМ путём (вызов того же инструмента со
    вторым аргументом), а не дёргаем хранилище напрямую: проверяется в том
    числе проводка «гейт → база», без которой отказ не переживал бы рестарт."""
    assert not s._MANIFESTS, "новый процесс не должен помнить планы"
    mid = sys.argv[4]
    answer = await s.create_tag(name=TAG_NAME, manifest_id=mid,
                                user_reply="нет, не надо этого делать")
    return {"answer": answer, "known_after": mid in s._MANIFESTS}


async def phase_retry_yes():
    """Второй вызов инструмента с чат-«да» ПОСЛЕ рестарта. Ответ должен быть
    про кнопку («ждём нажатия»), а не «манифест не найден/истёк»: план жив, и
    сервер обязан его узнать."""
    assert not s._MANIFESTS
    mid = sys.argv[4]
    answer = await s.create_tag(name=TAG_NAME, manifest_id=mid,
                                user_reply="да, давай")
    return {"answer": answer, "known_after": mid in s._MANIFESTS}


async def phase_press2():
    """Два тика ОДНОВРЕМЕННО в одном процессе — обе корутины дойдут до захвата
    плана и будут конкурировать в базе (сам захват выполняется в разных
    потоках через `_run_blocking`)."""
    assert not s._MANIFESTS
    await asyncio.gather(s._tg_auto_execute_tick(), s._tg_auto_execute_tick())
    return {"manifests_after": list(s._MANIFESTS)}


PHASES = {"plan": phase_plan, "press": phase_press, "press2": phase_press2,
          "refuse": phase_refuse, "retry_yes": phase_retry_yes}
result = asyncio.run(PHASES[PHASE]())
print(json.dumps(result, ensure_ascii=False))
