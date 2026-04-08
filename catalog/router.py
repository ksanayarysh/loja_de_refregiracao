import os
import json as _json
import httpx
from datetime import datetime
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse
from sqlalchemy import text

from dependencies import engine, templates
from articles import ARTICLES, ARTICLES_BY_SLUG

router = APIRouter()

# ── КЭШКЭШ ПРОДУКТОВ (5 минут) ───────────────────────────────────────────────
import time as _time
_products_cache: list = []
_products_cache_ts: float = 0
_CACHE_TTL = 300  # секунд

async def _get_products_cached(conn) -> list:
    global _products_cache, _products_cache_ts
    if _products_cache and (_time.time() - _products_cache_ts) < _CACHE_TTL:
        return _products_cache
    rows = await _get_products(conn)
    _products_cache = rows
    _products_cache_ts = _time.time()
    return rows

def _invalidate_cache():
    global _products_cache_ts
    _products_cache_ts = 0
# ─────────────────────────────────────────────────────────────────────────────

WA_NOTIFY_NUMBER = os.environ.get("WA_NOTIFY_NUMBER", "")
WA_OWNER_NUMBER  = os.environ.get("WA_OWNER_NUMBER", "")
SITE_URL         = os.environ.get("SITE_URL", "https://www.mtfrefrigeracao.com.br/")
GA_ID            = os.environ.get("GA_ID", "")
STORE_ADDRESS    = os.environ.get("STORE_ADDRESS", "Rua Exemplo, 123 — Cidade, UF")
STORE_PHONE      = os.environ.get("STORE_PHONE", "")
ADMIN_URL        = os.environ.get("ADMIN_URL", "https://lojaderefregiracao-production.up.railway.app")
TG_BOT_TOKEN     = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID       = os.environ.get("TG_CHAT_ID", "")


async def _send_tg(message: str):
    """Отправляет уведомление в Telegram."""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "HTML"}
            )
    except Exception:
        pass


async def _get_products(conn):
    res = await conn.execute(text("""
        SELECT p.id, p.name, p.sale_price, p.unit, p.image, p.description,
               COALESCE(c.name, 'Outro') AS category_name,
               c2.name AS category2_name
        FROM products p
        LEFT JOIN categories c  ON c.id  = p.category_id
        LEFT JOIN categories c2 ON c2.id = p.category2_id
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
        if r.get("sale_price") is not None:
            r["sale_price"] = float(r["sale_price"])
        result.append(r)
    return result


async def _get_banner_product(conn):
    res = await conn.execute(text("""
        SELECT p.id, p.name, p.sale_price, p.unit, p.image,
               COALESCE(c.name, 'Outro') AS category_name
        FROM products p
        LEFT JOIN categories c ON c.id = p.category_id
        WHERE p.active = TRUE AND p.image IS NOT NULL AND p.sale_price IS NOT NULL
        ORDER BY RANDOM()
        LIMIT 1
    """))
    row = res.mappings().first()
    if not row:
        return None
    r = dict(row)
    if r.get("image") and r["image"].startswith("/static/images/"):
        r["image"] = ADMIN_URL + r["image"]
    if r.get("sale_price") is not None:
        r["sale_price"] = float(r["sale_price"])
    return r

def _group_by_category(rows):
    groups = {}
    for r in rows:
        for cat in [r.get("category_name"), r.get("category2_name")]:
            if not cat:
                continue
            if cat not in groups:
                groups[cat] = []
            groups[cat].append(dict(r))
    return groups



async def _get_promo_product(conn):
    """Товар для промо-баннера — ищет Gas R32 3kg по имени."""
    res = await conn.execute(text("""
        SELECT p.id, p.name, p.sale_price, p.unit, p.image,
               COALESCE(c.name, 'Outro') AS category_name
        FROM products p
        LEFT JOIN categories c ON c.id = p.category_id
        WHERE p.active = TRUE AND LOWER(p.name) LIKE '%r32%' AND LOWER(p.name) LIKE '%3%'
        ORDER BY p.name
        LIMIT 1
    """))
    row = res.mappings().first()
    if not row:
        return None
    r = dict(row)
    if r.get("image") and r["image"].startswith("/static/images/"):
        r["image"] = ADMIN_URL + r["image"]
    if r.get("sale_price") is not None:
        r["sale_price"] = float(r["sale_price"])
    return r


@router.get("/", response_class=HTMLResponse)
async def catalog_page(request: Request):
    async with engine.connect() as conn:
        # Только категории с кол-вом товаров
        cats_res = await conn.execute(text("""
            SELECT COALESCE(c.name, 'Outro') AS name, COUNT(p.id) AS total
            FROM products p
            LEFT JOIN categories c ON c.id = p.category_id
            WHERE p.active = TRUE
            GROUP BY c.name
            ORDER BY
                CASE WHEN c.name ILIKE '%gas%' OR c.name ILIKE '%gás%' THEN 0 ELSE 1 END,
                c.name NULLS LAST
        """))
        categories_raw = [dict(r) for r in cats_res.mappings().all()]

        # 3 случайных товара с картинками для баннеров
        banners_res = await conn.execute(text("""
            SELECT p.id, p.name, p.sale_price, p.unit, p.image,
                   COALESCE(c.name, 'Outro') AS category_name
            FROM products p
            LEFT JOIN categories c ON c.id = p.category_id
            WHERE p.active = TRUE AND p.image IS NOT NULL AND p.sale_price IS NOT NULL
            ORDER BY RANDOM()
            LIMIT 3
        """))
        banners_raw = []
        for row in banners_res.mappings().all():
            r = dict(row)
            if r.get("image") and r["image"].startswith("/static/images/"):
                r["image"] = ADMIN_URL + r["image"]
            if r.get("sale_price") is not None:
                r["sale_price"] = float(r["sale_price"])
            r["slug"] = slugify(r["name"])
            r["cat_slug"] = slugify(r["category_name"])
            banners_raw.append(r)

        promo_product = await _get_promo_product(conn)

    categories = [
        {"name": c["name"], "total": c["total"], "slug": slugify(c["name"])}
        for c in categories_raw
    ]

    clean = _json.loads(_json.dumps(
        {"categories": categories, "banners": banners_raw, "promo_product": promo_product},
        default=str
    ))

    return templates.TemplateResponse("catalog.html", {
        "request":       request,
        "categories":    clean["categories"],
        "banners":       clean["banners"],
        "promo_product": clean["promo_product"],
        "ga_id":         GA_ID,
        "site_url":      SITE_URL,
        "store_address": STORE_ADDRESS,
        "store_phone":   STORE_PHONE,
        "wa_number":     WA_OWNER_NUMBER,
    })


@router.get("/product/{slug}", response_class=HTMLResponse)
async def product_page(request: Request, slug: str):
    async with engine.connect() as conn:
        rows = await _get_products_cached(conn)

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


@router.get("/category/{cat_slug}", response_class=HTMLResponse)
async def category_page(request: Request, cat_slug: str):
    async with engine.connect() as conn:
        rows = await _get_products_cached(conn)

    matched_cat = None
    for r in rows:
        if slugify(r["category_name"]) == cat_slug:
            matched_cat = r["category_name"]
            break

    if not matched_cat:
        return HTMLResponse("Categoria não encontrada", status_code=404)

    products = []
    seen_ids = set()
    for r in rows:
        if r["category_name"] == matched_cat or r.get("category2_name") == matched_cat:
            if r["id"] not in seen_ids:
                seen_ids.add(r["id"])
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


@router.get("/catalogo", response_class=HTMLResponse)
async def catalogo_page(request: Request):
    async with engine.connect() as conn:
        rows = await _get_products_cached(conn)
        banner_product = await _get_banner_product(conn)
        promo_product  = await _get_promo_product(conn)

    rows = [dict(r) for r in rows]
    groups = _group_by_category(rows)

    for cat, items in groups.items():
        for item in items:
            item["slug"] = slugify(item["name"])
    total = len(rows)
    cat_slugs = {cat: slugify(cat) for cat in groups.keys()}

    clean = _json.loads(_json.dumps(
        {"groups": groups, "cat_slugs": cat_slugs, "banner_product": banner_product, "promo_product": promo_product},
        default=str
    ))

    return templates.TemplateResponse("catalogo.html", {
        "request":        request,
        "groups":         clean["groups"],
        "total":          total,
        "categories":     list(clean["groups"].keys()),
        "cat_slugs":      clean["cat_slugs"],
        "ga_id":          GA_ID,
        "site_url":       SITE_URL,
        "store_address":  STORE_ADDRESS,
        "store_phone":    STORE_PHONE,
        "wa_number":      WA_OWNER_NUMBER,
        "banner_product": clean["banner_product"],
        "promo_product":  clean["promo_product"],
    })


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


@router.get("/servicos", response_class=HTMLResponse)
async def servicos_page(request: Request):
    return templates.TemplateResponse("servicos.html", {
        "request":       request,
        "ga_id":         GA_ID,
        "site_url":      SITE_URL,
        "store_address": STORE_ADDRESS,
        "store_phone":   STORE_PHONE,
        "wa_number":     WA_OWNER_NUMBER,
    })


@router.get("/calculadora-gas", response_class=HTMLResponse)
async def calculadora_gas_page(request: Request):
    return templates.TemplateResponse("calculadora-gas.html", {
        "request":       request,
        "ga_id":         GA_ID,
        "site_url":      SITE_URL,
        "store_address": STORE_ADDRESS,
        "store_phone":   STORE_PHONE,
        "wa_number":     WA_OWNER_NUMBER,
    })

@router.get("/politica-de-devolucao", response_class=HTMLResponse)
async def politica_page(request: Request):
    return templates.TemplateResponse("/politica-de-devolucao.html", {
        "request":       request
    })


@router.get("/blog", response_class=HTMLResponse)
async def blog_index(request: Request):
    from articles import ARTICLES, CATEGORY_LABELS
    cat_icons = {
        "geladeira":        "🧊",
        "ar-condicionado":  "❄️",
        "maquina-de-lavar": "🫧",
    }
    # группируем все статьи по категории (включая неопубликованные)
    groups: dict = {}
    for a in ARTICLES:
        cat = a["category"]
        if cat not in groups:
            groups[cat] = []
        groups[cat].append(a)
    categories = [
        (cat, CATEGORY_LABELS.get(cat, cat), cat_icons.get(cat, "📋"), articles)
        for cat, articles in groups.items()
    ]
    return templates.TemplateResponse("blog/index.html", {
        "request":       request,
        "categories":    categories,
        "ga_id":         GA_ID,
        "site_url":      SITE_URL,
        "store_address": STORE_ADDRESS,
        "store_phone":   STORE_PHONE,
        "wa_number":     WA_OWNER_NUMBER,
    })


@router.get("/blog/{slug}", response_class=HTMLResponse)
async def blog_article(request: Request, slug: str):
    article = ARTICLES_BY_SLUG.get(slug)
    if not article or not article.get("published"):
        return HTMLResponse("Artigo não encontrado", status_code=404)
    return templates.TemplateResponse(article["template"], {
        "request":       request,
        "ga_id":         GA_ID,
        "site_url":      SITE_URL,
        "store_address": STORE_ADDRESS,
        "store_phone":   STORE_PHONE,
        "wa_number":     WA_OWNER_NUMBER,
    })



@router.get("/api/catalog")
async def catalog_api():
    async with engine.connect() as conn:
        rows = await _get_products_cached(conn)
    return [
        {
            "id":          r["id"],
            "name":        r["name"],
            "sale_price":  float(r["sale_price"]) if r["sale_price"] else None,
            "unit":        r["unit"],
            "category":    r["category_name"],
            "image":       r["image"],
            "description": r["description"],
            "in_stock":    True,
        }
        for r in rows
    ]


@router.post("/api/track/whatsapp")
async def track_whatsapp_click(request: Request):
    try:
        body       = await request.json()
        product    = body.get("product", "Geral")
        user_agent = request.headers.get("user-agent", "")[:80]
        ip         = request.client.host
        async with engine.begin() as conn:
            await conn.execute(text("""
                INSERT INTO catalog_clicks (product_name, ip, user_agent, clicked_at)
                VALUES (:product, :ip, :ua, :now)
            """), {"product": product, "ip": ip, "ua": user_agent, "now": datetime.utcnow()})
        if WA_NOTIFY_NUMBER:
            msg = f"🛍 Novo interesse no catálogo!\nProduto: *{product}*\nHorário: {datetime.now().strftime('%d/%m %H:%M')}"
            await _send_wa_notification(msg)
        # Telegram — дублируем все WA клики
        now_str = datetime.now().strftime('%d/%m %H:%M')
        if product == "Telefone":
            await _send_tg(f"📞 <b>Alguém clicou em ligar!</b>\nHorário: {now_str}\nIP: {ip}")
        else:
            icon = "🛒" if "pedido" in product.lower() or product == "Carrinho" else "💬"
            await _send_tg(
                f"{icon} <b>Clique no WhatsApp!</b>\n"
                f"Produto: <b>{product}</b>\n"
                f"Horário: {now_str}\n"
                f"IP: {ip}"
            )
        return JSONResponse({"ok": True})
    except Exception:
        return JSONResponse({"ok": False})


async def _send_wa_notification(message: str):
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


@router.get("/api/stats")
async def catalog_stats():
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


from datetime import datetime, timezone
from xml.sax.saxutils import escape

@router.get("/sitemap.xml", response_class=PlainTextResponse)
async def sitemap():
    base = SITE_URL.rstrip("/")
    async with engine.connect() as conn:
        rows = await _get_products(conn)
    today = datetime.now(timezone.utc).date().isoformat()
    items = []
    def add_url(loc, priority, lastmod=today):
        items.append(f"\n  <url>\n    <loc>{escape(loc)}</loc>\n    <lastmod>{lastmod}</lastmod>\n    <priority>{priority}</priority>\n  </url>")
    add_url(f"{base}/", "1.0")
    add_url(f"{base}/sobre", "0.6")
    add_url(f"{base}/servicos", "0.7")
    add_url(f"{base}/blog", "0.7")
    for article in ARTICLES:
        if article.get("published"):
            add_url(f"{base}/blog/{article['slug']}", "0.8")
    seen_cats = set()
    for r in rows:
        cat = (r.get("category_name") or "").strip()
        if not cat: continue
        cs = slugify(cat)
        if not cs or cs in seen_cats: continue
        seen_cats.add(cs)
        add_url(f"{base}/category/{cs}", "0.9")
    seen_products = set()
    for r in rows:
        sl = slugify(r.get("name", ""))
        if not sl or sl in seen_products: continue
        seen_products.add(sl)
        add_url(f"{base}/product/{sl}", "0.8")
    xml = f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{"".join(items)}\n</urlset>'
    return PlainTextResponse(xml, media_type="application/xml")


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