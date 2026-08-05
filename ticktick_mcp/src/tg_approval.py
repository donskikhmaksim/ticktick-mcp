"""
tg_approval.py — опциональный внеполосный (out-of-band) Telegram-фактор
поверх текстового `user_reply` (docs/DESIGN_approval_gate.md §4.5: «модель
может сфабриковать user_reply — это не закрыто в рамках {без кнопок}»).

Портировано с TypeScript-модуля gmail-mcp/src/tg_approval.ts — тот же бот
(@maksim_mcp_approval_bot), та же таблица `tg_approvals` в ОБЩЕМ Postgres,
который уже используют gmail/sheets/calendar/docs/drive-mcp. gmail-mcp
остаётся ЕДИНСТВЕННЫМ владельцем вебхука (`TG_WEBHOOK_OWNER=true` только
там) — ticktick-mcp никогда не регистрирует `setWebhook` и не поднимает
`/tg/webhook`. Решение по кнопке доходит сюда через ту же таблицу: gmail-mcp's
`consumeTgDecisionAnyServer` уже server-agnostic (manifest_id — глобальный
PRIMARY KEY), так что строка с `server='ticktick'` обрабатывается ИМ без
единой правки на его стороне.

ВАЖНО про историю (docs/DESIGN_approval_gate.md §7): v1-дизайн с Telegram-
кнопками для ticktick-mcp был explicitly ОТВЕРГНУТ Максимом 2026-07-27
(«моё согласие в чате должно работать; без кнопок»). Этот модуль строится
2026-08-05 по прямому, более позднему указанию Максима того же дня («Все 5 +
тик тик», «тикток самое важное») — трактуется как обновление той позиции, а
не как игнорирование прежнего решения. Кнопка здесь — ОПЦИОНАЛЬНЫЙ ВТОРОЙ
фактор ПОВЕРХ существующего `_require_consent()` (chat «да» остаётся
обязательным первым фактором и работает без Telegram, если фича выключена):
`TG_APPROVAL_ENABLED` по умолчанию false — форк/деплой без бота ведёт себя
побайтово как раньше.

OFF BY DEFAULT: без `TG_APPROVAL_ENABLED=true` ни одна функция здесь не
делает сетевых обращений и не трогает Postgres.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Optional

import requests

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
PREVIEW_CAP = 3500  # см. gmail-mcp: Telegram sendMessage.text лимит 4096
TG_TIMEOUT_S = 8


@dataclass
class TgApprovalConfig:
    enabled: bool
    bot_token: str
    owner_chat_id: str
    server: str  # константа "ticktick", как CONSENT_SERVER у TS-серверов
    tools_allowlist: Optional[set]  # None = все гейтованные тулы
    ttl_s: int


def load_tg_approval_config() -> TgApprovalConfig:
    enabled = os.environ.get("TG_APPROVAL_ENABLED", "").strip().lower() == "true"
    bot_token = os.environ.get("TG_BOT_TOKEN", "").strip()
    owner_chat_id = os.environ.get("TG_OWNER_CHAT_ID", "").strip()
    tools_raw = os.environ.get("TG_APPROVAL_TOOLS", "").strip()
    tools_allowlist = (
        {t.strip() for t in tools_raw.split(",") if t.strip()} if tools_raw else None
    )
    ttl_s = int(os.environ.get("TG_APPROVAL_TTL_S", "3600"))
    if enabled and (not bot_token or not owner_chat_id):
        missing = ", ".join(
            n for n, v in (("TG_BOT_TOKEN", bot_token), ("TG_OWNER_CHAT_ID", owner_chat_id)) if not v
        )
        raise RuntimeError(
            f"TG_APPROVAL_ENABLED=true, но не задано: {missing}. Либо задай оба, либо "
            "убери TG_APPROVAL_ENABLED, чтобы работать без этого слоя."
        )
    return TgApprovalConfig(
        enabled=enabled, bot_token=bot_token, owner_chat_id=owner_chat_id,
        server="ticktick", tools_allowlist=tools_allowlist, ttl_s=ttl_s,
    )


def enabled_for(cfg: TgApprovalConfig, tool: str) -> bool:
    if not cfg.enabled:
        return False
    if cfg.tools_allowlist is None:
        return True
    return tool in cfg.tools_allowlist


# ───────────────────────── markdown → Telegram HTML ─────────────────────────
# Порт mdToTelegramHtml из gmail-mcp/src/tg_approval.ts — та же логика и та же
# защита от intraword-подчёркиваний (имена файлов/тегов с "_" не должны
# раскурсивливаться), см. комментарий в TS-оригинале.

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_CODE_RE = re.compile(r"`([^`]+)`")
_ITALIC_RE = re.compile(r"(?<!\w)_([^_\n]+)_(?!\w)")


def md_to_telegram_html(text: str) -> str:
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    lines = []
    for line in escaped.split("\n"):
        m = _HEADING_RE.match(line)
        lines.append(f"<b>{m.group(2)}</b>" if m else line)
    joined = "\n".join(lines)
    joined = _BOLD_RE.sub(r"<b>\1</b>", joined)
    joined = _CODE_RE.sub(r"<code>\1</code>", joined)
    joined = _ITALIC_RE.sub(r"<i>\1</i>", joined)
    return joined


def _clip(s: str, max_len: int = PREVIEW_CAP) -> str:
    one = s.strip()
    return one if len(one) <= max_len else one[: max_len] + "…"


# ───────────────────────── Telegram HTTP ─────────────────────────

def _tg_call(cfg: TgApprovalConfig, method: str, body: dict) -> dict:
    url = f"{TELEGRAM_API}/bot{cfg.bot_token}/{method}"
    try:
        res = requests.post(url, json=body, timeout=TG_TIMEOUT_S)
        data = res.json()
    except Exception as e:
        logger.warning(f"TG approval: {method} failed: {e}")
        return {"ok": False, "description": str(e)}
    return {"ok": bool(data.get("ok")) and res.ok, "result": data.get("result"),
            "description": data.get("description")}


# ───────────────────────── Postgres (общий с 5 TS-серверами) ─────────────────

_pg_pool = None


def init_store(database_url: str) -> None:
    """Ленивая инициализация — вызывается один раз при старте, если
    TG_APPROVAL_ENABLED=true и задан CONSENT_DATABASE_URL. psycopg2 — тот же
    выбор, что и остальной синхронный стиль этого сервера (requests вместо
    httpx, никакого asyncio Postgres-драйвера не требовалось до сих пор)."""
    global _pg_pool
    import psycopg2.pool

    _pg_pool = psycopg2.pool.SimpleConnectionPool(1, 5, dsn=database_url, sslmode="require")
    _ensure_schema()


def store_ready() -> bool:
    return _pg_pool is not None


def _ensure_schema() -> None:
    conn = _pg_pool.getconn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS tg_approvals (
                  manifest_id TEXT PRIMARY KEY,
                  server      TEXT NOT NULL,
                  chat_id     TEXT NOT NULL,
                  message_id  BIGINT,
                  status      TEXT NOT NULL DEFAULT 'PENDING',
                  created_at  BIGINT NOT NULL,
                  expires_at  BIGINT NOT NULL,
                  decided_at  BIGINT
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS tg_approvals_cleanup_idx "
                "ON tg_approvals (server, status, expires_at)"
            )
    finally:
        _pg_pool.putconn(conn)


def _now_ms() -> int:
    return int(time.time() * 1000)


def create_tg_approval(manifest_id: str, chat_id: str, message_id: Optional[int],
                        expires_at_ms: int) -> None:
    conn = _pg_pool.getconn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tg_approvals (manifest_id, server, chat_id, message_id, "
                "status, created_at, expires_at) VALUES (%s, %s, %s, %s, 'PENDING', %s, %s)",
                (manifest_id, "ticktick", chat_id, message_id, _now_ms(), expires_at_ms),
            )
    finally:
        _pg_pool.putconn(conn)


def get_tg_approval(manifest_id: str) -> Optional[dict]:
    """Читает строку, ОБЯЗАТЕЛЬНО фильтруя server='ticktick' — сервер не
    должен читать чужие approval-строки (та же дисциплина, что у TS-серверов'
    getTgApproval; см. tg_approval.ts's комментарий про cross-server read)."""
    conn = _pg_pool.getconn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT status, expires_at FROM tg_approvals "
                "WHERE manifest_id = %s AND server = 'ticktick'",
                (manifest_id,),
            )
            row = cur.fetchone()
    finally:
        _pg_pool.putconn(conn)
    if not row:
        return None
    return {"status": row[0], "expires_at": row[1]}


# ───────────────────────── Публичное API для server.py ─────────────────────

def notify_plan(cfg: TgApprovalConfig, manifest_id: str, preview_body: str,
                 tool: str) -> tuple[bool, str]:
    """Отправляет план в Telegram с кнопками [✅ Подтвердить][🛑 Отклонить],
    пишет строку в tg_approvals. Fail-closed по духу gmail-mcp: если это
    вернуло (False, ...), вызывающий код ОБЯЗАН инвалидировать манифест — тот
    же контракт, что requireConsent's notifyPlan в TS."""
    if not store_ready():
        return False, "Postgres для TG-approval не настроен (CONSENT_DATABASE_URL)"
    text = f"{md_to_telegram_html(_clip(preview_body))}\n\n{tool} · ticktick"
    sent = _tg_call(cfg, "sendMessage", {
        "chat_id": cfg.owner_chat_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "✅ Подтвердить", "callback_data": f"a:{manifest_id}"},
                {"text": "🛑 Отклонить", "callback_data": f"r:{manifest_id}"},
            ]]
        },
    })
    if not sent.get("ok"):
        return False, sent.get("description") or "Telegram sendMessage failed"
    message_id = (sent.get("result") or {}).get("message_id")
    expires_at = _now_ms() + cfg.ttl_s * 1000
    create_tg_approval(manifest_id, cfg.owner_chat_id, message_id, expires_at)
    return True, ""


def check_approval(manifest_id: str) -> str:
    """"approved" | "pending" | "rejected" | "none" — "none" покрывает и
    «никогда не спрашивали», и «TTL истёк» (та же семантика, что checkApproval
    в TS — фаза исполнения обрабатывает оба случая одинаково)."""
    if not store_ready():
        return "none"
    row = get_tg_approval(manifest_id)
    if not row:
        return "none"
    if row["status"] == "APPROVED":
        return "approved"
    if row["status"] == "REJECTED":
        return "rejected"
    if _now_ms() > row["expires_at"]:
        return "none"
    return "pending"
