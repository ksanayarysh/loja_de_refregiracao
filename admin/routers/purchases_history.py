from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import text

from dependencies import engine, templates, basic_auth

router = APIRouter()


@router.get("/reports/purchases-history", response_class=HTMLResponse)
async def purchases_history(
    request: Request,
    period: str = Query("month"),   # day | week | month | year | custom
    date_from: str = Query(""),
    date_to: str = Query(""),
    _=Depends(basic_auth),
):
    today = date.today()

    if period == "day":
        d_from = d_to = today
    elif period == "week":
        d_from = today - timedelta(days=today.weekday())
        d_to = today
    elif period == "year":
        d_from = date(today.year, 1, 1)
        d_to = today
    elif period == "custom" and date_from and date_to:
        try:
            d_from = date.fromisoformat(date_from)
            d_to = date.fromisoformat(date_to)
        except Exception:
            d_from = date(today.year, today.month, 1)
            d_to = today
    else:  # month default
        d_from = date(today.year, today.month, 1)
        d_to = today

    if d_from > d_to:
        d_from, d_to = d_to, d_from

    params = {"d_from": d_from, "d_to": d_to}

    async with engine.connect() as conn:
        rows_res = await conn.execute(text("""
            SELECT
                sm.id,
                sm.moved_at,
                sm.movement_type,
                sm.qty,
                sm.unit_cost,
                (sm.qty * COALESCE(sm.unit_cost, 0)) AS total_cost,
                sm.note,
                p.name AS product_name,
                p.unit AS product_unit
            FROM stock_movements sm
            JOIN products p ON p.id = sm.product_id
            WHERE sm.movement_type IN ('entrada', 'saldo_inicial')
              AND sm.moved_at::date BETWEEN :d_from AND :d_to
            ORDER BY sm.moved_at DESC
        """), params)
        raw_rows = rows_res.mappings().all()
        rows = [
            {
                "id": r["id"],
                "moved_at": r["moved_at"],
                "movement_type": r["movement_type"],
                "qty": float(r["qty"] or 0),
                "unit_cost": float(r["unit_cost"]) if r["unit_cost"] is not None else None,
                "total_cost": float(r["total_cost"] or 0),
                "note": r["note"],
                "product_name": r["product_name"],
                "product_unit": r["product_unit"],
            }
            for r in raw_rows
        ]

        summary_res = await conn.execute(text("""
            SELECT
                COUNT(*)                                        AS purchases_count,
                COALESCE(SUM(sm.qty), 0)                        AS total_qty,
                COALESCE(SUM(sm.qty * COALESCE(sm.unit_cost, 0)), 0) AS total_cost,
                COUNT(*) FILTER (WHERE sm.unit_cost IS NULL OR sm.unit_cost <= 0) AS without_cost_count
            FROM stock_movements sm
            WHERE sm.movement_type IN ('entrada', 'saldo_inicial')
              AND sm.moved_at::date BETWEEN :d_from AND :d_to
        """), params)
        sr = summary_res.mappings().first()
        summary = {
            "purchases_count": int(sr["purchases_count"] or 0),
            "total_qty": float(sr["total_qty"] or 0),
            "total_cost": float(sr["total_cost"] or 0),
            "without_cost_count": int(sr["without_cost_count"] or 0),
        }

        # Agrupado por produto, dentro do período
        by_product_res = await conn.execute(text("""
            SELECT
                p.name AS product_name,
                p.unit AS product_unit,
                COALESCE(SUM(sm.qty), 0) AS total_qty,
                COALESCE(SUM(sm.qty * COALESCE(sm.unit_cost, 0)), 0) AS total_cost
            FROM stock_movements sm
            JOIN products p ON p.id = sm.product_id
            WHERE sm.movement_type IN ('entrada', 'saldo_inicial')
              AND sm.moved_at::date BETWEEN :d_from AND :d_to
            GROUP BY p.name, p.unit
            ORDER BY total_cost DESC
        """), params)
        by_product = [
            {
                "product_name": r["product_name"],
                "product_unit": r["product_unit"],
                "total_qty": float(r["total_qty"] or 0),
                "total_cost": float(r["total_cost"] or 0),
            }
            for r in by_product_res.mappings().all()
        ]

    return templates.TemplateResponse("purchases_history.html", {
        "request": request,
        "active_page": "reports",
        "period": period,
        "date_from": d_from,
        "date_to": d_to,
        "date_from_input": d_from.isoformat(),
        "date_to_input": d_to.isoformat(),
        "rows": rows,
        "summary": summary,
        "by_product": by_product,
    })
