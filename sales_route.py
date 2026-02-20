from math import ceil
from datetime import date
from fastapi import Query
from fastapi.responses import HTMLResponse
from fastapi.requests import Request
from sqlalchemy import text


SALES_SORT_FIELDS = {
    "sold_at": "s.sold_at",
    "qty": "s.qty",
    "unit_price": "s.unit_price",
    "total": "s.total",
    "created_at": "s.created_at",
}


def _sales_sort_url(request: Request, field: str, current_sort: str, current_dir: str):
    """Вернуть URL для сортировки по полю."""
    params = dict(request.query_params)
    params["sort"] = field
    params["direction"] = "desc" if current_sort == field and current_dir == "asc" else "asc"
    params["page"] = "1"
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return f"?{qs}"


@app.get("/sales", response_class=HTMLResponse)
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
    sort_col = SALES_SORT_FIELDS.get(sort, "s.sold_at")
    direction_sql = "DESC" if direction.lower() == "desc" else "ASC"
    offset = (page - 1) * per_page

    # Строим WHERE
    where_parts = ["1=1"]
    params: dict = {"limit": per_page, "offset": offset}

    if date_from:
        where_parts.append("s.sold_at >= :date_from")
        params["date_from"] = date_from
    if date_to:
        where_parts.append("s.sold_at <= :date_to")
        params["date_to"] = date_to
    if product_id.strip():
        where_parts.append("s.product_id = :product_id")
        params["product_id"] = int(product_id)

    where_sql = " AND ".join(where_parts)

    async with engine.connect() as conn:
        # Список всех продуктов для фильтра
        all_prod_res = await conn.execute(text("SELECT id, name FROM products WHERE active=TRUE ORDER BY name"))
        all_products = all_prod_res.mappings().all()

        # Считаем итоги
        agg_res = await conn.execute(
            text(f"""
                SELECT COUNT(*) as cnt,
                       COALESCE(SUM(s.total), 0) as revenue
                FROM sales s
                WHERE {where_sql}
            """),
            params,
        )
        agg = agg_res.mappings().first()
        total_count = int(agg["cnt"])
        total_revenue = float(agg["revenue"])
        avg_ticket = total_revenue / total_count if total_count else 0.0

        # Строки
        rows_res = await conn.execute(
            text(f"""
                SELECT s.id, s.sold_at, s.qty, s.unit_price, s.total, s.note,
                       p.name AS product_name, p.unit
                FROM sales s
                JOIN products p ON p.id = s.product_id
                WHERE {where_sql}
                ORDER BY {sort_col} {direction_sql}, s.id DESC
                LIMIT :limit OFFSET :offset
            """),
            params,
        )
        rows = rows_res.mappings().all()

    total_pages = max(1, ceil(total_count / per_page))
    page = min(page, total_pages)

    def sort_url(field: str) -> str:
        return _sales_sort_url(request, field, sort, direction)

    return templates.TemplateResponse(
        "sales_list.html",
        {
            "request": request,
            "rows": rows,
            "all_products": all_products,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
            "total_count": total_count,
            "total_revenue": total_revenue,
            "avg_ticket": avg_ticket,
            "sort": sort if sort in SALES_SORT_FIELDS else "sold_at",
            "direction": "desc" if direction.lower() == "desc" else "asc",
            "date_from": date_from,
            "date_to": date_to,
            "product_id_filter": product_id,
            "sort_url": sort_url,
        },
    )
