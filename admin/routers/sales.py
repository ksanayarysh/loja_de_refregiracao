import os
from math import ceil
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

import httpx
from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import text

from dependencies import engine, templates, basic_auth

router = APIRouter()

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID   = os.environ.get("TG_CHAT_ID", "")


async def tg_notify_sale(product_name: str, qty, unit_price, total, payment_type: str, note: str = ""):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    payment_icons = {
        "dinheiro": "💵", "pix": "🟢 Pix", "cartao": "💳", "cartão": "💳",
    }
    pay_label = payment_icons.get(payment_type.lower(), payment_type)
    lines = [
        "🛒 *Nova venda registrada*",
        f"📦 {product_name}",
        f"📊 Qtd: {qty} × R$ {unit_price:.2f} = *R$ {total:.2f}*",
        f"💳 Pagamento: {pay_label}",
    ]
    if note:
        lines.append(f"📝 {note}")
    msg = "\n".join(lines)
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(url, json={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "Markdown"})
    except Exception:
        pass

SORT_FIELDS = {
    "sold_at": "s.sold_at",
    "qty": "s.qty",
    "unit_price": "s.unit_price",
    "total": "s.total",
    "created_at": "s.created_at",
}


def money2(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _parse_date(s: str):
    """Converte string ISO para objeto date — evita erro do asyncpg com strings."""
    try:
        return date.fromisoformat(s)
    except Exception:
        return None


def _sort_url(request: Request, field: str, current_sort: str, current_dir: str) -> str:
    params = dict(request.query_params)
    params["sort"] = field
    params["direction"] = "desc" if current_sort == field and current_dir == "asc" else "asc"
    params["page"] = "1"
    return "?" + "&".join(f"{k}={v}" for k, v in params.items())


async def _get_products(conn):
    res = await conn.execute(text("SELECT id, name, sale_price, unit FROM products WHERE active = TRUE ORDER BY name"))
    return res.mappings().all()


# ── NEW SALE ──

@router.get("/sales/new", response_class=HTMLResponse)
async def sales_new(request: Request, _=Depends(basic_auth)):
    async with engine.connect() as conn:
        products = await _get_products(conn)
    return templates.TemplateResponse("new_sale.html", {
        "request": request,
        "today": date.today().isoformat(),
        "products": products,
        "error": None,
        "success": None,
    })


@router.post("/sales/new", response_class=HTMLResponse)
async def sales_create(
    request: Request,
    sold_at: str = Form(...),
    product_id: int = Form(...),
    qty: str = Form(...),
    unit_price: str = Form(""),
    total: str = Form(""),
    note: str = Form(""),
    payment_type: str = Form("dinheiro"),
    client_id: str = Form(""),
    _=Depends(basic_auth),
):
    error = None
    success = None

    try:
        qty_d = Decimal(qty.replace(",", "."))
        if qty_d <= 0:
            raise ValueError
    except Exception:
        error = "Quantidade inválida. Ex: 1 ou 0,5"
        qty_d = None

    async with engine.begin() as conn:
        products = await _get_products(conn)

        p = await conn.execute(
            text("SELECT sale_price FROM products WHERE id = :pid AND active = TRUE"),
            {"pid": product_id},
        )
        row = p.first()
        if row is None:
            error = "Produto inválido (não encontrado)."

        if not error:
            default_price = Decimal(str(row[0] or 0))
            if unit_price.strip():
                try:
                    up = Decimal(unit_price.replace(",", "."))
                    price_d = up if up > 0 else default_price
                except Exception:
                    error = "Preço inválido. Ex: 35 ou 35,50"
                    price_d = None
            else:
                price_d = default_price

            if not error:
                price_d = money2(price_d)
                if total.strip():
                    try:
                        total_d = Decimal(total.replace(",", "."))
                    except Exception:
                        error = "Total inválido."
                        total_d = None
                else:
                    total_d = qty_d * price_d

                if not error:
                    total_d = money2(total_d)
                    sold_at_date = _parse_date(sold_at)
                    await conn.exec_driver_sql(
                        "INSERT INTO sales (sold_at, product_id, qty, unit_price, total, note, payment_type, client_id) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
                        (sold_at_date, product_id, qty_d, price_d, total_d, note.strip() or None, payment_type, int(client_id.strip()) if client_id.strip() else None)
                    )
                    # Автосписание стока
                    sale_res = await conn.execute(text("SELECT lastval()"))
                    sale_id = sale_res.scalar()
                    await conn.execute(
                        text("""INSERT INTO stock_movements (product_id, qty, movement_type, note, sale_id, moved_at)
                                VALUES (:pid, :qty, 'venda', :note, :sid, :dt)"""),
                        {"pid": product_id, "qty": -qty_d, "note": "Venda automática",
                         "sid": sale_id, "dt": sold_at_date}
                    )
                    success = "Venda registrada."
                    # Списать баланс клиента если saldo
                    if payment_type == "saldo" and client_id.strip():
                        try:
                            cid = int(client_id.strip())
                            await conn.execute(
                                text("UPDATE clients SET balance = balance - :a WHERE id = :id"),
                                {"a": total_d, "id": cid}
                            )
                        except Exception:
                            pass
                    # Получаем имя продукта для уведомления
                    pname_res = await conn.execute(
                        text("SELECT name FROM products WHERE id = :pid"), {"pid": product_id}
                    )
                    pname = (pname_res.scalar() or "")
                    await tg_notify_sale(pname, qty_d, price_d, total_d, payment_type, note.strip())

    return templates.TemplateResponse("new_sale.html", {
        "request": request,
        "today": sold_at or date.today().isoformat(),
        "products": products,
        "error": error,
        "success": success,
    })


# ── CHECKOUT (корзина → несколько продаж) ──

import json as _json

@router.get("/checkout", response_class=HTMLResponse)
async def checkout_page(request: Request, _=Depends(basic_auth)):
    return templates.TemplateResponse("checkout.html", {
        "request": request,
        "wa_number": os.environ.get("WA_OWNER_NUMBER", ""),
    })


@router.post("/sales/checkout")
async def sales_checkout(
    request: Request,
    sold_at: str = Form(...),
    payment_type: str = Form("dinheiro"),
    items_json: str = Form(...),
    client_id: str = Form(""),
    _=Depends(basic_auth),
):
    sold_at_date = _parse_date(sold_at)
    try:
        items = _json.loads(items_json)
    except Exception:
        return HTMLResponse("Dados inválidos", status_code=400)

    async with engine.begin() as conn:
        # Подтягиваем имена продуктов для уведомления
        product_ids = [int(i["product_id"]) for i in items]
        if product_ids:
            names_res = await conn.execute(
                text(f"SELECT id, name FROM products WHERE id = ANY(:ids)"),
                {"ids": product_ids}
            )
            names_map = {r["id"]: r["name"] for r in names_res.mappings()}
            for item in items:
                item["name"] = names_map.get(int(item["product_id"]), "")

        for item in items:
            qty_d   = money2(Decimal(str(item["qty"])))
            price_d = money2(Decimal(str(item["unit_price"])))
            total_d = money2(qty_d * price_d)
            await conn.exec_driver_sql(
                "INSERT INTO sales (sold_at, product_id, qty, unit_price, total, payment_type, client_id) VALUES ($1,$2,$3,$4,$5,$6,$7)",
                (sold_at_date, int(item["product_id"]), qty_d, price_d, total_d, payment_type, int(client_id.strip()) if client_id.strip() else None)
            )
            # Автосписание стока
            sale_res = await conn.execute(text("SELECT lastval()"))
            sale_id = sale_res.scalar()
            await conn.execute(
                text("""INSERT INTO stock_movements (product_id, qty, movement_type, note, sale_id, moved_at)
                        VALUES (:pid, :qty, 'venda', 'Venda automática', :sid, :dt)"""),
                {"pid": int(item["product_id"]), "qty": -qty_d, "sid": sale_id, "dt": sold_at_date}
            )

    # Списать баланс клиента если saldo
    if payment_type == "saldo" and client_id.strip():
        try:
            cid = int(client_id.strip())
            grand_total = sum(Decimal(str(i["qty"])) * Decimal(str(i["unit_price"])) for i in items)
            async with engine.begin() as conn2:
                await conn2.execute(
                    text("UPDATE clients SET balance = balance - :a WHERE id = :id"),
                    {"a": money2(grand_total), "id": cid}
                )
        except Exception:
            pass
    await _tg_notify_checkout(items, payment_type)
    return RedirectResponse(url="/sales?checkout=1", status_code=303)


async def _tg_notify_checkout(items: list, payment_type: str):
    """Отправляет одно суммарное уведомление по всем позициям чекаута."""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    payment_icons = {"dinheiro": "💵", "pix": "🟢 Pix", "cartao": "💳", "cartão": "💳"}
    pay_label = payment_icons.get(payment_type.lower(), payment_type)
    grand_total = sum(Decimal(str(i["qty"])) * Decimal(str(i["unit_price"])) for i in items)
    lines = ["🛒 *Nova venda (checkout)*"]
    for i in items:
        qty = Decimal(str(i["qty"]))
        price = Decimal(str(i["unit_price"]))
        name = i.get('name') or f'ID {i["product_id"]}'
        lines.append(f"  • {name} × {qty} = R$ {qty*price:.2f}")
    lines.append(f"💰 *Total: R$ {grand_total:.2f}*")
    lines.append(f"💳 Pagamento: {pay_label}")
    msg = "\n".join(lines)
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(url, json={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "Markdown"})
    except Exception:
        pass


# ── EDIT SALE ──

@router.get("/sales/{sale_id}/edit", response_class=HTMLResponse)
async def sale_edit_form(sale_id: int, request: Request, _=Depends(basic_auth)):
    async with engine.connect() as conn:
        res = await conn.execute(
            text("SELECT id, sold_at, product_id, qty, unit_price, total, note, payment_type FROM sales WHERE id = :id"),
            {"id": sale_id},
        )
        sale = res.mappings().first()
        if not sale:
            return HTMLResponse("Venda não encontrada", status_code=404)
        products = await _get_products(conn)

    return templates.TemplateResponse("edit_sale.html", {
        "request": request,
        "sale": sale,
        "products": products,
    })


@router.post("/sales/{sale_id}/edit")
async def sale_edit_save(
    sale_id: int,
    sold_at: str = Form(...),
    product_id: int = Form(...),
    qty: str = Form(...),
    unit_price: str = Form(""),
    total: str = Form(""),
    note: str = Form(""),
    payment_type: str = Form("dinheiro"),
    _=Depends(basic_auth),
):
    qty_d = Decimal(qty.replace(",", "."))
    if unit_price.strip():
        up = Decimal(unit_price.replace(",", "."))
        price_d = money2(up) if up > 0 else money2(total_d / qty_d if qty_d else Decimal(0))
    else:
        total_d_tmp = money2(Decimal(total.replace(",", ".")) if total.strip() else Decimal(0))
        price_d = money2(total_d_tmp / qty_d) if qty_d else Decimal(0)
    total_d = money2(Decimal(total.replace(",", ".")) if total.strip() else qty_d * price_d)
    sold_at_date = _parse_date(sold_at)

    async with engine.begin() as conn:
        await conn.execute(
            text("""UPDATE sales SET sold_at=:sold_at, product_id=:product_id, qty=:qty,
                        unit_price=:unit_price, total=:total, note=:note, payment_type=:payment_type
                    WHERE id=:id"""),
            {"id": sale_id, "sold_at": sold_at_date, "product_id": product_id,
             "qty": qty_d, "unit_price": price_d, "total": total_d,
             "note": note.strip() or None, "payment_type": payment_type},
        )
    return RedirectResponse(url=f"/sales/{sale_id}/edit?ok=1", status_code=303)


@router.post("/sales/{sale_id}/delete")
async def sale_delete(sale_id: int, _=Depends(basic_auth)):
    async with engine.begin() as conn:
        # Восстанавливаем баланс клиента если оплата была через saldo
        sale_res = await conn.execute(
            text("SELECT total, payment_type, client_id FROM sales WHERE id = :id"),
            {"id": sale_id}
        )
        sale = sale_res.mappings().first()
        if sale and sale["payment_type"] == "saldo" and sale["client_id"]:
            await conn.execute(
                text("UPDATE clients SET balance = balance + :a WHERE id = :id"),
                {"a": sale["total"], "id": sale["client_id"]}
            )
        # Удаление stock_movement автоматически возвращает сток
        await conn.execute(
            text("DELETE FROM stock_movements WHERE sale_id = :id"), {"id": sale_id}
        )
        await conn.execute(text("DELETE FROM sales WHERE id = :id"), {"id": sale_id})
    return RedirectResponse(url="/sales?deleted=1", status_code=303)


# ── SALES LIST ──

@router.get("/sales", response_class=HTMLResponse)
async def sales_list(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=5, le=200),
    sort: str = Query("sold_at"),
    direction: str = Query("desc"),
    date_from: str = Query(""),
    date_to: str = Query(""),
    product_id: str = Query(""),
    _=Depends(basic_auth),
):
    sort_col = SORT_FIELDS.get(sort, "s.sold_at")
    direction_sql = "DESC" if direction.lower() == "desc" else "ASC"
    offset = (page - 1) * per_page

    where_parts = ["1=1"]
    # FIX: передаём объекты date, а не строки
    params: dict = {"limit": per_page, "offset": offset}

    if date_from:
        d = _parse_date(date_from)
        if d:
            where_parts.append("s.sold_at >= :date_from")
            params["date_from"] = d
    if date_to:
        d = _parse_date(date_to)
        if d:
            where_parts.append("s.sold_at <= :date_to")
            params["date_to"] = d
    if product_id.strip():
        where_parts.append("s.product_id = :product_id")
        params["product_id"] = int(product_id)

    where_sql = " AND ".join(where_parts)

    async with engine.connect() as conn:
        all_prod_res = await conn.execute(text("SELECT id, name FROM products WHERE active=TRUE ORDER BY name"))
        all_products = all_prod_res.mappings().all()

        agg_res = await conn.execute(
            text(f"SELECT COUNT(*) as cnt, COALESCE(SUM(s.total), 0) as revenue FROM sales s WHERE {where_sql}"),
            params,
        )
        agg = agg_res.mappings().first()
        total_count = int(agg["cnt"])
        total_revenue = float(agg["revenue"])

        rows_res = await conn.execute(
            text(f"""
                SELECT s.id, s.sold_at, s.qty, s.unit_price, s.total, s.note,
                       s.payment_type, p.name AS product_name, p.unit
                FROM sales s
                JOIN products p ON p.id = s.product_id
                WHERE {where_sql}
                ORDER BY {sort_col} {direction_sql}, s.id DESC
                LIMIT :limit OFFSET :offset
            """),
            params,
        )
        rows = rows_res.mappings().all()

        # Итоги по дням за весь период (без пагинации)
        day_params = {k: v for k, v in params.items() if k not in ("limit", "offset")}
        day_res = await conn.execute(
            text(f"""
                SELECT s.sold_at::date AS day,
                       COUNT(*) AS cnt,
                       COALESCE(SUM(s.total), 0) AS total
                FROM sales s
                WHERE {where_sql}
                GROUP BY s.sold_at::date
            """),
            day_params,
        )
        day_totals = {
            str(r["day"]): {"total": float(r["total"]), "cnt": int(r["cnt"])}
            for r in day_res.mappings().all()
        }

    total_pages = max(1, ceil(total_count / per_page))
    page = min(page, total_pages)

    return templates.TemplateResponse("sales_list.html", {
        "request": request,
        "rows": rows,
        "all_products": all_products,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "total_count": total_count,
        "total_revenue": total_revenue,
        "sort": sort if sort in SORT_FIELDS else "sold_at",
        "direction": "desc" if direction.lower() == "desc" else "asc",
        "date_from": date_from,
        "date_to": date_to,
        "product_id_filter": product_id,
        "deleted": request.query_params.get("deleted") == "1",
        "day_totals": day_totals,
    })
