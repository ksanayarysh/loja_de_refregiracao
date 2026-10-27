import os
import asyncio
import json as _json
import httpx
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse, Response
from sqlalchemy import text
from lalamove import get_quote, LalamoveError

from dependencies import engine, templates
from articles import ARTICLES, ARTICLES_BY_SLUG
from blog_schema import build_article_context
import logging
_blog_log = logging.getLogger("uvicorn.error")


def _client_ip(request) -> str:
    """IP real do visitante. No Railway request.client.host é o proxy interno (100.64.x.x):
    o IP verdadeiro vem no cabeçalho X-Forwarded-For (o primeiro da lista)."""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    real = request.headers.get("x-real-ip", "")
    if real:
        return real.strip()
    return request.client.host if request.client else "?"

router = APIRouter()

BRT = ZoneInfo("America/Sao_Paulo")

# ── Jinja2 фильтр для ресайза изображений ─────────────────────────────────────
ADMIN_URL = os.environ.get("ADMIN_URL", "").rstrip("/")

def _imgurl(path: str, w: int = 0) -> str:
    """Возвращает URL с параметром ресайза если задана ширина."""
    if not path:
        return ""
    if w:
        return f"/img-proxy?src={path}&w={w}"
    return path

templates.env.filters["imgurl"] = _imgurl

# ── Endpoint ресайза изображений ──────────────────────────────────────────────
# ── In-memory кеш для img-proxy ───────────────────────────────────────────────
_img_cache: dict = {}  # key -> (content, media_type, timestamp)
_IMG_CACHE_TTL = 3600  # 1 час

@router.get("/img-proxy")
async def img_proxy(src: str, w: int = 0):
    """Проксирует и ресайзит изображение с admin сервиса."""
    import time
    cache_key = f"{src}:{w}"

    # Проверяем кеш
    if cache_key in _img_cache:
        content, media_type, ts = _img_cache[cache_key]
        if time.time() - ts < _IMG_CACHE_TTL:
            return Response(
                content=content,
                media_type=media_type,
                headers={"Cache-Control": "public, max-age=2592000, immutable"}
            )

    try:
        if src.startswith("/static/"):
            url = f"{ADMIN_URL}{src}"
        else:
            url = src

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return Response(status_code=404)

        content = resp.content

        if w > 0:
            try:
                from PIL import Image
                import io
                img = Image.open(io.BytesIO(content))
                ratio = w / img.width
                h = int(img.height * ratio)
                img = img.resize((w, h), Image.LANCZOS)
                buf = io.BytesIO()
                fmt = "WEBP" if src.endswith(".webp") else "JPEG"
                img.save(buf, fmt, quality=82, optimize=True)
                content = buf.getvalue()
                media_type = "image/webp" if fmt == "WEBP" else "image/jpeg"
            except Exception:
                media_type = resp.headers.get("content-type", "image/jpeg")
        else:
            media_type = resp.headers.get("content-type", "image/jpeg")

        # Сохраняем в кеш
        _img_cache[cache_key] = (content, media_type, time.time())
        # Ограничиваем размер кеша
        if len(_img_cache) > 500:
            oldest = min(_img_cache.keys(), key=lambda k: _img_cache[k][2])
            del _img_cache[oldest]

        return Response(
            content=content,
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=2592000, immutable"}
        )
    except Exception:
        return Response(status_code=500)

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
    asyncio.ensure_future(_remember_slugs(rows))   # histórico de URLs (não bloqueia a página)
    return rows

def _invalidate_cache():
    global _products_cache_ts
    _products_cache_ts = 0
# ─────────────────────────────────────────────────────────────────────────────

WA_NOTIFY_NUMBER = os.environ.get("WA_NOTIFY_NUMBER", "")
WA_OWNER_NUMBER  = os.environ.get("WA_OWNER_NUMBER", "")
SITE_URL         = os.environ.get("SITE_URL", "https://www.mtfrefrigeracao.com.br").rstrip("/")  # sem "/" no fim: evita "//product/..." no canonical
GA_ID            = os.environ.get("GA_ID", "")
STORE_ADDRESS    = os.environ.get("STORE_ADDRESS", "Rua Exemplo, 123 — Cidade, UF")
STORE_PHONE      = os.environ.get("STORE_PHONE", "")
ADMIN_URL        = os.environ.get("ADMIN_URL", "https://lojaderefregiracao-production.up.railway.app")
TG_BOT_TOKEN     = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID       = os.environ.get("TG_CHAT_ID", "")
META_PIXEL_ID    = os.environ.get("META_PIXEL_ID", "")
META_CAPI_TOKEN  = os.environ.get("META_CAPI_TOKEN", "")


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



# ── PRODUCT VIEW TRACKING ──────────────────────────────────────────────────────
_view_cache: dict = {}   # (ip, slug) -> timestamp, дедупликация 1 час
_view_table_ready = False

async def _ensure_views_table():
    global _view_table_ready
    if _view_table_ready:
        return
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS product_views (
                id          SERIAL PRIMARY KEY,
                product_id  INTEGER,
                slug        VARCHAR(200),
                ip          VARCHAR(64),
                user_agent  VARCHAR(200),
                viewed_at   TIMESTAMPTZ DEFAULT NOW()
            )
        """))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_pv_slug ON product_views(slug)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_pv_viewed ON product_views(viewed_at)"
        ))
        # idioma do navegador (Accept-Language): cliente real no Brasil manda "pt-BR"
        await conn.execute(text("ALTER TABLE product_views ADD COLUMN IF NOT EXISTS lang VARCHAR(60)"))
    _view_table_ready = True

# IPs de nuvem (Tencent, Alibaba, Huawei...) — robôs que fingem ser iPhone/Chrome vêm daí
import re as _re_dc
_DC_IP_RE = _re_dc.compile(r"^(43\.(12[89]|1[3-8][0-9]|19[01])\.|82\.156\.|62\.234\.|192\.144\.|49\.51\.|49\.232\.|49\.233\.|101\.3[2-5]\.|101\.4[23]\.|119\.2[89]\.|124\.156\.|129\.226\.|150\.109\.|162\.62\.|170\.106\.|1\.1[2-5]\.|81\.6[89]\.|81\.7[01]\.|106\.5[2-5]\.|111\.2(29|3[01])\.|118\.(25|89|126)\.|121\.[45]\.|122\.51\.|129\.(204|211)\.|134\.175\.|139\.(155|186|199)\.|140\.143\.|150\.158\.|152\.136\.|159\.75\.|175\.2[47]\.|182\.254\.|193\.112\.|203\.195\.|212\.64\.|42\.19[2-4]\.|47\.(7[4-9]|8[0-9]|9[0-9]|2[3-5][0-9])\.|8\.2(09|1[0-9]|2[0-2])\.|119\.13\.|159\.138\.|190\.92\.)")

def _is_datacenter(ip: str) -> bool:
    return bool(_DC_IP_RE.match(ip or ""))

_BOT_UA_PATTERNS = (
    "googlebot", "bingbot", "slurp", "duckduckbot", "baiduspider",
    "yandexbot", "sogou", "exabot", "facebot", "ia_archiver",
    "ahrefsbot", "semrushbot", "dotbot", "mj12bot", "blexbot",
    "petalbot", "bytespider", "applebot", "google-inspectiontool",
    "gptbot", "chatgpt-user", "claudebot", "anthropic-ai",
    "dataforseobot", "serpstatbot", "sistrix", "rogerbot",
    "screaming frog", "python-requests", "go-http-client",
    "curl/", "wget/", "jakarta commons-httpclient",
    # robôs do Google que NÃO têm "googlebot" no nome: Merchant Center (Storebot), Google Ads (AdsBot) etc.
    "storebot", "adsbot", "mediapartners-google", "googleother", "google-extended", "feedfetcher",
    "google-read-aloud", "apis-google", "google-safety", "google-pagerenderer",
    # prévias de link (WhatsApp, Telegram, Facebook/Instagram...)
    "facebookexternalhit", "meta-externalagent", "meta-externalfetcher", "whatsapp", "telegrambot",
    "twitterbot", "linkedinbot", "slackbot", "discordbot", "pinterest", "skypeuripreview",
    # outros robôs / ferramentas
    "amazonbot", "oai-searchbot", "perplexitybot", "headlesschrome", "lighthouse", "pagespeed",
    "crawler", "spider", "scrapy", "okhttp", "axios", "node-fetch", "python-httpx", "aiohttp",
    "uptimerobot", "bot/", "bot;", "bot)",
)

def _is_bot(ua: str) -> bool:
    u = ua.lower()
    return any(p in u for p in _BOT_UA_PATTERNS)

async def _track_product_view(product_id: int, slug: str, ip: str, ua: str, lang: str = ""):
    import time
    if ip in _EXCLUDED_IPS:
        return
    if _is_bot(ua) or _is_datacenter(ip):
        return
    key = (ip, slug)
    if time.time() - _view_cache.get(key, 0) < 3600:
        return
    _view_cache[key] = time.time()
    # Ограничиваем размер кеша
    if len(_view_cache) > 10000:
        oldest = min(_view_cache.keys(), key=lambda k: _view_cache[k])
        del _view_cache[oldest]
    try:
        await _ensure_views_table()
        async with engine.begin() as conn:
            await conn.execute(text("""
                INSERT INTO product_views (product_id, slug, ip, user_agent, lang, viewed_at)
                VALUES (:pid, :slug, :ip, :ua, :lang, NOW())
            """), {"pid": product_id, "slug": slug, "ip": ip, "ua": ua[:200], "lang": (lang or "")[:60]})
    except Exception:
        pass
# ──────────────────────────────────────────────────────────────────────────────

# ── VISIT TRACKING ─────────────────────────────────────────────────────────────
_visit_cache: dict = {}  # ip -> timestamp, чтобы не спамить с одного IP
_EXCLUDED_IPS = {"177.192.23.188"}  # свои IP — не трекать


def _is_owner(request: Request) -> bool:
    """Visita da própria loja: IP fixo excluído OU navegador marcado em /eu (cookie mtf_eu=1)."""
    return _client_ip(request) in _EXCLUDED_IPS or request.cookies.get("mtf_eu") == "1"


@router.get("/eu", response_class=HTMLResponse)
async def marcar_navegador_da_loja(request: Request, sair: int = 0):
    """Abra uma vez em cada celular/computador da loja: esse navegador deixa de contar nas estatísticas.
    /eu?sair=1 desfaz."""
    txt = ("Este navegador voltou a contar nas estatísticas." if sair else
           "Pronto! Este navegador é da loja e não conta mais nas estatísticas (visitas, views, cliques, checkout).")
    resp = HTMLResponse(f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                        f"<meta name=robots content=noindex><body style='font-family:sans-serif;padding:30px'>"
                        f"<h2>{txt}</h2><p><a href='/'>Ir para o site</a></p></body>")
    if sair:
        resp.delete_cookie("mtf_eu", path="/")
    else:
        resp.set_cookie("mtf_eu", "1", max_age=5 * 365 * 86400, path="/", samesite="lax", secure=True)
    return resp


# ── EVENTOS POR PRODUTO (carrinho, checkout, ligação) ─────────────────────────
_events_ready = False
_event_seen: dict = {}   # (ip, pid, event) -> ts: o mesmo evento da mesma pessoa conta 1x por hora


async def log_product_events(ids, event: str, ip: str):
    global _events_ready
    import time
    now = time.time()
    novos = []
    for pid in ids:
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            continue
        if pid <= 0 or now - _event_seen.get((ip, pid, event), 0) < 3600:
            continue
        _event_seen[(ip, pid, event)] = now
        novos.append(pid)
    if len(_event_seen) > 20000:
        for k, t in list(_event_seen.items()):
            if now - t > 3600:
                _event_seen.pop(k, None)
    if not novos:
        return
    try:
        async with engine.begin() as conn:
            if not _events_ready:
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS product_events (
                        id          SERIAL PRIMARY KEY,
                        product_id  INTEGER NOT NULL,
                        event       VARCHAR(20) NOT NULL,
                        ip          VARCHAR(64),
                        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """))
                await conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_pe_prod_time ON product_events(product_id, created_at)"))
                _events_ready = True
            for pid in novos:
                await conn.execute(text(
                    "INSERT INTO product_events (product_id, event, ip) VALUES (:p, :e, :ip)"
                ), {"p": pid, "e": event, "ip": ip})
    except Exception:
        _blog_log.exception("product_events: falha ao gravar")


@router.post("/api/track/event")
async def track_event(request: Request):
    """Do navegador: {product_id, event: 'cart' | 'phone'}."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False})
    event = str(body.get("event", ""))
    if (event not in ("cart", "phone") or _is_owner(request) or _is_bot(request.headers.get("user-agent", ""))
            or _is_datacenter(_client_ip(request))):
        return JSONResponse({"ok": True, "skip": True})
    await log_product_events([body.get("product_id")], event, _client_ip(request))
    return JSONResponse({"ok": True})

async def _ensure_visits_table():
    """Создаёт таблицу site_visits если не существует."""
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS site_visits (
                id SERIAL PRIMARY KEY,
                ip VARCHAR(64),
                page VARCHAR(200),
                referrer VARCHAR(500),
                user_agent VARCHAR(200),
                visited_at TIMESTAMPTZ DEFAULT NOW()
            )
        """))

@router.post("/api/track/visit")
async def track_visit(request: Request):
    try:
        body = await request.json()
        page = body.get("page", "/")
        referrer = body.get("referrer", "")[:500]
        ip = _client_ip(request)
        ua = request.headers.get("user-agent", "")[:200]
        now = datetime.now(BRT)
        now_str = now.strftime("%d/%m %H:%M")

        # Исключаем свои IP / navegador da loja (/eu)
        if _is_owner(request):
            return JSONResponse({"ok": True, "skip": True})

        # Один визит с IP раз в 24 часа (уникальные посетители в день)
        import time
        last = _visit_cache.get(ip, 0)
        is_new_today = time.time() - last > 86400
        if not is_new_today:
            return JSONResponse({"ok": True, "skip": True})
        _visit_cache[ip] = time.time()

        # Сохраняем в БД
        await _ensure_visits_table()
        async with engine.begin() as conn:
            await conn.execute(text("""
                INSERT INTO site_visits (ip, page, referrer, user_agent, visited_at)
                VALUES (:ip, :page, :referrer, :ua, :now)
            """), {"ip": ip, "page": page, "referrer": referrer,
                   "ua": ua, "now": now})

        # Определяем источник
        source = "direto"
        if referrer:
            if "google" in referrer: source = "🔍 Google"
            elif "facebook" in referrer or "instagram" in referrer: source = "📘 Facebook/IG"
            elif "whatsapp" in referrer: source = "💬 WhatsApp"
            else: source = f"🔗 {referrer[:40]}"

        # Não notificar visitas diretas (podem ser bots ou a própria equipe)
        if source == "direto":
            return JSONResponse({"ok": True, "skip": "direct"})

        device = "📱 mobile" if any(m in ua.lower() for m in ["mobile", "android", "iphone"]) else "🖥 desktop"
        await _send_tg(
            f"👀 <b>Novo visitante!</b>\n"
            f"Página: <b>{page}</b>\n"
            f"Fonte: {source}\n"
            f"Dispositivo: {device}\n"
            f"IP: <code>{ip}</code>\n"
            f"UA: <code>{ua[:80]}</code>\n"
            f"Horário: {now_str}"
        )
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False})


# ── DАЙДЖЕСТ DIÁRIO 20:00 BRT ──────────────────────────────────────────────────
async def _send_daily_digest():
    """Envia resumo diário às 20h BRT."""
    while True:
        now = datetime.now(BRT)
        # Calcula segundos até próximas 20:00
        target = now.replace(hour=20, minute=0, second=0, microsecond=0)
        if now >= target:
            from datetime import timedelta
            target = target + timedelta(days=1)
        wait = (target - now).total_seconds()
        await asyncio.sleep(wait)

        try:
            await _ensure_visits_table()
            async with engine.connect() as conn:
                visits_today = await conn.execute(text(
                    "SELECT COUNT(DISTINCT ip) FROM site_visits WHERE visited_at::date = CURRENT_DATE AND referrer != ''"
                ))
                wa_today = await conn.execute(text(
                    "SELECT COUNT(*) FROM catalog_clicks "
                    "WHERE clicked_at::date = CURRENT_DATE AND product_name != 'Telefone'"
                ))
                calls_today = await conn.execute(text(
                    "SELECT COUNT(*) FROM catalog_clicks "
                    "WHERE clicked_at::date = CURRENT_DATE AND product_name = 'Telefone'"
                ))
                top_products = await conn.execute(text("""
                    SELECT product_name, COUNT(*) as n
                    FROM catalog_clicks
                    WHERE clicked_at::date = CURRENT_DATE
                      AND product_name NOT IN ('Telefone', 'Geral', 'Banner comprar', 'Busca foto')
                    GROUP BY product_name ORDER BY n DESC LIMIT 5
                """))

                v = visits_today.scalar() or 0
                w = wa_today.scalar() or 0
                c = calls_today.scalar() or 0
                top = top_products.fetchall()

            today_str = datetime.now(BRT).strftime("%d/%m/%Y")
            top_txt = "\n".join([f"  • {r[0]} ({r[1]}x)" for r in top]) if top else "  —"

            await _send_tg(
                f"📊 <b>Resumo do dia — {today_str}</b>\n\n"
                f"👀 Visitantes: <b>{v}</b>\n"
                f"💬 Cliques WhatsApp: <b>{w}</b>\n"
                f"📞 Ligações: <b>{c}</b>\n\n"
                f"🏆 Produtos mais consultados:\n{top_txt}"
            )
        except Exception:
            pass

# Запускаем дайджест при старте (защита от двойного запуска)
_digest_started = False
@router.on_event("startup")
async def _start_digest():
    global _digest_started
    if _digest_started:
        return
    _digest_started = True
    asyncio.ensure_future(_send_daily_digest())


# ── ПРОМО-ЦЕНА ─────────────────────────────────────────────────────────────────
_promo_col_ready = False

async def _ensure_promo_column():
    """Колонку promo_price админка создаёт лениво; каталог проверяет сам, чтобы не упасть при деплое."""
    global _promo_col_ready
    if _promo_col_ready:
        return
    try:
        async with engine.begin() as conn:
            exists = (await conn.execute(text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'products' AND column_name = 'promo_price'"
            ))).first()
            if not exists:
                await conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS promo_price NUMERIC(10,2)"))
            # validade da promoção (opcional): depois desta data volta o preço normal sozinho
            await conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS promo_until DATE"))
        _promo_col_ready = True
    except Exception:
        _blog_log.exception("promo_price: não foi possível garantir a coluna")

@router.on_event("startup")
async def _startup_promo_column():
    await _ensure_promo_column()

# ── nomes bonitos no site (o banco fica como está) ───────────────────────────
import re as _re_nome
_GAS_RE = _re_nome.compile(r"\b[rR]-?(\d{2,3}[a-zA-Z]?)\b")

def _nice_name(name: str) -> str:
    """'gas refrigerante r32 3kg HULTER' -> 'Gas refrigerante R32 3kg HULTER'.
    Só a 1ª letra maiúscula + códigos de gás em maiúsculas; o slug não muda (slugify ignora maiúsculas)."""
    n = " ".join((name or "").split())
    n = _GAS_RE.sub(lambda m: "R" + m.group(0)[1:].upper(), n)
    return n[:1].upper() + n[1:] if n else n


def _apply_promo(r: dict) -> dict:
    """promo_price -> float; on_promo = промо действует (promo < sale); price = цена, по которой продаём сейчас."""
    sale, promo = r.get("sale_price"), r.get("promo_price")
    promo = float(promo) if promo is not None else None
    r["promo_price"] = promo
    r["on_promo"] = bool(promo and sale and promo < sale)
    # promo_until (DATE, opcional): vale até o fim desse dia no horário do Rio
    until = r.get("promo_until")
    r["promo_days_left"] = None
    if r["on_promo"] and until:
        left = (until - datetime.now(BRT).date()).days
        if left < 0:
            r["on_promo"] = False          # acabou: preço normal, sem precisar mexer na admin
        else:
            r["promo_days_left"] = left
    if hasattr(until, "isoformat"):
        r["promo_until"] = until.isoformat()
    r["price"] = promo if r["on_promo"] else sale
    if r.get("name"):
        r["name"] = _nice_name(r["name"])
    return r
# ─────────────────────────────────────────────────────────────────────────────


async def _get_products(conn):
    await _ensure_promo_column()
    res = await conn.execute(text("""
        SELECT p.id, p.name, p.sale_price, p.promo_price, p.promo_until, p.unit, p.image, p.description,
               p.google_category,
               COALESCE(c.name, 'Outro') AS category_name,
               c2.name AS category2_name,
               GREATEST(0, COALESCE(SUM(sm.qty), 0)) AS current_stock
        FROM products p
        LEFT JOIN categories c  ON c.id  = p.category_id
        LEFT JOIN categories c2 ON c2.id = p.category2_id
        LEFT JOIN stock_movements sm ON sm.product_id = p.id
        WHERE p.active = TRUE
        GROUP BY p.id, p.name, p.sale_price, p.promo_price, p.promo_until, p.unit, p.image, p.description,
               p.google_category, c.name, c2.name
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
        _apply_promo(r)
        r["current_stock"] = float(r.get("current_stock") or 0)
        result.append(r)
    return result


async def _get_banner_product(conn):
    await _ensure_promo_column()
    res = await conn.execute(text("""
        SELECT p.id, p.name, p.sale_price, p.promo_price, p.promo_until, p.unit, p.image,
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
    _apply_promo(r)
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
    await _ensure_promo_column()
    res = await conn.execute(text("""
        SELECT p.id, p.name, p.sale_price, p.promo_price, p.promo_until, p.unit, p.image,
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
    _apply_promo(r)
    return r


# ── 301 РЕДИРЕКТЫ для старых URL ──────────────────────────────────────────────
from fastapi.responses import RedirectResponse as _Redirect

@router.get("/category/refrigeracao")
async def redirect_category_refrigeracao():
    return _Redirect("/category/gases-refrigerantes", status_code=301)

@router.get("/product/protetor-termico-embraco")
async def redirect_protetor_termico():
    return _Redirect("/product/protetor-termico-ptc", status_code=301)

# Blog: slugs antigos de artigos -> slug atual (canonical mudou em out/2026)
_BLOG_OLD_SLUGS = {
    "sensor-ntc-geladeira":        "sensor-de-temperatura-geladeira",
    "timer-degelo":                "timer-de-degelo-refrigeracao",
    "timer-degelo-refrigeracao":   "timer-de-degelo-refrigeracao",
    "timer-de-degelo-refrigacao":  "timer-de-degelo-refrigeracao",
    "timer-de-degelo-refrigeração": "timer-de-degelo-refrigeracao",
}

for _old, _new in _BLOG_OLD_SLUGS.items():
    def _mk(dest):
        async def _r():
            return _Redirect(f"/blog/{dest}", status_code=301)
        return _r
    router.add_api_route(f"/blog/{_old}", _mk(_new), methods=["GET"], include_in_schema=False)

# (gas-r32-3kg agora está em _SLUG_SEED — o destino antigo também tinha mudado de nome)


# ── URLs antigas de produto (o slug vem do NOME: renomear = URL nova) ─────────
# 1) _SLUG_SEED: URLs antigas que o Google tinha indexadas (Search Console, out/2026) -> id do produto
# 2) product_slug_history: daqui pra frente, cada slug que já existiu fica guardado -> 301 automático
_SLUG_SEED = {
    "gas-r22-700g": 23,
    "gas-r32-3kg-jcm": 145,
    "gas-r22": 28,
    "botija-gas-r404-10kg-jcm": 317,
    "botija-r22-13kg": 298,
    "gas-r32-650g": 21,
    "botija-r410-10kg": 299,
    "gas-r32-3kg-hulter": 241,
    "gas-r32-3kg": 241,
    "termostato-electrolux-dc49a": 269,
    "tubo-de-1-4": 44,
    "sensor-de-temperatura-10k-para-geladeira-electrolux": 72,
    "gas-r134-1kg": 26,
    "gas-r410": 27,
    "gas-r134": 26,
    "capacitor-15uf": 254,
    "extrator-de-nucleos-valvula-schrader-servico-sem-perder-o-gas-hsv-3-hiatsu": 185,
    "conjunto-analisador-manifold-ecotools-et361-90cm": 327,
    "sensor-temperatura-e-degelo-ar-condicionado-5k": 80,
    "gas-para-macarico": 18,
    "cartucho-gas-mapp-para-macarico-400g-onu1077-classe-2-1-propileno-eos": 18,
    "compressor-embraco-1-4-plus-127v-emr80hlr-gas-r134a": 342,
    "gas-r290-400g": 24,
    "tubo-de-3-8": 45,
    "tubo-de-3-4": 46,
    "tubo-de-1-2": 43,
    "gas-r134-750g": 19,
    "esponjoso-1-4": 88,
    "esponjoso-1-2": 90,
    "esponjoso-3-4": 59,
    "esponjoso-5-8": 30,
    "mangueira-transparente": 305,
    "termostato-geladeira-consul-w11082462-original": 31,
    "termostato-geladeira-consul": 31,
    "manta-anti-chame": 172,
    "sensor-5k-split": 82,
    "fluido-refrigerante-r32-3kg-hulter": 241,
    "gas-r410-750g": 38,
    "sensor-biometal-com-fusivel": 85,
    "termostato-ar-condicionado-brastemp-7500-30000-btus-rcv1601-4p": 204,
    "gas-refrigerante-r134-13kg": 348,
    "sensor-de-temperatura-10k-d46-e-df50-compativel-com-70000949": 359,
    "eletrobomba": 37,
    "fluido-refrigerante-r134a-tetrafluoretano-750g-hulter": 19,
    "sensor-10k-symphony": 74,
    "fluido-refrigerante-r600-420g-hulter": 22,
    "gas-r600-420g": 22,
    "fluido-refrigerante-r134a-lata-750g-rlx": 357,
    "fluido-refrigerante-r32-650g-friven": 21,
    "resistencia-evoporador-br-cr-kr-225w-127v": 367,
    "rele-klixon-compressor-geladeira": 66,
    "manqueira-transparente-r22-1-5m-unidade": 266,
    "fluido-refrigerante-r290-400g-friven": 24,
}
_slug_known: set = set()


async def _remember_slugs(rows):
    """Guarda os slugs atuais. Se o produto for renomeado, a URL antiga continua levando a ele."""
    novos = {}
    for r in rows:
        sl = slugify(r["name"])
        if sl not in _slug_known:
            novos[sl] = r["id"]
    if not novos:
        return
    try:
        async with engine.begin() as conn:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS product_slug_history (
                    slug TEXT PRIMARY KEY, product_id INT NOT NULL, seen_at TIMESTAMPTZ DEFAULT NOW())
            """))
            await conn.execute(text("""
                INSERT INTO product_slug_history (slug, product_id) VALUES (:s, :i)
                ON CONFLICT (slug) DO UPDATE SET product_id = EXCLUDED.product_id, seen_at = NOW()
            """), [{"s": k, "i": v} for k, v in novos.items()])
        _slug_known.update(novos)
    except Exception:
        _blog_log.exception("slugs: falha ao salvar histórico")


async def _old_slug_target(slug: str, rows) -> str | None:
    """Slug antigo/variante -> slug atual do mesmo produto (ou None)."""
    current = {slugify(r["name"]): r for r in rows}
    s2 = slugify(slug)                       # acento, maiúscula etc.: /product/manifold-simples-de-alta-pressão
    if s2 != slug and s2 in current:
        return s2
    pid = _SLUG_SEED.get(s2)
    if pid is None:
        try:
            async with engine.connect() as conn:
                pid = (await conn.execute(text("SELECT product_id FROM product_slug_history WHERE slug = :s"),
                                          {"s": s2})).scalar()
        except Exception:
            pid = None
    if pid is None:
        return None
    for sl, r in current.items():
        if r["id"] == pid:
            return sl if sl != slug else None
    return None


def _promo_list(rows, limit: int = 12) -> list:
    """Produtos em promoção (promo_price < sale_price): em estoque primeiro, maior desconto primeiro."""
    promos = []
    for r in rows:
        if r.get("on_promo") and r.get("sale_price"):
            promos.append({
                "id": r["id"], "name": r["name"], "slug": slugify(r["name"]), "image": r.get("image") or "",
                "unit": r.get("unit") or "", "sale_price": r["sale_price"], "price": r["price"],
                "off": round((1 - r["price"] / r["sale_price"]) * 100),
                "days_left": r.get("promo_days_left"),
                "in_stock": float(r.get("current_stock") or 0) > 0,
            })
    promos.sort(key=lambda x: (not x["in_stock"], -x["off"]))
    return promos[:limit]


@router.get("/", response_class=HTMLResponse)
async def catalog_page(request: Request):
    await _ensure_promo_column()
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
            SELECT p.id, p.name, p.sale_price, p.promo_price, p.promo_until, p.unit, p.image,
                   COALESCE(c.name, 'Outro') AS category_name
            FROM products p
            LEFT JOIN categories c ON c.id = p.category_id
            WHERE p.active = TRUE AND p.image IS NOT NULL AND p.sale_price IS NOT NULL
            ORDER BY (p.promo_price IS NOT NULL AND p.promo_price < p.sale_price
                      AND (p.promo_until IS NULL OR p.promo_until >= CURRENT_DATE)) DESC, RANDOM()
            LIMIT 3
        """))
        banners_raw = []
        for row in banners_res.mappings().all():
            r = dict(row)
            if r.get("image") and r["image"].startswith("/static/images/"):
                r["image"] = ADMIN_URL + r["image"]
            if r.get("sale_price") is not None:
                r["sale_price"] = float(r["sale_price"])
            _apply_promo(r)
            r["slug"] = slugify(r["name"])
            r["cat_slug"] = slugify(r["category_name"])
            banners_raw.append(r)

        promo_product = await _get_promo_product(conn)
        all_rows = await _get_products_cached(conn)

    promos = _promo_list(all_rows)

    categories = [
        {"name": c["name"], "total": c["total"], "slug": slugify(c["name"])}
        for c in categories_raw
    ]

    clean = _json.loads(_json.dumps(
        {"categories": categories, "banners": banners_raw, "promo_product": promo_product, "promos": promos},
        default=str
    ))

    return templates.TemplateResponse("catalog.html", {
        "request":       request,
        "categories":    clean["categories"],
        "banners":       clean["banners"],
        "promo_product": clean["promo_product"],
        "promos":        clean["promos"],
        "total_products": len(all_rows),
        "ga_id":         GA_ID,
        "site_url":      SITE_URL,
        "store_address": STORE_ADDRESS,
        "store_phone":   STORE_PHONE,
        "wa_number":     WA_OWNER_NUMBER,
    })


# ── SEO da página de produto: title, description e JSON-LD ────────────────────
# Tudo montado aqui (não no template) para o JSON sair sempre válido.
# Preço e estoque vêm do banco na hora — nunca ficam velhos no Google.
SEO_TITLE_MAX = 62
SEO_DESC_MAX  = 158


def _seo_brl(v: float) -> str:
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _seo_plain(html: str) -> str:
    t = _re_nome.sub(r"<[^>]+>", " ", html or "")
    t = t.replace("&nbsp;", " ").replace("&amp;", "&")
    return _re_nome.sub(r"\s+", " ", t).strip()


def _seo_fill(base: str, extra: str, limit: int) -> str:
    """base + pedaço do texto do produto, cortando em palavra inteira."""
    if not extra or len(base) >= limit - 20:
        return base[:limit].rstrip()
    room = limit - len(base) - 1
    if len(extra) <= room:
        return f"{base} {extra}"
    cut = extra[:room - 1].rsplit(" ", 1)[0].rstrip(" ,.;:-–")
    return f"{base} {cut}…"


def _gtin_ok(g: str) -> bool:
    if not g or not g.isdigit() or len(g) not in (8, 12, 13, 14):
        return False
    d = [int(c) for c in g]
    s = sum(x * (3 if i % 2 == 0 else 1) for i, x in enumerate(reversed(d[:-1])))
    return (10 - s % 10) % 10 == d[-1]


def _product_seo(p: dict, cat_slug: str) -> dict:
    name     = p["name"]
    price    = p.get("price") or 0
    in_stock = (p.get("current_stock") or 0) > 0 and price > 0
    promo    = bool(p.get("on_promo")) and price > 0
    unit     = p.get("unit") or "un"
    per      = f"/{unit}" if unit in ("kg", "m", "l") else ""
    url      = f"{SITE_URL}/product/{p['slug']}"
    text_    = _seo_plain(p.get("description") or "")

    # title: nome primeiro (é o que a pessoa buscou), depois o que faz clicar
    if price and in_stock:
        tail = [f" – {_seo_brl(price)}{per} | Pronta entrega RJ", f" – {_seo_brl(price)}{per} no RJ", f" | {_seo_brl(price)}{per}"]
        if promo:
            off = round((1 - price / p["sale_price"]) * 100)
            tail = [f" – {_seo_brl(price)}{per} (-{off}%) | Pronta entrega RJ", f" – {_seo_brl(price)}{per} (-{off}%) no RJ"] + tail
    else:
        tail = [" | M.T.F Refrigeração RJ", " no RJ"]
    title = next((name + t for t in tail if len(name + t) <= SEO_TITLE_MAX), name)

    # description: preço + diferencial local + texto do produto
    if price and in_stock:
        if promo:
            head = f"De {_seo_brl(p['sale_price'])} por {_seo_brl(price)}{per} no Pix"
            if p.get("promo_until"):
                d = p["promo_until"][:10].split("-")
                head += f" (até {d[2]}/{d[1]})"
            head += "."
        else:
            head = f"{name} por {_seo_brl(price)}{per} no Pix."
        head += " Pronta entrega: retire na Av. Brasil (Bonsucesso) ou receba no mesmo dia no RJ."
    else:
        head = f"{name} na M.T.F Refrigeração, Av. Brasil (Bonsucesso, RJ). Consulte disponibilidade pelo WhatsApp."
    description = _seo_fill(head, text_, SEO_DESC_MAX)

    # JSON-LD Product (merchant listing)
    image = p.get("image") or f"{SITE_URL}/static/og-catalog.jpg"
    if image.startswith("/"):
        image = SITE_URL + image
    offer = {
        "@type": "Offer",
        "url": url,
        "priceCurrency": "BRL",
        "availability": "https://schema.org/InStock" if in_stock else "https://schema.org/OutOfStock",
        "itemCondition": "https://schema.org/NewCondition",
        "seller": {"@type": "Organization", "name": "M.T.F Refrigeração", "url": SITE_URL + "/"},
        "areaServed": {"@type": "City", "name": "Rio de Janeiro"},
        "hasMerchantReturnPolicy": {
            "@type": "MerchantReturnPolicy",
            "applicableCountry": "BR",
            "returnPolicyCategory": "https://schema.org/MerchantReturnFiniteReturnWindow",
            "merchantReturnDays": 7,                       # direito de arrependimento (CDC art. 49)
            "returnMethod": "https://schema.org/ReturnInStore",
            "returnFees": "https://schema.org/FreeReturn",
        },
    }
    if price:
        offer["price"] = f"{price:.2f}"
        if promo and p.get("promo_until"):
            offer["priceValidUntil"] = p["promo_until"][:10]
    prod = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": name,
        "url": url,
        "image": [image],
        "description": (text_ or description)[:5000],
        "sku": (p.get("sku") or "").strip() or str(p["id"]),
        "brand": {"@type": "Brand", "name": (p.get("brand") or "").strip() or "M.T.F Refrigeração"},
        "category": p.get("category_name") or "",
        "offers": offer,
    }
    if (p.get("sku") or "").strip():
        prod["mpn"] = p["sku"].strip()
    gtin = (p.get("gtin") or "").strip()
    if _gtin_ok(gtin):
        prod["gtin"] = gtin

    crumbs = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Início", "item": SITE_URL + "/"},
            {"@type": "ListItem", "position": 2, "name": p.get("category_name") or "Catálogo",
             "item": f"{SITE_URL}/category/{cat_slug}"},
            {"@type": "ListItem", "position": 3, "name": name, "item": url},
        ],
    }
    dump = lambda o: _json.dumps(o, ensure_ascii=False).replace("</", "<\\/")
    return {
        "title": title, "description": description, "url": url, "image": image,
        "in_stock": in_stock, "price": f"{price:.2f}" if price else "",
        "jsonld_product": dump(prod), "jsonld_crumbs": dump(crumbs),
    }


async def _product_ids(product_id: int) -> dict:
    """brand / sku / gtin (colunas criadas pela admin). Se faltar algo, segue sem."""
    try:
        async with engine.connect() as conn:
            r = (await conn.execute(text("SELECT brand, sku, gtin FROM products WHERE id = :id"),
                                    {"id": product_id})).mappings().first()
            return dict(r) if r else {}
    except Exception:
        return {}


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
        target = await _old_slug_target(slug, rows)
        if target:
            return _Redirect(f"/product/{target}", status_code=301)
        return HTMLResponse("Produto não encontrado", status_code=404)

    # Fire-and-forget трекинг просмотра
    ip = _client_ip(request)
    ua = request.headers.get("user-agent", "")
    if not _is_owner(request):
        asyncio.ensure_future(_track_product_view(product["id"], slug, ip, ua,
                                                  request.headers.get("accept-language", "")))

    related = []
    for r in rows:
        if r["category_name"] == product["category_name"] and slugify(r["name"]) != slug:
            rd = dict(r)
            rd["slug"] = slugify(rd["name"])
            related.append(rd)
            if len(related) >= 6:
                break

    product.update(await _product_ids(product["id"]))
    seo = _product_seo(product, slugify(product["category_name"]))

    return templates.TemplateResponse("product.html", {
        "request":       request,
        "product":       product,
        "seo":           seo,
        "related":       related,
        "cat_slug":      slugify(product["category_name"]),
        "cat2_slug":     slugify(product["category2_name"]) if product.get("category2_name") else "",
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

    products.sort(key=lambda x: x["name"].lower())

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


@router.get("/categories", response_class=HTMLResponse)
async def categories_page(request: Request):
    async with engine.connect() as conn:
        cats_res = await conn.execute(text("""
            SELECT COALESCE(c.name, 'Outro') AS name, COUNT(p.id) AS total,
                   MIN(p.image) AS image
            FROM products p
            LEFT JOIN categories c ON c.id = p.category_id
            WHERE p.active = TRUE
            GROUP BY c.name
            ORDER BY
                CASE WHEN c.name ILIKE '%gas%' OR c.name ILIKE '%gás%' THEN 0 ELSE 1 END,
                c.name NULLS LAST
        """))
        categories_raw = []
        for row in cats_res.mappings().all():
            r = dict(row)
            if r.get("image") and r["image"].startswith("/static/images/"):
                r["image"] = ADMIN_URL + r["image"]
            categories_raw.append(r)

    categories = [
        {"name": c["name"], "total": c["total"],
         "slug": slugify(c["name"]), "image": c.get("image")}
        for c in categories_raw
    ]

    return templates.TemplateResponse("categories.html", {
        "request":       request,
        "categories":    categories,
        "ga_id":         GA_ID,
        "site_url":      SITE_URL,
        "store_address": STORE_ADDRESS,
        "store_phone":   STORE_PHONE,
        "wa_number":     WA_OWNER_NUMBER,
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
        {"groups": groups, "cat_slugs": cat_slugs, "banner_product": banner_product, "promo_product": promo_product,
         "promos": _promo_list(rows)},
        default=str
    ))

    return templates.TemplateResponse("catalogo.html", {
        "request":        request,
        "groups":         clean["groups"],
        "total":          total,
        "categories":     sorted(
            [{"name": k, "slug": slugify(k)} for k in clean["groups"].keys()],
            key=lambda c: (
                0 if c["name"].lower() == "ar condicionado" else
                1 if c["name"].lower() == "geladeira" else
                2 if "lavar" in c["name"].lower() else
                3
            )
        ),
        "cat_slugs":      clean["cat_slugs"],
        "ga_id":          GA_ID,
        "site_url":       SITE_URL,
        "store_address":  STORE_ADDRESS,
        "store_phone":    STORE_PHONE,
        "wa_number":      WA_OWNER_NUMBER,
        "banner_product": clean["banner_product"],
        "promo_product":  clean["promo_product"],
        "promos":         clean["promos"],
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


@router.get("/parceiros/douglas")
async def parceiro_douglas():
    # Douglas não faz mais parte da loja: página antiga vai para "Sobre"
    return _Redirect("/sobre", status_code=301)

@router.get("/parceiros/ksenia", response_class=HTMLResponse)
async def parceiro_ksenia(request: Request):
    return templates.TemplateResponse("parceiro-ksenia.html", {
        "request":       request,
        "ga_id":         GA_ID,
        "site_url":      SITE_URL,
        "store_address": STORE_ADDRESS,
        "store_phone":   STORE_PHONE,
        "wa_number":     WA_OWNER_NUMBER,
    })

@router.get("/parceiros/fagal", response_class=HTMLResponse)
async def parceiro_fagal(request: Request):
    return templates.TemplateResponse("parceiro-fagal.html", {
        "request":       request,
        "ga_id":         GA_ID,
        "site_url":      SITE_URL,
        "store_address": STORE_ADDRESS,
        "store_phone":   STORE_PHONE,
        "wa_number":     WA_OWNER_NUMBER,
    })


@router.get("/ajuda", response_class=HTMLResponse)
async def ajuda_page(request: Request):
    return templates.TemplateResponse("ajuda.html", {
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

# ── CALCULADORA DE INSTALAÇÃO (kit) ──────────────────────────────────────────
_kit_settings_ready = False


async def _ensure_kit_settings():
    global _kit_settings_ready
    if _kit_settings_ready:
        return
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS kit_settings (
                id SMALLINT PRIMARY KEY DEFAULT 1,
                discount_percent NUMERIC(5,2) NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT kit_settings_single CHECK (id = 1)
            )
        """))
        await conn.execute(text(
            "INSERT INTO kit_settings (id, discount_percent) VALUES (1, 0) ON CONFLICT (id) DO NOTHING"
        ))
    _kit_settings_ready = True


@router.get("/calculadora-instalacao", response_class=HTMLResponse)
async def calculadora_instalacao_page(request: Request):
    return templates.TemplateResponse("calculadora-instalacao.html", {
        "request":       request,
        "ga_id":         GA_ID,
        "site_url":      SITE_URL,
        "store_address": STORE_ADDRESS,
        "store_phone":   STORE_PHONE,
        "wa_number":     WA_OWNER_NUMBER,
    })


@router.get("/api/kit-config")
async def api_kit_config():
    """Desconto (%) do kit de instalação, definido na admin (/kit-discount)."""
    pct = 0.0
    try:
        await _ensure_kit_settings()
        async with engine.connect() as conn:
            res = await conn.execute(text("SELECT discount_percent FROM kit_settings WHERE id = 1"))
            pct = float(res.scalar() or 0)
    except Exception:
        _blog_log.exception("kit-config: falha ao ler desconto")
    return JSONResponse({"discount_percent": pct}, headers={"Cache-Control": "no-store"})


# ── LALAMOVE: FRETE ───────────────────────────────────────────────────────────
LALA_DEBUG_TOKEN = os.environ.get("LALA_DEBUG_TOKEN", "")  # usado só pelo simular-pago (checkout.py)
LALA_SERVICES = set(os.environ.get("LALA_SERVICES", "LALAGO,LALAPRO,HATCHBACK").split(","))
_frete_hits: dict = {}  # ip -> [timestamps], anti-abuso

@router.post("/api/frete")
async def api_frete(request: Request):
    import time
    ip = _client_ip(request)
    now = time.time()
    hits = [t for t in _frete_hits.get(ip, []) if now - t < 600]
    if len(hits) >= 10:
        return JSONResponse({"error": "Muitas consultas. Tente novamente em alguns minutos."}, status_code=429)
    hits.append(now)
    _frete_hits[ip] = hits

    try:
        body = await request.json()
        lat, lng = float(body["lat"]), float(body["lng"])
        address = str(body.get("address", ""))[:300]
    except (KeyError, ValueError, TypeError):
        return JSONResponse({"error": "Endereço inválido"}, status_code=400)
    service = body.get("service", "LALAGO")
    if service not in LALA_SERVICES:
        return JSONResponse({"error": "Tipo de entrega indisponível"}, status_code=400)

    try:
        q = await get_quote(lat, lng, address, service)
    except LalamoveError as e:
        _blog_log.warning("lalamove quote: %s", e)
        return JSONResponse({"error": "Não foi possível calcular o frete para este endereço"}, status_code=502)

    return JSONResponse({
        "price": float(q["price"]),
        "distance_km": round(int(q["distance_m"] or 0) / 1000, 1),
        "service": service,
    }, headers={"Cache-Control": "no-store"})

# ── FRETE POR CEP (página do produto) ─────────────────────────────────────────
_geo_cache: dict = {}    # cep -> dados do endereço + coordenadas (não muda)
_quote_cache: dict = {}  # (cep, service) -> (ts, resposta), 10 min

def _frete_rate_ok(ip: str) -> bool:
    import time
    now = time.time()
    hits = [t for t in _frete_hits.get(ip, []) if now - t < 600]
    if len(hits) >= 10:
        return False
    hits.append(now)
    _frete_hits[ip] = hits
    return True

GEO_MAX_KM_FROM_BAIRRO = 5.0   # bairros grandes (Campo Grande) precisam de folga


def _km(a, b) -> float:
    """Distância em linha reta (km) entre (lat, lng)."""
    import math
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def _google_key() -> str:
    return (os.environ.get("GOOGLE_GEOCODING_KEY") or os.environ.get("GOOGLE_MAPS_KEY") or "").strip()


_GOOGLE_VAGUE = {"locality", "administrative_area_level_1", "administrative_area_level_2", "country"}
_GOOGLE_GOOD = {"street_address", "premise", "subpremise", "route", "postal_code", "sublocality",
                "sublocality_level_1", "neighborhood", "establishment", "point_of_interest"}


async def _google_geocode(client, address: str, cep: str = "", precise: bool = False):
    """Google Geocoding -> ((lat, lng), location_type, types) ou None.
    Recusa resultado que seja só a cidade (o problema do "centro do Rio").
    precise=True: só aceita endereço com número (ROOFTOP / RANGE_INTERPOLATED)."""
    key = _google_key()
    if not key:
        return None
    attempts = []
    if cep:
        attempts.append(f"country:BR|postal_code:{cep[:5]}-{cep[5:]}")
    attempts.append("country:BR")
    for comp in attempts:
        try:
            r = await client.get("https://maps.googleapis.com/maps/api/geocode/json", params={
                "address": address, "components": comp, "language": "pt-BR", "region": "br", "key": key,
            })
            j = r.json() if r.status_code == 200 else {}
        except Exception:
            _blog_log.exception("geo: google falhou (%s)", address)
            return None
        st = j.get("status")
        if st == "ZERO_RESULTS":
            continue
        if st != "OK":
            _blog_log.warning("geo: google %s %s (%s)", st, (j.get("error_message") or "")[:150], address)
            return None
        res0 = j["results"][0]
        types = set(res0.get("types") or [])
        loc_type = res0["geometry"].get("location_type", "")
        if (types & _GOOGLE_VAGUE) and not (types & _GOOGLE_GOOD):
            _blog_log.warning("geo: google impreciso %s (%s)", sorted(types), address)
            continue          # tenta a próxima variante (sem filtro de CEP)
        if precise and loc_type not in ("ROOFTOP", "RANGE_INTERPOLATED"):
            continue          # ex.: cliente errou 1 dígito do CEP -> sem filtro acha o número exato
        loc = res0["geometry"]["location"]
        try:
            return (float(loc["lat"]), float(loc["lng"])), loc_type, types
        except (KeyError, TypeError, ValueError):
            return None
    return None


async def geocode_endereco(rua: str, numero: str, bairro: str, cidade: str, uf: str, cep: str):
    """Endereço completo COM NÚMERO -> (lat, lng) exato para o entregador, ou None.
    Usado no checkout: o frete é calculado pelo CEP, mas o pino da Lalamove vai na porta do cliente."""
    address = ", ".join(x for x in [f"{rua}, {numero}", bairro, f"{cidade} - {uf}", f"{cep[:5]}-{cep[5:]}"] if x)
    async with httpx.AsyncClient(timeout=8) as client:
        g = await _google_geocode(client, address, cep, precise=True)
    if g:
        _blog_log.info("geo: %s -> %s (%s)", address, g[0], g[1])
        return g[0]
    return None


async def _cep_address(client, cep: str):
    """(rua, bairro, cidade, uf) do CEP. None = CEP inexistente.
    Levanta RuntimeError se NENHUMA fonte respondeu (aí o site mostra "tente novamente")."""
    not_found = 0
    sources = [
        ("viacep", f"https://viacep.com.br/ws/{cep}/json/",
         lambda j: None if j.get("erro") else (j.get("logradouro"), j.get("bairro"), j.get("localidade"), j.get("uf"))),
        ("brasilapi", f"https://brasilapi.com.br/api/cep/v2/{cep}",
         lambda j: (j.get("street"), j.get("neighborhood"), j.get("city"), j.get("state")) if j.get("city") else None),
        ("awesomeapi", f"https://cep.awesomeapi.com.br/json/{cep}",
         lambda j: (j.get("address"), j.get("district"), j.get("city"), j.get("state")) if j.get("city") else None),
    ]
    for name, url, parse in sources:
        try:
            r = await client.get(url)
        except Exception as e:
            _blog_log.warning("frete-cep: %s fora do ar (%s): %s", name, cep, type(e).__name__)
            continue
        if r.status_code in (400, 404):
            not_found += 1
            continue
        if r.status_code != 200:
            _blog_log.warning("frete-cep: %s respondeu %s (%s)", name, r.status_code, cep)
            continue
        try:
            got = parse(r.json())
        except Exception:
            continue
        if got is None:
            not_found += 1
            continue
        return tuple((x or "").strip() for x in got)
    if not_found:
        return None
    raise RuntimeError(f"nenhuma fonte de CEP respondeu para {cep}")


async def _google_cep_lookup(client, cep: str):
    """Reserva para CEP que ViaCEP/BrasilAPI/AwesomeAPI não conhecem (praças, CEPs novos ou de prédio).
    Pergunta ao Google pelo próprio CEP. Retorna (rua, bairro, cidade, uf, (lat, lng)) ou None."""
    key = _google_key()
    if not key:
        return None
    try:
        r = await client.get("https://maps.googleapis.com/maps/api/geocode/json", params={
            "components": f"country:BR|postal_code:{cep[:5]}-{cep[5:]}",
            "language": "pt-BR", "region": "br", "key": key,
        })
        j = r.json() if r.status_code == 200 else {}
    except Exception:
        _blog_log.exception("geo: google cep falhou (%s)", cep)
        return None
    if j.get("status") != "OK" or not j.get("results"):
        return None
    res0 = j["results"][0]
    comps = {}
    for c in res0.get("address_components") or []:
        for t in c.get("types") or []:
            comps.setdefault(t, c)
    g = lambda t, k="long_name": ((comps.get(t) or {}).get(k) or "").strip()
    pc = g("postal_code").replace("-", "")
    if not (pc == cep or (len(pc) == 5 and cep.startswith(pc))):
        _blog_log.warning("geo: google devolveu CEP %s para %s — ignorado", pc, cep)
        return None
    uf = g("administrative_area_level_1", "short_name")
    cidade = g("administrative_area_level_2") or g("locality")
    if not uf or not cidade:
        return None
    bairro = g("sublocality_level_1") or g("sublocality") or g("neighborhood")
    loc = res0["geometry"]["location"]
    return g("route"), bairro, cidade, uf, (float(loc["lat"]), float(loc["lng"]))


async def _cep_to_coords(cep: str):
    """CEP -> endereço (ViaCEP) + coordenadas: Google Geocoding; reserva AwesomeAPI/BrasilAPI/Nominatim conferidos pelo bairro."""
    if cep in _geo_cache:
        return _geo_cache[cep]

    def _ok(lat, lng):
        try:
            lat, lng = float(lat), float(lng)
            return (lat, lng) if lat and lng else None
        except (TypeError, ValueError):
            return None

    async with httpx.AsyncClient(timeout=8, headers={"User-Agent": "MTF-Refrigeracao/1.0 (frete)"}) as client:
        # 1. Endereço: ViaCEP -> BrasilAPI -> AwesomeAPI (qualquer um pode cair; não dependemos de um só)
        addr = await _cep_address(client, cep)
        if addr is None:
            # ViaCEP & cia. não conhecem (ex.: 20021-200, Praça Marechal Âncora). Última tentativa: Google.
            g = await _google_cep_lookup(client, cep)
            if not g:
                return None                  # CEP não existe mesmo
            street, bairro, cidade, uf, c = g
            address = ", ".join(x for x in [street, bairro, f"{cidade} - {uf}", f"{cep[:5]}-{cep[5:]}"] if x)
            _blog_log.info("frete-cep: %s -> %s via google-cep-reserva (%s)", cep, c, address)
            res = {"lat": c[0], "lng": c[1], "address": address, "rua": street,
                   "bairro": bairro, "cidade": cidade, "uf": uf}
            _geo_cache[cep] = res
            return res
        street, bairro, cidade, uf = addr
        address = ", ".join(x for x in [street, bairro, f"{cidade} - {uf}", f"{cep[:5]}-{cep[5:]}"] if x)

        coords, source = None, None
        candidates = []   # (coords, fonte) na ordem de preferência

        async def _nominatim(params):
            try:
                n = await client.get("https://nominatim.openstreetmap.org/search",
                                     params={**params, "format": "json", "limit": 1, "countrycodes": "br"})
                if n.status_code == 200 and n.json():
                    return _ok(n.json()[0].get("lat"), n.json()[0].get("lon"))
            except Exception:
                pass
            return None

        # 1b. Google. Rua sozinha NÃO serve: para avenida longa (Av. Brasil = 58 km) o Google devolve
        #     o meio da avenida inteira. Por isso: (a) o próprio CEP, (b) a rua, conferidos pelo bairro.
        if _google_key():
            cep_fmt = f"{cep[:5]}-{cep[5:]}"
            g_bairro = await _google_geocode(client, f"{bairro}, {cidade} - {uf}") if bairro else None
            ref_g = g_bairro[0] if g_bairro and g_bairro[2] & {"neighborhood", "sublocality", "sublocality_level_1"} else None
            # cidade do Rio vai no máximo ~55 km do Centro; "bairro" fora disso é homônimo de outra cidade
            if ref_g and cidade.strip().lower() == "rio de janeiro" and _km(ref_g, (-22.9068, -43.1729)) > 60:
                _blog_log.info("frete-cep: %s bairro %s do Google fora da cidade %s — ignorado", cep, bairro, ref_g)
                ref_g = None
            picks = []
            g_cep = await _google_geocode(client, f"{cep_fmt}, {cidade} - {uf}, Brasil", cep)
            if g_cep and g_cep[2] & {"postal_code", "street_address", "premise"}:
                picks.append((g_cep[0], f"google-cep ({g_cep[1]})"))
            if street:
                g_rua = await _google_geocode(client, address, cep)
                if g_rua:
                    picks.append((g_rua[0], f"google-rua ({g_rua[1]})"))
            chosen = None
            # CEP e rua concordam entre si (duas consultas independentes) -> confiamos neles,
            # mesmo que o "bairro" do Google esteja longe. Bairro com nome comum ("Centro")
            # às vezes vira o Centro de OUTRA cidade (ex.: Três Rios, 87 km) e descartava o ponto certo.
            if len(picks) >= 2 and _km(picks[0][0], picks[1][0]) <= 1.5:
                chosen = picks[0]
                if ref_g and _km(picks[0][0], ref_g) > GEO_MAX_KM_FROM_BAIRRO:
                    _blog_log.info("frete-cep: %s bairro do Google %s ignorado (CEP e rua concordam)", cep, ref_g)
            for c, src in ([] if chosen else picks):
                if ref_g and _km(c, ref_g) > GEO_MAX_KM_FROM_BAIRRO:
                    _blog_log.info("frete-cep: %s descartado %s %s (%.1f km do bairro %s)",
                                   cep, src, c, _km(c, ref_g), bairro)
                    continue
                if not ref_g and src.startswith("google-rua"):
                    continue      # sem bairro para conferir, rua é arriscado
                chosen = (c, src)
                break
            if not chosen and ref_g:
                chosen = (ref_g, "google-bairro")
            if chosen:
                c, src = chosen
                _blog_log.info("frete-cep: %s -> %s via %s", cep, c, src)
                res = {"lat": c[0], "lng": c[1], "address": address, "rua": street,
                       "bairro": bairro, "cidade": cidade, "uf": uf}
                _geo_cache[cep] = res
                return res

        # 2. AwesomeAPI (reserva, se o Google falhar)
        try:
            a = await client.get(f"https://cep.awesomeapi.com.br/json/{cep}")
            if a.status_code == 200:
                aj = a.json()
                c = _ok(aj.get("lat"), aj.get("lng"))
                if c:
                    candidates.append((c, "awesomeapi"))
        except Exception:
            pass

        # 3. BrasilAPI v2
        try:
            b = await client.get(f"https://brasilapi.com.br/api/cep/v2/{cep}")
            if b.status_code == 200:
                loc = ((b.json().get("location") or {}).get("coordinates") or {})
                c = _ok(loc.get("latitude"), loc.get("longitude"))
                if c:
                    candidates.append((c, "brasilapi"))
        except Exception:
            pass

        # 4. Nominatim (OpenStreetMap) pela rua
        if street:
            c = await _nominatim({"street": street, "city": cidade, "state": uf})
            if c:
                candidates.append((c, "nominatim-rua"))

        # 5. Referência: centro do bairro. As APIs de CEP às vezes devolvem o centro da
        #    cidade (Rio -> Centro), o que dá frete errado. Só aceitamos ponto perto do bairro.
        ref = None
        if bairro:
            ref = await _nominatim({"q": f"{bairro}, {cidade}, {uf}, Brasil"})
        if ref:
            near = [(c, src) for c, src in candidates if _km(c, ref) <= GEO_MAX_KM_FROM_BAIRRO]
            for c, src in candidates:
                if (c, src) not in near:
                    _blog_log.info("frete-cep: %s descartado %s %s (%.1f km do bairro %s)",
                                   cep, src, c, _km(c, ref), bairro)
            coords, source = near[0] if near else (ref, "nominatim-bairro")
        else:
            # sem referência do bairro não dá para conferir; a BrasilAPI costuma devolver
            # o centro da cidade, então sozinha ela não serve (frete sairia errado)
            trusted = [(c, src) for c, src in candidates if src != "brasilapi"]
            if trusted:
                coords, source = trusted[0]
            elif candidates:
                _blog_log.warning("frete-cep: %s só tem brasilapi %s sem bairro para conferir — ignorado",
                                  cep, candidates[0][0])

    if not coords:
        _blog_log.warning("frete-cep: sem coordenadas para %s (%s)", cep, address)
        raise RuntimeError(f"sem coordenadas para {cep}")

    _blog_log.info("frete-cep: %s -> %s via %s", cep, coords, source)
    res = {"lat": coords[0], "lng": coords[1], "address": address, "rua": street, "bairro": bairro, "cidade": cidade, "uf": uf}
    _geo_cache[cep] = res
    return res

FRETE_OPTIONS = {   # a ordem aqui é a ordem na tela
    "LALAGO":    ("Motoboy",      "Peças pequenas"),
    "HATCHBACK": ("Carro",        "Peças grandes ou pesadas"),
    "LALAPRO":   ("Moto com baú", "Caixas médias, protegidas da chuva"),
}

FRETE_MAX_KM = float(os.environ.get("FRETE_MAX_KM", "50"))

def _frete_combinar(geo: dict) -> dict:
    return {"combinar": True, "bairro": geo.get("bairro", ""), "cidade": geo.get("cidade", ""), "uf": geo.get("uf", "")}

async def _frete_cep_core(request: Request, cep: str = ""):
    import re as _re, time
    cep = _re.sub(r"\D", "", cep)
    if len(cep) != 8:
        return JSONResponse({"error": "Digite um CEP válido (8 dígitos)."}, status_code=400)

    cached = _quote_cache.get(cep)
    if cached and time.time() - cached[0] < 600:
        return JSONResponse(cached[1], headers={"Cache-Control": "no-store"})

    ip = _client_ip(request)
    if not _frete_rate_ok(ip):
        return JSONResponse({"error": "Muitas consultas. Tente novamente em alguns minutos."}, status_code=429)

    try:
        geo = await _cep_to_coords(cep)
    except Exception:
        _blog_log.exception("frete-cep: falha ao buscar CEP %s", cep)
        return JSONResponse({"error": "Não foi possível calcular o frete agora. Tente novamente."}, status_code=503)
    if not geo:
        return JSONResponse({"error": "CEP não encontrado."}, status_code=404)
    if geo["uf"] != "RJ":
        return JSONResponse(_frete_combinar(geo), headers={"Cache-Control": "no-store"})

    services = [s for s in FRETE_OPTIONS if s in LALA_SERVICES]
    results = await asyncio.gather(
        *(get_quote(geo["lat"], geo["lng"], geo["address"], s) for s in services),
        return_exceptions=True,
    )

    options, distance_m = [], 0
    for s, q in zip(services, results):
        if isinstance(q, Exception):
            _blog_log.warning("frete-cep: lalamove %s %s: %s", cep, s, q)
            continue
        label, hint = FRETE_OPTIONS[s]
        options.append({"service": s, "label": label, "hint": hint, "price": float(q["price"])})
        distance_m = distance_m or int(q["distance_m"] or 0)

    if not options or distance_m / 1000 > FRETE_MAX_KM:
        data = _frete_combinar(geo)
        _quote_cache[cep] = (time.time(), data)
        return JSONResponse(data, headers={"Cache-Control": "no-store"})

    data = {
        "rua": geo.get("rua", ""), "bairro": geo["bairro"], "cidade": geo["cidade"],
        "distance_km": round(distance_m / 1000, 1), "options": options,
    }
    _quote_cache[cep] = (time.time(), data)
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


# ── FRETE MAIS BARATO PARA PEDIDOS MAIORES ───────────────────────────────────
# Regra da loja: pedido (produtos) acima de FRETE_SUBSIDIO_MIN -> a loja paga parte do frete:
#   desconto = min(metade do frete, metade da margem do pedido).
# Margem = (preço pago - custo) dos itens; item sem custo cadastrado (NULL ou 0) conta margem 0.
# O cliente vê só o preço final do frete (sem aviso de desconto). Mesma conta no /api/frete-cep e no checkout.
FRETE_SUBSIDIO_MIN = float(os.environ.get("FRETE_SUBSIDIO_MIN", "50"))


async def margem_detalhe(linhas: list) -> tuple[float, list]:
    """linhas: [(product_id, qty, preço_unitário_pago)] -> (margem, nomes dos itens sem custo cadastrado)."""
    ids = list({int(p) for p, _, _ in linhas if p and int(p) > 0})
    if not ids:
        return 0.0, []
    async with engine.connect() as conn:
        rows = {x["id"]: x for x in (await conn.execute(text(
            "SELECT id, name, cost_price FROM products WHERE id = ANY(:ids)"), {"ids": ids})).mappings()}
    m, sem_custo = 0.0, []
    for pid, qty, preco in linhas:
        x = rows.get(int(pid))
        custo = float(x["cost_price"] or 0) if x else 0.0
        if custo <= 0:                     # sem custo cadastrado: não arrisca, margem 0
            if x and float(preco) > 0:
                sem_custo.append(x["name"])
            continue
        m += (float(preco) - custo) * float(qty)   # brinde (preço 0) entra negativo: só o custo
    return round(m, 2), sem_custo


async def margem_pedido(linhas: list) -> float:
    return (await margem_detalhe(linhas))[0]


def frete_final(preco_lalamove: float, subtotal: float, margem: float) -> float:
    if subtotal <= FRETE_SUBSIDIO_MIN or margem <= 0:
        return round(preco_lalamove, 2)
    desconto = min(preco_lalamove / 2, margem / 2)
    return round(preco_lalamove - desconto, 2)


def linhas_dos_itens(items: list) -> list:
    """Itens do checkout (_load_items) -> linhas (id, qty, preço pago). Kit da admin abre nos componentes."""
    out = []
    for it in items:
        if it.get("components"):
            n = float(it.get("qty") or 1)
            for c in it["components"]:
                q = float(c["qty"]) * n
                out.append((c["id"], q, (float(c["line_total"]) * n / q) if q else 0.0))
        else:
            out.append((it["id"], it["qty"], it["price"]))
    return out


# ── LOG DE CÁLCULOS DE FRETE (para entender se o preço da entrega espanta o cliente) ──
# Cada cálculo vai para a tabela frete_log + aviso no Telegram.
# Para ver quem calculou e depois comprou: JOIN com site_orders pelo CEP (ver consulta em frete_log_report.sql).
_frete_log_seen: dict = {}   # (ip, cep) -> ts, evita duplicar o mesmo cálculo em 10 min
_frete_log_table_ok = False

def _real_ip(request: Request) -> str:
    return _client_ip(request)

async def _frete_log(request: Request, cep: str, status: int, data: dict,
                     src: str, valor: float | None, pid: int | None, prod: str):
    global _frete_log_table_ok
    import time
    ip = _real_ip(request)
    if _is_owner(request):
        return
    now = time.time()
    if now - _frete_log_seen.get((ip, cep), 0) < 600:
        return
    _frete_log_seen[(ip, cep)] = now

    opts = data.get("options") or []
    prices = {o["service"]: o["price"] for o in opts}
    menor = min(prices.values()) if prices else None
    if status != 200:
        resultado = "erro"
    elif data.get("combinar"):
        resultado = "combinar"
    else:
        resultado = "ok"
    try:
        async with engine.begin() as conn:
            if not _frete_log_table_ok:
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS frete_log (
                        id          SERIAL PRIMARY KEY,
                        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        cep         VARCHAR(8),
                        bairro      VARCHAR(120),
                        cidade      VARCHAR(120),
                        distance_km NUMERIC(6,1),
                        menor_frete NUMERIC(10,2),
                        precos      JSONB,
                        resultado   VARCHAR(12),
                        origem      VARCHAR(20),
                        valor       NUMERIC(10,2),
                        product_id  INTEGER,
                        produto     VARCHAR(160),
                        ip          VARCHAR(64)
                    )"""))
                await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_frete_log_cep ON frete_log(cep, created_at)"))
                _frete_log_table_ok = True
            await conn.execute(text("""
                INSERT INTO frete_log (cep, bairro, cidade, distance_km, menor_frete, precos, resultado,
                                       origem, valor, product_id, produto, ip)
                VALUES (:cep, :bairro, :cidade, :km, :menor, CAST(:precos AS JSONB), :res,
                        :src, :valor, :pid, :prod, :ip)"""), {
                "cep": cep, "bairro": (data.get("bairro") or "")[:120], "cidade": (data.get("cidade") or "")[:120],
                "km": data.get("distance_km"), "menor": menor, "precos": _json.dumps(prices),
                "res": resultado, "src": src, "valor": valor, "pid": pid, "prod": prod, "ip": ip,
            })
    except Exception:
        _blog_log.exception("frete_log: falha ao gravar")

    def brl(v):
        return ("R$ %.2f" % v).replace(".", ",")
    lugar = ", ".join(x for x in (data.get("bairro"), data.get("cidade")) if x) or "?"
    cep_fmt = f"{cep[:5]}-{cep[5:]}"
    if resultado == "ok":
        base = {o["service"]: o.get("_lalamove") for o in opts}
        linhas = " · ".join(f"{FRETE_OPTIONS[s][0]} {brl(p)}" + (f" (Lalamove {brl(base[s])})" if base.get(s) else "")
                            for s, p in prices.items() if s in FRETE_OPTIONS)
        km = str(data.get('distance_km', '')).replace('.', ',')
        corpo = f"{lugar} · {km} km\n{linhas}"
    elif resultado == "combinar":
        corpo = f"{lugar} · frete a combinar (fora da área)"
    else:
        corpo = f"erro: {data.get('error', '')}"
    origem = {"produto": "página do produto", "checkout": "checkout", "home": "página inicial"}.get(src, src or "?")
    extra = ""
    if prod:
        extra = f"\n📦 {prod}"
    if valor:
        extra += f" ({brl(valor)})" if prod else f"\n🛒 carrinho {brl(valor)}"
        if menor:
            extra += f" · frete = {round(menor / valor * 100)}% do valor"
    calc = data.get("_calc")
    if calc and resultado == "ok":
        if calc["subtotal"] <= FRETE_SUBSIDIO_MIN:
            extra += f"\nℹ️ Pedido {brl(calc['subtotal'])} (até {brl(FRETE_SUBSIDIO_MIN)}): frete cheio"
        else:
            mg = calc.get("margem", 0)
            extra += f"\n💰 Pedido {brl(calc['subtotal'])} · lucro dos produtos {brl(mg)}"
            if mg > 0:
                # lucro que sobra em cada opção de entrega, depois da parte do frete que a loja paga
                partes = []
                for o in opts:
                    if o.get("_lalamove") is None:
                        continue
                    loja = max(0.0, float(o["_lalamove"]) - float(o["price"]))
                    nome = FRETE_OPTIONS.get(o["service"], (o["service"], ""))[0]
                    partes.append(f"{nome}: {brl(mg)} → <b>{brl(mg - loja)}</b> (loja paga {brl(loja)})")
                if partes:
                    extra += "\n📉 Lucro com o frete:\n  " + "\n  ".join(partes)
            else:
                extra += " · sem lucro conhecido: frete cheio"
            if calc.get("sem_custo"):
                extra += "\n⚠️ Sem custo cadastrado (margem 0): " + ", ".join(calc["sem_custo"][:5])
    msg = f"🚚 <b>Frete calculado</b> — {origem}\nCEP {cep_fmt} · {corpo}{extra}"
    await _send_tg(msg)


@router.get("/api/frete-cep")
async def api_frete_cep(request: Request, cep: str = "", src: str = "", valor: str = "",
                        pid: str = "", prod: str = "", itens: str = ""):
    import re as _re
    resp = await _frete_cep_core(request, cep)
    cep_d = _re.sub(r"\D", "", cep)
    data = {}
    if resp.status_code == 200:
        try:
            data = _json.loads(resp.body)
        except Exception:
            data = {}
    # frete mais barato (pedido > R$ 50): itens do carrinho (checkout) ou o produto da página (1 un)
    if data.get("options"):
        try:
            linhas, subtotal = [], 0.0
            if itens:
                from checkout import _load_items          # import tardio: checkout importa este módulo
                items, subtotal = await _load_items(_json.loads(itens)[:50])
                linhas = linhas_dos_itens(items)
            elif pid.isdigit():
                async with engine.connect() as conn:
                    x = (await conn.execute(text(
                        "SELECT id, name, sale_price, promo_price, promo_until FROM products "
                        "WHERE id = :id AND active = TRUE"), {"id": int(pid)})).mappings().first()
                if x and x["sale_price"]:
                    x = _apply_promo(dict(x, sale_price=float(x["sale_price"])))
                    subtotal = float(x["price"] or 0)
                    linhas = [(x["id"], 1, subtotal)]
            calc = {"subtotal": subtotal} if linhas else None
            if subtotal > FRETE_SUBSIDIO_MIN and linhas:
                margem, sem_custo = await margem_detalhe(linhas)
                calc.update(margem=margem, sem_custo=sem_custo)
                novas = []
                for o in data["options"]:
                    p = frete_final(o["price"], subtotal, margem)
                    novas.append(dict(o, price=p))
                if any(n["price"] != o["price"] for n, o in zip(novas, data["options"])):
                    log_opts = [dict(n, _lalamove=o["price"]) for n, o in zip(novas, data["options"])]
                    data = dict(data, options=novas)
                    resp = JSONResponse(data, headers={"Cache-Control": "no-store"})
                    data = dict(data, options=log_opts)
            if calc:
                data = dict(data, _calc=calc)
        except Exception:
            _blog_log.exception("frete: falha ao aplicar desconto do frete (segue preço cheio)")
    if len(cep_d) == 8 and resp.status_code != 429:
        if not data:
            try:
                data = _json.loads(resp.body)
            except Exception:
                data = {}
        try:
            v = float(valor.replace(",", ".")) if valor else None
        except ValueError:
            v = None
        p = int(pid) if pid.isdigit() else None
        asyncio.create_task(_frete_log(request, cep_d, resp.status_code, data,
                                       src[:20], v, p, prod[:160]))
    return resp


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
        "componentes":      "🌡️",
    }
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

    site_url = SITE_URL.rstrip("/")  # evita "//blog/..." no canonical se SITE_URL termina em "/"

    # Produtos com preço/estoque/promoção para os cartões dos artigos (blog/_produto.html).
    # Slugs antigos (produto renomeado) também resolvem: _SLUG_SEED + product_slug_history.
    # Se o banco falhar, o artigo renderiza normalmente, apenas sem produtos.
    blog_ctx = {"products": {}, "schema_product": None}
    try:
        async with engine.connect() as conn:
            rows = await _get_products_cached(conn)
            aliases = dict(_SLUG_SEED)
            try:
                for r in (await conn.execute(text("SELECT slug, product_id FROM product_slug_history"))).mappings():
                    aliases[r["slug"]] = r["product_id"]
            except Exception:
                pass   # tabela ainda não existe: fica só com _SLUG_SEED
        blog_ctx = build_article_context(article, rows, site_url, slugify, aliases)
        if blog_ctx.get("missing"):
            _blog_log.warning("blog/%s: slugs de produto NÃO encontrados no catálogo: %s",
                              slug, blog_ctx["missing"])
    except Exception:
        _blog_log.exception("blog/%s: falha ao carregar produtos (artigo abre sem produtos)", slug)

    return templates.TemplateResponse(article["template"], {
        "request":       request,
        "article":       article,
        "ga_id":         GA_ID,
        "site_url":      site_url,
        "store_address": STORE_ADDRESS,
        "store_phone":   STORE_PHONE,
        "wa_number":     WA_OWNER_NUMBER,
        **blog_ctx,
    })



@router.post("/api/diagnostico")
async def diagnostico_proxy(request: Request):
    """Проксирует запрос к Anthropic API — ключ остаётся на сервере."""
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
    if not ANTHROPIC_API_KEY:
        return JSONResponse({"error": "not configured"}, status_code=503)
    try:
        body = await request.json()
        symptom = str(body.get("symptom", ""))[:500]
        if not symptom:
            return JSONResponse({"error": "symptom required"}, status_code=400)
        system_prompt = (
            "Você é o assistente técnico da MTF Refrigeração, loja de peças e serviços de refrigeração "
            "no Rio de Janeiro (Av. Brasil, 6818).\n\n"
            "Ao receber a descrição de um problema, retorne APENAS um JSON válido com esta estrutura exata:\n"
            "{\"titulo\":\"string curto\",\"causas\":[{\"probabilidade\":\"alta|media|baixa\","
            "\"descricao\":\"string\",\"peca\":\"string|null\"}],"
            "\"cliente_pode_verificar\":[\"string\"],\"precisa_tecnico\":\"string\",\"alerta\":\"string|null\"}\n\n"
            "Regras:\n"
            "- Máximo 4 causas ordenadas por probabilidade\n"
            "- \"peca\": somente peças que a MTF vende (compressor, gás R22/R410a/R134a/R32/R290/R600, "
            "placa eletrônica, termostato, capacitor, válvula, resistência, turbina/motoventilador, "
            "dreno, bandeja condensado). null se não aplicável\n"
            "- \"cliente_pode_verificar\": máximo 3 itens, ações simples e seguras\n"
            "- \"alerta\": somente se houver risco elétrico real, senão null\n"
            "- Resposta APENAS JSON, sem markdown, sem texto fora do JSON"
        )
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 1000,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": symptom}],
                },
            )
        data = resp.json()
        raw = (data.get("content") or [{}])[0].get("text", "{}")
        import json as _json2
        result = _json2.loads(raw.replace("```json", "").replace("```", "").strip())
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/catalog")
async def catalog_api():
    async with engine.connect() as conn:
        rows = await _get_products_cached(conn)
    return [
        {
            "id":          r["id"],
            "name":        r["name"],
            "slug":        slugify(r["name"]),
            "sale_price":  float(r["sale_price"]) if r["sale_price"] else None,
            "promo_price": r.get("promo_price"),
            "on_promo":    r.get("on_promo", False),
            "price":       r.get("price"),
            "unit":        r["unit"],
            "category":    r["category_name"],
            "image":       r["image"],
            "description": r["description"],
            "in_stock":    (float(r.get("current_stock") or 0) > 0),
        }
        for r in rows
    ]


@router.post("/api/track/search")
async def track_search(request: Request):
    try:
        body  = await request.json()
        query = (body.get("query") or "").strip()[:100]
        page  = (body.get("page") or "").strip()[:30]
        if not query:
            return JSONResponse({"ok": False})
        now_str = datetime.now(BRT).strftime('%d/%m %H:%M')
        page_label = "🏠 Início" if page == "inicio" else "📋 Catálogo"
        await _send_tg(f"🔍 <b>Busca no site</b>\nPágina: {page_label}\nBusca: <b>{query}</b>\nHorário: {now_str}")
        return JSONResponse({"ok": True})
    except Exception:
        return JSONResponse({"ok": False})


@router.post("/api/track/whatsapp")
async def track_whatsapp_click(request: Request):
    try:
        body       = await request.json()
        product    = body.get("product", "Geral")
        user_agent = request.headers.get("user-agent", "")[:80]
        ip         = _client_ip(request)
        if _is_owner(request):
            return JSONResponse({"ok": True, "skip": True})
        async with engine.begin() as conn:
            await conn.execute(text("""
                INSERT INTO catalog_clicks (product_name, ip, user_agent, clicked_at)
                VALUES (:product, :ip, :ua, :now)
            """), {"product": product, "ip": ip, "ua": user_agent, "now": datetime.utcnow()})
        now_str = datetime.now(BRT).strftime('%d/%m %H:%M')
        if WA_NOTIFY_NUMBER:
            msg = f"🛍 Novo interesse no catálogo!\nProduto: *{product}*\nHorário: {now_str}"
            await _send_wa_notification(msg)
        # Telegram — дублируем все WA клики
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
        # Meta CAPI — дублируем клик server-side
        import uuid
        event_id  = f"wa_{uuid.uuid4().hex}"
        page_url  = request.headers.get("referer", SITE_URL)
        await _send_meta_capi(
            event_name="Contact",
            event_id=event_id,
            ip=ip,
            user_agent=user_agent,
            page_url=page_url,
        )
        return JSONResponse({"ok": True})
    except Exception:
        return JSONResponse({"ok": False})


async def _send_meta_capi(event_name: str, event_id: str, ip: str, user_agent: str, page_url: str = ""):
    """Отправляет событие в Meta Conversions API."""
    if not META_PIXEL_ID or not META_CAPI_TOKEN:
        return
    import hashlib, time
    # хешируем IP как client_ip_address (Meta требует SHA256)
    ip_hash = hashlib.sha256(ip.encode()).hexdigest()
    payload = {
        "data": [{
            "event_name":       event_name,
            "event_time":       int(time.time()),
            "event_id":         event_id,
            "event_source_url": page_url or SITE_URL,
            "action_source":    "website",
            "user_data": {
                "client_ip_address": ip,
                "client_user_agent": user_agent,
            },
        }],
        "test_event_code": None,  # убрать после тестирования
    }
    # убираем test_event_code если None
    payload["data"][0] = {k: v for k, v in payload["data"][0].items() if v is not None}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"https://graph.facebook.com/v19.0/{META_PIXEL_ID}/events",
                params={"access_token": META_CAPI_TOKEN},
                json=payload,
            )
    except Exception:
        pass


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
    add_url(f"{base}/calculadora-instalacao", "0.8")
    add_url(f"{base}/calculadora-gas", "0.6")
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

# Tipo do produto (Merchant Center pede "Tipo" na descrição). Ordem importa: o mais específico primeiro.
_TIPOS = [
    (("controle remoto", "remote"),                              "Controle remoto"),
    (("hélice", "helice"),                                       "Hélice"),
    (("turbina",),                                               "Turbina"),
    (("sensor", "termistor"),                                    "Sensor de temperatura NTC"),
    (("protetor térmico", "protetor termico"),                   "Protetor térmico"),
    (("botão", "botao"),                                         "Botão"),
    (("fluido de limpeza", "air flush", "air repair", "gpclear"), "Fluido de limpeza"),
    (("espuma de limpeza",),                                     "Espuma de limpeza"),
    (("higienizador",),                                          "Kit higienizador"),
    (("bolsa para limpeza", "bolsa de limpeza"),                 "Bolsa coletora para limpeza"),
    (("limpeza", "air shield"),                                  "Produto de limpeza"),
    (("placa",),                                                 "Placa eletrônica"),
    (("capacitor",),                                             "Capacitor"),
    (("compressor",),                                            "Compressor"),
    (("termostato",),                                            "Termostato"),
    (("timer",),                                                 "Timer de degelo"),
    (("relé", "rele "),                                          "Relé"),
    (("gás refrigerante", "gas refrigerante"),                   "Gás refrigerante"),
    (("tubo de cobre",),                                         "Tubo de cobre"),
    (("esponjoso",),                                             "Tubo isolante esponjoso"),
    (("suporte",),                                               "Suporte"),
    (("válvula", "valvula"),                                     "Válvula"),
    (("resistência", "resistencia"),                             "Resistência"),
    (("mangueira",),                                             "Mangueira"),
    (("gaxeta", "borracha"),                                     "Gaxeta"),
    (("motor",),                                                 "Motor"),
]


def _tipo_produto(name: str, description: str = "") -> str:
    for texto in (name.lower() + " ", description[:150].lower()):
        for chaves, tipo in _TIPOS:
            if any(k in texto for k in chaves):
                return tipo
    return ""


@router.get("/feed.xml", response_class=PlainTextResponse)
async def product_feed():
    """
    Google Merchant Center / Facebook Commerce Manager product feed.
    Формат: RSS 2.0 + Google Merchant namespace (g:).
    Обновляется при каждом запросе (Facebook обновляет раз в сутки).
    """
    base = SITE_URL.rstrip("/")
    async with engine.connect() as conn:
        rows = await _get_products_cached(conn)
    ids_by_product = {}
    try:   # brand / sku / gtin — colunas da admin; se faltar, o feed segue sem
        async with engine.connect() as conn:
            for x in (await conn.execute(text("SELECT id, brand, sku, gtin FROM products"))).mappings():
                ids_by_product[x["id"]] = x
    except Exception:
        _blog_log.exception("feed: falha ao ler brand/sku/gtin")

    items = []
    seen = set()

    for r in rows:
        name = (r.get("name") or "").strip()
        if not name:
            continue

        sl = slugify(name)
        if sl in seen:
            continue
        seen.add(sl)

        product_id  = str(r.get("id", sl))
        price       = r.get("sale_price")
        category    = r.get("category_name") or "Peças de Refrigeração"
        description = (r.get("description") or name).strip()
        tipo        = _tipo_produto(name, description)
        if tipo and "tipo:" not in description.lower():
            description = f"{description.rstrip()}\nTipo: {tipo}."
        image_url   = r.get("image") or ""
        in_stock    = float(r.get("current_stock") or 0) > 0
        availability = "in stock" if in_stock else "out of stock"
        unit        = r.get("unit") or "un"

        # Пропускаем товары без цены
        if not price:
            continue

        # Facebook/Google требует изображение — пропускаем без него
        if not image_url:
            continue

        # Если изображение относительное — делаем абсолютным
        if image_url.startswith("/"):
            image_url = f"{base}{image_url}"

        price_str = f"{price:.2f} BRL"
        sale_tag  = (f"\n    <g:sale_price>{r['promo_price']:.2f} BRL</g:sale_price>" if r.get("on_promo") else "")
        if r.get("on_promo") and r.get("promo_until"):
            sale_tag += (f"\n    <g:sale_price_effective_date>{datetime.now(BRT).date().isoformat()}T00:00-03:00/"
                         f"{r['promo_until']}T23:59-03:00</g:sale_price_effective_date>")
        # google_category из БД (ID таксономии Google); пусто — тег не выводим, Google определит сам
        gcat      = (r.get("google_category") or "").strip()
        gcat_tag  = (f"\n    <g:google_product_category>{escape(gcat)}</g:google_product_category>" if gcat else "")
        link = f"{base}/product/{sl}"

        # identificadores: GTIN válido e/ou marca real + MPN; senão identifier_exists=no
        ids   = ids_by_product.get(r.get("id")) or {}
        real_brand = (ids.get("brand") or "").strip()
        brand = real_brand or "M.T.F Refrigeração"
        gtin  = (ids.get("gtin") or "").strip()
        mpn   = (ids.get("sku") or "").strip()
        ident_tag = ""
        if _gtin_ok(gtin):
            ident_tag += f"\n    <g:gtin>{escape(gtin)}</g:gtin>"
        if real_brand and mpn:
            ident_tag += f"\n    <g:mpn>{escape(mpn)}</g:mpn>"
        if not ident_tag:
            ident_tag = "\n    <g:identifier_exists>no</g:identifier_exists>"

        item = f"""
  <item>
    <g:id>{escape(product_id)}</g:id>
    <g:title>{escape(name)}</g:title>
    <g:description>{escape(description[:5000])}</g:description>
    <g:link>{escape(link)}</g:link>
    <g:image_link>{escape(image_url)}</g:image_link>
    <g:price>{price_str}</g:price>{sale_tag}
    <g:availability>{availability}</g:availability>
    <g:condition>new</g:condition>
    <g:brand>{escape(brand)}</g:brand>{ident_tag}{gcat_tag}
    <g:product_type>{escape(category + (" > " + tipo if tipo else ""))}</g:product_type>
  </item>"""
        items.append(item)

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0">
  <channel>
    <title>MTF Refrigeração — Catálogo de Peças</title>
    <link>{escape(base)}</link>
    <description>Peças e gases para refrigeração no Rio de Janeiro</description>
{"".join(items)}
  </channel>
</rss>"""

    return PlainTextResponse(xml, media_type="application/xml")


# ── Estoque da loja física para o Google (anúncios/listagens locais gratuitas) ──
# Merchant Center -> Fontes de dados -> Adicionar "Inventário local" -> URL /feed-local.xml
# STORE_CODE tem que ser IGUAL ao "código da loja" do Perfil da Empresa no Google (diferencia maiúsculas).
STORE_CODE = os.environ.get("STORE_CODE", "")


@router.get("/feed-local.xml", response_class=PlainTextResponse)
async def local_inventory_feed():
    """Mesmos produtos (e mesmos g:id) do /feed.xml + quanto tem na loja da Av. Brasil."""
    if not STORE_CODE:
        return PlainTextResponse("STORE_CODE não configurado", status_code=503)
    async with engine.connect() as conn:
        rows = await _get_products_cached(conn)
    items, seen = [], set()
    today = datetime.now(BRT).date().isoformat()
    for r in rows:
        name = (r.get("name") or "").strip()
        sl = slugify(name)
        # mesmo filtro do /feed.xml: sem nome, repetido, sem preço ou sem foto não vai
        if not name or sl in seen or not r.get("sale_price") or not r.get("image"):
            continue
        seen.add(sl)
        qty = max(0, int(float(r.get("current_stock") or 0)))
        availability = "in_stock" if qty >= 3 else ("limited_availability" if qty > 0 else "out_of_stock")
        sale = ""
        if r.get("on_promo"):
            sale = f"\n    <g:sale_price>{r['promo_price']:.2f} BRL</g:sale_price>"
            if r.get("promo_until"):
                sale += (f"\n    <g:sale_price_effective_date>{today}T00:00-03:00/"
                         f"{r['promo_until']}T23:59-03:00</g:sale_price_effective_date>")
        items.append(f"""
  <item>
    <g:id>{escape(str(r["id"]))}</g:id>
    <g:store_code>{escape(STORE_CODE)}</g:store_code>
    <g:availability>{availability}</g:availability>
    <g:quantity>{qty}</g:quantity>
    <g:price>{r["sale_price"]:.2f} BRL</g:price>{sale}
    <g:pickup_method>buy</g:pickup_method>
    <g:pickup_sla>same day</g:pickup_sla>
  </item>""")
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0">
  <channel>
    <title>MTF Refrigeração — Estoque da loja</title>
    <link>{escape(SITE_URL)}</link>
    <description>Inventário local — Av. Brasil, 6818 (Bonsucesso)</description>
{"".join(items)}
  </channel>
</rss>"""
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
