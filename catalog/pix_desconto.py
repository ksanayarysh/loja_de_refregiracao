"""Desconto no Pix (site).

Regra: PIX_DESCONTO_PCT % (padrão 3) nos produtos avulsos pagos por Pix.
Fica de fora: kit, item de kit, "2ª unidade", produto em promoção, pedido com preço de técnico
e produto com margem menor que PIX_MARGEM_MIN % (padrão 15) ou sem custo cadastrado.
"""
import os

from sqlalchemy import text

from dependencies import engine


def pix_pct() -> float:
    try:
        return max(0.0, min(10.0, float(os.environ.get("PIX_DESCONTO_PCT", "3") or 0)))
    except ValueError:
        return 0.0


def _margem_min() -> float:
    try:
        return float(os.environ.get("PIX_MARGEM_MIN", "15") or 15) / 100
    except ValueError:
        return 0.15


async def ids_elegiveis(ids) -> set:
    """Produtos que podem ter o desconto do Pix (preço cheio, não promoção, margem >= mínimo)."""
    ids = [int(i) for i in ids if int(i) > 0]
    if not ids or pix_pct() <= 0:
        return set()
    async with engine.connect() as conn:
        rows = (await conn.execute(text("""
            SELECT id, sale_price, cost_price, promo_price, promo_until, promo2_pct, promo2_until
            FROM products WHERE active = TRUE AND id = ANY(:ids)
        """), {"ids": ids})).mappings().all()
    from datetime import datetime, timedelta, timezone
    hoje = datetime.now(timezone(timedelta(hours=-3))).date()
    ok, mm = set(), _margem_min()
    for r in rows:
        venda = float(r["sale_price"] or 0)
        custo = float(r["cost_price"] or 0)
        if venda <= 0 or custo <= 0:
            continue
        promo = r["promo_price"]
        if promo is not None and 0 < float(promo) < venda and (r["promo_until"] is None or r["promo_until"] >= hoje):
            continue                                   # já está em promoção
        if r["promo2_pct"] and (r["promo2_until"] is None or r["promo2_until"] >= hoje):
            continue                                   # já tem "2ª unidade"
        if (venda - custo) / venda < mm:
            continue                                   # margem baixa (gás, azoto, compressor...)
        ok.add(int(r["id"]))
    return ok


async def aplicar_pix(items: list) -> tuple[list, float, float]:
    """-> (itens com preço do Pix, subtotal novo, total do desconto)."""
    pct = pix_pct()
    elig = await ids_elegiveis([i["id"] for i in items if isinstance(i.get("id"), int) and i["id"] > 0])
    novos, sub, desc = [], 0.0, 0.0
    for it in items:
        it = dict(it)
        if (pct > 0 and it.get("id") in elig and not it.get("kit") and not it.get("kit_id")
                and not it.get("promo2") and not it.get("tecnico_pct")):
            cheio = float(it["price"])
            preco = round(cheio * (100 - pct) / 100, 2)
            desc += (cheio - preco) * float(it["qty"])
            it.update({"price": preco, "subtotal": round(preco * float(it["qty"]), 2),
                       "pix_pct": pct, "full_price": it.get("full_price") or cheio})
        sub += float(it["price"]) * float(it["qty"])
        novos.append(it)
    return novos, round(sub, 2), round(desc, 2)
