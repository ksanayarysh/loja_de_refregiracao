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
        # 1. РЕЗЮМЕ ПО ПРОДАЖАМ
        summary_res = await conn.execute(text("""
            SELECT
                COUNT(*)                        AS total_sales,
                COALESCE(SUM(s.total), 0)      AS total_revenue,
                COALESCE(AVG(s.total), 0)      AS avg_ticket,
                COALESCE(MAX(s.total), 0)      AS max_sale
            FROM sales s
            WHERE s.sold_at::date BETWEEN :d_from AND :d_to
        """), params)
        summary = summary_res.mappings().first()

        # 1b. БЛОК СРЕДНИХ ЗНАЧЕНИЙ
        # Средние считаются только по рабочим дням (пн-пт), а не по дням с продажами.
        avg_res = await conn.execute(text("""
            WITH bounds AS (
                SELECT
                    CURRENT_DATE::date AS today,
                    (CURRENT_DATE - INTERVAL '6 days')::date AS week_7_from,
                    date_trunc('week', CURRENT_DATE)::date AS week_from,
                    date_trunc('month', CURRENT_DATE)::date AS month_from
            )
            SELECT
                COALESCE((
                    SELECT SUM(s.total)
                    FROM sales s
                    WHERE s.sold_at::date = b.today
                ), 0) AS today_revenue,

                COALESCE((
                    SELECT SUM(s.total)
                    FROM sales s
                    WHERE s.sold_at::date BETWEEN b.week_7_from AND b.today
                      AND EXTRACT(ISODOW FROM s.sold_at::date) BETWEEN 1 AND 5
                ), 0)
                /
                GREATEST((
                    SELECT COUNT(*)
                    FROM generate_series(b.week_7_from, b.today, '1 day'::interval) AS d
                    WHERE EXTRACT(ISODOW FROM d) BETWEEN 1 AND 5
                ), 1) AS avg_day_week,

                COALESCE((
                    SELECT SUM(s.total)
                    FROM sales s
                    WHERE s.sold_at::date BETWEEN b.month_from AND b.today
                      AND EXTRACT(ISODOW FROM s.sold_at::date) BETWEEN 1 AND 5
                ), 0)
                /
                GREATEST((
                    SELECT COUNT(*)
                    FROM generate_series(b.month_from, b.today, '1 day'::interval) AS d
                    WHERE EXTRACT(ISODOW FROM d) BETWEEN 1 AND 5
                ), 1) AS avg_day_month,

                COALESCE((
                    SELECT SUM(s.total)
                    FROM sales s
                    WHERE s.sold_at::date BETWEEN b.week_from AND b.today
                ), 0) AS this_week,

                COALESCE((
                    SELECT SUM(s.total)
                    FROM sales s
                    WHERE s.sold_at::date BETWEEN b.month_from AND b.today
                ), 0) AS this_month,

                (
                    SELECT COUNT(*)
                    FROM generate_series(b.week_7_from, b.today, '1 day'::interval) AS d
                    WHERE EXTRACT(ISODOW FROM d) BETWEEN 1 AND 5
                ) AS week_workdays,

                (
                    SELECT COUNT(*)
                    FROM generate_series(b.month_from, b.today, '1 day'::interval) AS d
                    WHERE EXTRACT(ISODOW FROM d) BETWEEN 1 AND 5
                ) AS month_workdays
            FROM bounds b
        """))
        avg_data = dict(avg_res.mappings().first())

        # 1c. ЛУКРО СЧИТАЕТСЯ ТОЛЬКО ПО ПОЗИЦИЯМ, У КОТОРЫХ ЗАПОЛНЕН COST_PRICE.
        # Продажи без закупочной цены в lucro не участвуют.
        profit_res = await conn.execute(text("""
            SELECT
                COALESCE(SUM(CASE WHEN p.cost_price IS NOT NULL AND p.cost_price > 0 THEN s.total ELSE 0 END), 0) AS revenue_with_cost,
                COALESCE(SUM(CASE WHEN p.cost_price IS NOT NULL AND p.cost_price > 0 THEN s.qty * p.cost_price ELSE 0 END), 0) AS cost_with_cost,
                COALESCE(SUM(CASE WHEN p.cost_price IS NULL OR p.cost_price <= 0 THEN s.total ELSE 0 END), 0) AS revenue_without_cost,
                COUNT(*) FILTER (WHERE p.cost_price IS NULL OR p.cost_price <= 0) AS sales_without_cost
            FROM sales s
            JOIN products p ON p.id = s.product_id
            WHERE s.sold_at::date BETWEEN :d_from AND :d_to
        """), params)
        profit_row = profit_res.mappings().first()

        revenue_with_cost = float(profit_row["revenue_with_cost"] or 0)
        cost_with_cost = float(profit_row["cost_with_cost"] or 0)
        revenue_without_cost = float(profit_row["revenue_without_cost"] or 0)
        total_revenue = float(summary["total_revenue"] or 0)
        total_profit = revenue_with_cost - cost_with_cost
        profit_summary = {
            "revenue_with_cost": revenue_with_cost,
            "cost_with_cost": cost_with_cost,
            "revenue_without_cost": revenue_without_cost,
            "sales_without_cost": int(profit_row["sales_without_cost"] or 0),
            "total_profit": total_profit,
        }

        # 2. ВЫРУЧКА ПО ДНЯМ
        daily_res = await conn.execute(text("""
            SELECT
                s.sold_at::date AS sold_at,
                COUNT(*) AS cnt,
                COALESCE(SUM(s.total), 0) AS revenue
            FROM sales s
            WHERE s.sold_at::date BETWEEN :d_from AND :d_to
            GROUP BY s.sold_at::date
            ORDER BY s.sold_at::date ASC
        """), params)
        daily = daily_res.mappings().all()

        # 3. ТОП ПРОДУКТОВ
        top_res = await conn.execute(text("""
            SELECT
                p.id,
                p.name,
                p.unit,
                p.cost_price,
                COUNT(*) AS sales_count,
                COALESCE(SUM(s.qty), 0) AS total_qty,
                COALESCE(SUM(s.total), 0) AS total_revenue,
                COALESCE(SUM(CASE WHEN p.cost_price IS NOT NULL AND p.cost_price > 0 THEN s.qty * p.cost_price ELSE 0 END), 0) AS total_cost
            FROM sales s
            JOIN products p ON p.id = s.product_id
            WHERE s.sold_at::date BETWEEN :d_from AND :d_to
            GROUP BY p.id, p.name, p.unit, p.cost_price
            ORDER BY total_revenue DESC, p.name ASC
            LIMIT 10
        """), params)
        top_rows = top_res.mappings().all()

        top_products = []
        for row in top_rows:
            revenue = float(row["total_revenue"] or 0)
            cost = float(row["total_cost"] or 0)
            has_cost = row["cost_price"] is not None and float(row["cost_price"]) > 0
            profit = (revenue - cost) if has_cost else None
            margin = ((profit / revenue) * 100) if has_cost and revenue > 0 else None

            top_products.append({
                "id": row["id"],
                "name": row["name"],
                "unit": row["unit"],
                "cost_price": float(row["cost_price"]) if row["cost_price"] is not None else None,
                "sales_count": int(row["sales_count"] or 0),
                "total_qty": float(row["total_qty"] or 0),
                "total_revenue": revenue,
                "total_cost": cost,
                "has_cost": has_cost,
                "profit_est": profit,
                "margin_pct": margin,
            })

        margin_products = sorted(
            [p for p in top_products if p["has_cost"]],
            key=lambda item: item["margin_pct"] if item["margin_pct"] is not None else -10**9,
            reverse=True,
        )
        products_no_cost = [p["name"] for p in top_products if not p["has_cost"]]

        # 4. ПО СПОСОБУ ОПЛАТЫ
        payment_res = await conn.execute(text("""
            SELECT
                s.payment_type,
                COUNT(*) AS cnt,
                COALESCE(SUM(s.total), 0) AS revenue
            FROM sales s
            WHERE s.sold_at::date BETWEEN :d_from AND :d_to
            GROUP BY s.payment_type
            ORDER BY revenue DESC
        """), params)
        by_payment = payment_res.mappings().all()

        # 4b. ВЫРУЧКА ПО ДНЯМ НЕДЕЛИ
        dow_res = await conn.execute(text("""
            SELECT
                EXTRACT(DOW FROM s.sold_at::date)::int AS dow,
                COUNT(*) AS cnt,
                COALESCE(SUM(s.total), 0) AS revenue
            FROM sales s
            WHERE s.sold_at::date BETWEEN :d_from AND :d_to
            GROUP BY EXTRACT(DOW FROM s.sold_at::date)
            ORDER BY dow
        """), params)
        by_dow = dow_res.mappings().all()

        # 4c. ВЫРУЧКА ПО ДНЯМ МЕСЯЦА
        dom_res = await conn.execute(text("""
            SELECT
                EXTRACT(DAY FROM s.sold_at::date)::int AS dom,
                COUNT(*) AS cnt,
                COALESCE(SUM(s.total), 0) AS revenue
            FROM sales s
            WHERE s.sold_at::date BETWEEN :d_from AND :d_to
            GROUP BY EXTRACT(DAY FROM s.sold_at::date)
            ORDER BY dom
        """), params)
        by_dom = dom_res.mappings().all()

        # 5. ДВИЖЕНИЕ СКЛАДА
        stock_res = await conn.execute(text("""
            SELECT
                sm.movement_type,
                COALESCE(SUM(sm.qty), 0) AS total_qty
            FROM stock_movements sm
            WHERE sm.moved_at::date BETWEEN :d_from AND :d_to
            GROUP BY sm.movement_type
        """), params)
        stock_moves = {r["movement_type"]: float(r["total_qty"]) for r in stock_res.mappings().all()}

        # 6. ТОП КАТЕГОРИЙ
        cat_res = await conn.execute(text("""
            SELECT
                COALESCE(c.name, 'Outro') AS category,
                COALESCE(SUM(s.total), 0) AS revenue,
                COUNT(*) AS cnt
            FROM sales s
            JOIN products p ON p.id = s.product_id
            LEFT JOIN categories c ON c.id = p.category_id
            WHERE s.sold_at::date BETWEEN :d_from AND :d_to
            GROUP BY COALESCE(c.name, 'Outro')
            ORDER BY revenue DESC, category ASC
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
        "profit_summary": profit_summary,
        "daily": daily,
        "top_products": top_products,
        "margin_products": margin_products,
        "products_no_cost": products_no_cost,
        "by_payment": by_payment,
        "by_category": by_category,
        "stock_moves": stock_moves,
        "total_revenue": total_revenue,
        "by_dow": by_dow,
        "by_dom": by_dom,
        "avg_data": avg_data,
    })
