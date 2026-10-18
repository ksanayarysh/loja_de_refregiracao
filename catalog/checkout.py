"""
Checkout do site: pedido + pagamento Pix (Mercado Pago, API de Pagamentos).

Variáveis de ambiente:
  MP_ACCESS_TOKEN       TEST-... (teste) / APP_USR-... (produção)
  MP_NOTIFICATION_URL   (opcional, só em produção) https://.../api/checkout/mp-webhook
  PIX_EXPIRA_MIN        minutos de validade do Pix (padrão 30)

Inclua no main.py do catálogo, ao lado do router principal:
  from checkout import router as checkout_router
  app.include_router(checkout_router)
"""
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import text

from dependencies import engine, templates
from lalamove import get_quote, LalamoveError
from router import (
    _apply_promo, _cep_to_coords, geocode_endereco, _km, _quote_cache, _send_tg, _blog_log, _ensure_kit_settings, slugify,
    FRETE_MAX_KM, FRETE_OPTIONS, LALA_SERVICES, LALA_DEBUG_TOKEN,
    GA_ID, SITE_URL, STORE_ADDRESS, STORE_PHONE, WA_OWNER_NUMBER, ADMIN_URL,
)
import push_utils as push

router = APIRouter()

MP_API = "https://api.mercadopago.com"


def _mp_token() -> str:
    return os.environ.get("MP_ACCESS_TOKEN", "").strip()


# ── TABELA ─────────────────────────────────────────────────────────────────────
_orders_ready = False


async def _ensure_orders_table():
    global _orders_ready
    if _orders_ready:
        return
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS site_orders (
                id               SERIAL PRIMARY KEY,
                token            VARCHAR(40) NOT NULL,
                created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                status           VARCHAR(20) NOT NULL DEFAULT 'pending',
                customer_name    VARCHAR(120),
                customer_phone   VARCHAR(30),
                customer_email   VARCHAR(160),
                delivery_type    VARCHAR(20),
                delivery_service VARCHAR(20),
                cep              VARCHAR(9),
                address          TEXT,
                numero           VARCHAR(20),
                complemento      VARCHAR(120),
                lat              DOUBLE PRECISION,
                lng              DOUBLE PRECISION,
                items            JSONB NOT NULL,
                subtotal         NUMERIC(10,2),
                frete            NUMERIC(10,2),
                total            NUMERIC(10,2),
                mp_payment_id    VARCHAR(40),
                pix_qr           TEXT,
                pix_qr_base64    TEXT,
                pix_expires_at   TIMESTAMPTZ,
                paid_at          TIMESTAMPTZ,
                lalamove_order_id VARCHAR(40),
                lalamove_link    TEXT
            )
        """))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_site_orders_status ON site_orders(status)"))
    _orders_ready = True


# ── HELPERS ────────────────────────────────────────────────────────────────────
def _brl(v: float) -> str:
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


# Produtos que podem entrar no kit de instalação (mesmos slugs do KIT em calculadora-instalacao.html).
# Só estes recebem o desconto do kit — o navegador não decide quem tem desconto.
# Kit de instalação (tubos de cobre, suporte) não vai de moto: só carro.
KIT_BLOCKED_SERVICES = {"LALAGO", "LALAPRO"}

KIT_SLUGS = {
    "tubo-de-cobre-de-1-4", "tubo-de-cobre-de-3-8", "tubo-de-cobre-de-1-2", "tubo-de-cobre-de-3-4",
    "tubo-esponjoso-blindado-1-4-2-metros", "tubo-esponjoso-blindado-3-8-2-metros",
    "tubo-esponjoso-blindado-1-2-2-metros", "tubo-esponjoso-blindado-5-8-2-metros",
    "tubo-esponjoso-blindado-3-4-2-metros",
    "suporte-ar-condicionado-split-7000-12000-btus-40cm-chapa-pintada",
    "suporte-para-instalacao-de-evaporadoras-7-30-mil-btus",
    "fita-pvc", "cabo-pp-4v-x-1-5mm", "cabo-pp-4v-x-2-5mm", "mangueira-transparente-ar-condicionado",
}
async def _kit_discount_pct() -> float:
    try:
        await _ensure_kit_settings()
        async with engine.connect() as conn:
            pct = float((await conn.execute(text("SELECT discount_percent FROM kit_settings WHERE id = 1"))).scalar() or 0)
        return min(max(pct, 0.0), 100.0)
    except Exception:
        _blog_log.exception("checkout: falha ao ler desconto do kit")
        return 0.0


async def _fetch_products(ids: list) -> dict:
    if not ids:
        return {}
    async with engine.connect() as conn:
        res = await conn.execute(text("""
            SELECT id, name, unit, sale_price, promo_price
            FROM products
            WHERE active = TRUE AND id = ANY(:ids)
        """), {"ids": list(ids)})
        rows = [dict(r) for r in res.mappings().all()]
    out = {}
    for r in rows:
        r["sale_price"] = float(r["sale_price"]) if r["sale_price"] is not None else None
        _apply_promo(r)
        if r.get("price"):
            r["price"] = round(float(r["price"]), 2)
            out[r["id"]] = r
    return out


async def _load_items(raw_items: list) -> tuple[list, float]:
    """Preços SEMPRE do banco, nunca do navegador.
    Linhas marcadas kit:true (vindas da calculadora) recebem o desconto do kit,
    mas só se o produto estiver em KIT_SLUGS; senão vão pelo preço normal."""
    qty_by_key = {}   # (id, is_kit) -> qty
    for it in raw_items or []:
        try:
            pid, qty = int(it.get("id")), int(it.get("qty"))
        except (TypeError, ValueError, AttributeError):
            continue
        if pid > 0 and 0 < qty <= 100:
            key = (pid, bool(it.get("kit")))
            qty_by_key[key] = qty_by_key.get(key, 0) + qty
    if not qty_by_key:
        return [], 0.0

    products = await _fetch_products({pid for pid, _ in qty_by_key})
    pct = await _kit_discount_pct() if any(k for _, k in qty_by_key) else 0.0

    items, subtotal = [], 0.0
    for (pid, is_kit), qty in qty_by_key.items():
        r = products.get(pid)
        if not r:
            continue
        price = r["price"]
        kit_ok = is_kit and slugify(r["name"]) in KIT_SLUGS
        if kit_ok and pct:
            price = round(price * (100 - pct) / 100, 2)   # mesma conta da calculadora
        item = {"id": pid, "name": r["name"], "unit": r["unit"], "price": price, "qty": qty,
                "subtotal": round(price * qty, 2)}
        if kit_ok:
            item.update({"kit": True, "discount_pct": pct, "full_price": r["price"]})
        items.append(item)
        subtotal += price * qty
    return items, round(subtotal, 2)


async def _calc_frete(cep: str, service: str):
    """Recalcula o frete no servidor. Retorna (preço, geo) ou (None, motivo)."""
    if service not in LALA_SERVICES or service not in FRETE_OPTIONS:
        return None, "Tipo de entrega indisponível."

    geo = await _cep_to_coords(cep)
    if not geo:
        return None, "CEP não encontrado."
    if geo["uf"] != "RJ":
        return None, "Para este endereço, o frete é combinado com o vendedor."

    cached = _quote_cache.get(cep)
    if cached and time.time() - cached[0] < 600 and not cached[1].get("combinar"):
        for o in cached[1].get("options", []):
            if o["service"] == service:
                return float(o["price"]), geo

    try:
        q = await get_quote(geo["lat"], geo["lng"], geo["address"], service)
    except LalamoveError as e:
        _blog_log.warning("checkout: lalamove %s %s: %s", cep, service, e)
        return None, "Para este endereço, o frete é combinado com o vendedor."
    if int(q["distance_m"] or 0) / 1000 > FRETE_MAX_KM:
        return None, "Para este endereço, o frete é combinado com o vendedor."
    return float(q["price"]), geo


async def _mp_create_pix(order_id: int, total: float, email: str, name: str, description: str) -> dict:
    minutes = int(os.environ.get("PIX_EXPIRA_MIN", "30"))
    expires = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    body = {
        "transaction_amount": round(total, 2),
        "description": description[:250],
        "payment_method_id": "pix",
        "external_reference": str(order_id),
        "date_of_expiration": expires.isoformat(timespec="milliseconds"),
        "payer": {"email": email, "first_name": (name.split() or ["Cliente"])[0]},
    }
    notification_url = os.environ.get("MP_NOTIFICATION_URL", "").strip()
    if notification_url:
        body["notification_url"] = notification_url

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(f"{MP_API}/v1/payments", json=body, headers={
            "Authorization": f"Bearer {_mp_token()}",
            "X-Idempotency-Key": f"mtf-order-{order_id}",
        })
    data = r.json()
    if r.status_code >= 400:
        raise RuntimeError(f"Mercado Pago {r.status_code}: {data}")
    tx = (data.get("point_of_interaction") or {}).get("transaction_data") or {}
    return {
        "id": str(data["id"]),
        "status": data.get("status"),
        "qr": tx.get("qr_code"),
        "qr_base64": tx.get("qr_code_base64"),
        "expires_at": expires,
    }


async def _mp_get_status(payment_id: str) -> str | None:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{MP_API}/v1/payments/{payment_id}",
                             headers={"Authorization": f"Bearer {_mp_token()}"})
    if r.status_code != 200:
        _blog_log.warning("checkout: MP status %s -> %s %s", payment_id, r.status_code, r.text[:200])
        return None
    return r.json().get("status")


async def _mark_paid(order_id: int):
    """Marca como pago uma única vez e avisa no Telegram."""
    async with engine.begin() as conn:
        row = (await conn.execute(text("""
            UPDATE site_orders SET status = 'paid', paid_at = NOW()
            WHERE id = :id AND status = 'pending'
            RETURNING id, customer_name, customer_phone, delivery_type, delivery_service,
                      address, numero, complemento, items, frete, total
        """), {"id": order_id})).mappings().first()
    if not row:
        return
    items = row["items"] if isinstance(row["items"], list) else json.loads(row["items"])
    lines = "\n".join(f"  • {i['name']} × {i['qty']}" + (" (kit)" if i.get("kit") else "") for i in items)
    if row["delivery_type"] == "lalamove":
        label = FRETE_OPTIONS.get(row["delivery_service"], (row["delivery_service"], ""))[0]
        entrega = (f"🛵 {label} ({_brl(float(row['frete']))})\n"
                   f"{row['address']}, nº {row['numero']}"
                   + (f" — {row['complemento']}" if row["complemento"] else ""))
    else:
        entrega = "🏪 Retirada na loja"
    # push no celular (admin instalada como app) — não bloqueia se falhar
    entrega_curta = "Retirada" if row["delivery_type"] != "lalamove" else \
        FRETE_OPTIONS.get(row["delivery_service"], (row["delivery_service"], ""))[0]
    await push.send_all(
        engine, f"💰 Pedido #{row['id']} pago — {_brl(float(row['total']))}",
        f"{row['customer_name']} · {entrega_curta}",
        url=f"{ADMIN_URL.rstrip('/')}/site-orders/{row['id']}", tag=f"pedido-{row['id']}",
    )
    await _send_tg(
        f"💰 <b>Pedido #{row['id']} PAGO (Pix)</b>\n"
        f"Total: <b>{_brl(float(row['total']))}</b>\n"
        f"Cliente: {row['customer_name']} · {row['customer_phone']}\n\n"
        f"{lines}\n\n{entrega}"
    )


async def _refresh_status(order: dict) -> str:
    """Consulta o MP se o pedido ainda está pendente."""
    if order["status"] != "pending" or not order.get("mp_payment_id"):
        return order["status"]
    mp_status = await _mp_get_status(order["mp_payment_id"])
    if mp_status == "approved":
        await _mark_paid(order["id"])
        return "paid"
    if mp_status in ("cancelled", "rejected", "expired"):
        async with engine.begin() as conn:
            await conn.execute(text(
                "UPDATE site_orders SET status = 'expired' WHERE id = :id AND status = 'pending'"
            ), {"id": order["id"]})
        return "expired"
    return "pending"


async def _check_pending_loop():
    """A cada 2 min confere no MP os pedidos pendentes das últimas 3 horas.
    Garante o aviso de pagamento mesmo se o cliente fechar a página e o webhook falhar."""
    import asyncio
    while True:
        await asyncio.sleep(120)
        try:
            if not _mp_token():
                continue
            await _ensure_orders_table()
            async with engine.connect() as conn:
                rows = (await conn.execute(text("""
                    SELECT id, status, mp_payment_id FROM site_orders
                    WHERE status = 'pending' AND mp_payment_id IS NOT NULL
                      AND created_at > NOW() - INTERVAL '3 hours'
                """))).mappings().all()
            for r in rows:
                await _refresh_status(dict(r))
        except Exception:
            _blog_log.exception("checkout: falha na verificação periódica de pagamentos")


_checker_started = False


@router.on_event("startup")
async def _start_checker():
    global _checker_started
    if _checker_started:
        return
    _checker_started = True
    import asyncio
    asyncio.ensure_future(_check_pending_loop())


def _page_ctx(request: Request, **extra) -> dict:
    return {
        "request": request, "ga_id": GA_ID, "site_url": SITE_URL,
        "store_address": STORE_ADDRESS, "store_phone": STORE_PHONE, "wa_number": WA_OWNER_NUMBER,
        **extra,
    }


# ── PÁGINAS ────────────────────────────────────────────────────────────────────
@router.get("/checkout", response_class=HTMLResponse)
async def checkout_page(request: Request):
    return templates.TemplateResponse("checkout.html", _page_ctx(request))


@router.get("/pedido/{order_id}", response_class=HTMLResponse)
async def pedido_page(request: Request, order_id: int, t: str = ""):
    await _ensure_orders_table()
    async with engine.connect() as conn:
        order = (await conn.execute(text(
            "SELECT * FROM site_orders WHERE id = :id AND token = :t"
        ), {"id": order_id, "t": t})).mappings().first()
    if not order:
        return HTMLResponse("Pedido não encontrado", status_code=404)
    order = dict(order)
    order["items"] = order["items"] if isinstance(order["items"], list) else json.loads(order["items"])
    for k in ("subtotal", "frete", "total"):
        order[k] = float(order[k] or 0)
    order["frete_label"] = FRETE_OPTIONS.get(order.get("delivery_service") or "", ("", ""))[0]
    order["pix_expires_iso"] = order["pix_expires_at"].isoformat() if order.get("pix_expires_at") else ""
    return templates.TemplateResponse("pedido.html", _page_ctx(request, order=order))


# ── API ────────────────────────────────────────────────────────────────────────
_pix_hits: dict = {}  # ip -> [timestamps]


# DDDs existentes no Brasil
_DDDS = {11,12,13,14,15,16,17,18,19,21,22,24,27,28,31,32,33,34,35,37,38,41,42,43,44,45,46,47,48,49,
         51,53,54,55,61,62,63,64,65,66,67,68,69,71,73,74,75,77,79,81,82,83,84,85,86,87,88,89,
         91,92,93,94,95,96,97,98,99}


def _norm_cel(raw) -> str:
    """Celular brasileiro -> 11 dígitos (DDD + 9xxxxxxxx), ou '' se inválido. Aceita +55 na frente."""
    d = re.sub(r"\D", "", str(raw or ""))
    if len(d) in (12, 13) and d.startswith("55"):
        d = d[2:]
    if len(d) == 12 and d.startswith("0"):          # 0 21 9xxxx-xxxx
        d = d[1:]
    if len(d) != 11 or int(d[:2]) not in _DDDS or d[2] != "9":
        return ""
    if len(set(d[2:])) == 1:                         # 99999-9999, 999999999...
        return ""
    return d


@router.post("/api/checkout/pix")
async def checkout_pix(request: Request):
    ip = request.client.host if request.client else "?"
    now = time.time()
    hits = [t for t in _pix_hits.get(ip, []) if now - t < 3600]
    if len(hits) >= 8:
        return JSONResponse({"error": "Muitas tentativas. Tente novamente mais tarde ou fale conosco no WhatsApp."},
                            status_code=429)
    _pix_hits[ip] = hits + [now]
    if not _mp_token():
        return JSONResponse({"error": "Pagamento indisponível no momento."}, status_code=503)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Dados inválidos."}, status_code=400)

    name = str(body.get("name", "")).strip()[:120]
    phone = _norm_cel(body.get("phone", ""))
    email = str(body.get("email", "")).strip()[:160]
    if len(name) < 2:
        return JSONResponse({"error": "Informe seu nome."}, status_code=400)
    if not phone:
        return JSONResponse({"error": "Informe um celular (WhatsApp) válido com DDD, ex.: (21) 99999-9999."},
                            status_code=400)
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return JSONResponse({"error": "Informe um e-mail válido."}, status_code=400)

    items, subtotal = await _load_items(body.get("items"))
    if not items:
        return JSONResponse({"error": "Seu carrinho está vazio."}, status_code=400)
    asked = {int(i["id"]) for i in (body.get("items") or []) if str(i.get("id", "")).isdigit()}
    missing = asked - {i["id"] for i in items}
    if missing:
        return JSONResponse({"error": "Algum produto do carrinho não está mais disponível. "
                                      "Remova-o do carrinho ou fale conosco no WhatsApp.",
                             "missing": sorted(missing)}, status_code=409)

    delivery = body.get("delivery") or {}
    dtype = "lalamove" if delivery.get("type") == "lalamove" else "retirada"
    frete, geo = 0.0, None
    cep = numero = complemento = address = service = None
    if dtype == "lalamove":
        cep = re.sub(r"\D", "", str(delivery.get("cep", "")))
        service = str(delivery.get("service", ""))
        if service in KIT_BLOCKED_SERVICES and any(i.get("kit") for i in items):
            return JSONResponse({"error": "O kit de instalação não cabe na moto. Escolha a entrega de carro."},
                                status_code=400)
        numero = str(delivery.get("numero", "")).strip()[:20]
        complemento = str(delivery.get("complemento", "")).strip()[:120]
        rua = str(delivery.get("rua", "")).strip()[:200]
        if len(cep) != 8:
            return JSONResponse({"error": "Informe um CEP válido."}, status_code=400)
        if not rua or not numero:
            return JSONResponse({"error": "Informe a rua e o número para a entrega."}, status_code=400)
        try:
            frete, geo = await _calc_frete(cep, service)
        except Exception:
            _blog_log.exception("checkout: falha no frete %s", cep)
            frete, geo = None, "Não foi possível calcular o frete agora. Tente novamente."
        if frete is None:
            return JSONResponse({"error": geo, "combinar": "combinado" in str(geo)}, status_code=400)
        address = ", ".join(x for x in [rua, geo["bairro"], f'{geo["cidade"]} - {geo["uf"]}',
                                        f"{cep[:5]}-{cep[5:]}"] if x)
        # Pino exato (rua + número) para o entregador. O preço continua o do CEP, que o cliente viu.
        geo = dict(geo)
        try:
            exact = await geocode_endereco(rua, numero, geo["bairro"], geo["cidade"], geo["uf"], cep)
        except Exception:
            _blog_log.exception("checkout: geocode do endereço falhou")
            exact = None
        if exact and _km(exact, (geo["lat"], geo["lng"])) <= 8:
            geo["lat"], geo["lng"] = exact
        elif exact:
            _blog_log.warning("checkout: pino %s longe demais do CEP %s — mantido o do CEP", exact, cep)

    total = round(subtotal + (frete or 0), 2)
    token = secrets.token_urlsafe(16)

    await _ensure_orders_table()
    async with engine.begin() as conn:
        order_id = (await conn.execute(text("""
            INSERT INTO site_orders (token, customer_name, customer_phone, customer_email,
                delivery_type, delivery_service, cep, address, numero, complemento, lat, lng,
                items, subtotal, frete, total)
            VALUES (:token, :name, :phone, :email, :dtype, :service, :cep, :address, :numero, :compl,
                :lat, :lng, CAST(:items AS JSONB), :subtotal, :frete, :total)
            RETURNING id
        """), {
            "token": token, "name": name, "phone": phone, "email": email,
            "dtype": dtype, "service": service, "cep": cep, "address": address,
            "numero": numero, "compl": complemento,
            "lat": geo["lat"] if isinstance(geo, dict) else None,
            "lng": geo["lng"] if isinstance(geo, dict) else None,
            "items": json.dumps(items, ensure_ascii=False),
            "subtotal": subtotal, "frete": frete or 0, "total": total,
        })).scalar()

    try:
        pix = await _mp_create_pix(order_id, total, email, name, f"MTF Refrigeração — pedido #{order_id}")
    except Exception:
        _blog_log.exception("checkout: falha ao criar Pix do pedido %s", order_id)
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE site_orders SET status = 'error' WHERE id = :id"), {"id": order_id})
        return JSONResponse({"error": "Não foi possível gerar o Pix. Tente novamente."}, status_code=502)

    async with engine.begin() as conn:
        await conn.execute(text("""
            UPDATE site_orders SET mp_payment_id = :pid, pix_qr = :qr, pix_qr_base64 = :b64, pix_expires_at = :exp
            WHERE id = :id
        """), {"pid": pix["id"], "qr": pix["qr"], "b64": pix["qr_base64"], "exp": pix["expires_at"], "id": order_id})

    await _send_tg(f"🧾 Pedido #{order_id} criado no site — aguardando Pix ({_brl(total)})")
    return JSONResponse({"order_id": order_id, "token": token, "url": f"/pedido/{order_id}?t={token}"})


@router.get("/api/checkout/status/{order_id}")
async def checkout_status(order_id: int, t: str = ""):
    await _ensure_orders_table()
    async with engine.connect() as conn:
        order = (await conn.execute(text(
            "SELECT id, status, mp_payment_id, pix_expires_at FROM site_orders WHERE id = :id AND token = :t"
        ), {"id": order_id, "t": t})).mappings().first()
    if not order:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        status = await _refresh_status(dict(order))
    except Exception:
        _blog_log.exception("checkout: falha ao consultar status do pedido %s", order_id)
        status = order["status"]
    return JSONResponse({"status": status}, headers={"Cache-Control": "no-store"})


@router.post("/api/checkout/mp-webhook")
async def mp_webhook(request: Request):
    """Mercado Pago avisa que um pagamento mudou. Conferimos o status direto na API (não confiamos no corpo)."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    payment_id = str((body.get("data") or {}).get("id") or request.query_params.get("data.id") or "")
    if not payment_id:
        return JSONResponse({"ok": True})
    await _ensure_orders_table()
    async with engine.connect() as conn:
        order = (await conn.execute(text(
            "SELECT id, status, mp_payment_id FROM site_orders WHERE mp_payment_id = :pid"
        ), {"pid": payment_id})).mappings().first()
    if order:
        try:
            await _refresh_status(dict(order))
        except Exception:
            _blog_log.exception("checkout: webhook falhou para pagamento %s", payment_id)
    return JSONResponse({"ok": True})


@router.get("/api/checkout/simular-pago/{order_id}")
async def simular_pago(order_id: int, token: str = ""):
    """SÓ TESTE: marca o pedido como pago. Funciona apenas com token TEST- do MP."""
    if not _mp_token().startswith("TEST-") or not LALA_DEBUG_TOKEN or token != LALA_DEBUG_TOKEN:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    await _mark_paid(order_id)
    return JSONResponse({"ok": True})
