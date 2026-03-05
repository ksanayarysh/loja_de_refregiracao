from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import text

from dependencies import engine, templates, basic_auth

router = APIRouter()


@router.get("/reports", response_class=HTMLResponse)
async def reports(
    request: Request,
    period: str = Query("month"),   # day | week | month | year | custom
    date_from: str = Query(""),
    date_to: str = Query(""),
    _=Depends(basic_auth),
):
    today = date.today()

    # Вычисляем диапазон
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
            d_to   = date.fromisoformat(date_to)
        except Exception:
            d_from = date(today.year, today.month, 1)
            d_to = today
    else:  # month default
        d_from = date(today.year, today.month, 1)
        d_to = today

    params = {"d_from": d_from, "d_to": d_to}

    async with engine.connect() as conn:

        # 1. RESUMO GERAL
        summary_res = await conn.execute(text("""
            SELECT
                COUNT(*)                          AS total_sales,
                COALESCE(SUM(total), 0)           AS total_revenue,
                COALESCE(AVG(total), 0)           AS avg_ticket,
                COALESCE(MAX(total), 0)           AS max_sale
            FROM sales
            WHERE sold_at BETWEEN :d_from AND :d_to
        """), params)
        summary = summary_res.mappings().first()

        # 1b. MÉDIA POR DIA / SEMANA / MÊS (fixos, independente do período)
        avg_res = await conn.execute(text("""
            SELECT
                COALESCE(SUM(CASE WHEN sold_at = CURRENT_DATE THEN total END), 0)               AS today_revenue,
                COALESCE(SUM(CASE WHEN sold_at >= CURRENT_DATE - 6 THEN total END), 0) / 7.0    AS avg_day_week,
                COALESCE(SUM(CASE WHEN sold_at >= date_trunc('month', CURRENT_DATE) THEN total END), 0)
                    / GREATEST(EXTRACT(DAY FROM CURRENT_DATE), 1)                               AS avg_day_month,
                COALESCE(SUM(CASE WHEN date_trunc('week', sold_at) = date_trunc('week', CURRENT_DATE) THEN total END), 0) AS this_week,
                COALESCE(SUM(CASE WHEN date_trunc('month', sold_at) = date_trunc('month', CURRENT_DATE) THEN total END), 0) AS this_month
            FROM sales
        """))
        avg_data = avg_res.mappings().first()

        # 2. VENDAS POR DIA (últimos 30 pontos)
        daily_res = await conn.execute(text("""
            SELECT sold_at, COUNT(*) AS cnt, COALESCE(SUM(total), 0) AS revenue
            FROM sales
            WHERE sold_at BETWEEN :d_from AND :d_to
            GROUP BY sold_at
            ORDER BY sold_at ASC
        """), params)
        daily = daily_res.mappings().all()

        # 3. TOP PRODUTOS
        top_res = await conn.execute(text("""
            SELECT p.name, p.unit, p.cost_price,
                   COUNT(*)                      AS sales_count,
                   COALESCE(SUM(s.qty), 0)       AS total_qty,
                   COALESCE(SUM(s.total), 0)     AS total_revenue,
                   COALESCE(SUM(s.qty * COALESCE(p.cost_price, 0)), 0) AS total_cost
            FROM sales s
            JOIN products p ON p.id = s.product_id
            WHERE s.sold_at BETWEEN :d_from AND :d_to
            GROUP BY p.id, p.name, p.unit, p.cost_price
            ORDER BY total_revenue DESC
            LIMIT 10
        """), params)
        top_products = top_res.mappings().all()

        # 4. POR FORMA DE PAGAMENTO
        payment_res = await conn.execute(text("""
            SELECT payment_type,
                   COUNT(*)                  AS cnt,
                   COALESCE(SUM(total), 0)   AS revenue
            FROM sales
            WHERE sold_at BETWEEN :d_from AND :d_to
            GROUP BY payment_type
            ORDER BY revenue DESC
        """), params)
        by_payment = payment_res.mappings().all()

        # 5. MOVIMENTO DE ESTOQUE (entradas vs vendas)
        stock_res = await conn.execute(text("""
            SELECT
                movement_type,
                COALESCE(SUM(qty), 0) AS total_qty
            FROM stock_movements
            WHERE moved_at BETWEEN :d_from AND :d_to
            GROUP BY movement_type
        """), params)
        stock_moves = {r["movement_type"]: float(r["total_qty"]) for r in stock_res.mappings().all()}

        # 6. TOP CATEGORIAS
        cat_res = await conn.execute(text("""
            SELECT COALESCE(c.name, 'Outro') AS category,
                   COALESCE(SUM(s.total), 0) AS revenue,
                   COUNT(*) AS cnt
            FROM sales s
            JOIN products p ON p.id = s.product_id
            LEFT JOIN categories c ON c.id = p.category_id
            WHERE s.sold_at BETWEEN :d_from AND :d_to
            GROUP BY c.name
            ORDER BY revenue DESC
        """), params)
        by_category = cat_res.mappings().all()

    return templates.TemplateResponse("reports.html", {
        "request": request,
        "period": period,
        "date_from": d_from.isoformat(),
        "date_to": d_to.isoformat(),
        "date_from_input": date_from,
        "date_to_input": date_to,
        "summary": summary,
        "daily": daily,
        "top_products": top_products,
        "by_payment": by_payment,
        "by_category": by_category,
        "stock_moves": stock_moves,
        "total_revenue": float(summary["total_revenue"]),
        "avg_data": avg_data,
    })
