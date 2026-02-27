import os
import httpx
from datetime import datetime
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse
from sqlalchemy import text

from dependencies import engine, templates

router = APIRouter()

# ── CONFIG ──────────────────────────────────────────────
WA_NOTIFY_NUMBER = os.environ.get("WA_NOTIFY_NUMBER", "")   # номер для уведомлений (55219XXXXXXXX)
WA_OWNER_NUMBER  = os.environ.get("WA_OWNER_NUMBER", "")    # твой номер магазина
SITE_URL         = os.environ.get("SITE_URL", "https://seu-catalogo.railway.app")
GA_ID            = os.environ.get("GA_ID", "")              # G-XXXXXXXXXX
STORE_ADDRESS    = os.environ.get("STORE_ADDRESS", "Rua Exemplo, 123 — Cidade, UF")
STORE_PHONE      = os.environ.get("STORE_PHONE", "")
# ────────────────────────────────────────────────────────


async def _get_products(conn):
    res = await conn.execute(text("""
        SELECT p.id, p.name, p.sale_price, p.unit,
               COALESCE(c.name, 'Outro') AS category_name
        FROM products p
        LEFT JOIN categories c ON c.id = p.category_id
        WHERE p.active = TRUE
        ORDER BY c.name NULLS LAST, p.name
    """))
    return res.mappings().all()


def _group_by_category(rows):
    groups = {}
    for r in rows:
        cat = r["category_name"]
        if cat not in groups:
            groups[cat] = []
        groups[cat].append(r)
    return groups


# ── MAIN PAGE (server-side rendered for SEO) ────────────
@router.get("/", response_class=HTMLResponse)
async def catalog_page(request: Request):
    async with engine.connect() as conn:
        rows = await _get_products(conn)

    groups = _group_by_category(rows)
    total  = len(rows)

    return templates.TemplateResponse("catalog.html", {
        "request":       request,
        "groups":        groups,
        "total":         total,
        "categories":    list(groups.keys()),
        "ga_id":         GA_ID,
        "site_url":      SITE_URL,
        "store_address": STORE_ADDRESS,
        "store_phone":   STORE_PHONE,
        "wa_number":     WA_OWNER_NUMBER,
    })


# ── API (still available for JS filtering) ──────────────
@router.get("/api/catalog")
async def catalog_api():
    async with engine.connect() as conn:
        rows = await _get_products(conn)
    return [
        {
            "id":         r["id"],
            "name":       r["name"],
            "sale_price": float(r["sale_price"]) if r["sale_price"] else None,
            "unit":       r["unit"],
            "category":   r["category_name"],
            "in_stock":   True,
        }
        for r in rows
    ]


# ── CLICK TRACKING ───────────────────────────────────────
@router.post("/api/track/whatsapp")
async def track_whatsapp_click(request: Request):
    """Registra clique no WhatsApp e notifica o dono."""
    try:
        body        = await request.json()
        product     = body.get("product", "Geral")
        user_agent  = request.headers.get("user-agent", "")[:80]
        ip          = request.client.host

        # Salva no banco
        async with engine.begin() as conn:
            await conn.execute(text("""
                INSERT INTO catalog_clicks (product_name, ip, user_agent, clicked_at)
                VALUES (:product, :ip, :ua, :now)
            """), {"product": product, "ip": ip, "ua": user_agent, "now": datetime.utcnow()})

        # Notifica via WhatsApp (Evolution API ou similar)
        # Se WA_NOTIFY_NUMBER configurado, envia mensagem
        if WA_NOTIFY_NUMBER:
            msg = f"🛍 Novo interesse no catálogo!\nProduto: *{product}*\nHorário: {datetime.now().strftime('%d/%m %H:%M')}"
            await _send_wa_notification(msg)

        return JSONResponse({"ok": True})
    except Exception:
        return JSONResponse({"ok": False})


async def _send_wa_notification(message: str):
    """Envia notificação via WhatsApp Business API."""
    wa_api_url   = os.environ.get("WA_API_URL", "")
    wa_api_token = os.environ.get("WA_API_TOKEN", "")
    if not wa_api_url or not WA_NOTIFY_NUMBER:
        return
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"{wa_api_url}/message/sendText/{os.environ.get('WA_INSTANCE', '')}",
                headers={"apikey": wa_api_token},
                json={"number": WA_NOTIFY_NUMBER, "text": message}
            )
    except Exception:
        pass


# ── STATS ────────────────────────────────────────────────
@router.get("/api/stats")
async def catalog_stats():
    """Estatísticas básicas de cliques."""
    try:
        async with engine.connect() as conn:
            total = await conn.execute(text("SELECT COUNT(*) FROM catalog_clicks"))
            today = await conn.execute(text(
                "SELECT COUNT(*) FROM catalog_clicks WHERE clicked_at::date = CURRENT_DATE"
            ))
            top = await conn.execute(text("""
                SELECT product_name, COUNT(*) as clicks
                FROM catalog_clicks
                GROUP BY product_name
                ORDER BY clicks DESC LIMIT 10
            """))
            return {
                "total_clicks": total.scalar(),
                "today_clicks": today.scalar(),
                "top_products": [{"name": r[0], "clicks": r[1]} for r in top.fetchall()],
            }
    except Exception:
        return {"total_clicks": 0, "today_clicks": 0, "top_products": []}


# ── SITEMAP ──────────────────────────────────────────────
@router.get("/sitemap.xml", response_class=PlainTextResponse)
async def sitemap():
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>{SITE_URL}/</loc>
    <changefreq>daily</changefreq>
    <priority>1.0</priority>
  </url>
</urlset>"""
    return PlainTextResponse(xml, media_type="application/xml")


# ── ROBOTS.TXT ───────────────────────────────────────────
@router.get("/robots.txt", response_class=PlainTextResponse)
async def robots():
    return f"User-agent: *\nAllow: /\nSitemap: {SITE_URL}/sitemap.xml\n"
