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

    async with engine.connect() as conn:
        # Выручка и кол-во продаж
        agg = await conn.execute(text("""
            SELECT
                COALESCE(SUM(total), 0)  AS revenue,
                COUNT(*)                  AS sales_count,
                COUNT(DISTINCT sold_at)   AS working_days
            FROM sales
            WHERE sold_at >= :from AND sold_at <= :to
        """), {"from": first_prev, "to": last_prev})
        row = agg.mappings().first()
        revenue     = float(row["revenue"])
        sales_count = int(row["sales_count"])
        work_days   = int(row["working_days"]) or 1
        avg_day     = revenue / work_days

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

    month_name = first_prev.strftime("%B/%Y")

    lines = [f"📊 <b>Relatório mensal — {month_name}</b>\n"]
    lines.append(f"💰 Receita total: <b>R$ {revenue:,.2f}</b>")
    lines.append(f"🛒 Vendas: <b>{sales_count}</b>")
    lines.append(f"📅 Média/dia: <b>R$ {avg_day:,.2f}</b> ({work_days} dias com venda)\n")

    lines.append("🏆 <b>Top 3 produtos:</b>")
    for i, r in enumerate(top_p, 1):
        lines.append(f"  {i}. {r['name']} — R$ {float(r['total']):,.2f}")

    lines.append("\n📂 <b>Top 3 categorias:</b>")
    for i, r in enumerate(top_c, 1):
        lines.append(f"  {i}. {r['cat']} — R$ {float(r['total']):,.2f}")

    msg = "\n".join(lines)
    await _send_tg(msg)

    return JSONResponse({"ok": True, "month": str(first_prev), "revenue": revenue})
