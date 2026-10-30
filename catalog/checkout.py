"""
Checkout do site: pedido + pagamento Pix (Mercado Pago, API de Pagamentos)
ou cartão de crédito (Mercado Pago Checkout Pro: o cliente paga na página segura do MP).

Variáveis de ambiente:
  MP_ACCESS_TOKEN       TEST-... (teste) / APP_USR-... (produção)
  MP_NOTIFICATION_URL   (opcional, só em produção) https://.../api/checkout/mp-webhook
  PIX_EXPIRA_MIN        minutos de validade do Pix (padrão 30)
  CARTAO_EXPIRA_MIN     minutos para pagar com cartão (padrão 60)
  CARTAO_PARCELAS       máximo de parcelas no cartão (padrão 12; juros por conta do cliente)
  CARTAO_MAX            (opcional) total máximo de pedido no cartão, ex.: 1500 — acima disso só Pix
  GCR_MERCHANT_ID       (opcional) Merchant ID do Google Merchant Center -> ativa o opt-in
                        das "Avaliações do consumidor do Google" na página do pedido pago

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
    margem_detalhe, frete_final, linhas_dos_itens, FRETE_SUBSIDIO_MIN, _client_ip, _is_owner, log_product_events,
    FRETE_MAX_KM, FRETE_OPTIONS, LALA_SERVICES, LALA_DEBUG_TOKEN,
    GA_ID, SITE_URL, STORE_ADDRESS, STORE_PHONE, WA_OWNER_NUMBER, ADMIN_URL,
)
import push_utils as push

router = APIRouter()

GCR_MERCHANT_ID = os.environ.get("GCR_MERCHANT_ID", "").strip()
_BRT = timezone(timedelta(hours=-3))


def _gcr_delivery_date(order: dict) -> str:
    """Data estimada de entrega para as Avaliações do consumidor (YYYY-MM-DD, horário de Brasília).
    Regra da loja: pago até 16h -> entrega/retirada hoje; depois das 16h -> amanhã (domingo -> segunda)."""
    base = order.get("paid_at") or datetime.now(timezone.utc)
    local = base.astimezone(_BRT)
    d = local.date() if local.hour < 16 else local.date() + timedelta(days=1)
    if order.get("delivery_type") == "correios":
        # prazo em dias úteis (já com o dia da postagem) -> aproximação em dias corridos
        dias = int(order.get("delivery_days") or 7)
        d = local.date() + timedelta(days=dias + 2 * ((dias + 4) // 5))
    if d.weekday() == 6:          # domingo a loja fecha -> segunda
        d += timedelta(days=1)
    return d.isoformat()


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
        # quanto a Lalamove cobra de verdade (frete = o que o cliente pagou)
        await conn.execute(text("ALTER TABLE site_orders ADD COLUMN IF NOT EXISTS frete_lalamove NUMERIC(10,2)"))
        await conn.execute(text("ALTER TABLE site_orders ADD COLUMN IF NOT EXISTS margem NUMERIC(10,2)"))
        await conn.execute(text("ALTER TABLE site_orders ADD COLUMN IF NOT EXISTS margem_obs TEXT"))
        # forma de pagamento: 'pix' ou 'card' (Checkout Pro); link da página de pagamento do MP
        await conn.execute(text("ALTER TABLE site_orders ADD COLUMN IF NOT EXISTS pay_method VARCHAR(10) DEFAULT 'pix'"))
        await conn.execute(text("ALTER TABLE site_orders ADD COLUMN IF NOT EXISTS mp_init_point TEXT"))
        # envio por transportadora (Melhor Envio): nome do serviço, prazo e CPF/CNPJ de quem recebe
        await conn.execute(text("ALTER TABLE site_orders ADD COLUMN IF NOT EXISTS delivery_label VARCHAR(80)"))
        await conn.execute(text("ALTER TABLE site_orders ADD COLUMN IF NOT EXISTS delivery_days INTEGER"))
        await conn.execute(text("ALTER TABLE site_orders ADD COLUMN IF NOT EXISTS customer_doc VARCHAR(20)"))
        # quem fez o pedido (para ligar ao que a pessoa viu antes — admin: Relatórios → Quem quis comprar)
        await conn.execute(text("ALTER TABLE site_orders ADD COLUMN IF NOT EXISTS ip VARCHAR(64)"))
        await conn.execute(text("ALTER TABLE site_orders ADD COLUMN IF NOT EXISTS src VARCHAR(80)"))
    _orders_ready = True


# ── HELPERS ────────────────────────────────────────────────────────────────────
# Número do pedido que o cliente vê = id no banco + PEDIDO_OFFSET (id 3 → "Pedido #1003").
# No banco e nos links (/pedido/3, /site-orders/3) continua o id real.
PEDIDO_OFFSET = int(os.environ.get("PEDIDO_OFFSET", "1000") or 0)


def num_pedido(order_id) -> int:
    return int(order_id) + PEDIDO_OFFSET


def _brl(v: float) -> str:
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


# Produtos que podem entrar no kit de instalação (mesmos slugs do KIT em calculadora-instalacao.html).
# Só estes recebem o desconto do kit — o navegador não decide quem tem desconto.
# Kit de instalação (tubos de cobre, suporte) não vai de moto: só carro.
KIT_BLOCKED_SERVICES = {"LALAGO", "LALAPRO", "UBER"}

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
            SELECT id, name, unit, sale_price, promo_price, promo_until
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
    kit_qty = {}      # kit montado na admin (kits_public) -> quantidade
    for it in raw_items or []:
        if isinstance(it, dict) and it.get("kit_id"):
            try:
                kid, kq = int(it["kit_id"]), int(it.get("qty"))
            except (TypeError, ValueError):
                continue
            if kid > 0 and 0 < kq <= 20:
                kit_qty[kid] = kit_qty.get(kid, 0) + kq
            continue
        try:
            pid, qty = int(it.get("id")), int(it.get("qty"))
        except (TypeError, ValueError, AttributeError):
            continue
        if pid > 0 and 0 < qty <= 100:
            key = (pid, bool(it.get("kit")))
            qty_by_key[key] = qty_by_key.get(key, 0) + qty
    if not qty_by_key and not kit_qty:
        return [], 0.0

    items, subtotal = [], 0.0
    if kit_qty:
        # preço do kit sempre recalculado do banco (mesma conta da admin)
        from kits_public import load_site_kits
        for k in await load_site_kits(list(kit_qty)):
            n = kit_qty[k["id"]]
            items.append({
                "id": -k["id"], "kit_id": k["id"], "unit": "kit",
                "name": k["name"] if k["name"].lower().startswith("kit") else f"Kit {k['name']}",
                "price": k["total"], "qty": n, "subtotal": round(k["total"] * n, 2),
                "kit_name": k["name"], "car_only": k["car_only"],
                # a admin (Registrar venda) transforma em uma venda por produto
                "components": [{"id": c["id"], "name": c["name"], "unit": c["unit"], "qty": c["qty"],
                                "line_total": c["line_total"], "is_gift": c["is_gift"]} for c in k["items"]],
            })
            subtotal += k["total"] * n
    if not qty_by_key:
        return items, round(subtotal, 2)

    products = await _fetch_products({pid for pid, _ in qty_by_key})
    pct = await _kit_discount_pct() if any(k for _, k in qty_by_key) else 0.0

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


# ── ESTOQUE ────────────────────────────────────────────────────────────────────
# Gás no varejo (kg) é vendido só no balcão: não entra em pedido do site.
def _is_kg(unit) -> bool:
    return str(unit or "").strip().lower() == "kg"


def _so_balcao(i: dict) -> bool:
    """kg ou botija: só com o vendedor (mesma regra do so_balcao dos templates)."""
    return _is_kg(i.get("unit")) or "botija" in str(i.get("name") or "").lower()


def _fmt_qty(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:.2f}".rstrip("0").rstrip(".").replace(".", ",")


async def _stock_map(ids) -> dict:
    """id -> estoque atual (mesma conta do catálogo: soma de stock_movements, mínimo 0)."""
    ids = [int(i) for i in ids if i]
    if not ids:
        return {}
    async with engine.connect() as conn:
        rows = (await conn.execute(text("""
            SELECT p.id, GREATEST(0, COALESCE(SUM(sm.qty), 0)) AS stock
            FROM products p
            LEFT JOIN stock_movements sm ON sm.product_id = p.id
            WHERE p.id = ANY(:ids)
            GROUP BY p.id
        """), {"ids": ids})).mappings().all()
    return {r["id"]: float(r["stock"] or 0) for r in rows}


async def _stock_problems(items: list) -> tuple[list, list, dict]:
    """Confere o carrinho (já normalizado por _load_items) contra o estoque.
    Retorna (itens em kg, linhas sem estoque suficiente, estoque por produto).
    Kits contam pelos componentes, somados aos mesmos produtos avulsos."""
    kg = [i for i in items if not i.get("kit_id") and _so_balcao(i)]
    need: dict = {}
    for i in items:
        if i.get("kit_id"):
            for c in i.get("components") or []:
                if c.get("id"):
                    need[c["id"]] = need.get(c["id"], 0) + float(c.get("qty") or 0) * i["qty"]
        elif not _so_balcao(i):
            need[i["id"]] = need.get(i["id"], 0) + i["qty"]
    stock = await _stock_map(need)
    short_ids = {pid for pid, q in need.items() if q > stock.get(pid, 0) + 1e-9}
    short = []
    for i in items:
        if i.get("kit_id"):
            if any(c.get("id") in short_ids for c in i.get("components") or []):
                short.append({"kit_id": i["kit_id"], "name": i["name"]})
        elif i["id"] in short_ids:
            short.append({"id": i["id"], "name": i["name"], "qty": i["qty"], "stock": stock.get(i["id"], 0)})
    return kg, short, stock


def _stock_msg(kg: list, short: list) -> str:
    parts = []
    if kg:
        parts.append("Gás por kg e botijas são vendidos só com o vendedor (na loja ou pelo WhatsApp): " + ", ".join(i["name"] for i in kg)
                     + ". Remova do carrinho ou fale conosco no WhatsApp.")
    if short:
        linhas = []
        for s in short:
            if s.get("kit_id"):
                linhas.append(f'{s["name"]} (faltam peças do kit)')
            elif s["stock"] <= 0:
                linhas.append(f'{s["name"]} (sem estoque)')
            else:
                linhas.append(f'{s["name"]} (temos {_fmt_qty(s["stock"])})')
        parts.append("Estoque insuficiente: " + "; ".join(linhas) + ". Ajuste a quantidade.")
    return " ".join(parts)


async def _calc_frete(cep: str, service: str):
    """Recalcula o frete no servidor. Retorna (preço, geo) ou (None, motivo)."""
    if service not in FRETE_OPTIONS or (service not in LALA_SERVICES and service != "UBER"):
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
        if service == "UBER":
            import uber_direct
            if not uber_direct.ativo():
                return None, "Tipo de entrega indisponível."
            q = await uber_direct.get_quote(geo["lat"], geo["lng"], geo.get("rua") or geo["address"],
                                            cep=cep, cidade=geo["cidade"], uf=geo["uf"])
        else:
            q = await get_quote(geo["lat"], geo["lng"], geo["address"], service)
    except Exception as e:      # LalamoveError / UberError / rede
        _blog_log.warning("checkout: frete %s %s: %s", cep, service, e)
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


async def _mp_create_preference(order_id: int, token: str, total: float, email: str, name: str,
                                description: str) -> dict:
    """Cartão: cria a 'preferência' do Checkout Pro. O cliente paga na página do Mercado Pago
    (dados do cartão nunca passam pelo nosso site; antifraude e 3DS são do MP) e volta para /pedido."""
    minutes = int(os.environ.get("CARTAO_EXPIRA_MIN", "60"))
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=minutes)
    back = f"{SITE_URL}/pedido/{order_id}?t={token}"
    parts = (name or "Cliente").split()
    body = {
        "items": [{
            "id": str(order_id), "title": description[:250], "quantity": 1,
            "unit_price": round(total, 2), "currency_id": "BRL", "category_id": "others",
        }],
        "payer": {"email": email, "name": parts[0], "surname": " ".join(parts[1:])},
        "external_reference": f"site-{order_id}",
        "back_urls": {"success": back, "pending": back, "failure": back},
        "auto_return": "approved",
        "payment_methods": {
            # só cartão: Pix já tem botão próprio; boleto/lotérica não servem para entrega no mesmo dia
            "excluded_payment_types": [{"id": "ticket"}, {"id": "bank_transfer"}, {"id": "atm"}],
            "installments": int(os.environ.get("CARTAO_PARCELAS", "12")),
        },
        "statement_descriptor": "MTF REFRIGERACAO",
        "expires": True,
        "expiration_date_from": now.isoformat(timespec="milliseconds"),
        "expiration_date_to": expires.isoformat(timespec="milliseconds"),
    }
    notification_url = os.environ.get("MP_NOTIFICATION_URL", "").strip()
    if notification_url:
        body["notification_url"] = notification_url
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(f"{MP_API}/checkout/preferences", json=body, headers={
            "Authorization": f"Bearer {_mp_token()}",
            "X-Idempotency-Key": f"mtf-pref-{order_id}",
        })
    data = r.json()
    if r.status_code >= 400:
        raise RuntimeError(f"Mercado Pago {r.status_code}: {data}")
    url = data.get("init_point")
    if _mp_token().startswith("TEST-"):
        url = data.get("sandbox_init_point") or url
    return {"id": data.get("id"), "url": url, "expires_at": expires}


async def _mp_get_payment(payment_id: str) -> dict | None:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{MP_API}/v1/payments/{payment_id}",
                             headers={"Authorization": f"Bearer {_mp_token()}"})
    if r.status_code != 200:
        _blog_log.warning("checkout: MP payment %s -> %s %s", payment_id, r.status_code, r.text[:200])
        return None
    return r.json()


async def _mp_search_card(order_id: int) -> list | None:
    """Pagamentos (tentativas) feitos no Checkout Pro para este pedido, mais recentes primeiro."""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{MP_API}/v1/payments/search", params={
            "external_reference": f"site-{order_id}", "sort": "date_created", "criteria": "desc",
        }, headers={"Authorization": f"Bearer {_mp_token()}"})
    if r.status_code != 200:
        _blog_log.warning("checkout: MP search site-%s -> %s %s", order_id, r.status_code, r.text[:200])
        return None
    return r.json().get("results") or []


def _sem_acento(s: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn").lower()


def _card_info(p: dict, cliente: str) -> str:
    """Detalhes do cartão para o Telegram: ajuda a pegar fraude antes de despachar."""
    card = p.get("card") or {}
    titular = ((card.get("cardholder") or {}).get("name") or "").strip()
    final = card.get("last_four_digits") or "?"
    parc = p.get("installments") or 1
    if not card:
        return "\n\n💳 Pago com saldo/conta Mercado Pago"
    txt = f"\n\n💳 Cartão {p.get('payment_method_id') or ''} final {final} · {parc}x"
    if titular:
        txt += f"\nTitular: {titular}"
        nomes_cli = set(_sem_acento(cliente).split())
        if not (set(_sem_acento(titular).split()) & nomes_cli):
            txt += "\n⚠️ <b>Titular do cartão ≠ nome do cliente</b>"
    liq = (p.get("transaction_details") or {}).get("net_received_amount")
    try:
        if liq and float(liq) > 0:
            bruto = float(p.get("transaction_amount") or 0)
            txt += f"\nVocê recebe: {_brl(float(liq))} (taxa MP {_brl(bruto - float(liq))})"
    except (TypeError, ValueError):
        pass
    txt += "\n👉 Cartão online: antes de despachar, confirme com o cliente no WhatsApp."
    return txt


async def _mark_paid(order_id: int, extra: str = ""):
    """Marca como pago uma única vez e avisa no Telegram."""
    async with engine.begin() as conn:
        row = (await conn.execute(text("""
            UPDATE site_orders SET status = 'paid', paid_at = NOW()
            WHERE id = :id AND status = 'pending'
            RETURNING id, customer_name, customer_phone, delivery_type, delivery_service,
                      address, numero, complemento, items, frete, total, frete_lalamove, margem, margem_obs,
                      pay_method, delivery_label, delivery_days, customer_doc
        """), {"id": order_id})).mappings().first()
    if not row:
        return
    items = row["items"] if isinstance(row["items"], list) else json.loads(row["items"])
    lines = "\n".join(f"  • {i['name']} × {i['qty']}" + (" (kit)" if i.get("kit") else "") for i in items)
    if row["delivery_type"] == "lalamove":
        label = FRETE_OPTIONS.get(row["delivery_service"], (row["delivery_service"], ""))[0]
        pago_loja = (float(row["frete_lalamove"]) - float(row["frete"])) if row.get("frete_lalamove") is not None else 0.0
        entrega = (f"🛵 {label} ({_brl(float(row['frete']))}"
                   + (f" · {'Uber' if row['delivery_service'] == 'UBER' else 'Lalamove'} {_brl(float(row['frete_lalamove']))}, loja paga {_brl(pago_loja)}" if pago_loja > 0.004 else "")
                   + ")\n"
                   f"{row['address']}, nº {row['numero']}"
                   + (f" — {row['complemento']}" if row["complemento"] else ""))
    elif row["delivery_type"] == "correios":
        doc = row.get("customer_doc") or ""
        doc_fmt = (f"{doc[:3]}.{doc[3:6]}.{doc[6:9]}-{doc[9:]}" if len(doc) == 11 else doc)
        entrega = (f"📮 {row.get('delivery_label') or 'Transportadora'} ({_brl(float(row['frete']))})"
                   + (f" · até {row['delivery_days']} dias úteis" if row.get("delivery_days") else "") + "\n"
                   f"{row['address']}, nº {row['numero']}"
                   + (f" — {row['complemento']}" if row["complemento"] else "")
                   + (f"\nCPF/CNPJ: {doc_fmt}" if doc else "")
                   + "\n👉 Gerar etiqueta no Melhor Envio + NF-e (outro estado: CFOP 6102) e postar")
    else:
        entrega = "🏪 Retirada na loja"
    # push no celular (admin instalada como app) — não bloqueia se falhar
    entrega_curta = ("Retirada" if row["delivery_type"] not in ("lalamove", "correios") else
                     (row.get("delivery_label") or "Transportadora") if row["delivery_type"] == "correios" else
                     FRETE_OPTIONS.get(row["delivery_service"], (row["delivery_service"], ""))[0])
    await push.send_all(
        engine, f"💰 Pedido #{num_pedido(row['id'])} pago — {_brl(float(row['total']))}",
        f"{row['customer_name']} · {entrega_curta}",
        url=f"{ADMIN_URL.rstrip('/')}/site-orders/{row['id']}", tag=f"pedido-{row['id']}",
    )
    await _send_tg(
        f"💰 <b>Pedido #{num_pedido(row['id'])} PAGO ({'Cartão' if row.get('pay_method') == 'card' else 'Pix'})</b>\n"
        f"Total: <b>{_brl(float(row['total']))}</b>\n"
        f"Cliente: {row['customer_name']} · {row['customer_phone']}\n\n"
        f"{lines}\n\n{entrega}" + _margem_txt(row) + extra
    )
    await _send_tg(_msg_cliente_pago(row))


def _msg_cliente_pago(row) -> str:
    """Segunda mensagem no Telegram: texto pronto para o cliente + link que abre o WhatsApp dele."""
    import html
    from urllib.parse import quote
    nome = ((row["customer_name"] or "").split() or [""])[0].capitalize()
    if row["delivery_type"] == "correios":
        fim = "Vamos enviar em até 1 dia útil e mandar o código de rastreio por aqui. 📦"
    elif row["delivery_type"] == "lalamove":
        fim = "Avisaremos você assim que ele estiver pronto e o entregador sair para a entrega. 🛵"
    else:
        fim = ("Avisaremos você assim que ele estiver pronto para retirada na loja: "
               "Av. Brasil, 6818 – Bonsucesso (seg. a sáb., 8h30–18h).")
    texto = (f"Olá{', ' + nome if nome else ''}! Aqui é da MTF Refrigeração. 😊\n"
             f"Recebemos o pagamento do seu pedido nº {num_pedido(row['id'])} ✅\n"
             f"Ele já está em processamento e estamos separando os produtos.\n"
             f"{fim}\nObrigado pela compra!")
    tel = re.sub(r"\D", "", row["customer_phone"] or "")
    if len(tel) in (10, 11):
        tel = "55" + tel
    link = f"https://wa.me/{tel}?text={quote(texto)}"
    return (f"📲 <b>Mensagem para o cliente</b> (pedido #{num_pedido(row['id'])})\n"
            f'<a href="{html.escape(link)}">👉 Abrir WhatsApp do cliente com o texto pronto</a>\n\n'
            f"<pre>{html.escape(texto)}</pre>")


def _margem_txt(row) -> str:
    """Linha de margem do aviso de pedido pago (só para a loja)."""
    if row.get("margem") is None:
        return ""
    m = float(row["margem"])
    pago_loja = 0.0
    if row.get("frete_lalamove") is not None:
        pago_loja = max(0.0, float(row["frete_lalamove"]) - float(row["frete"] or 0))
    txt = f"\n\n💰 Margem dos produtos: {_brl(m)}"
    if pago_loja > 0.004:
        txt += f" · menos frete pago pela loja {_brl(pago_loja)} = <b>{_brl(m - pago_loja)}</b>"
    if row.get("margem_obs"):
        txt += f"\n⚠️ {row['margem_obs']} (margem 0 nesses itens)"
    return txt


async def _refresh_status(order: dict) -> str:
    """Consulta o MP se o pedido ainda está pendente."""
    if order["status"] != "pending":
        return order["status"]
    if order.get("pay_method") == "card":
        return await _refresh_card(order)
    if not order.get("mp_payment_id"):
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


async def _refresh_card(order: dict) -> str:
    """Cartão: o cliente pode tentar vários cartões na mesma página do MP — procuramos pelo pedido."""
    pays = await _mp_search_card(order["id"])
    if pays is None:
        return "pending"
    ok = next((p for p in pays if p.get("status") == "approved"), None)
    if ok:
        async with engine.begin() as conn:
            await conn.execute(text(
                "UPDATE site_orders SET mp_payment_id = :pid WHERE id = :id AND status = 'pending'"
            ), {"pid": str(ok["id"]), "id": order["id"]})
        await _mark_paid(order["id"], _card_info(ok, order.get("customer_name") or ""))
        return "paid"
    em_analise = any(p.get("status") in ("pending", "in_process", "authorized") for p in pays)
    exp = order.get("pix_expires_at")
    if not em_analise and exp and datetime.now(timezone.utc) > exp + timedelta(minutes=15):
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
                    SELECT id, status, mp_payment_id, pay_method, pix_expires_at, customer_name FROM site_orders
                    WHERE status = 'pending' AND (mp_payment_id IS NOT NULL OR pay_method = 'card')
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
    order["frete_label"] = (order.get("delivery_label") or "Transportadora") if order.get("delivery_type") == "correios" \
        else FRETE_OPTIONS.get(order.get("delivery_service") or "", ("", ""))[0]
    order["pix_expires_iso"] = order["pix_expires_at"].isoformat() if order.get("pix_expires_at") else ""
    order["gcr_delivery"] = _gcr_delivery_date(order)
    order["num"] = num_pedido(order["id"])
    return templates.TemplateResponse("pedido.html", _page_ctx(request, order=order,
                                                              gcr_merchant_id=GCR_MERCHANT_ID))


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
@router.post("/api/checkout/pagar")
async def checkout_pix(request: Request):
    ip = _client_ip(request)
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
    method = "card" if str(body.get("method", "")).lower() in ("card", "cartao", "cartão") else "pix"

    items, subtotal = await _load_items(body.get("items"))
    if not items:
        return JSONResponse({"error": "Seu carrinho está vazio."}, status_code=400)
    asked = {int(i["id"]) for i in (body.get("items") or []) if str(i.get("id", "")).isdigit()}
    missing = asked - {i["id"] for i in items}
    asked_kits = {int(i["kit_id"]) for i in (body.get("items") or [])
                  if isinstance(i, dict) and str(i.get("kit_id", "")).isdigit()}
    if asked_kits - {i.get("kit_id") for i in items}:
        return JSONResponse({"error": "Um kit do carrinho não está mais disponível. "
                                      "Remova-o do carrinho ou fale conosco no WhatsApp."}, status_code=409)
    if missing:
        return JSONResponse({"error": "Algum produto do carrinho não está mais disponível. "
                                      "Remova-o do carrinho ou fale conosco no WhatsApp.",
                             "missing": sorted(missing)}, status_code=409)

    kg, short, _ = await _stock_problems(items)
    if kg or short:
        return JSONResponse({"error": _stock_msg(kg, short), "stock_issue": True}, status_code=409)

    # margem dos produtos (custo da admin; item sem custo conta 0) — para o frete e para o aviso no Telegram
    try:
        margem, sem_custo = await margem_detalhe(linhas_dos_itens(items))
    except Exception:
        _blog_log.exception("checkout: falha ao calcular margem")
        margem, sem_custo = 0.0, []

    delivery = body.get("delivery") or {}
    dtype = "lalamove" if delivery.get("type") == "lalamove" else "retirada"
    frete, geo = 0.0, None
    cep = numero = complemento = address = service = None
    me_label = me_dias = doc = None
    if dtype == "lalamove" and str(delivery.get("service", "")).startswith("ME-"):
        # fora da área da Lalamove: Correios/transportadora (Melhor Envio). Preço recalculado aqui no servidor.
        import correios
        dtype = "correios"
        cep = re.sub(r"\D", "", str(delivery.get("cep", "")))
        service = str(delivery.get("service", ""))[:20]
        numero = str(delivery.get("numero", "")).strip()[:20]
        complemento = str(delivery.get("complemento", "")).strip()[:120]
        rua = str(delivery.get("rua", "")).strip()[:200]
        if len(cep) != 8:
            return JSONResponse({"error": "Informe um CEP válido."}, status_code=400)
        if not rua or not numero:
            return JSONResponse({"error": "Informe a rua e o número para a entrega."}, status_code=400)
        doc = correios.doc_ok(delivery.get("doc") or "")
        if not doc:
            return JSONResponse({"error": "Informe o CPF (ou CNPJ) de quem vai receber — a transportadora exige."},
                                status_code=400)
        if not correios.ativo():
            return JSONResponse({"error": "Envio por transportadora indisponível agora. Fale conosco no WhatsApp."},
                                status_code=503)
        q = await correios.cotar(cep, items)
        if q.get("bloqueados"):
            return JSONResponse({"error": "Estes itens não podem ir por transportadora (gás/inflamável): "
                                          + ", ".join(q["bloqueados"][:5]) + ". Retire na loja ou fale conosco."},
                                status_code=400)
        opt = next((o for o in q.get("options") or [] if o["service"] == service), None)
        if not opt:
            return JSONResponse({"error": "Esse tipo de envio não está mais disponível. Calcule o frete de novo."},
                                status_code=400)
        info = await correios.cep_info(cep) or {}
        frete, me_label, me_dias = float(opt["price"]), opt["label"][:80], opt.get("dias")
        geo = {"bairro": info.get("bairro", ""), "cidade": info.get("cidade", ""), "uf": info.get("uf", ""),
               "lat": None, "lng": None}
        address = ", ".join(x for x in [rua, geo["bairro"], f'{geo["cidade"]} - {geo["uf"]}' if geo["cidade"] else "",
                                        f"{cep[:5]}-{cep[5:]}"] if x)
    if dtype == "lalamove":
        cep = re.sub(r"\D", "", str(delivery.get("cep", "")))
        service = str(delivery.get("service", ""))
        if service in KIT_BLOCKED_SERVICES and any(i.get("kit") or i.get("car_only") for i in items):
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
        # mesma regra do /api/frete-cep: pedido > R$ 50 -> loja paga parte do frete (até metade da margem)
        frete_lalamove = frete
        if subtotal > FRETE_SUBSIDIO_MIN:
            try:
                frete = frete_final(frete, subtotal, margem)
            except Exception:
                _blog_log.exception("checkout: falha no desconto do frete (segue preço cheio)")
        address = ", ".join(x for x in [rua, geo["bairro"], f'{geo["cidade"]} - {geo["uf"]}',
                                        f"{cep[:5]}-{cep[5:]}"] if x)
        # Pino exato (rua + número) para o entregador. O preço continua o do CEP, que o cliente viu.
        geo = dict(geo)
        try:
            exact = await geocode_endereco(rua, numero, geo["bairro"], geo["cidade"], geo["uf"], cep)
        except Exception:
            _blog_log.exception("checkout: geocode do endereço falhou")
            exact = None
        if exact and _km(exact, (geo["lat"], geo["lng"])) <= 4:
            geo["lat"], geo["lng"] = exact
        elif exact:
            _blog_log.warning("checkout: pino %s longe demais do CEP %s — mantido o do CEP", exact, cep)

    if dtype != "lalamove":
        frete_lalamove = None
    total = round(subtotal + (frete or 0), 2)
    if method == "card":
        try:
            card_max = float(os.environ.get("CARTAO_MAX", "0") or 0)
        except ValueError:
            card_max = 0
        if card_max and total > card_max:
            return JSONResponse({"error": f"Para pedidos acima de {_brl(card_max)}, o pagamento é só por Pix. "
                                          "Dúvidas? Fale conosco no WhatsApp."}, status_code=400)
    token = secrets.token_urlsafe(16)

    await _ensure_orders_table()
    async with engine.begin() as conn:
        order_id = (await conn.execute(text("""
            INSERT INTO site_orders (token, customer_name, customer_phone, customer_email,
                delivery_type, delivery_service, cep, address, numero, complemento, lat, lng,
                items, subtotal, frete, total, frete_lalamove, margem, margem_obs, pay_method,
                delivery_label, delivery_days, customer_doc, ip, src)
            VALUES (:token, :name, :phone, :email, :dtype, :service, :cep, :address, :numero, :compl,
                :lat, :lng, CAST(:items AS JSONB), :subtotal, :frete, :total, :frete_lalamove, :margem, :margem_obs,
                :pay_method, :delivery_label, :delivery_days, :customer_doc, :ip, :src)
            RETURNING id
        """), {
            "token": token, "name": name, "phone": phone, "email": email,
            "dtype": dtype, "service": service, "cep": cep, "address": address,
            "numero": numero, "compl": complemento,
            "lat": geo["lat"] if isinstance(geo, dict) else None,
            "lng": geo["lng"] if isinstance(geo, dict) else None,
            "items": json.dumps(items, ensure_ascii=False),
            "subtotal": subtotal, "frete": frete or 0, "total": total, "frete_lalamove": frete_lalamove,
            "margem": margem, "margem_obs": ("sem custo: " + ", ".join(sem_custo))[:500] if sem_custo else None,
            "pay_method": method,
            "delivery_label": me_label, "delivery_days": me_dias, "customer_doc": doc,
            "ip": _client_ip(request), "src": str(body.get("src") or "")[:80] or None,
        })).scalar()

    if method == "card":
        try:
            pref = await _mp_create_preference(order_id, token, total, email, name,
                                               f"MTF Refrigeração — pedido #{num_pedido(order_id)}")
            if not pref["url"]:
                raise RuntimeError("Mercado Pago sem init_point")
        except Exception:
            _blog_log.exception("checkout: falha ao criar pagamento com cartão do pedido %s", order_id)
            async with engine.begin() as conn:
                await conn.execute(text("UPDATE site_orders SET status = 'error' WHERE id = :id"), {"id": order_id})
            return JSONResponse({"error": "Não foi possível abrir o pagamento com cartão. "
                                          "Tente novamente ou pague com Pix."}, status_code=502)
        async with engine.begin() as conn:
            await conn.execute(text(
                "UPDATE site_orders SET mp_init_point = :u, pix_expires_at = :exp WHERE id = :id"
            ), {"u": pref["url"], "exp": pref["expires_at"], "id": order_id})
        await _send_tg(f"🧾 Pedido #{num_pedido(order_id)} criado no site — aguardando cartão ({_brl(total)})")
        return JSONResponse({"order_id": order_id, "token": token, "url": pref["url"],
                             "pedido": f"/pedido/{order_id}?t={token}"})

    try:
        pix = await _mp_create_pix(order_id, total, email, name, f"MTF Refrigeração — pedido #{num_pedido(order_id)}")
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

    await _send_tg(f"🧾 Pedido #{num_pedido(order_id)} criado no site — aguardando Pix ({_brl(total)})")
    return JSONResponse({"order_id": order_id, "token": token, "url": f"/pedido/{order_id}?t={token}"})


_checkout_vistos: dict = {}  # (ip, carrinho) -> timestamp do último aviso


def _avisar_checkout_aberto(ip: str, items: list, subtotal: float, src: str = ""):
    """Telegram assim que alguém abre o checkout com produtos (antes de pagar).
    Mesmo IP + mesmo carrinho: no máximo um aviso a cada 30 min."""
    import asyncio
    now = time.time()
    chave = (ip, tuple(sorted((str(i.get("id")), str(i.get("qty"))) for i in items)))
    if now - _checkout_vistos.get(chave, 0) < 1800:
        return
    _checkout_vistos[chave] = now
    for k, t in list(_checkout_vistos.items()):     # limpa os antigos
        if now - t > 7200:
            _checkout_vistos.pop(k, None)
    ids = set()
    for i in items:
        if i.get("components"):
            ids.update(c.get("id") for c in i["components"])
        else:
            ids.add(i.get("id"))
    asyncio.ensure_future(log_product_events(ids, "checkout", ip, None, src or None))
    linhas = "\n".join(f"  • {i['name']} × {i['qty']}" for i in items)
    asyncio.ensure_future(_send_tg(
        f"👀 <b>Alguém abriu o checkout</b> (ainda não pagou)\n{linhas}\n"
        f"Produtos: <b>{_brl(float(subtotal or 0))}</b>"
        + (f"\nVeio de: <b>{src}</b>" if src else "")
    ))


@router.post("/api/checkout/stock")
async def checkout_stock(request: Request):
    """O checkout chama ao abrir: diz o que tirar/ajustar no carrinho antes do cliente preencher tudo."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Dados inválidos."}, status_code=400)
    items, subtotal = await _load_items(body.get("items"))
    if items and not _is_owner(request):
        _avisar_checkout_aberto(_client_ip(request), items, subtotal, str(body.get("src") or "")[:80])
    kg, short, stock = await _stock_problems(items)
    return JSONResponse({
        "kg": [i["id"] for i in kg],
        "stock": {str(s["id"]): s["stock"] for s in short if s.get("id")},
        "kits": [s["kit_id"] for s in short if s.get("kit_id")],
        "message": _stock_msg(kg, short),
    }, headers={"Cache-Control": "no-store"})


@router.get("/api/checkout/status/{order_id}")
async def checkout_status(order_id: int, t: str = ""):
    await _ensure_orders_table()
    async with engine.connect() as conn:
        order = (await conn.execute(text(
            "SELECT id, status, mp_payment_id, pix_expires_at, pay_method, customer_name "
            "FROM site_orders WHERE id = :id AND token = :t"
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
    qp = request.query_params
    topic = str(body.get("type") or body.get("topic") or qp.get("type") or qp.get("topic") or "")
    payment_id = str((body.get("data") or {}).get("id") or qp.get("data.id") or "")
    if not payment_id and topic == "payment":
        payment_id = str(qp.get("id") or "")
    if not payment_id or (topic and topic != "payment"):   # merchant_order etc.: ignoramos
        return JSONResponse({"ok": True})
    await _ensure_orders_table()
    cols = "id, status, mp_payment_id, pay_method, pix_expires_at, customer_name"
    async with engine.connect() as conn:
        order = (await conn.execute(text(
            f"SELECT {cols} FROM site_orders WHERE mp_payment_id = :pid"
        ), {"pid": payment_id})).mappings().first()
    if not order:
        # cartão: o id do pagamento só existe depois que o cliente paga -> achamos pelo external_reference
        try:
            p = await _mp_get_payment(payment_id)
        except Exception:
            p = None
        ref = str((p or {}).get("external_reference") or "")
        if ref.startswith("site-") and ref[5:].isdigit():
            async with engine.connect() as conn:
                order = (await conn.execute(text(
                    f"SELECT {cols} FROM site_orders WHERE id = :id AND pay_method = 'card'"
                ), {"id": int(ref[5:])})).mappings().first()
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
