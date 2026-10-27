"""
Lalamove para vendas do balcão / WhatsApp (página "Finalizar Pedido" da admin).

Fluxo: CEP -> rua/bairro (ViaCEP) -> número -> "Cotar" (Google Geocoding acha o pino da porta,
ou usa a localização que o cliente mandou no WhatsApp) -> escolher Motoboy/Carro/Baú -> "Chamar entregador".

Variáveis de ambiente (admin):
  GOOGLE_GEOCODING_KEY (ou GOOGLE_MAPS_KEY) — a mesma chave do catálogo. Sem ela, só funciona colando a localização.
  MTF_LAT / MTF_LNG / MTF_PHONE / LALAMOVE_* — já usadas pelos pedidos do site
  LALA_SERVICES (opcional, padrão LALAGO,HATCHBACK,LALAPRO)
"""
import asyncio
import logging
import os
import re

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from dependencies import engine, basic_auth

try:
    from routers.lalamove import get_quote, place_order, cancel_order, LalamoveError
except ImportError:  # pragma: no cover
    from lalamove import get_quote, place_order, cancel_order, LalamoveError

router = APIRouter()
log = logging.getLogger("uvicorn.error")

SERVICOS = {   # mesma ordem/nomes do site
    "LALAGO":    "Motoboy",
    "HATCHBACK": "Carro",
    "LALAPRO":   "Moto com baú",
}
ATIVOS = [s for s in os.environ.get("LALA_SERVICES", "LALAGO,HATCHBACK,LALAPRO").split(",") if s in SERVICOS]

_ready = False


async def _ensure_table():
    global _ready
    if _ready:
        return
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS lalamove_balcao (
                id                SERIAL PRIMARY KEY,
                created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                lalamove_order_id VARCHAR(40),
                share_link        TEXT,
                service           VARCHAR(20),
                price             NUMERIC(10,2),
                customer_name     VARCHAR(120),
                customer_phone    VARCHAR(30),
                address           TEXT,
                lat               DOUBLE PRECISION,
                lng               DOUBLE PRECISION,
                obs               TEXT,
                status            VARCHAR(20) DEFAULT 'chamado'
            )
        """))
    _ready = True


def _google_key() -> str:
    return (os.environ.get("GOOGLE_GEOCODING_KEY") or os.environ.get("GOOGLE_MAPS_KEY") or "").strip()


async def _tg(msg: str):
    token, chat = os.environ.get("TG_BOT_TOKEN", ""), os.environ.get("TG_CHAT_ID", "")
    if not token or not chat:
        return
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            await c.post(f"https://api.telegram.org/bot{token}/sendMessage",
                         json={"chat_id": chat, "text": msg, "parse_mode": "HTML",
                               "disable_web_page_preview": True})
    except Exception:
        pass


def _coords_do_texto(s: str):
    """Localização colada (link do Google Maps / WhatsApp, ou '-22.87, -43.25') -> (lat, lng) ou None."""
    s = (s or "").strip()
    if not s:
        return None
    for pat in (r"@(-?\d+\.\d+),(-?\d+\.\d+)", r"[?&](?:q|query|ll|destination)=(-?\d+\.\d+)(?:,|%2C)\s*(-?\d+\.\d+)",
                r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)", r"(-?\d{1,2}\.\d{3,})\s*,\s*(-?\d{1,3}\.\d{3,})"):
        m = re.search(pat, s)
        if m:
            lat, lng = float(m.group(1)), float(m.group(2))
            if -34 < lat < 6 and -75 < lng < -30:      # dentro do Brasil
                return lat, lng
    return None


async def _cep(cep: str):
    async with httpx.AsyncClient(timeout=8) as c:
        try:
            r = await c.get(f"https://viacep.com.br/ws/{cep}/json/")
            j = r.json()
            if r.status_code == 200 and not j.get("erro"):
                return {"rua": j.get("logradouro") or "", "bairro": j.get("bairro") or "",
                        "cidade": j.get("localidade") or "", "uf": j.get("uf") or ""}
        except Exception:
            pass
        try:
            r = await c.get(f"https://brasilapi.com.br/api/cep/v2/{cep}")
            if r.status_code == 200:
                j = r.json()
                return {"rua": j.get("street") or "", "bairro": j.get("neighborhood") or "",
                        "cidade": j.get("city") or "", "uf": j.get("state") or ""}
        except Exception:
            pass
    return None


async def _geocode(endereco: str):
    key = _google_key()
    if not key:
        return None
    async with httpx.AsyncClient(timeout=8) as c:
        r = await c.get("https://maps.googleapis.com/maps/api/geocode/json",
                        params={"address": endereco, "key": key, "region": "br", "language": "pt-BR"})
    j = r.json()
    if j.get("status") != "OK" or not j.get("results"):
        log.warning("lalamove-balcao: geocode %s -> %s", endereco, j.get("status"))
        return None
    res = j["results"][0]
    loc = res["geometry"]["location"]
    return float(loc["lat"]), float(loc["lng"]), res.get("formatted_address") or endereco, \
        res["geometry"].get("location_type", "")


@router.get("/api/lalamove/cep/{cep}")
async def lala_cep(cep: str, _=Depends(basic_auth)):
    cep = re.sub(r"\D", "", cep)
    if len(cep) != 8:
        return JSONResponse({"error": "CEP inválido"}, status_code=400)
    d = await _cep(cep)
    if not d:
        return JSONResponse({"error": "CEP não encontrado"}, status_code=404)
    return d


@router.post("/api/lalamove/cotar")
async def lala_cotar(request: Request, _=Depends(basic_auth)):
    b = await request.json()
    cep = re.sub(r"\D", "", str(b.get("cep", "")))
    rua, numero = str(b.get("rua", "")).strip(), str(b.get("numero", "")).strip()
    bairro, cidade, uf = str(b.get("bairro", "")).strip(), str(b.get("cidade", "")).strip(), str(b.get("uf", "")).strip()
    compl = str(b.get("complemento", "")).strip()

    endereco = ", ".join(x for x in [f"{rua}, {numero}" if rua else "", bairro,
                                     f"{cidade} - {uf}" if cidade else "",
                                     f"{cep[:5]}-{cep[5:]}" if len(cep) == 8 else ""] if x)
    aviso = ""
    pin = _coords_do_texto(str(b.get("localizacao", "")))
    if pin:
        lat, lng = pin
        if not endereco:
            endereco = f"Localização enviada pelo cliente ({lat:.5f}, {lng:.5f})"
    else:
        if not rua or not numero:
            return JSONResponse({"error": "Informe rua e número (ou cole a localização do cliente)."}, status_code=400)
        if not _google_key():
            return JSONResponse({"error": "Sem GOOGLE_GEOCODING_KEY na admin: cole a localização que o cliente mandou "
                                          "no WhatsApp (ou adicione a chave no Railway)."}, status_code=400)
        g = await _geocode(endereco)
        if not g:
            return JSONResponse({"error": "Não achei esse endereço no mapa. Confira ou cole a localização do cliente."},
                                status_code=400)
        lat, lng, fmt, tipo = g
        if tipo not in ("ROOFTOP", "RANGE_INTERPOLATED"):
            aviso = "Pino aproximado (rua/bairro). Confira no mapa ou peça a localização ao cliente."

    full = endereco + (f" — {compl}" if compl else "")

    async def q(s):
        try:
            r = await get_quote(lat, lng, full, s)
            return {"service": s, "label": SERVICOS[s], "price": float(r["price"]),
                    "km": round(int(r["distance_m"] or 0) / 1000, 1)}
        except LalamoveError as e:
            log.warning("lalamove-balcao: quote %s: %s", s, e)
            return None
    opts = [o for o in await asyncio.gather(*(q(s) for s in ATIVOS)) if o]
    if not opts:
        return JSONResponse({"error": "A Lalamove não cotou esse endereço."}, status_code=502)
    return {"lat": lat, "lng": lng, "address": full, "aviso": aviso,
            "maps": f"https://www.google.com/maps?q={lat},{lng}", "options": opts}


@router.post("/api/lalamove/chamar")
async def lala_chamar(request: Request, _=Depends(basic_auth)):
    b = await request.json()
    try:
        lat, lng = float(b["lat"]), float(b["lng"])
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"error": "Cote o frete primeiro."}, status_code=400)
    service = str(b.get("service", ""))
    if service not in SERVICOS:
        return JSONResponse({"error": "Escolha o tipo de entrega."}, status_code=400)
    name = str(b.get("name", "")).strip()[:120]
    phone = re.sub(r"\D", "", str(b.get("phone", "")))
    if phone.startswith("55") and len(phone) in (12, 13):
        phone = phone[2:]
    if len(name) < 2 or len(phone) not in (10, 11):
        return JSONResponse({"error": "Informe nome e celular do cliente (com DDD)."}, status_code=400)
    address = str(b.get("address", ""))[:300]
    obs = str(b.get("obs", "")).strip()[:200]

    await _ensure_table()
    async with engine.begin() as conn:
        rid = (await conn.execute(text("""
            INSERT INTO lalamove_balcao (service, customer_name, customer_phone, address, lat, lng, obs, status)
            VALUES (:s, :n, :p, :a, :lat, :lng, :o, 'cotando') RETURNING id
        """), {"s": service, "n": name, "p": phone, "a": address, "lat": lat, "lng": lng, "o": obs or None})).scalar()
    try:
        quote = await get_quote(lat, lng, address, service)       # cotação nova (a da tela vale só 5 min)
        res = await place_order(quote, name, "+55" + phone,
                                remarks=f"MTF balcão #{rid}. {address}" + (f". {obs}" if obs else ""),
                                sale_id=None)
    except LalamoveError as e:
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE lalamove_balcao SET status = 'erro' WHERE id = :id"), {"id": rid})
        return JSONResponse({"error": f"Lalamove: {str(e.payload)[:200]}"}, status_code=502)

    price = float(quote["price"])
    async with engine.begin() as conn:
        await conn.execute(text("""
            UPDATE lalamove_balcao SET lalamove_order_id = :lid, share_link = :link, price = :pr, status = 'chamado'
            WHERE id = :id
        """), {"lid": res["order_id"], "link": res.get("share_link"), "pr": price, "id": rid})
    await _tg(f"🛵 <b>Lalamove chamado — venda balcão #{rid}</b>\n"
              f"{SERVICOS[service]} · R$ {price:.2f}\n{name} · {phone}\n{address}"
              + (f"\n📝 {obs}" if obs else "")
              + (f"\n<a href=\"{res['share_link']}\">Acompanhar</a>" if res.get("share_link") else ""))
    return {"id": rid, "order_id": res["order_id"], "share_link": res.get("share_link"),
            "price": price, "label": SERVICOS[service]}


@router.post("/api/lalamove/cancelar/{rid}")
async def lala_cancelar(rid: int, _=Depends(basic_auth)):
    await _ensure_table()
    async with engine.connect() as conn:
        lid = (await conn.execute(text("SELECT lalamove_order_id FROM lalamove_balcao WHERE id = :id"),
                                  {"id": rid})).scalar()
    if not lid:
        return JSONResponse({"error": "Entrega não encontrada"}, status_code=404)
    try:
        await cancel_order(lid)
    except LalamoveError as e:
        return JSONResponse({"error": "A Lalamove não deixou cancelar (entregador já a caminho há mais de 5 min?). "
                                      f"Resolva no app. {str(e.payload)[:120]}"}, status_code=502)
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE lalamove_balcao SET status = 'cancelado' WHERE id = :id"), {"id": rid})
    await _tg(f"⚠️ Lalamove da venda balcão #{rid} cancelado")
    return {"ok": True}
