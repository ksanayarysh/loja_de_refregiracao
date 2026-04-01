import os
import httpx
from datetime import date
from dateutil.relativedelta import relativedelta
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from dependencies import engine

router = APIRouter()

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID   = os.environ.get("TG_CHAT_ID", "")
CRON_SECRET  = os.environ.get("CRON_SECRET", "")  # защита endpoint от чужих вызовов


async def _send_tg(message: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "HTML"}
            )
    except Exception:
        pass


@router.post("/api/cron/monthly-report")
async def monthly_report(request: Request):
    # Защита секретом
    secret = request.headers.get("x-cron-secret", "")
    if CRON_SECRET and secret != CRON_SECRET:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    # Прошлый месяц
    today = date.today()
    first_this = today.replace(day=1)
    first_prev = first_this - relativedelta(months=1)
    last_prev  = first_this - relativedelta(days=1)

    # Считаем рабочие дни (пн-сб) в прошлом месяце
    from calendar import monthrange
    import datetime as dt
    days_in_month = (last_prev - first_prev).days + 1
    work_days_expected = sum(
        1 for d in range(days_in_month)
        if (first_prev + dt.timedelta(days=d)).weekday() != 6  # не воскресенье
    )

    DAYS_PT = {0: "Segunda", 1: "Terça", 2: "Quarta", 3: "Quinta", 4: "Sexta", 5: "Sábado", 6: "Domingo"}

    async with engine.connect() as conn:
        # Выручка, кол-во продаж и себестоимость
        agg = await conn.execute(text("""
            SELECT
                COALESCE(SUM(s.total), 0)               AS revenue,
                COUNT(*)                                  AS sales_count,
                COALESCE(SUM(p.cost_price * s.qty), 0)  AS cost
            FROM sales s
            JOIN products p ON p.id = s.product_id
            WHERE s.sold_at >= :from AND s.sold_at <= :to
        """), {"from": first_prev, "to": last_prev})
        row = agg.mappings().first()
        revenue     = float(row["revenue"])
        sales_count = int(row["sales_count"])
        cost        = float(row["cost"])
        profit      = revenue - cost
        avg_day     = revenue / work_days_expected

        # Топ-3 товаров
        top_products = await conn.execute(text("""
            SELECT p.name, SUM(s.total) AS total
            FROM sales s
            JOIN products p ON p.id = s.product_id
            WHERE s.sold_at >= :from AND s.sold_at <= :to
            GROUP BY p.name
            ORDER BY total DESC
            LIMIT 3
        """), {"from": first_prev, "to": last_prev})
        top_p = top_products.mappings().all()

        # Топ-3 категорий
        top_cats = await conn.execute(text("""
            SELECT COALESCE(c.name, 'Outro') AS cat, SUM(s.total) AS total
            FROM sales s
            JOIN products p ON p.id = s.product_id
            LEFT JOIN categories c ON c.id = p.category_id
            WHERE s.sold_at >= :from AND s.sold_at <= :to
            GROUP BY cat
            ORDER BY total DESC
            LIMIT 3
        """), {"from": first_prev, "to": last_prev})
        top_c = top_cats.mappings().all()

        # Продажи по дням недели
        by_dow = await conn.execute(text("""
            SELECT EXTRACT(DOW FROM sold_at)::int AS dow,
                   SUM(total) AS total,
                   COUNT(DISTINCT sold_at) AS days_count
            FROM sales
            WHERE sold_at >= :from AND sold_at <= :to
            GROUP BY dow
            ORDER BY total DESC
        """), {"from": first_prev, "to": last_prev})
        dow_rows = by_dow.mappings().all()

    month_name = first_prev.strftime("%B/%Y")

    # Лучший день недели (по средней выручке за день)
    best_dow_name = "—"
    best_dow_avg  = 0.0
    for r in dow_rows:
        dow_int = int(r["dow"])  # 0=воскресенье в postgres
        # конвертируем postgres DOW (0=вс) в python weekday (0=пн)
        py_dow = (dow_int - 1) % 7
        avg = float(r["total"]) / max(int(r["days_count"]), 1)
        if avg > best_dow_avg:
            best_dow_avg  = avg
            best_dow_name = DAYS_PT.get(py_dow, str(py_dow))

    lines = [f"📊 <b>Relatório mensal — {month_name}</b>\n"]
    lines.append(f"💰 Receita total: <b>R$ {revenue:,.2f}</b>")
    lines.append(f"📦 Custo materiais: <b>R$ {cost:,.2f}</b>")
    lines.append(f"✅ Lucro estimado: <b>R$ {profit:,.2f}</b>")
    lines.append(f"🛒 Vendas: <b>{sales_count}</b>")
    lines.append(f"📅 Média/dia útil: <b>R$ {avg_day:,.2f}</b> ({work_days_expected} dias úteis)\n")
    lines.append(f"📆 Melhor dia da semana: <b>{best_dow_name}</b> (média R$ {best_dow_avg:,.2f})\n")

    lines.append("🏆 <b>Top 3 produtos:</b>")
    for i, r in enumerate(top_p, 1):
        lines.append(f"  {i}. {r['name']} — R$ {float(r['total']):,.2f}")

    lines.append("\n📂 <b>Top 3 categorias:</b>")
    for i, r in enumerate(top_c, 1):
        lines.append(f"  {i}. {r['cat']} — R$ {float(r['total']):,.2f}")

    msg = "\n".join(lines)
    await _send_tg(msg)

    return JSONResponse({"ok": True, "month": str(first_prev), "revenue": revenue, "profit": profit})
