import base64
import io
from math import ceil
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Form, Query, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import text

from dependencies import engine, templates, basic_auth

router = APIRouter()

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
    """Сжимает до квадрата 400x400, возвращает base64 data URL."""
    if not file or not file.filename:
        return None
    try:
        from PIL import Image
        data = await file.read()
        if len(data) > MAX_FILE_MB * 1024 * 1024:
            return None
        img  = Image.open(io.BytesIO(data)).convert("RGB")
        w, h = img.size
        side = min(w, h)
        left = (w - side) // 2
        top  = (h - side) // 2
        img  = img.crop((left, top, left + side, top + side))
        img  = img.resize((IMAGE_SIZE, IMAGE_SIZE), Image.LANCZOS)
        buf  = io.BytesIO()
        img.save(buf, format="JPEG", quality=82, optimize=True)
        b64  = base64.b64encode(buf.getvalue()).decode()
        return f"data:image/jpeg;base64,{b64}"
    except Exception:
        return None


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
            text("""INSERT INTO products (name, category_id, unit, sale_price, cost_price, min_stock, active, image)
                    VALUES (:name, :category_id, :unit, :sale_price, :cost_price, :min_stock, TRUE, :image)"""),
            {"name": name.strip(), "category_id": category_id, "unit": unit,
             "sale_price": price, "cost_price": cost, "min_stock": min_stock, "image": image_b64},
        )
    return RedirectResponse(url="/products/new?ok=1", status_code=303)


@router.get("/products/{product_id}/edit", response_class=HTMLResponse)
async def edit_product_form(product_id: int, request: Request, _=Depends(basic_auth)):
    async with engine.connect() as conn:
        res = await conn.execute(
            text("SELECT id, name, category_id, unit, sale_price, cost_price, min_stock, image FROM products WHERE id = :id AND active = TRUE"),
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
                text("SELECT id, name, category_id, unit, sale_price, cost_price, min_stock, image FROM products WHERE id = :id"),
                {"id": product_id}
            )
            product    = res.mappings().first()
            categories = await _get_categories(conn)
            return templates.TemplateResponse("edit_product.html", {
                "request": request, "product": product, "categories": categories,
                "error": f'Produto "{name.strip()}" já existe! Escolha outro nome.',
            }, status_code=400)

        if image_b64:
            extra_sql = ", image=:image"
            extra_val = {"image": image_b64}
        elif remove_image == "1":
            extra_sql = ", image=NULL"
            extra_val = {}
        else:
            extra_sql = ""
            extra_val = {}

        await conn.execute(
            text(f"""UPDATE products
                    SET name=:name, category_id=:category_id, unit=:unit,
                        sale_price=:sale_price, cost_price=:cost_price, min_stock=:min_stock
                        {extra_sql}
                    WHERE id=:id"""),
            {"id": product_id, "name": name.strip(), "category_id": category_id,
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
                ORDER BY COALESCE(c.name, 'Outro') ASC, {sort_col} {direction_sql}
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
async def api_products(search: str = Query(""), _=Depends(basic_auth)):
    async with engine.connect() as conn:
        res = await conn.execute(
            text("""SELECT p.id, p.name, p.sale_price, p.unit, p.image,
                           GREATEST(0, COALESCE(SUM(sm.qty), 0)) as current_stock
                    FROM products p
                    LEFT JOIN stock_movements sm ON sm.product_id = p.id
                    WHERE p.active = TRUE AND LOWER(p.name) LIKE LOWER(:q)
                    GROUP BY p.id ORDER BY p.name LIMIT 30"""),
            {"q": f"%{search}%"},
        )
        rows = res.mappings().all()
    return [
        {"id": r["id"], "name": r["name"], "sale_price": float(r["sale_price"] or 0),
         "unit": r["unit"] or "un", "image": r["image"],
         "current_stock": float(r["current_stock"])}
        for r in rows
    ]
