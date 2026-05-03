from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import text

from dependencies import engine, templates, basic_auth

router = APIRouter()


@router.get("/reports/price-history", response_class=HTMLResponse)
async def price_history(
    request: Request,
    product_id: int = Query(None),
    _=Depends(basic_auth),
):
    async with engine.connect() as conn:
        # Все продукты у которых есть история — для фильтра
        prods_res = await conn.execute(text("""
            SELECT DISTINCT p.id, p.name
            FROM price_history ph
            JOIN products p ON p.id = ph.product_id
            ORDER BY p.name
        """))
        products = prods_res.mappings().all()

        # История изменений
        where = "WHERE ph.product_id = :pid" if product_id else ""
        params = {"pid": product_id} if product_id else {}
        rows_res = await conn.execute(text(f"""
            SELECT
                ph.id,
                p.name              AS product_name,
                p.unit              AS unit,
                ph.old_price,
                ph.new_price,
                ph.changed_at
            FROM price_history ph
            JOIN products p ON p.id = ph.product_id
            {where}
            ORDER BY ph.changed_at DESC
            LIMIT 200
        """), params)
        rows = rows_res.mappings().all()

    return templates.TemplateResponse("price_history.html", {
        "request": request,
        "rows": rows,
        "products": products,
        "selected_product_id": product_id,
    })


@router.get("/reports/stock-alert", response_class=HTMLResponse)
async def stock_alert(
    request: Request,
    threshold: float = Query(2.0),
    _=Depends(basic_auth),
):
    async with engine.connect() as conn:
        res = await conn.execute(text("""
            SELECT
                p.id,
                p.name,
                p.unit,
                p.min_stock,
                COALESCE(c.name, 'Outro') AS category_name,
                COALESCE(SUM(sm.qty), 0) AS current_stock
            FROM products p
            LEFT JOIN categories c ON c.id = p.category_id
            LEFT JOIN stock_movements sm ON sm.product_id = p.id
            WHERE p.active = TRUE
            GROUP BY p.id, p.name, p.unit, p.min_stock, c.name
            HAVING COALESCE(SUM(sm.qty), 0) < :threshold
            ORDER BY COALESCE(SUM(sm.qty), 0) ASC, p.name ASC
        """), {"threshold": threshold})
        rows = res.mappings().all()

    return templates.TemplateResponse("stock_alert.html", {
        "request": request,
        "rows": rows,
        "threshold": threshold,
    })


@router.get("/reports/stock-forecast", response_class=HTMLResponse)
async def stock_forecast(
    request: Request,
    days: int = Query(30),
    _=Depends(basic_auth),
):
    from datetime import date as _date
    today = _date.today()
    since = today - timedelta(days=days)

    async with engine.connect() as conn:
        res = await conn.execute(text("""
            SELECT
                p.id                                        AS product_id,
                p.name,
                p.unit,
                COALESCE(stock_sub.stock, 0)               AS stock,
                COALESCE(sales_sub.sold_in_period, 0)      AS sold_in_period
            FROM products p
            LEFT JOIN (
                SELECT product_id, SUM(qty) AS stock
                FROM stock_movements
                GROUP BY product_id
            ) stock_sub ON stock_sub.product_id = p.id
            LEFT JOIN (
                SELECT product_id, SUM(qty) AS sold_in_period
                FROM sales
                WHERE sold_at::date >= :since
                GROUP BY product_id
            ) sales_sub ON sales_sub.product_id = p.id
            WHERE p.active = TRUE
              AND COALESCE(stock_sub.stock, 0) > 0
            ORDER BY p.name
        """), {"since": since})
        rows = res.mappings().all()

    critical, warning, ok, no_sales = [], [], [], []
    for r in rows:
        stock = float(r["stock"])
        sold  = float(r["sold_in_period"])
        daily_avg = sold / days if sold > 0 else 0
        days_left = int(stock / daily_avg) if daily_avg > 0 else None
        item = {
            "product_id": r["product_id"],
            "name": r["name"],
            "unit": r["unit"],
            "stock": stock,
            "daily_avg": daily_avg,
            "days_left": days_left,
        }
        if daily_avg == 0:
            no_sales.append(item)
        elif days_left is not None and days_left <= 7:
            critical.append(item)
        elif days_left is not None and days_left <= 30:
            warning.append(item)
        else:
            ok.append(item)

    critical.sort(key=lambda x: x["days_left"] if x["days_left"] is not None else 0)
    warning.sort(key=lambda x: x["days_left"])
    ok.sort(key=lambda x: x["days_left"])

    return templates.TemplateResponse("stock_forecast.html", {
        "request": request,
        "days": days,
        "critical": critical,
        "warning": warning,
        "ok": ok,
        "no_sales": no_sales,
    })




async def stock_alert(
    request: Request,
    threshold: float = Query(2.0),
    _=Depends(basic_auth),
):
    async with engine.connect() as conn:
        res = await conn.execute(text("""
            SELECT
                p.id,
                p.name,
                p.unit,
                p.min_stock,
                COALESCE(c.name, 'Outro') AS category_name,
                COALESCE(SUM(sm.qty), 0) AS current_stock
            FROM products p
            LEFT JOIN categories c ON c.id = p.category_id
            LEFT JOIN stock_movements sm ON sm.product_id = p.id
            WHERE p.active = TRUE
            GROUP BY p.id, p.name, p.unit, p.min_stock, c.name
            HAVING COALESCE(SUM(sm.qty), 0) < :threshold
            ORDER BY COALESCE(SUM(sm.qty), 0) ASC, p.name ASC
        """), {"threshold": threshold})
        rows = res.mappings().all()

    return templates.TemplateResponse("stock_alert.html", {
        "request": request,
        "rows": rows,
        "threshold": threshold,
    })


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
                      AND EXTRACT(ISODOW FROM s.sold_at::date) BETWEEN 1 AND 6
                ), 0)
                /
                GREATEST((
                    SELECT COUNT(*)
                    FROM generate_series(b.week_7_from, b.today, '1 day'::interval) AS d
                    WHERE EXTRACT(ISODOW FROM d) BETWEEN 1 AND 6
                ), 1) AS avg_day_week,

                COALESCE((
                    SELECT SUM(s.total)
                    FROM sales s
                    WHERE s.sold_at::date BETWEEN b.month_from AND b.today
                      AND EXTRACT(ISODOW FROM s.sold_at::date) BETWEEN 1 AND 6
                ), 0)
                /
                GREATEST((
                    SELECT COUNT(*)
                    FROM generate_series(b.month_from, b.today, '1 day'::interval) AS d
                    WHERE EXTRACT(ISODOW FROM d) BETWEEN 1 AND 6
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
                    WHERE EXTRACT(ISODOW FROM d) BETWEEN 1 AND 6
                ) AS week_workdays,

                (
                    SELECT COUNT(*)
                    FROM generate_series(b.month_from, b.today, '1 day'::interval) AS d
                    WHERE EXTRACT(ISODOW FROM d) BETWEEN 1 AND 6
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

        # 5b. GASTO EM MATERIAIS (entradas com unit_cost preenchido)
        mat_res = await conn.execute(text("""
            SELECT
                p.name,
                COALESCE(c.name, 'Outro') AS category_name,
                SUM(sm.qty)               AS total_qty,
                p.unit,
                sm.unit_cost,
                SUM(sm.qty * sm.unit_cost) AS total_cost
            FROM stock_movements sm
            JOIN products p ON p.id = sm.product_id
            LEFT JOIN categories c ON c.id = p.category_id
            WHERE sm.moved_at::date BETWEEN :d_from AND :d_to
              AND sm.movement_type IN ('entrada', 'saldo_inicial')
              AND sm.unit_cost IS NOT NULL
              AND sm.unit_cost > 0
              AND sm.qty > 0
            GROUP BY p.name, c.name, p.unit, sm.unit_cost
            ORDER BY total_cost DESC
        """), params)
        material_rows = mat_res.mappings().all()
        material_total = sum(float(r["total_cost"] or 0) for r in material_rows)

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

        # 7. СРАВНЕНИЕ МЕСЯЦЕВ (последние 6 месяцев)
        monthly_res = await conn.execute(text("""
            SELECT
                DATE_TRUNC('month', s.sold_at::date)::date AS month,
                COALESCE(SUM(s.total), 0)                  AS revenue,
                COUNT(*)                                    AS cnt
            FROM sales s
            WHERE s.sold_at::date >= DATE_TRUNC('month', CURRENT_DATE) - INTERVAL '5 months'
            GROUP BY DATE_TRUNC('month', s.sold_at::date)
            ORDER BY month ASC
        """))
        monthly_raw = monthly_res.mappings().all()

        # Заполняем все 6 месяцев (даже пустые)
        from datetime import date as _date
        month_map = {r["month"]: {"revenue": float(r["revenue"]), "cnt": int(r["cnt"])} for r in monthly_raw}
        monthly = []
        for i in range(5, -1, -1):
            # первый день месяца i месяцев назад
            ref = today.replace(day=1)
            m = ref.month - i
            y = ref.year
            while m <= 0:
                m += 12
                y -= 1
            key = _date(y, m, 1)
            monthly.append({
                "month": key,
                "revenue": month_map.get(key, {}).get("revenue", 0),
                "cnt": month_map.get(key, {}).get("cnt", 0),
            })

        # 8b. VENDAS DIÁRIAS POR MÊS (últimos 3 meses)
        three_months_ago = today.replace(day=1)
        m = three_months_ago.month - 3
        y = three_months_ago.year
        while m <= 0:
            m += 12
            y -= 1
        three_months_start = _date(y, m, 1)

        daily_by_month_res = await conn.execute(text("""
            SELECT
                DATE_TRUNC('month', s.sold_at::date)::date AS month,
                s.sold_at::date AS day,
                COALESCE(SUM(s.total), 0) AS revenue,
                COUNT(*) AS cnt
            FROM sales s
            WHERE s.sold_at::date >= :since
            GROUP BY DATE_TRUNC('month', s.sold_at::date), s.sold_at::date
            ORDER BY month ASC, day ASC
        """), {"since": three_months_start})
        daily_by_month_raw = daily_by_month_res.mappings().all()

        # Строим структуру: список месяцев, каждый с массивом дней
        import calendar
        from collections import defaultdict as _defaultdict
        months_daily = {}
        for r in daily_by_month_raw:
            mk = r["month"]
            if mk not in months_daily:
                months_daily[mk] = {}
            months_daily[mk][r["day"]] = {"revenue": float(r["revenue"]), "cnt": int(r["cnt"])}

        monthly_daily = []
        for i in range(3, -1, -1):
            ref = today.replace(day=1)
            mo = ref.month - i
            yr = ref.year
            while mo <= 0:
                mo += 12
                yr -= 1
            first = _date(yr, mo, 1)
            last_day = calendar.monthrange(yr, mo)[1]
            last = _date(yr, mo, last_day)
            days_data = months_daily.get(first, {})
            days_list = []
            for d in range(1, last_day + 1):
                dk = _date(yr, mo, d)
                entry = days_data.get(dk, {"revenue": 0, "cnt": 0})
                days_list.append({"day": d, "date": dk.isoformat(), "revenue": entry["revenue"], "cnt": entry["cnt"]})
            monthly_daily.append({
                "month": first,
                "label": first.strftime('%B %Y'),
                "days": days_list,
                "total": sum(d["revenue"] for d in days_list),
            })


        gas_res = await conn.execute(text("""
            SELECT
                DATE_TRUNC('month', s.sold_at::date)::date AS month,
                p.name                                      AS gas_name,
                COALESCE(SUM(s.qty), 0)                    AS total_qty,
                COALESCE(SUM(s.total), 0)                  AS total_revenue
            FROM sales s
            JOIN products p ON p.id = s.product_id
            JOIN categories c ON c.id = p.category_id
            WHERE s.sold_at::date >= DATE_TRUNC('month', CURRENT_DATE) - INTERVAL '11 months'
              AND c.name ILIKE '%gases refrigerantes%'
            GROUP BY DATE_TRUNC('month', s.sold_at::date), p.name
            ORDER BY month ASC, total_qty DESC
        """))
        gas_raw = gas_res.mappings().all()

        # Строим структуру: {gas_name: {month: qty}}
        from collections import defaultdict
        gas_by_name = defaultdict(dict)
        gas_months_set = set()
        for r in gas_raw:
            gas_by_name[r["gas_name"]][r["month"]] = {
                "qty": float(r["total_qty"]),
                "revenue": float(r["total_revenue"]),
            }
            gas_months_set.add(r["month"])

        # Все 12 месяцев
        gas_months = []
        for i in range(11, -1, -1):
            ref = today.replace(day=1)
            m = ref.month - i
            y = ref.year
            while m <= 0:
                m += 12
                y -= 1
            gas_months.append(_date(y, m, 1))

        # Сортируем газы по суммарному объёму
        gas_names = sorted(
            gas_by_name.keys(),
            key=lambda n: sum(v["qty"] for v in gas_by_name[n].values()),
            reverse=True
        )
        gas_seasonality = {
            "months": gas_months,
            "gases": [
                {
                    "name": n,
                    "data": [gas_by_name[n].get(m, {"qty": 0, "revenue": 0}) for m in gas_months],
                }
                for n in gas_names
            ],
        }

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
        "material_rows": material_rows,
        "material_total": material_total,
        "total_revenue": total_revenue,
        "by_dow": by_dow,
        "by_dom": by_dom,
        "avg_data": avg_data,
        "monthly": monthly,
        "gas_seasonality": gas_seasonality,
        "monthly_daily": monthly_daily,
    })
