"""
Admin: pedidos feitos no site (tabela site_orders, criada pelo catálogo).

Incluir no main.py da admin:
  from routers.site_orders import router as site_orders_router   # ajuste o caminho como nos outros routers
  app.include_router(site_orders_router)

Para "Chamar Lalamove", a admin precisa do lalamove.py (mesmo arquivo do catálogo)
e das variáveis LALAMOVE_API_KEY, LALAMOVE_API_SECRET, LALAMOVE_BASE, MTF_LAT, MTF_LNG, MTF_PHONE.
"""
import asyncio
import json
import logging
import os
import re
from urllib.parse import urlencode
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
import httpx
from sqlalchemy import text

from dependencies import engine, templates, basic_auth

router = APIRouter()
log = logging.getLogger("uvicorn.error")
BRT = ZoneInfo("America/Sao_Paulo")

FULFILLMENT = {
    "novo":      "Novo",
    "separado":  "Separado",
    "enviado":   "Enviado",
    "entregue":  "Entregue / retirado",
    "cancelado": "Cancelado",
}
SERVICE_LABEL = {"LALAGO": "Motoboy", "LALAPRO": "Moto com baú", "HATCHBACK": "Carro"}

_cols_ready = False


def _go(order_id: int, ok: str = "", err: str = "") -> RedirectResponse:
    q = urlencode({k: v for k, v in (("ok", ok), ("err", err)) if v})
    return RedirectResponse(f"/site-orders/{order_id}" + (f"?{q}" if q else ""), status_code=303)


def _money(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


async def _table_exists(conn) -> bool:
    return bool((await conn.execute(text("SELECT to_regclass('public.site_orders')"))).scalar())


async def _ensure_admin_cols():
    """Colunas só da admin. A tabela é criada pelo catálogo no primeiro pedido."""
    global _cols_ready
    if _cols_ready:
        return True
    async with engine.begin() as conn:
        if not await _table_exists(conn):
            return False
        await conn.execute(text("ALTER TABLE site_orders ADD COLUMN IF NOT EXISTS fulfillment VARCHAR(20) NOT NULL DEFAULT 'novo'"))
        await conn.execute(text("ALTER TABLE site_orders ADD COLUMN IF NOT EXISTS sale_registered BOOLEAN NOT NULL DEFAULT FALSE"))
        await conn.execute(text("ALTER TABLE site_orders ADD COLUMN IF NOT EXISTS admin_note TEXT"))
        await conn.execute(text("ALTER TABLE site_orders ADD COLUMN IF NOT EXISTS lalamove_order_id VARCHAR(40)"))
        await conn.execute(text("ALTER TABLE site_orders ADD COLUMN IF NOT EXISTS lalamove_link TEXT"))
        await conn.execute(text("ALTER TABLE site_orders ADD COLUMN IF NOT EXISTS lalamove_status VARCHAR(30)"))
        await conn.execute(text("ALTER TABLE site_orders ADD COLUMN IF NOT EXISTS lalamove_checked_at TIMESTAMPTZ"))
    _cols_ready = True
    _ensure_poller()
    return True


def _prep(o) -> dict:
    o = dict(o)
    o["items"] = o["items"] if isinstance(o["items"], list) else json.loads(o["items"] or "[]")
    for k in ("subtotal", "frete", "total"):
        o[k] = float(o[k] or 0)
    o["service_label"] = SERVICE_LABEL.get(o.get("delivery_service") or "", o.get("delivery_service") or "")
    o["fulfillment_label"] = FULFILLMENT.get(o.get("fulfillment") or "novo", o.get("fulfillment"))
    o["created_brt"] = o["created_at"].astimezone(BRT).strftime("%d/%m %H:%M") if o.get("created_at") else ""
    o["paid_brt"] = o["paid_at"].astimezone(BRT).strftime("%d/%m %H:%M") if o.get("paid_at") else ""
    phone = re.sub(r"\D", "", o.get("customer_phone") or "")
    o["wa_phone"] = phone if phone.startswith("55") else "55" + phone
    # só exclui pedido sem venda registrada e sem entregador chamado (senão: cancelar primeiro)
    o["can_delete"] = not o.get("sale_registered") and not o.get("lalamove_order_id")
    o["can_cancel"] = o.get("fulfillment") != "cancelado" and o.get("status") in ("pending", "paid")
    ls = o.get("lalamove_status") or ""
    o["lalamove_status_label"] = LALA_STATUS.get(ls, ls)
    o["lalamove_failed"] = ls in LALA_FAILED
    o["lalamove_checked_brt"] = (o["lalamove_checked_at"].astimezone(BRT).strftime("%H:%M")
                                 if o.get("lalamove_checked_at") else "")
    return o


# ── CONTADOR (menu pisca só quando há pedido pago NOVO, ainda não tratado) ──────
@router.get("/site-orders/count")
async def site_orders_count(_=Depends(basic_auth)):
    n = 0
    if await _ensure_admin_cols():
        async with engine.connect() as conn:
            n = (await conn.execute(text(
                "SELECT COUNT(*) FROM site_orders WHERE status = 'paid' AND fulfillment = 'novo'"
            ))).scalar() or 0
    return JSONResponse({"open": int(n)}, headers={"Cache-Control": "no-store"})


# ── LISTA ──────────────────────────────────────────────────────────────────────
@router.get("/site-orders", response_class=HTMLResponse)
async def site_orders_list(request: Request, filtro: str = "pagos", _=Depends(basic_auth)):
    orders = []
    if await _ensure_admin_cols():
        where = {
            "pagos":     "status = 'paid' AND fulfillment NOT IN ('entregue', 'cancelado')",
            "pendentes": "status = 'pending'",
            "todos":     "TRUE",
        }.get(filtro, "status = 'paid' AND fulfillment NOT IN ('entregue', 'cancelado')")
        async with engine.connect() as conn:
            rows = (await conn.execute(text(
                f"SELECT * FROM site_orders WHERE {where} ORDER BY id DESC LIMIT 200"
            ))).mappings().all()
        orders = [_prep(r) for r in rows]
    return templates.TemplateResponse("site_orders.html", {
        "request": request, "active_page": "site_orders", "orders": orders, "filtro": filtro,
        "order": None, "fulfillment": FULFILLMENT,
    })


# ── DETALHE ────────────────────────────────────────────────────────────────────
@router.get("/site-orders/{order_id}", response_class=HTMLResponse)
async def site_order_detail(request: Request, order_id: int, _=Depends(basic_auth)):
    if not await _ensure_admin_cols():
        return HTMLResponse("Ainda não há pedidos do site.", status_code=404)
    async with engine.connect() as conn:
        row = (await conn.execute(text("SELECT * FROM site_orders WHERE id = :id"), {"id": order_id})).mappings().first()
    if not row:
        return HTMLResponse("Pedido não encontrado", status_code=404)
    return templates.TemplateResponse("site_orders.html", {
        "request": request, "active_page": "site_orders", "orders": [], "filtro": "",
        "order": _prep(row), "fulfillment": FULFILLMENT,
        "ok": request.query_params.get("ok"), "err": request.query_params.get("err"),
    })


# ── AÇÕES ──────────────────────────────────────────────────────────────────────
@router.post("/site-orders/{order_id}/registrar-venda")
async def site_order_register_sale(order_id: int, _=Depends(basic_auth)):
    """Cria as vendas (uma por produto) e dá baixa no estoque, como no checkout da admin."""
    async with engine.begin() as conn:
        o = (await conn.execute(text(
            "SELECT * FROM site_orders WHERE id = :id FOR UPDATE"
        ), {"id": order_id})).mappings().first()
        if not o:
            return _go(order_id, err="Pedido não encontrado")
        if o["status"] != "paid":
            return _go(order_id, err="Pedido ainda não pago")
        if o["sale_registered"]:
            return _go(order_id, err="Venda já registrada")

        items = o["items"] if isinstance(o["items"], list) else json.loads(o["items"])
        sold_at = (o["paid_at"] or datetime.now(BRT)).astimezone(BRT).date()
        note = f"Pedido site #{order_id}"
        # o kit (id -1) vira uma venda por componente, já com o desconto do kit
        lines = []
        for it in items:
            if it.get("components"):
                kq = Decimal(str(it["qty"]))
                pct = it.get("discount_pct") or 0
                if it.get("kit_name"):          # kit montado na admin (/kits)
                    tag = f"{note} (Kit: {it['kit_name']})"
                else:
                    tag = f"{note} (kit -{pct:g}%)" if pct else f"{note} (kit)"
                for c in it["components"]:
                    q = _money(Decimal(str(c["qty"])) * kq)
                    t = _money(Decimal(str(c["line_total"])) * kq)
                    ctag = f"{tag} · 🎁 brinde" if c.get("is_gift") else tag
                    lines.append((int(c["id"]), q, _money(t / q) if q else _money(0), t, ctag))
            else:
                q = _money(it["qty"])
                p = _money(it["price"])
                pct = it.get("discount_pct") or 0
                tag = (f"{note} (kit -{pct:g}%)" if pct else f"{note} (kit)") if it.get("kit") else note
                lines.append((int(it["id"]), q, p, _money(q * p), tag))

        for pid, qty_d, price_d, total_d, line_note in lines:
            await conn.exec_driver_sql(
                "INSERT INTO sales (sold_at, product_id, qty, unit_price, total, note, payment_type, client_id) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
                (sold_at, pid, qty_d, price_d, total_d, line_note,
                 "cartao" if o.get("pay_method") == "card" else "pix", None),
            )
            sale_id = (await conn.execute(text("SELECT lastval()"))).scalar()
            await conn.execute(text("""
                INSERT INTO stock_movements (product_id, qty, movement_type, note, sale_id, moved_at)
                VALUES (:pid, :qty, 'venda', :note, :sid, :dt)
            """), {"pid": pid, "qty": -qty_d, "note": line_note, "sid": sale_id, "dt": sold_at})
        await conn.execute(text("UPDATE site_orders SET sale_registered = TRUE WHERE id = :id"), {"id": order_id})
    return _go(order_id, ok="Venda registrada e estoque atualizado")


@router.post("/site-orders/{order_id}/status")
async def site_order_status(order_id: int, fulfillment: str = Form(...), admin_note: str = Form(""),
                            _=Depends(basic_auth)):
    if fulfillment not in FULFILLMENT:
        return _go(order_id, err="Status inválido")
    async with engine.begin() as conn:
        await conn.execute(text(
            "UPDATE site_orders SET fulfillment = :f, admin_note = :n WHERE id = :id"
        ), {"f": fulfillment, "n": admin_note.strip() or None, "id": order_id})
    return _go(order_id, ok="Status atualizado")


@router.post("/site-orders/{order_id}/lalamove")
async def site_order_lalamove(order_id: int, _=Depends(basic_auth)):
    """Nova cotação (a do site já expirou) + pedido na Lalamove."""
    try:
        try:
            from routers.lalamove import get_quote, place_order, LalamoveError
        except ImportError:
            from lalamove import get_quote, place_order, LalamoveError
    except ImportError:
        return _go(order_id, err="lalamove.py não está na admin")

    async with engine.connect() as conn:
        o = (await conn.execute(text("SELECT * FROM site_orders WHERE id = :id"), {"id": order_id})).mappings().first()
    if not o or o["status"] != "paid" or o["delivery_type"] != "lalamove":
        return _go(order_id, err="Pedido não é entrega paga")
    if o["lalamove_order_id"]:
        return _go(order_id, err="Lalamove já chamado")
    if o["lat"] is None or o["lng"] is None:
        return _go(order_id, err="Pedido sem coordenadas")

    full_address = f'{o["address"]}, nº {o["numero"]}' + (f' — {o["complemento"]}' if o["complemento"] else "")
    phone = re.sub(r"\D", "", o["customer_phone"] or "")
    phone = "+" + (phone if phone.startswith("55") else "55" + phone)
    try:
        quote = await get_quote(o["lat"], o["lng"], full_address, o["delivery_service"] or "LALAGO")
        res = await place_order(quote, o["customer_name"], phone,
                                remarks=f"Pedido MTF #{order_id}. {full_address}", sale_id=order_id)
    except LalamoveError as e:
        return _go(order_id, err=f"Lalamove: {str(e.payload)[:150]}")

    async with engine.begin() as conn:
        await conn.execute(text("""
            UPDATE site_orders SET lalamove_order_id = :lid, lalamove_link = :link, fulfillment = 'enviado',
                   lalamove_status = :st, lalamove_checked_at = NOW()
            WHERE id = :id
        """), {"lid": res["order_id"], "link": res.get("share_link"),
               "st": (res.get("status") or "ASSIGNING_DRIVER")[:30], "id": order_id})
    price_note = f'Cobrado do cliente R$ {float(o["frete"]):.2f} · Lalamove agora R$ {float(quote["price"]):.2f}'
    return _go(order_id, ok=f"Lalamove chamado. {price_note}")


LALA_STATUS = {
    "ASSIGNING_DRIVER": "Procurando entregador",
    "ON_GOING": "Entregador a caminho da loja",
    "PICKED_UP": "Pedido coletado, em rota",
    "COMPLETED": "Entregue",
    "CANCELED": "Cancelado",
    "REJECTED": "Recusado pelos entregadores",
    "EXPIRED": "Expirado (nenhum entregador aceitou)",
}


# entregador não veio: libera o pedido para chamar a Lalamove de novo
LALA_FAILED = {"CANCELED", "REJECTED", "EXPIRED"}
LALA_PUSH_TEXT = {
    "ON_GOING":  "🛵 Entregador aceitou e está indo à loja",
    "PICKED_UP": "📦 Entregador coletou, em rota",
    "COMPLETED": "✅ Entregue ao cliente",
    "CANCELED":  "⚠️ Entrega cancelada — chame de novo",
    "REJECTED":  "⚠️ Nenhum entregador aceitou — chame de novo",
    "EXPIRED":   "⚠️ Entrega expirou sem entregador — chame de novo",
}
LALA_POLL_SECONDS = max(30, int(os.environ.get("LALA_POLL_SECONDS", "90")))


def _lala_get_order():
    try:
        from routers.lalamove import get_order, LalamoveError
    except ImportError:
        from lalamove import get_order, LalamoveError
    return get_order, LalamoveError


async def _push(title: str, body: str, url: str, tag: str):
    try:
        try:
            from routers import push_utils as push
        except ImportError:
            import push_utils as push
        await push.send_all(engine, title, body, url=url, tag=tag)
    except Exception:
        log.exception("push lalamove falhou")


async def _sync_lalamove(order_id: int, lid: str, prev: str | None, notify: bool = True) -> str:
    """Busca o status na Lalamove, grava e avisa por push se mudou. Levanta LalamoveError."""
    get_order, _ = _lala_get_order()
    data = await get_order(lid)
    status = (data.get("status") or "?")[:30]
    link = data.get("shareLink")

    async with engine.begin() as conn:
        # "AND lalamove_order_id = :lid": se alguém cancelou/rechamou nesse meio-tempo, não mexe
        await conn.execute(text("""
            UPDATE site_orders SET lalamove_status = :st, lalamove_checked_at = NOW(),
                   lalamove_link = COALESCE(:link, lalamove_link)
            WHERE id = :id AND lalamove_order_id = :lid
        """), {"st": status, "link": link, "id": order_id, "lid": lid})
        if status == "COMPLETED":
            await conn.execute(text(
                "UPDATE site_orders SET fulfillment = 'entregue' WHERE id = :id AND fulfillment <> 'cancelado'"
            ), {"id": order_id})
        elif status in LALA_FAILED:
            await conn.execute(text("""
                UPDATE site_orders SET lalamove_order_id = NULL, lalamove_link = NULL,
                       fulfillment = CASE WHEN fulfillment = 'enviado' THEN 'separado' ELSE fulfillment END
                WHERE id = :id AND lalamove_order_id = :lid
            """), {"id": order_id, "lid": lid})

    # prev vazio = pedido chamado antes desta versão: só grava, sem notificar
    if notify and prev and status != prev and status in LALA_PUSH_TEXT:
        await _push(f"Pedido #{order_id}", LALA_PUSH_TEXT[status],
                    url=f"/site-orders/{order_id}", tag=f"lala-{order_id}")
    return status


async def _poll_lalamove_once():
    async with engine.connect() as conn:
        rows = (await conn.execute(text("""
            SELECT id, lalamove_order_id, lalamove_status FROM site_orders
            WHERE lalamove_order_id IS NOT NULL AND fulfillment NOT IN ('entregue', 'cancelado')
            ORDER BY id DESC LIMIT 30
        """))).mappings().all()
    if not rows:
        return
    _, LalamoveError = _lala_get_order()
    for r in rows:
        try:
            await _sync_lalamove(r["id"], r["lalamove_order_id"], r["lalamove_status"])
        except LalamoveError as e:
            log.warning("lalamove poll #%s: %s", r["id"], str(e.payload)[:150])
        except Exception:
            log.exception("lalamove poll #%s", r["id"])


async def _poll_lalamove_loop():
    await asyncio.sleep(10)
    while True:
        try:
            if _cols_ready:
                await _poll_lalamove_once()
        except ImportError:
            log.warning("lalamove poll: lalamove.py não está na admin — parando")
            return
        except Exception:
            log.exception("lalamove poll: erro")
        await asyncio.sleep(LALA_POLL_SECONDS)


_poll_task = None


def _ensure_poller():
    """Liga o loop uma vez. Chamado no startup e também na 1ª visita à admin
    (caso o main.py use lifespan e ignore on_event)."""
    global _poll_task
    if _poll_task is None or _poll_task.done():
        try:
            _poll_task = asyncio.get_running_loop().create_task(_poll_lalamove_loop())
        except RuntimeError:
            pass


@router.on_event("startup")
async def _start_lalamove_poller():
    try:
        await _ensure_admin_cols()
    except Exception:
        log.exception("site_orders: startup")
    _ensure_poller()


@router.post("/site-orders/{order_id}/lalamove-status")
async def site_order_lalamove_status(order_id: int, _=Depends(basic_auth)):
    """Consulta agora (o mesmo que o loop faz sozinho a cada LALA_POLL_SECONDS)."""
    try:
        _, LalamoveError = _lala_get_order()
    except ImportError:
        return _go(order_id, err="lalamove.py não está na admin")

    async with engine.connect() as conn:
        o = (await conn.execute(text(
            "SELECT lalamove_order_id, lalamove_status FROM site_orders WHERE id = :id"
        ), {"id": order_id})).mappings().first()
    if not o or not o["lalamove_order_id"]:
        return _go(order_id, err="Lalamove ainda não foi chamado")
    lid = o["lalamove_order_id"]
    try:
        status = await _sync_lalamove(order_id, lid, o["lalamove_status"], notify=False)
    except LalamoveError as e:
        return _go(order_id, err=f"Lalamove: {str(e.payload)[:150]}")
    extra = " Pode chamar de novo." if status in LALA_FAILED else ""
    return _go(order_id, ok=f"Lalamove #{lid}: {LALA_STATUS.get(status, status)} ({status}).{extra}")



# ── CANCELAMENTO / EXCLUSÃO ────────────────────────────────────────────────────
MP_API = "https://api.mercadopago.com"


def _lala():
    try:
        from routers.lalamove import cancel_order, LalamoveError
    except ImportError:
        from lalamove import cancel_order, LalamoveError
    return cancel_order, LalamoveError


def _mp_token() -> str:
    return os.environ.get("MP_ACCESS_TOKEN", "").strip()


async def _mp_refund(payment_id: str, order_id: int):
    """Devolução total do Pix ao cliente."""
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(f"{MP_API}/v1/payments/{payment_id}/refunds", json={}, headers={
            "Authorization": f"Bearer {_mp_token()}",
            "X-Idempotency-Key": f"mtf-refund-{order_id}",
        })
    if r.status_code >= 400:
        raise RuntimeError(f"Mercado Pago {r.status_code}: {r.text[:150]}")


async def _mp_cancel_pending(payment_id: str):
    """Cancela um Pix ainda não pago (o QR deixa de funcionar)."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.put(f"{MP_API}/v1/payments/{payment_id}", json={"status": "cancelled"},
                             headers={"Authorization": f"Bearer {_mp_token()}"})
    except Exception:
        pass


async def _revert_sales(conn, order_id: int):
    """Desfaz as vendas criadas por "Registrar venda" (e devolve o estoque)."""
    note = f"Pedido site #{order_id}"
    ids = [r[0] for r in (await conn.execute(text(
        "SELECT id FROM sales WHERE note = :n OR note LIKE :like"
    ), {"n": note, "like": note + " (%"})).fetchall()]
    if ids:
        await conn.execute(text("DELETE FROM stock_movements WHERE sale_id = ANY(:ids)"), {"ids": ids})
        await conn.execute(text("DELETE FROM sales WHERE id = ANY(:ids)"), {"ids": ids})
    await conn.execute(text("UPDATE site_orders SET sale_registered = FALSE WHERE id = :id"), {"id": order_id})


@router.post("/site-orders/{order_id}/lalamove-cancel")
async def site_order_lalamove_cancel(order_id: int, _=Depends(basic_auth)):
    """Cancela o entregador. A Lalamove só permite enquanto procura motorista
    ou até 5 min depois de um motorista aceitar."""
    async with engine.connect() as conn:
        lid = (await conn.execute(text("SELECT lalamove_order_id FROM site_orders WHERE id = :id"), {"id": order_id})).scalar()
    if not lid:
        return _go(order_id, err="Lalamove não foi chamado")
    try:
        cancel_order, LalamoveError = _lala()
    except ImportError:
        return _go(order_id, err="lalamove.py não está na admin")
    try:
        await cancel_order(lid)
    except LalamoveError as e:
        return _go(order_id, err=f"A Lalamove não deixou cancelar (motorista já a caminho há mais de 5 min?). "
                                 f"Resolva no app/portal. Detalhe: {str(e.payload)[:120]}")
    async with engine.begin() as conn:
        await conn.execute(text("""
            UPDATE site_orders SET lalamove_order_id = NULL, lalamove_link = NULL, lalamove_status = 'CANCELED',
                   fulfillment = CASE WHEN fulfillment = 'enviado' THEN 'separado' ELSE fulfillment END
            WHERE id = :id
        """), {"id": order_id})
    return _go(order_id, ok=f"Entregador Lalamove #{lid} cancelado. Pode chamar de novo quando quiser.")


@router.post("/site-orders/{order_id}/cancelar")
async def site_order_cancel(order_id: int, refund: str = Form(""), _=Depends(basic_auth)):
    async with engine.connect() as conn:
        o = (await conn.execute(text("SELECT * FROM site_orders WHERE id = :id"), {"id": order_id})).mappings().first()
    if not o:
        return _go(order_id, err="Pedido não encontrado")
    if o["fulfillment"] == "cancelado":
        return _go(order_id, err="Pedido já cancelado")
    want_refund = bool(refund) and o["status"] == "paid"
    if want_refund and (not _mp_token() or not o["mp_payment_id"]):
        return _go(order_id, err="Sem MP_ACCESS_TOKEN na admin: não dá para devolver o pagamento daqui")

    # 1. entregador (se não der para cancelar, para tudo aqui — nada foi alterado ainda)
    if o["lalamove_order_id"]:
        try:
            cancel_order, LalamoveError = _lala()
            await cancel_order(o["lalamove_order_id"])
        except ImportError:
            return _go(order_id, err="lalamove.py não está na admin")
        except LalamoveError as e:
            return _go(order_id, err=f"Não foi possível cancelar o entregador Lalamove — resolva no app/portal "
                                     f"e tente de novo. Detalhe: {str(e.payload)[:120]}")

    # 2. dinheiro
    new_status = o["status"]
    if want_refund:
        try:
            await _mp_refund(o["mp_payment_id"], order_id)
        except Exception as e:
            return _go(order_id, err=f"Entregador cancelado, mas o estorno falhou: {e}. Faça a devolução no painel do Mercado Pago.")
        new_status = "refunded"
    elif o["status"] == "pending":
        if o["mp_payment_id"] and _mp_token():
            await _mp_cancel_pending(o["mp_payment_id"])
        new_status = "cancelled"

    # 3. vendas e estoque
    async with engine.begin() as conn:
        if o["sale_registered"]:
            await _revert_sales(conn, order_id)
        await conn.execute(text("""
            UPDATE site_orders SET fulfillment = 'cancelado', status = :st,
                   lalamove_order_id = NULL, lalamove_link = NULL
            WHERE id = :id
        """), {"st": new_status, "id": order_id})

    msg = "Pedido cancelado."
    if o["sale_registered"]:
        msg += " Vendas removidas e estoque devolvido."
    if want_refund:
        msg += f" R$ {float(o['total']):.2f} devolvido ao cliente ({'cartão' if o.get('pay_method') == 'card' else 'Pix'})."
    elif o["status"] == "paid":
        msg += " O dinheiro NÃO foi devolvido automaticamente."
    return _go(order_id, ok=msg)


@router.post("/site-orders/{order_id}/delete")
async def site_order_delete(order_id: int, _=Depends(basic_auth)):
    async with engine.begin() as conn:
        o = (await conn.execute(text(
            "SELECT id, sale_registered, lalamove_order_id FROM site_orders WHERE id = :id FOR UPDATE"
        ), {"id": order_id})).mappings().first()
        if not o:
            return RedirectResponse("/site-orders?filtro=todos", status_code=303)
        if o["sale_registered"] or o["lalamove_order_id"]:
            return _go(order_id, err="Este pedido tem venda registrada ou entregador chamado. Cancele o pedido primeiro.")
        await conn.execute(text("DELETE FROM site_orders WHERE id = :id"), {"id": order_id})
    return RedirectResponse("/site-orders?filtro=todos", status_code=303)
