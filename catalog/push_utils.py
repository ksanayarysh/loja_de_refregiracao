"""
Notificações push (Web Push) — o MESMO arquivo vai na admin e no catálogo.
  admin:    admin/routers/push_utils.py   (salva aparelhos + botão de teste)
  catálogo: catalog/push_utils.py         (avisa quando um pedido é pago)

Variáveis (iguais nos dois serviços): VAPID_PRIVATE_KEY, VAPID_PUBLIC_KEY
Opcional: VAPID_SUBJECT (padrão: https://www.mtfrefrigeracao.com.br)
Dependência: pywebpush  (adicionar no requirements.txt dos dois)
"""
import asyncio
import json
import logging
import os

from sqlalchemy import text

log = logging.getLogger("uvicorn.error")
_table_ready = False


def public_key() -> str:
    return os.environ.get("VAPID_PUBLIC_KEY", "").strip()


def enabled() -> bool:
    return bool(public_key() and os.environ.get("VAPID_PRIVATE_KEY", "").strip())


async def ensure_table(engine):
    global _table_ready
    if _table_ready:
        return
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id         SERIAL PRIMARY KEY,
                endpoint   TEXT UNIQUE NOT NULL,
                p256dh     TEXT NOT NULL,
                auth       TEXT NOT NULL,
                label      VARCHAR(120),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
    _table_ready = True


async def save_subscription(engine, sub: dict, label: str = "") -> bool:
    endpoint = (sub or {}).get("endpoint")
    keys = (sub or {}).get("keys") or {}
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        return False
    await ensure_table(engine)
    async with engine.begin() as conn:
        await conn.execute(text("""
            INSERT INTO push_subscriptions (endpoint, p256dh, auth, label)
            VALUES (:e, :p, :a, :l)
            ON CONFLICT (endpoint) DO UPDATE SET p256dh = EXCLUDED.p256dh, auth = EXCLUDED.auth, label = EXCLUDED.label
        """), {"e": endpoint, "p": keys["p256dh"], "a": keys["auth"], "l": label[:120] or None})
    return True


async def remove_subscription(engine, endpoint: str):
    await ensure_table(engine)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM push_subscriptions WHERE endpoint = :e"), {"e": endpoint})


def _send_one(row: dict, payload: str):
    """Síncrono (pywebpush usa requests) — roda em thread."""
    from pywebpush import webpush, WebPushException
    try:
        webpush(
            subscription_info={"endpoint": row["endpoint"], "keys": {"p256dh": row["p256dh"], "auth": row["auth"]}},
            data=payload,
            vapid_private_key=os.environ["VAPID_PRIVATE_KEY"].strip(),
            vapid_claims={"sub": os.environ.get("VAPID_SUBJECT", "https://www.mtfrefrigeracao.com.br").strip()},
            ttl=60 * 60 * 12,
        )
        return "ok"
    except WebPushException as e:
        status = getattr(e.response, "status_code", None)
        if status in (404, 410):          # aparelho desinstalou / revogou: apagar
            return "gone"
        log.warning("push: falha %s: %s", status, str(e)[:200])
        return "error"


async def send_all(engine, title: str, body: str, url: str = "/site-orders", tag: str = "") -> dict:
    """Envia para todos os aparelhos cadastrados. Nunca levanta exceção."""
    result = {"ok": 0, "gone": 0, "error": 0}
    if not enabled():
        return result
    try:
        await ensure_table(engine)
        async with engine.connect() as conn:
            rows = [dict(r) for r in (await conn.execute(text(
                "SELECT endpoint, p256dh, auth FROM push_subscriptions"
            ))).mappings().all()]
        payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag}, ensure_ascii=False)
        outcomes = await asyncio.gather(*(asyncio.to_thread(_send_one, r, payload) for r in rows))
        for r, out in zip(rows, outcomes):
            result[out] += 1
            if out == "gone":
                await remove_subscription(engine, r["endpoint"])
    except Exception:
        log.exception("push: falha ao enviar")
    return result
