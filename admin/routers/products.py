import io
import os
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
    image: UploadFile = File(None),
    _=Depends(basic_auth),
):
    price     = _parse_price(sale_price)
    cost      = _parse_price(cost_price)
    image_b64 = await _process_image(image)

    async with engine.begin() as conn:
        dup = await conn.execute(
            text("SELECT id FROM products WHERE LOWER(TRIM(name)) = LOWER(TRIM(:name)) AND active = TRUE"),
            {"name": name.strip()}
        )
        if dup.first():
            categories = await _get_categories(conn)
            return templates.TemplateResponse("new_product.html", {
                "request": request, "categories": categories,
                "error": f'Produto "{name.strip()}" já existe! Verifique a lista de produtos.',
            }, status_code=400)

        await conn.execute(
            text("""INSERT INTO products (name, category_id, unit, sale_price, cost_price, min_stock, active, image, description)
                    VALUES (:name, :category_id, :unit, :sale_price, :cost_price, :min_stock, TRUE, :image, :description)"""),
            {"name": name.strip(), "category_id": category_id, "unit": unit,
             "sale_price": price, "cost_price": cost, "min_stock": min_stock, "image": image_b64, "description": description.strip() or None},
        )
    return RedirectResponse(url="/products/new?ok=1", status_code=303)


@router.get("/products/{product_id}/edit", response_class=HTMLResponse)
async def edit_product_form(product_id: int, request: Request, _=Depends(basic_auth)):
    async with engine.connect() as conn:
        res = await conn.execute(
            text("SELECT id, name, category_id, unit, sale_price, cost_price, min_stock, image, description FROM products WHERE id = :id AND active = TRUE"),
            {"id": product_id},
        )
        product = res.mappings().first()
        if not product:
            return HTMLResponse("Produto não encontrado", status_code=404)
        categories = await _get_categories(conn)
    return templates.TemplateResponse("edit_product.html", {
        "request": request, "product": product, "categories": categories,
    })


@router.post("/products/{product_id}/edit")
async def update_product(
    product_id: int,
    request: Request,
    name: str = Form(...),
    category_id: int = Form(None),
    unit: str = Form("un"),
    sale_price: str = Form("0"),
    cost_price: str = Form("0"),
    min_stock: int = Form(0),
    description: str = Form(""),
    image: UploadFile = File(None),
    remove_image: str = Form(""),
    _=Depends(basic_auth),
):
    price     = _parse_price(sale_price)
    cost      = _parse_price(cost_price)
    image_b64 = await _process_image(image)

    async with engine.begin() as conn:
        dup = await conn.execute(
            text("SELECT id FROM products WHERE LOWER(TRIM(name)) = LOWER(TRIM(:name)) AND active = TRUE AND id != :id"),
            {"name": name.strip(), "id": product_id}
        )
        if dup.first():
            res = await conn.execute(
                text("SELECT id, name, category_id, unit, sale_price, cost_price, min_stock, image, description FROM products WHERE id = :id"),
                {"id": product_id}
            )
            product    = res.mappings().first()
            categories = await _get_categories(conn)
            return templates.TemplateResponse("edit_product.html", {
                "request": request, "product": product, "categories": categories,
                "error": f'Produto "{name.strip()}" já existe! Escolha outro nome.',
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
                    SET name=:name, category_id=:category_id, unit=:unit, description=:description,
                        sale_price=:sale_price, cost_price=:cost_price, min_stock=:min_stock
                        {extra_sql}
                    WHERE id=:id"""),
            {"id": product_id, "name": name.strip(), "category_id": category_id, "description": description.strip() or None,
             "unit": unit, "sale_price": price, "cost_price": cost, "min_stock": min_stock, **extra_val},
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

    async with engine.connect() as conn:
        total       = await conn.execute(text("SELECT COUNT(*) FROM products p WHERE p.active = TRUE"))
        total_count = int(total.scalar() or 0)

        rows_res = await conn.execute(
            text(f"""
                SELECT p.id, p.name, p.sale_price, p.cost_price, p.unit, p.min_stock,
                       p.image,
                       c.name as category_name,
                       COALESCE(SUM(sm.qty), 0) as current_stock
                FROM products p
                LEFT JOIN categories c ON c.id = p.category_id
                LEFT JOIN stock_movements sm ON sm.product_id = p.id
                WHERE p.active = TRUE
                GROUP BY p.id, p.name, p.sale_price, p.cost_price, p.unit, p.min_stock, p.image, c.name
                ORDER BY LOWER(COALESCE(c.name, 'Outro')) ASC, {sort_col} {direction_sql}
                LIMIT :limit OFFSET :offset
            """),
            {"limit": per_page, "offset": offset},
        )
        rows = rows_res.mappings().all()

    total_pages = max(1, ceil(total_count / per_page))
    page        = min(page, total_pages)

    return templates.TemplateResponse("products_list.html", {
        "request": request, "rows": rows, "page": page, "per_page": per_page,
        "total_pages": total_pages, "total_count": total_count,
        "sort": sort if sort in SORT_FIELDS else "name",
        "direction": "desc" if direction.lower() == "desc" else "asc",
        "deleted": request.query_params.get("deleted") == "1",
    })


@router.get("/api/products")
async def api_products(search: str = Query(""), unit: str = Query(""), category: str = Query(""), _=Depends(basic_auth)):
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
            text(f"""SELECT p.id, p.name, p.sale_price, p.unit, p.image,
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
        {"id": r["id"], "name": r["name"], "sale_price": float(r["sale_price"] or 0),
         "unit": r["unit"] or "un", "image": r["image"],
         "current_stock": float(r["current_stock"])}
        for r in rows
    ]
