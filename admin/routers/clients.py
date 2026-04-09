import os
from math import ceil
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

import httpx
from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy import text

from dependencies import engine, templates, basic_auth

router = APIRouter()

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID   = os.environ.get("TG_CHAT_ID", "")
WA_OWNER_NUMBER = os.environ.get("WA_OWNER_NUMBER", "")


def money2(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _parse_date(s: str):
    try:
        return date.fromisoformat(s)
    except Exception:
        return None


async def _tg(msg: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML"}
            )
    except Exception:
        pass


# ── LIST ──────────────────────────────────────────────────────────────────────

@router.get("/clients", response_class=HTMLResponse)
async def clients_list(request: Request, _=Depends(basic_auth)):
    async with engine.connect() as conn:
        res = await conn.execute(text("""
            SELECT id, name, phone, note, balance, created_at
            FROM clients
            ORDER BY name
        """))
        clients = [dict(r) for r in res.mappings().all()]
    return templates.TemplateResponse("clients_list.html", {
        "request": request,
        "clients": clients,
        "success": request.query_params.get("ok"),
    })


# ── NEW ───────────────────────────────────────────────────────────────────────

@router.get("/clients/new", response_class=HTMLResponse)
async def client_new_form(request: Request, _=Depends(basic_auth)):
    return templates.TemplateResponse("client_new.html", {
        "request": request,
        "error": None,
    })


@router.post("/clients/new", response_class=HTMLResponse)
async def client_new_save(
    request: Request,
    name: str = Form(...),
    phone: str = Form(""),
    note: str = Form(""),
    _=Depends(basic_auth),
):
    name = name.strip()
    if not name:
        return templates.TemplateResponse("client_new.html", {
            "request": request,
            "error": "Nome é obrigatório.",
        })
    async with engine.begin() as conn:
        res = await conn.execute(
            text("INSERT INTO clients (name, phone, note) VALUES (:n, :p, :no) RETURNING id"),
            {"n": name, "p": phone.strip() or None, "no": note.strip() or None}
        )
        new_id = res.scalar()
    return RedirectResponse(url=f"/clients/{new_id}?ok=criado", status_code=303)


# ── DETAIL ────────────────────────────────────────────────────────────────────

@router.get("/clients/{client_id}", response_class=HTMLResponse)
async def client_detail(client_id: int, request: Request, _=Depends(basic_auth)):
    async with engine.connect() as conn:
        c_res = await conn.execute(
            text("SELECT id, name, phone, note, balance, created_at FROM clients WHERE id = :id"),
            {"id": client_id}
        )
        client = c_res.mappings().first()
        if not client:
            return HTMLResponse("Cliente não encontrado", status_code=404)
        client = dict(client)

        # Депозиты
        dep_res = await conn.execute(text("""
            SELECT id, amount, note, created_at FROM client_deposits
            WHERE client_id = :id ORDER BY created_at DESC
        """), {"id": client_id})
        deposits = [dict(r) for r in dep_res.mappings().all()]

        # Продажи
        sales_res = await conn.execute(text("""
            SELECT s.id, s.sold_at, s.total, s.payment_type, p.name AS product_name, s.qty, s.unit_price
            FROM sales s
            JOIN products p ON p.id = s.product_id
            WHERE s.client_id = :id
            ORDER BY s.sold_at DESC, s.id DESC
        """), {"id": client_id})
        sales = [dict(r) for r in sales_res.mappings().all()]

        # Продукты для быстрой продажи
        prod_res = await conn.execute(text(
            "SELECT id, name, sale_price, unit FROM products WHERE active=TRUE ORDER BY name"
        ))
        products = [dict(r) for r in prod_res.mappings().all()]

    return templates.TemplateResponse("client_detail.html", {
        "request": request,
        "client": client,
        "deposits": deposits,
        "sales": sales,
        "products": products,
        "today": date.today().isoformat(),
        "wa_number": WA_OWNER_NUMBER,
        "success": request.query_params.get("ok"),
        "error": request.query_params.get("err"),
    })


# ── DEPOSIT ───────────────────────────────────────────────────────────────────

@router.post("/clients/{client_id}/deposit")
async def client_deposit(
    client_id: int,
    amount: str = Form(...),
    note: str = Form(""),
    _=Depends(basic_auth),
):
    try:
        amount_d = money2(Decimal(amount.replace(",", ".")))
        if amount_d <= 0:
            raise ValueError
    except Exception:
        return RedirectResponse(url=f"/clients/{client_id}?err=valor+inválido", status_code=303)

    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO client_deposits (client_id, amount, note) VALUES (:cid, :a, :n)"),
            {"cid": client_id, "a": amount_d, "n": note.strip() or None}
        )
        await conn.execute(
            text("UPDATE clients SET balance = balance + :a WHERE id = :id"),
            {"a": amount_d, "id": client_id}
        )
        name_res = await conn.execute(text("SELECT name FROM clients WHERE id=:id"), {"id": client_id})
        cname = name_res.scalar() or ""

    await _tg(f"💰 <b>Depósito de cliente</b>\nCliente: <b>{cname}</b>\nValor: R$ {amount_d:.2f}\n{('Obs: ' + note.strip()) if note.strip() else ''}")
    return RedirectResponse(url=f"/clients/{client_id}?ok=deposito", status_code=303)


# ── SALE FROM CLIENT PAGE ─────────────────────────────────────────────────────

@router.post("/clients/{client_id}/sale")
async def client_sale(
    client_id: int,
    sold_at: str = Form(...),
    product_id: int = Form(...),
    qty: str = Form(...),
    unit_price: str = Form(""),
    total: str = Form(""),
    note: str = Form(""),
    _=Depends(basic_auth),
):
    try:
        qty_d = money2(Decimal(qty.replace(",", ".")))
        if qty_d <= 0:
            raise ValueError
    except Exception:
        return RedirectResponse(url=f"/clients/{client_id}?err=quantidade+inválida", status_code=303)

    async with engine.begin() as conn:
        p_res = await conn.execute(
            text("SELECT sale_price, name FROM products WHERE id=:pid AND active=TRUE"),
            {"pid": product_id}
        )
        p = p_res.mappings().first()
        if not p:
            return RedirectResponse(url=f"/clients/{client_id}?err=produto+inválido", status_code=303)

        price_d = money2(Decimal(unit_price.replace(",", ".")) if unit_price.strip() else Decimal(str(p["sale_price"] or 0)))
        total_d = money2(Decimal(total.replace(",", ".")) if total.strip() else qty_d * price_d)

        # Проверяем баланс
        bal_res = await conn.execute(text("SELECT balance, name FROM clients WHERE id=:id"), {"id": client_id})
        client = bal_res.mappings().first()
        if not client or client["balance"] < total_d:
            return RedirectResponse(url=f"/clients/{client_id}?err=saldo+insuficiente", status_code=303)

        sold_at_date = _parse_date(sold_at)

        # Создаём продажу
        await conn.exec_driver_sql(
            "INSERT INTO sales (sold_at, product_id, qty, unit_price, total, note, payment_type, client_id) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
            (sold_at_date, product_id, qty_d, price_d, total_d, note.strip() or None, "saldo", client_id)
        )
        sale_res = await conn.execute(text("SELECT lastval()"))
        sale_id = sale_res.scalar()

        # Списываем сток
        await conn.execute(text("""
            INSERT INTO stock_movements (product_id, qty, movement_type, note, sale_id, moved_at)
            VALUES (:pid, :qty, 'venda', 'Venda automática', :sid, :dt)
        """), {"pid": product_id, "qty": -qty_d, "sid": sale_id, "dt": sold_at_date})

        # Списываем баланс
        await conn.execute(
            text("UPDATE clients SET balance = balance - :a WHERE id = :id"),
            {"a": total_d, "id": client_id}
        )

    await _tg(
        f"🛒 <b>Venda por saldo</b>\nCliente: <b>{client['name']}</b>\n"
        f"Produto: {p['name']} × {qty_d}\nTotal: R$ {total_d:.2f}\n"
        f"Saldo restante: R$ {float(client['balance']) - float(total_d):.2f}"
    )
    return RedirectResponse(url=f"/clients/{client_id}?ok=venda", status_code=303)


# ── EDIT ──────────────────────────────────────────────────────────────────────

@router.post("/clients/{client_id}/edit")
async def client_edit(
    client_id: int,
    name: str = Form(...),
    phone: str = Form(""),
    note: str = Form(""),
    _=Depends(basic_auth),
):
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE clients SET name=:n, phone=:p, note=:no WHERE id=:id"),
            {"n": name.strip(), "p": phone.strip() or None, "no": note.strip() or None, "id": client_id}
        )
    return RedirectResponse(url=f"/clients/{client_id}?ok=editado", status_code=303)


# ── API: баланс клиента (для JS в new_sale) ───────────────────────────────────

@router.get("/api/clients")
async def api_clients(_=Depends(basic_auth)):
    async with engine.connect() as conn:
        res = await conn.execute(text("SELECT id, name, balance FROM clients ORDER BY name"))
        return [dict(r) for r in res.mappings().all()]


@router.get("/api/clients/{client_id}/balance")
async def api_client_balance(client_id: int, _=Depends(basic_auth)):
    async with engine.connect() as conn:
        res = await conn.execute(
            text("SELECT balance FROM clients WHERE id=:id"), {"id": client_id}
        )
        row = res.first()
        if not row:
            return JSONResponse({"error": "not found"}, status_code=404)
        return {"balance": float(row[0])}
