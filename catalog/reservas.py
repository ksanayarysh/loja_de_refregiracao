"""Reservar e retirar na loja: o cliente deixa nome + WhatsApp, a loja separa a peça e ele paga na retirada.
Para quem está no Rio e quer a peça hoje (não faz sentido pagar online + motoboy por um capacitor).
Admin: /reservas (marcar como retirada ou cancelar, WhatsApp com mensagem pronta)."""
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from dependencies import engine
from router import _send_tg, _client_ip, _is_bot, _is_owner, _blog_log, log_product_events, BRT

router = APIRouter()
_ok = False
_por_ip: dict = {}          # ip -> [timestamps] (no máximo 5 reservas por hora por pessoa)

ABRE, FECHA = (8, 30), (18, 0)   # seg. a sáb.
# número que o cliente e a loja veem: id + 1000 (id 1 -> "R-1001"), como os pedidos do site
RESERVA_OFFSET = 1000


def num_reserva(rid: int) -> str:
    return f"R-{int(rid) + RESERVA_OFFSET}"


async def _ensure():
    global _ok
    if _ok:
        return
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS reservas (
                id           SERIAL PRIMARY KEY,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                product_id   INTEGER,
                product_name VARCHAR(200),
                qty          NUMERIC(10,3) NOT NULL DEFAULT 1,
                price        NUMERIC(10,2),
                name         VARCHAR(120),
                phone        VARCHAR(20),
                ip           VARCHAR(64),
                src          VARCHAR(80),
                status       VARCHAR(12) NOT NULL DEFAULT 'nova',     -- nova / retirada / cancelada
                status_at    TIMESTAMPTZ
            )"""))
    _ok = True


def quando_retirar(agora: datetime | None = None) -> str:
    """Texto curto de quando dá para retirar: hoje até 18h / amanhã a partir de 8h30 / segunda..."""
    agora = agora or datetime.now(BRT)
    dia = agora.weekday()                       # 0 = segunda ... 6 = domingo
    limite = agora.replace(hour=FECHA[0] - 1, minute=30, second=0, microsecond=0)   # até 17h30 dá tempo de separar
    if dia <= 5 and agora < limite:
        return "hoje até as 18h"
    prox = agora + timedelta(days=1)
    while prox.weekday() == 6:                  # domingo fechado
        prox += timedelta(days=1)
    nome = "amanhã" if (prox.date() - agora.date()).days == 1 else "na segunda-feira"
    return f"{nome} a partir das 8h30"


@router.post("/api/reserva")
async def criar_reserva(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Dados inválidos."}, status_code=400)
    from checkout import _norm_cel, _load_items, _stock_problems, _brl   # import tardio: checkout importa router
    name = str(body.get("name") or "").strip()[:120]
    phone = _norm_cel(body.get("phone", ""))
    if len(name) < 2:
        return JSONResponse({"error": "Informe seu nome."}, status_code=400)
    if not phone:
        return JSONResponse({"error": "Informe um WhatsApp válido com DDD, ex.: (21) 99999-9999."}, status_code=400)
    ip = _client_ip(request)
    if _is_bot(request.headers.get("user-agent", "")):
        return JSONResponse({"ok": True, "quando": quando_retirar()})
    agora = time.time()
    hist = [t for t in _por_ip.get(ip, []) if agora - t < 3600]
    if len(hist) >= 5:
        return JSONResponse({"error": "Muitas reservas seguidas. Fale com a gente no WhatsApp."}, status_code=429)
    try:
        qty = max(1, min(50, int(body.get("qty") or 1)))
    except (TypeError, ValueError):
        qty = 1
    items, _sub = await _load_items([{"id": body.get("product_id"), "qty": qty}])
    if not items:
        return JSONResponse({"error": "Produto não encontrado."}, status_code=404)
    kg, short, _ = await _stock_problems(items)
    if short:
        return JSONResponse({"error": "Não temos essa quantidade em estoque agora. Fale com a gente no WhatsApp."},
                            status_code=409)
    it = items[0]
    src = str(body.get("src") or "")[:80] or None
    await _ensure()
    async with engine.begin() as conn:
        rid = (await conn.execute(text("""
            INSERT INTO reservas (product_id, product_name, qty, price, name, phone, ip, src)
            VALUES (:pid, :pn, :q, :p, :n, :ph, :ip, :src) RETURNING id"""),
            {"pid": it["id"], "pn": it["name"][:200], "q": qty, "p": it["price"], "n": name, "ph": phone,
             "ip": ip, "src": src})).scalar()
    hist.append(agora)
    _por_ip[ip] = hist
    quando = quando_retirar()
    if not _is_owner(request):
        try:
            await log_product_events([it["id"]], "reserva", ip, None, src)
        except Exception:
            pass
    from urllib.parse import quote
    primeiro = name.split(" ")[0].title()
    msg_cliente = (f"Olá, {primeiro}! Aqui é da MTF Refrigeração 😊 Recebemos sua reserva {num_reserva(rid)}: {it['name']} × {qty}. "
                   f"Já está separado para você — pode retirar {quando} na Av. Brasil, 6818 – Bonsucesso. "
                   f"Pagamento na loja (Pix, cartão ou dinheiro).")
    tel = f"({phone[:2]}) {phone[2:7]}-{phone[7:]}"
    try:
        await _send_tg(
            f"🏪 <b>RESERVA para retirar na loja</b> {num_reserva(rid)}\n"
            f"{it['name']} × {qty} — {_brl(float(it['price']) * qty)}\n"
            f"Cliente: {name} · {tel}\nRetirada: {quando}"
            + (f"\nVeio de: {src}" if src else "")
            + ("\n(teste: navegador da loja)" if _is_owner(request) else "")
            + f"\n\n👉 Separe a peça e confirme: <a href=\"https://wa.me/55{phone}?text={quote(msg_cliente)}\">abrir WhatsApp com a mensagem</a>")
    except Exception:
        _blog_log.exception("reserva: falha ao avisar")
    return JSONResponse({"ok": True, "id": rid, "numero": num_reserva(rid), "quando": quando})
