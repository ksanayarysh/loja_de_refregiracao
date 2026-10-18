"""
Админка: скидка на кит установки кондиционера (страница /calculadora-instalacao на сайте).

Скидка хранится в таблице kit_settings (одна строка, id = 1). Эту же таблицу читает каталог
через /api/kit-config. На странице видно, какая останется маржа кита при выбранной скидке
(по cost_price из products); если маржа ниже MIN_MARGIN — красное предупреждение.

ВАЖНО: состав базового кита (KIT_BASE) дублирует `KIT` с base:true из
catalog/templates/calculadora-instalacao.html. Меняешь кит там — поменяй и здесь.
"""
import math
import re
import unicodedata
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import text

from dependencies import engine, templates, basic_auth

router = APIRouter()

MIN_MARGIN = 10.0        # %, ниже — предупреждение
MAX_DISCOUNT = 50.0      # %, больше сохранить нельзя
METRAGENS = [3, 5, 7, 10, 15]

# (slug товара в каталоге, правило количества)
#   meters = метраж | bars = барры по 2 м, ceil(метраж / 2) | one = 1 шт
KIT_BASE = [
    ("tubo-de-cobre-de-1-4", "meters"),
    ("tubo-de-cobre-de-3-8", "meters"),
    ("tubo-esponjoso-blindado-1-4-2-metros", "bars"),
    ("tubo-esponjoso-blindado-3-8-2-metros", "bars"),
    ("suporte-ar-condicionado-split-7000-12000-btus-40cm-chapa-pintada", "one"),
    ("fita-pvc", "one"),
    ("cabo-pp-4v-x-1-5mm", "meters"),
    ("mangueira-transparente-ar-condicionado", "meters"),
]


def _slugify(value: str) -> str:
    """Та же функция, что в catalog/router.py: slug товара = slugify(name)."""
    value = (value or "").strip().lower()
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.replace("+", " plus ")
    value = re.sub(r"[^\w\s-]", " ", value)
    value = re.sub(r"[\s_]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value


def _qty(rule: str, metros: float) -> int:
    if rule == "meters":
        return max(1, math.ceil(metros))
    if rule == "bars":
        return max(1, math.ceil(metros / 2))
    return 1


def _parse_percent(value: str) -> Optional[float]:
    s = (value or "").strip().replace("%", "").replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _fmt_pct(p: float) -> str:
    return ("%g" % p).replace(".", ",")


# ── ТАБЛИЦА НАСТРОЕК (та же DDL, что в catalog/router.py) ─────────────────────
_kit_settings_ready = False


async def _ensure_kit_settings(conn):
    global _kit_settings_ready
    if _kit_settings_ready:
        return
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
    # promo_price создаётся и в products.py; дублируем, чтобы страница не падала при любом порядке деплоя
    await conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS promo_price NUMERIC(10,2)"))
    _kit_settings_ready = True
# ─────────────────────────────────────────────────────────────────────────────


async def _load_context(conn):
    res = await conn.execute(text("SELECT discount_percent FROM kit_settings WHERE id = 1"))
    pct = float(res.scalar() or 0)

    res = await conn.execute(text(
        "SELECT name, sale_price, promo_price, cost_price FROM products WHERE active = TRUE"
    ))
    by_slug = {}
    for r in res.mappings().all():
        by_slug.setdefault(_slugify(r["name"]), r)

    items, missing, no_cost = [], [], []
    for slug, rule in KIT_BASE:
        r = by_slug.get(slug)
        if not r or not r["sale_price"]:
            missing.append(slug)
            continue
        cost = float(r["cost_price"] or 0)
        if cost <= 0:
            no_cost.append(r["name"])
        sale = float(r["sale_price"])
        promo = float(r["promo_price"]) if r["promo_price"] else None
        if promo and promo < sale:   # на сайте кит считается по промо-цене
            sale = promo
        items.append({"rule": rule, "sale": sale, "cost": cost})

    table = []
    for m in METRAGENS:
        sale = sum(i["sale"] * _qty(i["rule"], m) for i in items)
        cost = sum(i["cost"] * _qty(i["rule"], m) for i in items)
        table.append({"m": m, "sale": round(sale, 2), "cost": round(cost, 2)})
    return pct, table, missing, no_cost


async def _render(request: Request, pct: float, pct_input: str, error: Optional[str], status: int = 200):
    async with engine.connect() as conn:
        _, table, missing, no_cost = await _load_context(conn)
    return templates.TemplateResponse("kit_discount.html", {
        "request": request,
        "active_page": "kit",
        "pct": pct,
        "pct_input": pct_input,
        "table": table,
        "missing": missing,
        "no_cost": no_cost,
        "min_margin": MIN_MARGIN,
        "max_discount": MAX_DISCOUNT,
        "error": error,
    }, status_code=status)


@router.get("/kit-discount", response_class=HTMLResponse)
async def kit_discount_form(request: Request, _=Depends(basic_auth)):
    async with engine.begin() as conn:
        await _ensure_kit_settings(conn)
    async with engine.connect() as conn:
        res = await conn.execute(text("SELECT discount_percent FROM kit_settings WHERE id = 1"))
        pct = float(res.scalar() or 0)
    return await _render(request, pct, _fmt_pct(pct), None)


@router.post("/kit-discount")
async def kit_discount_save(
    request: Request,
    discount_percent: str = Form("0"),
    _=Depends(basic_auth),
):
    value = _parse_percent(discount_percent)
    if value is None or value < 0 or value > MAX_DISCOUNT:
        async with engine.begin() as conn:
            await _ensure_kit_settings(conn)
        async with engine.connect() as conn:
            res = await conn.execute(text("SELECT discount_percent FROM kit_settings WHERE id = 1"))
            current = float(res.scalar() or 0)
        return await _render(
            request, current, discount_percent,
            f"Informe um número entre 0 e {_fmt_pct(MAX_DISCOUNT)}.", status=400,
        )

    async with engine.begin() as conn:
        await _ensure_kit_settings(conn)
        await conn.execute(text("""
            INSERT INTO kit_settings (id, discount_percent, updated_at)
            VALUES (1, :p, NOW())
            ON CONFLICT (id) DO UPDATE SET discount_percent = :p, updated_at = NOW()
        """), {"p": round(value, 2)})
    return RedirectResponse(url="/kit-discount?ok=1", status_code=303)
