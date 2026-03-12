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
SITE_URL         = os.environ.get("SITE_URL", "https://mtf-catalog.up.railway.app")
GA_ID            = os.environ.get("GA_ID", "")              # G-XXXXXXXXXX
STORE_ADDRESS    = os.environ.get("STORE_ADDRESS", "Rua Exemplo, 123 — Cidade, UF")
STORE_PHONE      = os.environ.get("STORE_PHONE", "")
ADMIN_URL        = os.environ.get("ADMIN_URL", "https://lojaderefregiracao-production.up.railway.app")
# ────────────────────────────────────────────────────────


async def _get_products(conn):
    res = await conn.execute(text("""
        SELECT p.id, p.name, p.sale_price, p.unit, p.image, p.description,
               COALESCE(c.name, 'Outro') AS category_name
        FROM products p
        LEFT JOIN categories c ON c.id = p.category_id
        WHERE p.active = TRUE
        ORDER BY
            CASE WHEN c.name ILIKE '%gas%' OR c.name ILIKE '%gás%' THEN 0 ELSE 1 END,
            c.name NULLS LAST,
            CASE WHEN p.unit = 'kg' THEN 0 ELSE 1 END,
            p.name
    """))
    rows = res.mappings().all()
    result = []
    for r in rows:
        r = dict(r)
        if r.get("image") and r["image"].startswith("/static/images/"):
            r["image"] = ADMIN_URL + r["image"]
        result.append(r)
    return result


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

    rows = [dict(r) for r in rows]  # <- превращаем в обычные dict
    groups = _group_by_category(rows)

    for cat, items in groups.items():
        for item in items:
            item["slug"] = slugify(item["name"])
    total  = len(rows)
    cat_slugs = {cat: slugify(cat) for cat in groups.keys()}

    return templates.TemplateResponse("catalog.html", {
        "request":       request,
        "groups":        groups,
        "total":         total,
        "categories":    list(groups.keys()),
        "cat_slugs":     cat_slugs,
        "ga_id":         GA_ID,
        "site_url":      SITE_URL,
        "store_address": STORE_ADDRESS,
        "store_phone":   STORE_PHONE,
        "wa_number":     WA_OWNER_NUMBER,
    })


@router.get("/product/{slug}", response_class=HTMLResponse)
async def product_page(request: Request, slug: str):
    async with engine.connect() as conn:
        rows = await _get_products(conn)

    product = None
    for r in rows:
        if slugify(r["name"]) == slug:
            product = dict(r)
            product["slug"] = slug
            break

    if not product:
        return HTMLResponse("Produto não encontrado", status_code=404)

    related = []
    for r in rows:
        if r["category_name"] == product["category_name"] and slugify(r["name"]) != slug:
            rd = dict(r)
            rd["slug"] = slugify(rd["name"])
            related.append(rd)
            if len(related) >= 6:
                break

    return templates.TemplateResponse("product.html", {
        "request":       request,
        "product":       product,
        "related":       related,
        "cat_slug":      slugify(product["category_name"]),
        "ga_id":         GA_ID,
        "site_url":      SITE_URL,
        "wa_number":     WA_OWNER_NUMBER,
        "store_address": STORE_ADDRESS,
        "store_phone":   STORE_PHONE,
    })


# ── CATEGORY PAGE ───────────────────────────────────────
@router.get("/category/{cat_slug}", response_class=HTMLResponse)
async def category_page(request: Request, cat_slug: str):
    async with engine.connect() as conn:
        rows = await _get_products(conn)

    # найти категорию по slug
    matched_cat = None
    for r in rows:
        if slugify(r["category_name"]) == cat_slug:
            matched_cat = r["category_name"]
            break

    if not matched_cat:
        return HTMLResponse("Categoria não encontrada", status_code=404)

    products = []
    for r in rows:
        if r["category_name"] == matched_cat:
            rd = dict(r)
            rd["slug"] = slugify(rd["name"])
            products.append(rd)

    all_cats = list(dict.fromkeys(r["category_name"] for r in rows))
    cat_slugs_map = {cat: slugify(cat) for cat in all_cats}

    return templates.TemplateResponse("category.html", {
        "request":       request,
        "category":      matched_cat,
        "cat_slug":      cat_slug,
        "products":      products,
        "all_cats":      all_cats,
        "cat_slugs_map": cat_slugs_map,
        "total":         len(products),
        "ga_id":         GA_ID,
        "site_url":      SITE_URL,
        "wa_number":     WA_OWNER_NUMBER,
        "store_address": STORE_ADDRESS,
        "store_phone":   STORE_PHONE,
    })


# ── ABOUT PAGE ───────────────────────────────────────────
@router.get("/sobre", response_class=HTMLResponse)
async def about_page(request: Request):
    return templates.TemplateResponse("about.html", {
        "request":       request,
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
            "image":      r["image"],
            "description": r["description"],
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
@router.get("/sitemap.xml")
async def sitemap(request: Request):
    base = SITE_URL.rstrip("/")

    async with engine.connect() as conn:
        rows = await _get_products(conn)

    items = []
    today = datetime.utcnow().date().isoformat()

    items.append(f"""
    <url>
        <loc>{base}/</loc>
        <priority>1.0</priority>
        <lastmod>{today}</lastmod>
    </url>
    """)

    items.append(f"""
    <url>
    <loc>{base}/sobre</loc>
    <priority>0.6</priority>
    <lastmod>{today}</lastmod>
    </url>
    """)

    seen_cats = set()
    for r in rows:
        cat = r["category_name"]
        if cat not in seen_cats:
            seen_cats.add(cat)
            items.append(f"""
        <url>
            <loc>{base}/category/{slugify(cat)}</loc>
            <priority>0.9</priority>
            <lastmod>{today}</lastmod>
        </url>
        """)

    for r in rows:
        slug = slugify(r["name"])
        items.append(f"""
        <url>
            <loc>{base}/product/{slug}</loc>
            <priority>0.8</priority>
            <lastmod>{today}</lastmod>
        </url>
        """)

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="https://www.sitemaps.org/schemas/sitemap/0.9">
        {''.join(items)}
        </urlset>
        """

    return PlainTextResponse(xml, media_type="application/xml")


# ── ROBOTS.TXT ───────────────────────────────────────────
@router.get("/robots.txt", response_class=PlainTextResponse)
async def robots():
    return f"User-agent: *\nAllow: /\nSitemap: {SITE_URL}/sitemap.xml\n"


import re
import unicodedata

def slugify(text: str) -> str:
    text = (text or "").strip().lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("+", " plus ")
    text = re.sub(r"[^\w\s-]", " ", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text