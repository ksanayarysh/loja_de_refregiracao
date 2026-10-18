"""
Kits na admin: um kit = nome + desconto (%) + lista de produtos com quantidade.
Na venda, o kit vira UMA LINHA POR PRODUTO em `sales` (preço já com desconto,
nota "Kit: nome (-X%)") e cada produto baixa o seu estoque.

Arquivos:
  admin/routers/kits.py        (este)
  admin/templates/kits.html

Incluir no main.py da admin:
  from routers.kits import router as kits_router
  app.include_router(kits_router)

Independente do desconto do kit de instalação do site (/kit-discount).
"""
import json
from datetime import date
from urllib.parse import urlencode
from decimal import Decimal, ROUND_HALF_UP

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import text

from dependencies import engine, templates, basic_auth

router = APIRouter()
_ready = False


def _go(path: str, **q) -> RedirectResponse:
    q = {k: v for k, v in q.items() if v}
    return RedirectResponse(path + ("?" + urlencode(q) if q else ""), status_code=303)


def _money(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


async def _ensure_tables():
    global _ready
    if _ready:
        return
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS kits (
                id               SERIAL PRIMARY KEY,
                name             VARCHAR(160) NOT NULL,
                discount_percent NUMERIC(5,2) NOT NULL DEFAULT 0,
                active           BOOLEAN NOT NULL DEFAULT TRUE,
                created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS kit_items (
                id         SERIAL PRIMARY KEY,
                kit_id     INTEGER NOT NULL REFERENCES kits(id) ON DELETE CASCADE,
                product_id INTEGER NOT NULL REFERENCES products(id),
                qty        NUMERIC(12,3) NOT NULL
            )
        """))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_kit_items_kit ON kit_items(kit_id)"))
        # preço fixo do kit (opcional). Se preenchido, vale ele; senão, o desconto em %.
        await conn.execute(text("ALTER TABLE kits ADD COLUMN IF NOT EXISTS fixed_price NUMERIC(10,2)"))
        # brinde: vai no kit de graça, mas baixa estoque e entra no custo (margem)
        await conn.execute(text("ALTER TABLE kit_items ADD COLUMN IF NOT EXISTS is_gift BOOLEAN NOT NULL DEFAULT FALSE"))
        # site: mostrar na página inicial, texto curto, e se só vai de carro (moto não leva)
        await conn.execute(text("ALTER TABLE kits ADD COLUMN IF NOT EXISTS show_on_site BOOLEAN NOT NULL DEFAULT FALSE"))
        await conn.execute(text("ALTER TABLE kits ADD COLUMN IF NOT EXISTS description TEXT"))
        await conn.execute(text("ALTER TABLE kits ADD COLUMN IF NOT EXISTS car_only BOOLEAN NOT NULL DEFAULT FALSE"))
    _ready = True


async def _load_kits(conn, only_active: bool = False, kit_id: int | None = None) -> list[dict]:
    """Kits com itens, preço atual dos produtos, preço do kit e estoque."""
    where, params = ["TRUE"], {}
    if only_active:
        where.append("k.active = TRUE")
    if kit_id is not None:
        where.append("k.id = :kid")
        params["kid"] = kit_id
    kits = [dict(r) for r in (await conn.execute(text(
        f"SELECT * FROM kits k WHERE {' AND '.join(where)} ORDER BY k.active DESC, k.name"
    ), params)).mappings().all()]
    if not kits:
        return []
    rows = (await conn.execute(text("""
        SELECT ki.kit_id, ki.product_id, ki.qty, ki.is_gift, p.name, p.unit, p.sale_price, p.cost_price, p.active,
               COALESCE((SELECT SUM(sm.qty) FROM stock_movements sm WHERE sm.product_id = p.id), 0) AS stock
        FROM kit_items ki JOIN products p ON p.id = ki.product_id
        WHERE ki.kit_id = ANY(:ids)
        ORDER BY ki.id
    """), {"ids": [k["id"] for k in kits]})).mappings().all()
    by_kit: dict[int, list] = {}
    for r in rows:
        by_kit.setdefault(r["kit_id"], []).append(dict(r))
    for k in kits:
        k.pop("created_at", None)          # datetime não vai para JSON
        pct = Decimal(str(k["discount_percent"] or 0))
        fixed = Decimal(str(k["fixed_price"])) if k.get("fixed_price") is not None else None
        raw = []
        full = gift_value = cost_total = Decimal(0)
        for it in by_kit.get(k["id"], []):
            price = Decimal(str(it["sale_price"] or 0))
            cost = Decimal(str(it["cost_price"] or 0))
            qty = Decimal(str(it["qty"]))
            gift = bool(it.get("is_gift"))
            line_full = _money(qty * price)
            if gift:
                gift_value += line_full
            else:
                full += line_full
            cost_total += qty * cost
            raw.append((it, price, cost, qty, gift, line_full))

        # total de cada linha (1 kit). Brinde = 0. Preço fixo: rateado entre os itens pagos pelo
        # valor de cada um; o último item pago absorve os centavos -> soma = preço fixo exato.
        paid_idx = [i for i, r in enumerate(raw) if not r[4]]
        lines = [Decimal(0)] * len(raw)
        if fixed is not None and full > 0:
            acc = Decimal(0)
            for n_, i in enumerate(paid_idx):
                line_full = raw[i][5]
                lt = fixed - acc if n_ == len(paid_idx) - 1 else _money(fixed * line_full / full)
                acc += lt
                lines[i] = lt
            eff_pct = ((full - fixed) / full * 100).quantize(Decimal("0.1"))
        else:
            factor = (Decimal(100) - pct) / Decimal(100)
            for i in paid_idx:
                it, price, cost, qty, gift, line_full = raw[i]
                lines[i] = _money(qty * _money(price * factor))
            eff_pct = pct

        items = []
        for (it, price, cost, qty, gift, line_full), lt in zip(raw, lines):
            items.append({
                "product_id": it["product_id"], "name": it["name"], "unit": it["unit"] or "un",
                "qty": float(qty), "price": float(price), "cost": float(cost), "is_gift": gift,
                "kit_price": float(_money(lt / qty)) if qty else 0.0,   # preço unitário (informativo)
                "line_total": float(lt),                                # total da linha para 1 kit
                "stock": float(it["stock"] or 0), "product_active": bool(it["active"]),
                "no_price": not it["sale_price"], "no_cost": it["cost_price"] is None,
            })
        total = sum(lines, Decimal(0))
        k["mode"] = "fixo" if fixed is not None else "desconto"
        k["fixed_price"] = float(fixed) if fixed is not None else None
        k["discount_percent"] = float(eff_pct)
        k["discount_set"] = float(pct)
        k["items"] = items
        k["full_total"] = float(full)
        k["gift_value"] = float(gift_value)
        k["total"] = float(total)
        k["cost_total"] = float(_money(cost_total))
        k["profit"] = float(_money(total - cost_total))
        k["margin"] = float((total - cost_total) / total * 100) if total > 0 else 0.0
        k["cost_missing"] = any(i["no_cost"] for i in items)
    return kits


# ── LISTA + EDITOR ─────────────────────────────────────────────────────────────
@router.get("/kits", response_class=HTMLResponse)
async def kits_page(request: Request, edit: int | None = None, _=Depends(basic_auth)):
    await _ensure_tables()
    async with engine.connect() as conn:
        kits = await _load_kits(conn)
        products = [dict(r) for r in (await conn.execute(text(
            "SELECT id, name, unit, sale_price, cost_price FROM products WHERE active = TRUE ORDER BY name"
        ))).mappings().all()]
    for p in products:
        p["sale_price"] = float(p["sale_price"] or 0)
        p["cost_price"] = float(p["cost_price"]) if p["cost_price"] is not None else None
    editing = next((k for k in kits if k["id"] == edit), None) if edit else None
    return templates.TemplateResponse("kits.html", {
        "request": request, "active_page": "kits", "kits": kits, "products": products,
        "editing": editing, "new": request.query_params.get("new") == "1",
        "ok": request.query_params.get("ok"), "err": request.query_params.get("err"),
    })


@router.post("/kits/save")
async def kits_save(
    kit_id: str = Form(""),
    name: str = Form(...),
    discount_percent: str = Form("0"),
    price_mode: str = Form("desconto"),
    fixed_price: str = Form(""),
    active: str = Form(""),
    show_on_site: str = Form(""),
    car_only: str = Form(""),
    description: str = Form(""),
    items_json: str = Form("[]"),
    _=Depends(basic_auth),
):
    await _ensure_tables()
    name = name.strip()[:160]
    fixed = None
    pct = Decimal(0)
    if price_mode == "fixo":
        try:
            fixed = _money(fixed_price.replace(".", "").replace(",", ".") if "," in fixed_price else fixed_price)
            if fixed <= 0:
                raise ValueError
        except Exception:
            return _go("/kits", err="Preço fixo inválido. Ex.: 150 ou 149,90")
    else:
        try:
            pct = Decimal(discount_percent.replace(",", ".") or "0")
            if pct < 0 or pct > 90:
                raise ValueError
        except Exception:
            return _go("/kits", err="Desconto inválido (0–90%)")
    try:
        raw = json.loads(items_json)
        items = []
        for it in raw:
            pid = int(it["product_id"])
            q = Decimal(str(it["qty"]).replace(",", "."))
            if q > 0:
                items.append((pid, q, bool(it.get("gift"))))
    except Exception:
        return _go("/kits", err="Itens inválidos")
    if not name or not any(not g for _, _, g in items):
        if name and items:
            return _go("/kits", err="O kit precisa de pelo menos um produto pago (não só brindes)")
        return _go("/kits", err="Informe o nome e pelo menos um produto")

    async with engine.begin() as conn:
        if kit_id.strip():
            kid = int(kit_id)
            await conn.execute(text(
                "UPDATE kits SET name = :n, discount_percent = :d, fixed_price = :f, active = :a, "
                "show_on_site = :s, car_only = :c, description = :ds WHERE id = :id"
            ), {"n": name, "d": pct, "f": fixed, "a": bool(active), "s": bool(show_on_site),
                "c": bool(car_only), "ds": description.strip()[:300] or None, "id": kid})
            await conn.execute(text("DELETE FROM kit_items WHERE kit_id = :id"), {"id": kid})
        else:
            kid = (await conn.execute(text(
                "INSERT INTO kits (name, discount_percent, fixed_price, active, show_on_site, car_only, description) "
                "VALUES (:n, :d, :f, :a, :s, :c, :ds) RETURNING id"
            ), {"n": name, "d": pct, "f": fixed, "a": bool(active), "s": bool(show_on_site),
                "c": bool(car_only), "ds": description.strip()[:300] or None})).scalar()
        for pid, q, gift in items:
            await conn.execute(text(
                "INSERT INTO kit_items (kit_id, product_id, qty, is_gift) VALUES (:k, :p, :q, :g)"
            ), {"k": kid, "p": pid, "q": q, "g": gift})
    return _go("/kits", ok=f"Kit “{name}” salvo")


@router.post("/kits/{kit_id}/delete")
async def kits_delete(kit_id: int, _=Depends(basic_auth)):
    await _ensure_tables()
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM kits WHERE id = :id"), {"id": kit_id})
    # vendas já feitas não mudam: elas guardam cada produto separadamente
    return _go("/kits", ok="Kit excluído")


# ── PARA A TELA DE VENDA ───────────────────────────────────────────────────────
@router.get("/api/kits")
async def api_kits(_=Depends(basic_auth)):
    await _ensure_tables()
    async with engine.connect() as conn:
        kits = await _load_kits(conn, only_active=True)
    return JSONResponse(kits, headers={"Cache-Control": "no-store"})


@router.post("/sales/kit")
async def sales_kit(
    sold_at: str = Form(...),
    kit_id: int = Form(...),
    kit_qty: str = Form("1"),
    payment_type: str = Form("dinheiro"),
    client_id: str = Form(""),
    note: str = Form(""),
    _=Depends(basic_auth),
):
    """Registra o kit: uma venda por produto, com desconto, e baixa de estoque de cada um."""
    await _ensure_tables()
    try:
        n = Decimal(kit_qty.replace(",", "."))
        if n <= 0 or n != n.to_integral_value():
            raise ValueError
    except Exception:
        return _go("/sales/new", kit_err="Quantidade de kits inválida (número inteiro)")
    try:
        sold_at_date = date.fromisoformat(sold_at)
    except Exception:
        sold_at_date = date.today()
    cid = int(client_id) if client_id.strip().isdigit() else None

    async with engine.begin() as conn:
        kits = await _load_kits(conn, kit_id=kit_id)
        if not kits or not kits[0]["items"]:
            return _go("/sales/new", kit_err="Kit não encontrado ou vazio")
        kit = kits[0]
        pct = Decimal(str(kit["discount_set"]))
        if kit["mode"] == "fixo":
            tag = f"Kit: {kit['name']} (preço fixo R$ {kit['fixed_price']:.2f})".replace(".", ",")
        else:
            tag = f"Kit: {kit['name']}" + (f" (-{pct.normalize():f}%)" if pct else "")
        line_note = f"{tag} · {note.strip()}" if note.strip() else tag

        cost_map = {r["id"]: Decimal(str(r["cost_price"] or 0)) for r in (await conn.execute(text(
            "SELECT id, cost_price FROM products WHERE id = ANY(:ids)"
        ), {"ids": [i["product_id"] for i in kit["items"]]})).mappings().all()}

        grand_total = total_cost = Decimal(0)
        tg_items = []
        for it in kit["items"]:
            qty = (Decimal(str(it["qty"])) * n).quantize(Decimal("0.001"))
            line_total = _money(Decimal(str(it["line_total"])) * n)   # soma das linhas = preço do kit × n
            price = _money(line_total / qty) if qty else Decimal(0)
            grand_total += line_total
            total_cost += qty * cost_map.get(it["product_id"], Decimal(0))
            await conn.exec_driver_sql(
                "INSERT INTO sales (sold_at, product_id, qty, unit_price, total, note, payment_type, client_id) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
                (sold_at_date, it["product_id"], qty, price, line_total,
                 f"{line_note} · 🎁 brinde" if it["is_gift"] else line_note, payment_type, cid),
            )
            sale_id = (await conn.execute(text("SELECT lastval()"))).scalar()
            await conn.execute(text("""
                INSERT INTO stock_movements (product_id, qty, movement_type, note, sale_id, moved_at)
                VALUES (:pid, :qty, 'venda', :note, :sid, :dt)
            """), {"pid": it["product_id"], "qty": -qty, "note": tag, "sid": sale_id, "dt": sold_at_date})
            tg_items.append({"product_id": it["product_id"], "name": it["name"] + (" 🎁" if it["is_gift"] else ""), "qty": qty,
                             "unit_price": price, "_discounted_price": price, "_discounted_total": line_total})

        if payment_type == "saldo" and cid:
            await conn.execute(text("UPDATE clients SET balance = balance - :a WHERE id = :id"),
                               {"a": _money(grand_total), "id": cid})

    try:
        try:
            from routers.sales import _tg_notify_checkout
        except ImportError:
            from sales import _tg_notify_checkout
        await _tg_notify_checkout(tg_items, payment_type, Decimal(0), _money(grand_total), _money(total_cost))
    except Exception:
        pass
    return _go("/sales/new", kit_ok=f"Kit vendido: {int(n)}× {kit['name']} — R$ {float(grand_total):.2f}".replace(".", ","))
