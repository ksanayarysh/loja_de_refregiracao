import io
import os
import re
import uuid
from math import ceil
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Form, Query, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import text

from dependencies import engine, templates, basic_auth

router = APIRouter()

# ── ИСТОРИЯ ЦЕН ──────────────────────────────────────────────────────────────
_price_history_ready = False

async def _ensure_price_history(conn):
    global _price_history_ready
    if _price_history_ready:
        return
    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS price_history (
            id          SERIAL PRIMARY KEY,
            product_id  INTEGER NOT NULL REFERENCES products(id),
            old_price   NUMERIC(10,2),
            new_price   NUMERIC(10,2) NOT NULL,
            changed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))
    _price_history_ready = True
# ─────────────────────────────────────────────────────────────────────────────

# ── КОД ПОЛКИ ────────────────────────────────────────────────────────────────
_shelf_code_ready = False

async def _ensure_shelf_code_column(conn):
    global _shelf_code_ready
    if _shelf_code_ready:
        return
    await conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS shelf_code VARCHAR(50)"))
    _shelf_code_ready = True
# ─────────────────────────────────────────────────────────────────────────────

# ── СВЯЗЬ БУТИЖА ↔ ГАЗ ПО КГ ─────────────────────────────────────────────────
_gas_link_ready = False

async def _ensure_gas_link_columns(conn):
    global _gas_link_ready
    if _gas_link_ready:
        return
    await conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS linked_product_id INTEGER REFERENCES products(id)"))
    await conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS linked_qty NUMERIC(10,3)"))
    _gas_link_ready = True
# ─────────────────────────────────────────────────────────────────────────────

# ── GOOGLE PRODUCT CATEGORY (Merchant Center) ────────────────────────────────
_google_cat_ready = False

async def _ensure_google_category_column(conn):
    global _google_cat_ready
    if _google_cat_ready:
        return
    await conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS google_category VARCHAR(255)"))
    await conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS gtin VARCHAR(20)"))
    await conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS brand VARCHAR(100)"))
    await conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS sku VARCHAR(100)"))
    _google_cat_ready = True
# ─────────────────────────────────────────────────────────────────────────────

# ── ПРОМО-ЦЕНА ───────────────────────────────────────────────────────────────
_promo_ready = False

async def _ensure_promo_column(conn):
    global _promo_ready
    if _promo_ready:
        return
    await conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS promo_price NUMERIC(10,2)"))
    _promo_ready = True
# ─────────────────────────────────────────────────────────────────────────────

SORT_FIELDS = {
    "name": "p.name",
    "unit": "p.unit",
    "sale_price": "p.sale_price",
    "min_stock": "p.min_stock",
    "active": "p.active",
    "created_at": "p.created_at",
    "id": "p.id",
}

IMAGE_SIZE = 400
MAX_FILE_MB = 5
STATIC_DIR = os.environ.get("STATIC_DIR", os.path.join(os.path.dirname(__file__), "static", "images"))
os.makedirs(STATIC_DIR, exist_ok=True)


def _clean_gtin(value: str) -> Optional[str]:
    """GTIN/EAN/UPC: оставляем только цифры (пробелы, дефисы убираем)."""
    digits = re.sub(r"\D", "", value or "")
    return digits or None


def _parse_price(value: str) -> Decimal:
    s = value.strip().replace("R$", "").replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return Decimal(s)
    except Exception:
        return Decimal("0")


def _parse_promo(value: str) -> Optional[Decimal]:
    """Пустое поле или 0 = без промо."""
    if not (value or "").strip():
        return None
    p = _parse_price(value)
    return p if p > 0 else None


async def _process_image(file: UploadFile) -> Optional[str]:
    """Сжимает до 400x400, сохраняет в static/images/, возвращает URL."""
    if not file or not file.filename:
        return None
    try:
        from PIL import Image
        data = await file.read()
        if len(data) > MAX_FILE_MB * 1024 * 1024:
            return None
        img = Image.open(io.BytesIO(data)).convert("RGB")
        img.thumbnail((IMAGE_SIZE, IMAGE_SIZE), Image.LANCZOS)
        canvas = Image.new("RGB", (IMAGE_SIZE, IMAGE_SIZE), (255, 255, 255))
        offset = ((IMAGE_SIZE - img.width) // 2, (IMAGE_SIZE - img.height) // 2)
        canvas.paste(img, offset)
        fname = f"{uuid.uuid4().hex}.jpg"
        canvas.save(os.path.join(STATIC_DIR, fname), format="JPEG", quality=82, optimize=True)
        return f"/static/images/{fname}"
    except Exception:
        return None


def _delete_image(url: Optional[str]):
    """Удаляет файл если это /static/images/... (не base64)."""
    if url and url.startswith("/static/images/"):
        try:
            os.remove(os.path.join(os.path.dirname(__file__), url.lstrip("/")))
        except Exception:
            pass


async def _get_categories(conn):
    cats = await conn.execute(text("SELECT id, name FROM categories ORDER BY name"))
    return cats.mappings().all()


async def _get_gas_products(conn, exclude_id: Optional[int] = None):
    """Товары по кг — кандидаты для привязки к закрытой бутиже."""
    where = "unit = 'kg' AND active = TRUE"
    params: dict = {}
    if exclude_id:
        where += " AND id != :exclude_id"
        params["exclude_id"] = exclude_id
    res = await conn.execute(text(f"SELECT id, name FROM products WHERE {where} ORDER BY name"), params)
    return res.mappings().all()


@router.get("/products/new", response_class=HTMLResponse)
async def new_product_form(request: Request, _=Depends(basic_auth)):
    async with engine.connect() as conn:
        categories = await _get_categories(conn)
    return templates.TemplateResponse("new_product.html", {"request": request, "categories": categories})


@router.post("/products/new")
async def create_product(
    request: Request,
    name: str = Form(...),
    category_id: int = Form(None),
    unit: str = Form("un"),
    sale_price: str = Form("0"),
    cost_price: str = Form("0"),
    min_stock: int = Form(0),
    description: str = Form(""),
    shelf_code: str = Form(""),
    promo_price: str = Form(""),
    google_category: str = Form(""),
    gtin: str = Form(""),
    brand: str = Form(""),
    sku: str = Form(""),
    image: UploadFile = File(None),
    _=Depends(basic_auth),
):
    price     = _parse_price(sale_price)
    cost      = _parse_price(cost_price)
    promo     = _parse_promo(promo_price)
    image_b64 = await _process_image(image)

    async with engine.begin() as conn:
        await _ensure_shelf_code_column(conn)
        await _ensure_google_category_column(conn)
        await _ensure_promo_column(conn)

        dup = await conn.execute(
            text("SELECT id FROM products WHERE LOWER(TRIM(name)) = LOWER(TRIM(:name)) AND active = TRUE"),
            {"name": name.strip()}
        )
        dup_found = dup.first() is not None
        promo_bad = promo is not None and promo >= price
        if dup_found or promo_bad:
            categories = await _get_categories(conn)
            return templates.TemplateResponse("new_product.html", {
                "request": request, "categories": categories,
                "error": (f'Produto "{name.strip()}" já existe! Verifique a lista de produtos.' if dup_found
                          else "O preço promocional deve ser menor que o preço de venda."),
            }, status_code=400)

        await conn.execute(
            text("""INSERT INTO products (name, category_id, unit, sale_price, promo_price, cost_price, min_stock, active, image, description, shelf_code, google_category, gtin, brand, sku)
                    VALUES (:name, :category_id, :unit, :sale_price, :promo_price, :cost_price, :min_stock, TRUE, :image, :description, :shelf_code, :google_category, :gtin, :brand, :sku)"""),
            {"name": name.strip(), "category_id": category_id, "unit": unit,
             "sale_price": price, "promo_price": promo, "cost_price": cost, "min_stock": min_stock, "image": image_b64, "description": description.strip() or None,
             "shelf_code": shelf_code.strip() or None,
             "google_category": google_category.strip() or None,
             "gtin": _clean_gtin(gtin), "brand": brand.strip() or None, "sku": sku.strip() or None},
        )
    return RedirectResponse(url="/products/new?ok=1", status_code=303)


@router.get("/products/{product_id}/edit", response_class=HTMLResponse)
async def edit_product_form(product_id: int, request: Request, _=Depends(basic_auth)):
    async with engine.begin() as conn:
        await _ensure_shelf_code_column(conn)
        await _ensure_gas_link_columns(conn)
        await _ensure_google_category_column(conn)
        await _ensure_promo_column(conn)

    async with engine.connect() as conn:
        res = await conn.execute(
            text("""SELECT id, name, category_id, category2_id, unit, sale_price, promo_price, cost_price, min_stock,
                           image, description, shelf_code, linked_product_id, linked_qty, google_category,
                           gtin, brand, sku
                    FROM products WHERE id = :id AND active = TRUE"""),
            {"id": product_id},
        )
        product = res.mappings().first()
        if not product:
            return HTMLResponse("Produto não encontrado", status_code=404)
        categories = await _get_categories(conn)
        gas_products = await _get_gas_products(conn, exclude_id=product_id)
    return templates.TemplateResponse("edit_product.html", {
        "request": request, "product": product, "categories": categories,
        "gas_products": gas_products,
    })


@router.post("/products/{product_id}/edit")
async def update_product(
    product_id: int,
    request: Request,
    name: str = Form(...),
    category_id: int = Form(None),
    category2_id: Optional[int] = Form(None),
    unit: str = Form("un"),
    sale_price: str = Form("0"),
    cost_price: str = Form("0"),
    min_stock: int = Form(0),
    description: str = Form(""),
    shelf_code: str = Form(""),
    promo_price: str = Form(""),
    linked_product_id: str = Form(""),
    linked_qty: str = Form(""),
    google_category: str = Form(""),
    gtin: str = Form(""),
    brand: str = Form(""),
    sku: str = Form(""),
    image: UploadFile = File(None),
    remove_image: str = Form(""),
    _=Depends(basic_auth),
):
    price     = _parse_price(sale_price)
    cost      = _parse_price(cost_price)
    image_b64 = await _process_image(image)

    linked_pid = int(linked_product_id) if linked_product_id.strip().isdigit() else None
    linked_qty_d = _parse_price(linked_qty) if linked_qty.strip() else None
    if not linked_pid:
        linked_qty_d = None  # без связанного товара количество бессмысленно

    async with engine.begin() as conn:
        await _ensure_shelf_code_column(conn)
        await _ensure_gas_link_columns(conn)
        await _ensure_google_category_column(conn)
        await _ensure_promo_column(conn)

        promo = _parse_promo(promo_price)
        dup = await conn.execute(
            text("SELECT id FROM products WHERE LOWER(TRIM(name)) = LOWER(TRIM(:name)) AND active = TRUE AND id != :id"),
            {"name": name.strip(), "id": product_id}
        )
        dup_found = dup.first() is not None
        promo_bad = promo is not None and promo >= price
        if dup_found or promo_bad:
            res = await conn.execute(
                text("""SELECT id, name, category_id, category2_id, unit, sale_price, promo_price, cost_price, min_stock,
                               image, description, shelf_code, linked_product_id, linked_qty, google_category,
                               gtin, brand, sku
                        FROM products WHERE id = :id"""),
                {"id": product_id}
            )
            product      = res.mappings().first()
            categories   = await _get_categories(conn)
            gas_products = await _get_gas_products(conn, exclude_id=product_id)
            return templates.TemplateResponse("edit_product.html", {
                "request": request, "product": product, "categories": categories,
                "gas_products": gas_products,
                "error": (f'Produto "{name.strip()}" já existe! Escolha outro nome.' if dup_found
                          else "O preço promocional deve ser menor que o preço de venda."),
            }, status_code=400)

        cur = await conn.execute(text("SELECT image, sale_price FROM products WHERE id=:id"), {"id": product_id})
        cur_row   = cur.mappings().first() or {}
        cur_image = cur_row.get("image")
        cur_price = cur_row.get("sale_price")

        # Создаём таблицу если не существует (один раз за жизнь процесса)
        await _ensure_price_history(conn)

        # Записываем историю если цена изменилась
        if cur_price is not None and Decimal(str(cur_price)) != price:
            await conn.execute(
                text("INSERT INTO price_history (product_id, old_price, new_price) VALUES (:pid, :old, :new)"),
                {"pid": product_id, "old": cur_price, "new": price},
            )

        if image_b64:
            _delete_image(cur_image)
            extra_sql = ", image=:image"
            extra_val = {"image": image_b64}
        elif remove_image == "1":
            _delete_image(cur_image)
            extra_sql = ", image=NULL"
            extra_val = {}
        else:
            extra_sql = ""
            extra_val = {}

        await conn.execute(
            text(f"""UPDATE products
                    SET name=:name, category_id=:category_id, category2_id=:category2_id,
                        unit=:unit, description=:description, shelf_code=:shelf_code,
                        sale_price=:sale_price, promo_price=:promo_price, cost_price=:cost_price, min_stock=:min_stock,
                        linked_product_id=:linked_product_id, linked_qty=:linked_qty,
                        google_category=:google_category,
                        gtin=:gtin, brand=:brand, sku=:sku
                        {extra_sql}
                    WHERE id=:id"""),
            {"id": product_id, "name": name.strip(), "category_id": category_id,
             "category2_id": category2_id if category2_id and category2_id > 0 else None,
             "description": description.strip() or None, "shelf_code": shelf_code.strip() or None,
             "unit": unit, "sale_price": price, "promo_price": promo, "cost_price": cost, "min_stock": min_stock,
             "linked_product_id": linked_pid, "linked_qty": linked_qty_d,
             "google_category": google_category.strip() or None,
             "gtin": _clean_gtin(gtin), "brand": brand.strip() or None, "sku": sku.strip() or None,
             **extra_val},
        )
    return RedirectResponse(url=f"/products/{product_id}/edit?ok=1", status_code=303)


@router.post("/products/{product_id}/delete")
async def delete_product(product_id: int, _=Depends(basic_auth)):
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE products SET active = FALSE WHERE id = :id"), {"id": product_id})
    return RedirectResponse(url="/products?deleted=1", status_code=303)


@router.get("/products", response_class=HTMLResponse)
async def products_list(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=5, le=200),
    sort: str = Query("name"),
    direction: str = Query("asc"),
    _=Depends(basic_auth),
):
    sort_col      = SORT_FIELDS.get(sort, SORT_FIELDS["name"])
    direction_sql = "DESC" if direction.lower() == "desc" else "ASC"
    offset        = (page - 1) * per_page

    async with engine.begin() as conn:
        await _ensure_shelf_code_column(conn)
        await _ensure_gas_link_columns(conn)
        await _ensure_promo_column(conn)

    async with engine.connect() as conn:
        total       = await conn.execute(text("SELECT COUNT(*) FROM products p WHERE p.active = TRUE"))
        total_count = int(total.scalar() or 0)

        rows_res = await conn.execute(
            text(f"""
                SELECT p.id, p.name, p.sale_price, p.promo_price, p.cost_price, p.unit, p.min_stock,
                       p.image, p.shelf_code, p.linked_product_id,
                       (LOWER(p.name) LIKE '%botija%') AS is_botija,
                       c.name as category_name,
                       COALESCE(SUM(sm.qty), 0) as current_stock
                FROM products p
                LEFT JOIN categories c ON c.id = p.category_id
                LEFT JOIN stock_movements sm ON sm.product_id = p.id
                WHERE p.active = TRUE
                GROUP BY p.id, p.name, p.sale_price, p.promo_price, p.cost_price, p.unit, p.min_stock, p.image, p.shelf_code,
                         p.linked_product_id, c.name
                ORDER BY LOWER(COALESCE(c.name, 'Outro')) ASC, {sort_col} {direction_sql}
                LIMIT :limit OFFSET :offset
            """),
            {"limit": per_page, "offset": offset},
        )
        # sale_price = preço efetivo (promo se houver); regular_price = preço normal
        rows = [{**dict(r), **_eff_price(r["sale_price"], r["promo_price"])} for r in rows_res.mappings().all()]
        categories = await _get_categories(conn)

    total_pages = max(1, ceil(total_count / per_page))
    page        = min(page, total_pages)

    return templates.TemplateResponse("products_list.html", {
        "request": request, "rows": rows, "page": page, "per_page": per_page,
        "total_pages": total_pages, "total_count": total_count,
        "categories": categories,
        "sort": sort if sort in SORT_FIELDS else "name",
        "direction": "desc" if direction.lower() == "desc" else "asc",
        "deleted": request.query_params.get("deleted") == "1",
    })


@router.post("/api/generate-description")
async def generate_description(request: Request, _=Depends(basic_auth)):
    try:
        body = await request.json()
        name = (body.get("name") or "").strip()
        cat  = (body.get("category") or "").strip()
        if not name:
            return {"error": "Nome obrigatório"}

        import httpx
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not anthropic_key:
            return {"error": "ANTHROPIC_API_KEY não configurada"}

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": anthropic_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 400,
                    "messages": [{
                        "role": "user",
                        "content": f"""Escreva uma descrição de produto para loja de peças de refrigeração no Rio de Janeiro.
Produto: {name}
Categoria: {cat}

Regras:
- 3 a 5 frases em português
- Mencione compatibilidade, uso e benefício
- Mencione "Rio de Janeiro" ou "RJ" e "WhatsApp"
- Tom profissional mas acessível
- SÓ texto puro, sem markdown
- NÃO invente especificações técnicas"""
                    }]
                }
            )
        data = resp.json()
        text_out = data.get("content", [{}])[0].get("text", "")
        if not text_out:
            return {"error": "Resposta vazia da IA"}
        return {"description": text_out.strip()}
    except Exception as e:
        return {"error": str(e)}


def _eff_price(sale, promo) -> dict:
    """Preço efetivo para PDV/API: promo (se menor que o preço de venda) senão sale_price."""
    sale = float(sale or 0)
    promo = float(promo) if promo else None
    on = bool(promo and sale and promo < sale)
    return {"sale_price": promo if on else sale, "regular_price": sale, "on_promo": on}


@router.get("/api/products/model-search")
async def api_model_search(
    q: str = Query(""),
    category_id: Optional[int] = Query(None),
    _=Depends(basic_auth),
):
    """Busca por modelo ou marca no nome e descrição do produto."""
    if not q.strip():
        return []
    async with engine.begin() as conn:
        await _ensure_shelf_code_column(conn)
        await _ensure_promo_column(conn)
    async with engine.connect() as conn:
        where = "p.active = TRUE AND (LOWER(p.name) LIKE LOWER(:q) OR LOWER(COALESCE(p.description,'')) LIKE LOWER(:q))"
        params: dict = {"q": f"%{q.strip()}%"}
        if category_id:
            where += " AND p.category_id = :cat_id"
            params["cat_id"] = category_id
        res = await conn.execute(
            text(f"""SELECT p.id, p.name, p.sale_price, p.promo_price, p.unit, p.image, p.description, p.shelf_code,
                           c.name as category_name,
                           GREATEST(0, COALESCE(SUM(sm.qty), 0)) as current_stock
                    FROM products p
                    LEFT JOIN categories c ON c.id = p.category_id
                    LEFT JOIN stock_movements sm ON sm.product_id = p.id
                    WHERE {where}
                    GROUP BY p.id, p.name, p.sale_price, p.promo_price, p.unit, p.image, p.description, p.shelf_code, c.name
                    ORDER BY p.name
                    LIMIT 100"""),
            params,
        )
        rows = res.mappings().all()
    return [
        {
            "id": r["id"],
            "name": r["name"],
            **_eff_price(r["sale_price"], r["promo_price"]),
            "unit": r["unit"] or "un",
            "image": r["image"],
            "category_name": r["category_name"] or "Outro",
            "current_stock": float(r["current_stock"]),
            "shelf_code": r["shelf_code"],
            "description": (r["description"] or "")[:120],
        }
        for r in rows
    ]


@router.get("/api/products/batch")
async def api_products_batch(ids: str = Query(""), _=Depends(basic_auth)):
    try:
        id_list = [int(x) for x in ids.split(",") if x.strip()]
    except Exception:
        id_list = []
    if not id_list:
        return []
    async with engine.begin() as conn:
        await _ensure_promo_column(conn)
    async with engine.connect() as conn:
        res = await conn.execute(
            text("SELECT id, sale_price, promo_price, cost_price FROM products WHERE id = ANY(:ids)"),
            {"ids": id_list},
        )
        rows = res.mappings().all()
    return [
        {"id": r["id"], **_eff_price(r["sale_price"], r["promo_price"]),
         "cost_price": float(r["cost_price"]) if r["cost_price"] is not None else None}
        for r in rows
    ]


@router.get("/api/products")
async def api_products(search: str = Query(""), unit: str = Query(""), category: str = Query(""), _=Depends(basic_auth)):
    async with engine.begin() as conn:
        await _ensure_shelf_code_column(conn)
        await _ensure_promo_column(conn)
    async with engine.connect() as conn:
        where = "p.active = TRUE AND LOWER(p.name) LIKE LOWER(:q)"
        params: dict = {"q": f"%{search}%"}
        if unit:
            where += " AND p.unit = :unit"
            params["unit"] = unit
        if category:
            where += " AND LOWER(COALESCE(c.name, '')) LIKE LOWER(:cat)"
            params["cat"] = f"%{category}%"
        res = await conn.execute(
            text(f"""SELECT p.id, p.name, p.sale_price, p.promo_price, p.cost_price, p.unit, p.image, p.shelf_code,
                           GREATEST(0, COALESCE(SUM(sm.qty), 0)) as current_stock
                    FROM products p
                    LEFT JOIN categories c ON c.id = p.category_id
                    LEFT JOIN stock_movements sm ON sm.product_id = p.id
                    WHERE {where}
                    GROUP BY p.id ORDER BY p.name LIMIT 50"""),
            params,
        )
        rows = res.mappings().all()
    return [
        {"id": r["id"], "name": r["name"], **_eff_price(r["sale_price"], r["promo_price"]),
         "cost_price": float(r["cost_price"]) if r["cost_price"] else None,
         "unit": r["unit"] or "un", "image": r["image"],
         "current_stock": float(r["current_stock"]),
         "shelf_code": r["shelf_code"]}
        for r in rows
    ]
