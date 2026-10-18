"""
Kits montados na admin (/kits) mostrados no site.

Arquivo: catalog/kits_public.py
Incluir no main.py do catálogo:
  from kits_public import router as kits_router
  app.include_router(kits_router)

A conta do preço é a MESMA da admin (admin/routers/kits.py): desconto em % ou preço fixo
rateado pelos itens pagos; brinde = R$ 0. Se mudar lá, mude aqui também.
Custo e margem NUNCA saem daqui (é público).
"""
from decimal import Decimal, ROUND_HALF_UP

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from dependencies import engine

router = APIRouter()


def _money(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _admin_url() -> str:
    try:
        from router import ADMIN_URL
        return ADMIN_URL
    except Exception:
        return ""


async def load_site_kits(kit_ids: list[int] | None = None) -> list[dict]:
    """Kits ativos e marcados 'mostrar no site'. Lista vazia se a admin ainda não criou as tabelas."""
    try:
        async with engine.connect() as conn:
            if not (await conn.execute(text("SELECT to_regclass('public.kits')"))).scalar():
                return []
            where = "k.active = TRUE AND k.show_on_site = TRUE"
            params = {}
            if kit_ids:
                where += " AND k.id = ANY(:ids)"
                params["ids"] = list(kit_ids)
            kits = [dict(r) for r in (await conn.execute(text(
                f"SELECT id, name, description, discount_percent, fixed_price, car_only "
                f"FROM kits k WHERE {where} ORDER BY k.name"
            ), params)).mappings().all()]
            if not kits:
                return []
            rows = (await conn.execute(text("""
                SELECT ki.kit_id, ki.product_id, ki.qty, ki.is_gift, p.name, p.unit, p.sale_price, p.image, p.active
                FROM kit_items ki JOIN products p ON p.id = ki.product_id
                WHERE ki.kit_id = ANY(:ids)
                ORDER BY ki.id
            """), {"ids": [k["id"] for k in kits]})).mappings().all()
    except Exception:
        import logging
        logging.getLogger("uvicorn.error").exception("kits do site: falha ao carregar")
        return []

    admin = _admin_url()
    try:
        from router import _nice_name          # mesmo "nome bonito" do resto do site
    except Exception:
        _nice_name = lambda n: n
    by_kit: dict[int, list] = {}
    for r in rows:
        by_kit.setdefault(r["kit_id"], []).append(dict(r))

    out = []
    for k in kits:
        its = by_kit.get(k["id"], [])
        # produto desativado ou sem preço: o kit some do site (não dá para vender certo)
        if not its or any(not it["active"] or (not it["is_gift"] and not it["sale_price"]) for it in its):
            continue
        pct = Decimal(str(k["discount_percent"] or 0))
        fixed = Decimal(str(k["fixed_price"])) if k["fixed_price"] is not None else None
        raw, full, gift_value = [], Decimal(0), Decimal(0)
        for it in its:
            price = Decimal(str(it["sale_price"] or 0))
            qty = Decimal(str(it["qty"]))
            line_full = _money(qty * price)
            if it["is_gift"]:
                gift_value += line_full
            else:
                full += line_full
            raw.append((it, price, qty, bool(it["is_gift"]), line_full))
        paid = [i for i, r in enumerate(raw) if not r[3]]
        if not paid:
            continue
        lines = [Decimal(0)] * len(raw)
        if fixed is not None and full > 0:
            acc = Decimal(0)
            for n, i in enumerate(paid):
                lt = fixed - acc if n == len(paid) - 1 else _money(fixed * raw[i][4] / full)
                acc += lt
                lines[i] = lt
        else:
            factor = (Decimal(100) - pct) / Decimal(100)
            for i in paid:
                it, price, qty, _, _ = raw[i]
                lines[i] = _money(qty * _money(price * factor))
        total = sum(lines, Decimal(0))
        items = []
        for (it, price, qty, gift, line_full), lt in zip(raw, lines):
            img = it["image"] or ""
            if img.startswith("/static/images/"):
                img = admin + img
            items.append({
                "id": it["product_id"], "name": _nice_name(it["name"]), "unit": it["unit"] or "un",
                "qty": float(qty), "price": float(price), "line_total": float(lt),
                "is_gift": gift, "image": img,
            })
        out.append({
            "id": k["id"], "name": _nice_name(k["name"]), "description": k["description"] or "",
            "car_only": bool(k["car_only"]),
            "full_total": float(full + gift_value),      # quanto custaria tudo separado (com brindes)
            "total": float(total),
            "save": float(full + gift_value - total),
            "gift_value": float(gift_value),
            "items": items,
        })
    return out


@router.get("/api/kits-site")
async def api_kits_site():
    return JSONResponse(await load_site_kits(), headers={"Cache-Control": "public, max-age=60"})
