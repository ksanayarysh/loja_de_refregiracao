from math import ceil
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
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
        res = await conn.execute(text("SELECT id, name, unit FROM products WHERE active=TRUE ORDER BY name"))
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
            res = await conn.execute(text("SELECT id, name, unit FROM products WHERE active=TRUE ORDER BY name"))
            products = res.mappings().all()
        return templates.TemplateResponse("stock_new.html", {
            "request": request, "products": products,
            "today": moved_at or date.today().isoformat(),
            "error": "Quantidade inválida.", "success": None,
        })

    # Para saída manual, gravar como negativo
    if movement_type == "saida":
        qty_d = -qty_d

    moved_at_date = _parse_date(moved_at) if moved_at else date.today()

    async with engine.begin() as conn:
        await conn.execute(
            text("""INSERT INTO stock_movements (product_id, qty, movement_type, note, moved_at)
                    VALUES (:product_id, :qty, :movement_type, :note, :moved_at)"""),
            {"product_id": product_id, "qty": qty_d,
             "movement_type": movement_type, "note": note.strip() or None,
             "moved_at": moved_at_date},
        )
        products_res = await conn.execute(text("SELECT id, name, unit FROM products WHERE active=TRUE ORDER BY name"))
        products = products_res.mappings().all()

    return templates.TemplateResponse("stock_new.html", {
        "request": request, "products": products,
        "today": date.today().isoformat(),
        "error": None,
        "success": "Movimento registrado com sucesso!",
    })


# ── STOCK LIST / REPORT ──

@router.get("/stock", response_class=HTMLResponse)
async def stock_list(
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

        # Resumo por produto (saldo atual)
        summary_res = await conn.execute(text("""
            SELECT p.id, p.name, p.unit, p.min_stock,
                   COALESCE(SUM(sm.qty), 0) as current_stock
            FROM products p
            LEFT JOIN stock_movements sm ON sm.product_id = p.id
            WHERE p.active = TRUE
            GROUP BY p.id, p.name, p.unit, p.min_stock
            ORDER BY p.name
        """))
        stock_summary = summary_res.mappings().all()

        # Movimentos
        total_res = await conn.execute(
            text(f"SELECT COUNT(*) FROM stock_movements sm WHERE {where_sql}"), params
        )
        total_count = int(total_res.scalar() or 0)

        rows_res = await conn.execute(
            text(f"""
                SELECT sm.id, sm.moved_at, sm.qty, sm.movement_type, sm.note,
                       p.name AS product_name, p.unit
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

    return templates.TemplateResponse("stock_list.html", {
        "request": request,
        "rows": rows,
        "stock_summary": stock_summary,
        "all_products": all_products,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "total_count": total_count,
        "product_id_filter": product_id,
        "movement_type_filter": movement_type,
        "date_from": date_from,
        "date_to": date_to,
    })
