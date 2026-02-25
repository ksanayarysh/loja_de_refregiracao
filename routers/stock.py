from math import ceil
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy import text

from dependencies import engine, templates, basic_auth

router = APIRouter()


def _parse_date(s: str):
    try:
        return date.fromisoformat(s)
    except Exception:
        return date.today()


def money2(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


async def _add_movement(conn, product_id: int, qty, movement_type: str, note: str = None, sale_id: int = None):
    """Registra um movimento de estoque."""
    await conn.execute(
        text("""INSERT INTO stock_movements (product_id, qty, movement_type, note, sale_id, moved_at)
                VALUES (:product_id, :qty, :movement_type, :note, :sale_id, CURRENT_DATE)"""),
        {"product_id": product_id, "qty": qty, "movement_type": movement_type,
         "note": note, "sale_id": sale_id},
    )


# ── STOCK RECEIPT (entrada de mercadoria) ──

@router.get("/stock/new", response_class=HTMLResponse)
async def stock_new(request: Request, _=Depends(basic_auth)):
    async with engine.connect() as conn:
        res = await conn.execute(text("SELECT id, name, unit, cost_price FROM products WHERE active=TRUE ORDER BY name"))
        products = res.mappings().all()
    return templates.TemplateResponse("stock_new.html", {
        "request": request,
        "products": products,
        "today": date.today().isoformat(),
        "error": None,
        "success": None,
    })


@router.post("/stock/new", response_class=HTMLResponse)
async def stock_create(
    request: Request,
    product_id: int = Form(...),
    qty: str = Form(...),
    unit_cost: str = Form(""),
    movement_type: str = Form("entrada"),
    note: str = Form(""),
    moved_at: str = Form(""),
    _=Depends(basic_auth),
):
    try:
        qty_d = Decimal(qty.replace(",", "."))
        if qty_d <= 0:
            raise ValueError
    except Exception:
        async with engine.connect() as conn:
            res = await conn.execute(text("SELECT id, name, unit, cost_price FROM products WHERE active=TRUE ORDER BY name"))
            products = res.mappings().all()
        return templates.TemplateResponse("stock_new.html", {
            "request": request, "products": products,
            "today": moved_at or date.today().isoformat(),
            "error": "Quantidade inválida.", "success": None,
        })

    cost_d = None
    if unit_cost.strip():
        try:
            cost_d = Decimal(unit_cost.replace(",", "."))
        except Exception:
            cost_d = None

    # Para saída manual, gravar como negativo
    if movement_type == "saida":
        qty_d = -qty_d

    moved_at_date = _parse_date(moved_at) if moved_at else date.today()

    async with engine.begin() as conn:
        await conn.execute(
            text("""INSERT INTO stock_movements (product_id, qty, unit_cost, movement_type, note, moved_at)
                    VALUES (:product_id, :qty, :unit_cost, :movement_type, :note, :moved_at)"""),
            {"product_id": product_id, "qty": qty_d, "unit_cost": cost_d,
             "movement_type": movement_type, "note": note.strip() or None,
             "moved_at": moved_at_date},
        )
        # Atualiza cost_price no produto se informado na entrada
        if cost_d is not None and movement_type in ("entrada", "saldo_inicial"):
            await conn.execute(
                text("UPDATE products SET cost_price = :cost WHERE id = :id"),
                {"cost": cost_d, "id": product_id}
            )
        # Se não informou custo agora, tenta pegar o último custo conhecido dos movimentos
        elif cost_d is None and movement_type in ("entrada", "saldo_inicial"):
            last_cost = await conn.execute(
                text("""SELECT unit_cost FROM stock_movements
                        WHERE product_id = :pid AND unit_cost IS NOT NULL AND unit_cost > 0
                        ORDER BY moved_at DESC, id DESC LIMIT 1"""),
                {"pid": product_id}
            )
            last = last_cost.scalar()
            if last:
                await conn.execute(
                    text("UPDATE products SET cost_price = :cost WHERE id = :id"),
                    {"cost": last, "id": product_id}
                )
        products_res = await conn.execute(text("SELECT id, name, unit, cost_price FROM products WHERE active=TRUE ORDER BY name"))
        products = products_res.mappings().all()

    return templates.TemplateResponse("stock_new.html", {
        "request": request, "products": products,
        "today": date.today().isoformat(),
        "error": None,
        "success": "Movimento registrado com sucesso!",
    })


# ── STOCK BALANCE (saldo atual) ──

@router.get("/stock", response_class=HTMLResponse)
async def stock_balance(request: Request, _=Depends(basic_auth)):
    async with engine.connect() as conn:
        summary_res = await conn.execute(text("""
            SELECT p.id, p.name, p.unit, p.min_stock,
                   GREATEST(0, COALESCE(SUM(sm.qty), 0)) as current_stock
            FROM products p
            LEFT JOIN stock_movements sm ON sm.product_id = p.id
            WHERE p.active = TRUE
            GROUP BY p.id, p.name, p.unit, p.min_stock
            ORDER BY p.name
        """))
        stock_summary = summary_res.mappings().all()

    return templates.TemplateResponse("stock_balance.html", {
        "request": request,
        "stock_summary": stock_summary,
        "added": request.query_params.get("added") == "1",
        "deleted": request.query_params.get("deleted") == "1",
        "edited": request.query_params.get("edited") == "1",
    })


# ── STOCK HISTORY (histórico de movimentos) ──

@router.get("/stock/history", response_class=HTMLResponse)
async def stock_history(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=5, le=200),
    product_id: str = Query(""),
    movement_type: str = Query(""),
    date_from: str = Query(""),
    date_to: str = Query(""),
    _=Depends(basic_auth),
):
    offset = (page - 1) * per_page
    where_parts = ["1=1"]
    params: dict = {"limit": per_page, "offset": offset}

    if product_id.strip():
        where_parts.append("sm.product_id = :product_id")
        params["product_id"] = int(product_id)
    if movement_type.strip():
        where_parts.append("sm.movement_type = :movement_type")
        params["movement_type"] = movement_type
    if date_from:
        d = _parse_date(date_from)
        where_parts.append("sm.moved_at >= :date_from")
        params["date_from"] = d
    if date_to:
        d = _parse_date(date_to)
        where_parts.append("sm.moved_at <= :date_to")
        params["date_to"] = d

    where_sql = " AND ".join(where_parts)

    async with engine.connect() as conn:
        all_prod_res = await conn.execute(text("SELECT id, name FROM products WHERE active=TRUE ORDER BY name"))
        all_products = all_prod_res.mappings().all()

        total_res = await conn.execute(
            text(f"SELECT COUNT(*) FROM stock_movements sm WHERE {where_sql}"), params
        )
        total_count = int(total_res.scalar() or 0)

        rows_res = await conn.execute(
            text(f"""
                SELECT sm.id, sm.moved_at, sm.qty, sm.unit_cost, sm.movement_type, sm.note,
                       p.name AS product_name, p.unit,
                       GREATEST(0, SUM(sm.qty) OVER (
                           PARTITION BY sm.product_id
                           ORDER BY sm.moved_at ASC, sm.id ASC
                       )) AS balance_after
                FROM stock_movements sm
                JOIN products p ON p.id = sm.product_id
                WHERE {where_sql}
                ORDER BY sm.moved_at DESC, sm.id DESC
                LIMIT :limit OFFSET :offset
            """),
            params,
        )
        rows = rows_res.mappings().all()

    total_pages = max(1, ceil(total_count / per_page))

    return templates.TemplateResponse("stock_history.html", {
        "request": request,
        "rows": rows,
        "all_products": all_products,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "total_count": total_count,
        "product_id_filter": product_id,
        "movement_type_filter": movement_type,
        "date_from": date_from,
        "date_to": date_to,
        "deleted": request.query_params.get("deleted") == "1",
        "edited": request.query_params.get("edited") == "1",
    })


# ── DELETE MOVEMENT ──

@router.post("/stock/{movement_id}/delete")
async def stock_delete(movement_id: int, request: Request, _=Depends(basic_auth)):
    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM stock_movements WHERE id = :id"),
            {"id": movement_id}
        )
    return RedirectResponse("/stock/history?deleted=1", status_code=303)


# ── EDIT MOVEMENT ──

@router.get("/stock/{movement_id}/edit", response_class=HTMLResponse)
async def stock_edit_form(movement_id: int, request: Request, _=Depends(basic_auth)):
    async with engine.connect() as conn:
        row = await conn.execute(
            text("SELECT * FROM stock_movements WHERE id = :id"), {"id": movement_id}
        )
        movement = row.mappings().first()
        if not movement:
            return RedirectResponse("/stock", status_code=303)
        products_res = await conn.execute(
            text("SELECT id, name, unit, cost_price FROM products WHERE active=TRUE ORDER BY name")
        )
        products = products_res.mappings().all()
    return templates.TemplateResponse("stock_edit.html", {
        "request": request,
        "m": movement,
        "products": products,
        "error": None,
    })


@router.post("/stock/{movement_id}/edit", response_class=HTMLResponse)
async def stock_edit_save(
    movement_id: int,
    request: Request,
    product_id: int = Form(...),
    qty: str = Form(...),
    unit_cost: str = Form(""),
    movement_type: str = Form("entrada"),
    note: str = Form(""),
    moved_at: str = Form(""),
    _=Depends(basic_auth),
):
    try:
        qty_d = Decimal(qty.replace(",", "."))
        if qty_d <= 0:
            raise ValueError
    except Exception:
        async with engine.connect() as conn:
            row = await conn.execute(text("SELECT * FROM stock_movements WHERE id = :id"), {"id": movement_id})
            movement = row.mappings().first()
            products_res = await conn.execute(text("SELECT id, name, unit, cost_price FROM products WHERE active=TRUE ORDER BY name"))
            products = products_res.mappings().all()
        return templates.TemplateResponse("stock_edit.html", {
            "request": request, "m": movement, "products": products,
            "error": "Quantidade inválida.",
        })

    cost_d = None
    if unit_cost.strip():
        try:
            cost_d = Decimal(unit_cost.replace(",", "."))
        except Exception:
            cost_d = None

    if movement_type == "saida":
        qty_d = -abs(qty_d)
    else:
        qty_d = abs(qty_d)

    moved_at_date = _parse_date(moved_at) if moved_at else date.today()

    async with engine.begin() as conn:
        await conn.execute(
            text("""UPDATE stock_movements
                    SET product_id=:product_id, qty=:qty, unit_cost=:unit_cost,
                        movement_type=:movement_type, note=:note, moved_at=:moved_at
                    WHERE id=:id"""),
            {"product_id": product_id, "qty": qty_d, "unit_cost": cost_d,
             "movement_type": movement_type, "note": note.strip() or None,
             "moved_at": moved_at_date, "id": movement_id}
        )
        if cost_d is not None and movement_type in ("entrada", "saldo_inicial"):
            await conn.execute(
                text("UPDATE products SET cost_price = :cost WHERE id = :id"),
                {"cost": cost_d, "id": product_id}
            )
        elif cost_d is None and movement_type in ("entrada", "saldo_inicial"):
            last_cost = await conn.execute(
                text("""SELECT unit_cost FROM stock_movements
                        WHERE product_id = :pid AND unit_cost IS NOT NULL AND unit_cost > 0
                        ORDER BY moved_at DESC, id DESC LIMIT 1"""),
                {"pid": product_id}
            )
            last = last_cost.scalar()
            if last:
                await conn.execute(
                    text("UPDATE products SET cost_price = :cost WHERE id = :id"),
                    {"cost": last, "id": product_id}
                )
    return RedirectResponse("/stock/history?edited=1", status_code=303)


# ── API ENDPOINTS ──

@router.get("/api/stock/alerts")
async def stock_alerts(_=Depends(basic_auth)):
    """Товары с нулевым или низким стоком."""
    async with engine.connect() as conn:
        res = await conn.execute(text("""
            SELECT p.id, p.name, p.unit, p.min_stock,
                   GREATEST(0, COALESCE(SUM(sm.qty), 0)) as current_stock
            FROM products p
            LEFT JOIN stock_movements sm ON sm.product_id = p.id
            WHERE p.active = TRUE
            GROUP BY p.id, p.name, p.unit, p.min_stock
            HAVING GREATEST(0, COALESCE(SUM(sm.qty), 0)) <= GREATEST(COALESCE(p.min_stock, 0), 0)
            ORDER BY COALESCE(SUM(sm.qty), 0) ASC
        """))
        rows = res.mappings().all()
    return [dict(r) for r in rows]


@router.get("/api/stock/levels")
async def stock_levels(_=Depends(basic_auth)):
    """Текущий остаток по всем товарам."""
    async with engine.connect() as conn:
        res = await conn.execute(text("""
            SELECT p.id, p.name, p.unit, p.min_stock,
                   COALESCE(SUM(sm.qty), 0) as current_stock
            FROM products p
            LEFT JOIN stock_movements sm ON sm.product_id = p.id
            WHERE p.active = TRUE
            GROUP BY p.id, p.name, p.unit, p.min_stock
            ORDER BY p.name
        """))
        rows = res.mappings().all()
    return [dict(r) for r in rows]
