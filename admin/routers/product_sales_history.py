from datetime import date, datetime

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import text

from dependencies import engine, templates, basic_auth

router = APIRouter()


def _to_date(value):
    """Преобразует date или datetime в date, безопасно."""
    if isinstance(value, datetime):
        return value.date()
    return value


@router.get("/reports/product-sales-history", response_class=HTMLResponse)
async def product_sales_history(
    request: Request,
    product_id: int = Query(None),
    _=Depends(basic_auth),
):
    async with engine.connect() as conn:
        # Lista de produtos que já tiveram alguma venda (para o filtro)
        prods_res = await conn.execute(text("""
            SELECT DISTINCT p.id, p.name
            FROM sales s
            JOIN products p ON p.id = s.product_id
            ORDER BY p.name
        """))
        products = prods_res.mappings().all()

        rows = []
        summary = None
        if product_id:
            rows_res = await conn.execute(text("""
                SELECT
                    s.id,
                    s.sold_at,
                    s.qty,
                    s.total,
                    (s.total / NULLIF(s.qty, 0)) AS unit_price,
                    s.payment_type
                FROM sales s
                WHERE s.product_id = :pid
                ORDER BY s.sold_at DESC
                LIMIT 300
            """), {"pid": product_id})
            raw_rows = rows_res.mappings().all()
            rows = [
                {
                    "id": r["id"],
                    "sold_at": r["sold_at"],
                    "qty": float(r["qty"] or 0),
                    "total": float(r["total"] or 0),
                    "unit_price": float(r["unit_price"]) if r["unit_price"] is not None else None,
                    "payment_type": r["payment_type"],
                }
                for r in raw_rows
            ]

            sum_res = await conn.execute(text("""
                SELECT
                    COUNT(*)                    AS sales_count,
                    COALESCE(SUM(s.qty), 0)     AS total_qty,
                    COALESCE(SUM(s.total), 0)   AS total_revenue,
                    MIN(s.sold_at)               AS first_sale,
                    MAX(s.sold_at)               AS last_sale
                FROM sales s
                WHERE s.product_id = :pid
            """), {"pid": product_id})
            sr = sum_res.mappings().first()
            total_qty = float(sr["total_qty"] or 0)
            total_revenue = float(sr["total_revenue"] or 0)
            summary = {
                "sales_count": int(sr["sales_count"] or 0),
                "total_qty": total_qty,
                "total_revenue": total_revenue,
                "avg_unit_price": (total_revenue / total_qty) if total_qty > 0 else None,
                "first_sale": _to_date(sr["first_sale"]) if sr["first_sale"] else None,
                "last_sale": _to_date(sr["last_sale"]) if sr["last_sale"] else None,
            }

    return templates.TemplateResponse("product_sales_history.html", {
        "request": request,
        "active_page": "reports",
        "products": products,
        "selected_product_id": product_id,
        "rows": rows,
        "summary": summary,
    })
