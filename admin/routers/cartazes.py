"""
Cartazes para a loja: escolhe promoções / kits / qualquer produto e imprime
cartazes A4, A5 (2 por folha) ou etiquetas A6 (4 por folha), com QR code
que abre a página do produto ou do kit no site (utm_source=cartaz).

Arquivos:
  admin/routers/cartazes.py      (este)
  admin/templates/cartazes.html
"""
import os
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import text

from dependencies import engine, templates, basic_auth
from routers.kit_discount import _slugify
from routers.kits import _ensure_tables as _ensure_kits, _load_kits
from routers.products import _ensure_promo_column

router = APIRouter()
BRT = timezone(timedelta(hours=-3))


def _site() -> str:
    return os.environ.get("SITE_URL", "https://www.mtfrefrigeracao.com.br").rstrip("/")


def _nice(name: str) -> str:
    n = " ".join((name or "").split())
    return n[:1].upper() + n[1:] if n else n


def _foto(img) -> str:
    img = (img or "").strip()
    return img if img.startswith(("http", "/static", "data:image")) else ""


def _produto(r, hoje: date) -> dict:
    """Linha de products -> dados do cartaz (preço atual, promo, 2ª unidade)."""
    sale = float(r["sale_price"] or 0)
    promo = float(r["promo_price"]) if r["promo_price"] is not None else None
    on_promo = bool(promo and sale and promo < sale and (not r["promo_until"] or r["promo_until"] >= hoje))
    price = promo if on_promo else sale
    p2 = float(r["promo2_pct"]) if r["promo2_pct"] is not None else None
    if not p2 or not (1 <= p2 <= 90) or (r["promo2_until"] and r["promo2_until"] < hoje):
        p2 = None
    ate = [d for d in ((r["promo_until"] if on_promo else None), (r["promo2_until"] if p2 else None)) if d]
    return {
        "key": f"p{r['id']}", "tipo": "produto", "id": r["id"],
        "nome": _nice(r["name"]), "unit": r["unit"] or "un",
        "preco": round(price, 2), "de": round(sale, 2) if on_promo else None,
        "p2": p2, "par": round(price + price * (100 - p2) / 100, 2) if p2 else None,
        "ate": min(ate).isoformat() if ate else "",
        "url": f"{_site()}/product/{_slugify(r['name'])}?utm_source=cartaz",
        "foto": _foto(r["image"]),
    }


_COLS = "id, name, unit, sale_price, promo_price, promo_until, promo2_pct, promo2_until, image"


@router.get("/cartazes", response_class=HTMLResponse)
async def cartazes_page(request: Request, _=Depends(basic_auth)):
    hoje = datetime.now(BRT).date()
    await _ensure_kits()
    async with engine.begin() as conn:
        await _ensure_promo_column(conn)
        rows = (await conn.execute(text(f"""
            SELECT {_COLS} FROM products
            WHERE active = TRUE AND (
                  (promo_price IS NOT NULL AND promo_price < sale_price AND (promo_until IS NULL OR promo_until >= :hoje))
               OR (promo2_pct BETWEEN 1 AND 90 AND (promo2_until IS NULL OR promo2_until >= :hoje)))
            ORDER BY name
        """), {"hoje": hoje})).mappings().all()
        kits = await _load_kits(conn, only_active=True)
    itens = []                      # kits primeiro (com arte pronta = banner), depois os produtos
    for k in kits:
        if not k["items"] or k["total"] <= 0:
            continue
        itens.append({
            "key": f"k{k['id']}", "tipo": "kit", "id": k["id"], "nome": _nice(k["name"]), "unit": "kit",
            "preco": round(k["total"], 2),
            "de": round(k["full_total"], 2) if k["full_total"] - k["total"] >= 0.01 else None,
            "p2": None, "par": None, "ate": "",
            "url": f"{_site()}/kit/{k['id']}?utm_source=cartaz",
            "foto": _foto(k.get("banner")),
            "lista": [f"{i['qty']:g} {i['unit']} {i['name']}" for i in k["items"]][:6],
            "no_site": bool(k.get("show_on_site")),
            "banner": bool(_foto(k.get("banner"))),
        })
    itens += [_produto(r, hoje) for r in rows]
    return templates.TemplateResponse("cartazes.html", {
        "request": request, "active_page": "cartazes", "itens": itens, "site_url": _site(),
        "store_phone": "(21) 97153-8839",
    })


@router.get("/api/cartazes/busca")
async def cartazes_busca(q: str = Query(""), _=Depends(basic_auth)):
    """Qualquer produto ativo (ex.: novidade, preço normal) para virar cartaz."""
    q = q.strip()
    if len(q) < 2:
        return JSONResponse([])
    hoje = datetime.now(BRT).date()
    termos = [t for t in q.split() if t][:5]
    where = " AND ".join(f"name ILIKE :t{i}" for i in range(len(termos)))
    async with engine.connect() as conn:
        rows = (await conn.execute(text(
            f"SELECT {_COLS} FROM products WHERE active = TRUE AND {where} ORDER BY name LIMIT 15"
        ), {f"t{i}": f"%{t}%" for i, t in enumerate(termos)})).mappings().all()
    return JSONResponse([_produto(r, hoje) for r in rows])
