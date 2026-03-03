from math import ceil
from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Query, Request
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


def _parse_price(value: str) -> Decimal:
    s = value.strip().replace("R$", "").replace(" ", "")
    # Если есть и точка и запятая — значит точка тысячный разделитель: "1.234,56"
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    # Если только запятая — десятичный разделитель: "33,00"
    elif "," in s:
        s = s.replace(",", ".")
    # Если только точка — уже правильно: "33.00"
    try:
        return Decimal(s)
    except Exception:
        return Decimal("0")


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
    _=Depends(basic_auth),
):
    price = _parse_price(sale_price)
    cost  = _parse_price(cost_price)
    async with engine.begin() as conn:
        # Verificar duplicata
        dup = await conn.execute(
            text("SELECT id FROM products WHERE LOWER(TRIM(name)) = LOWER(TRIM(:name)) AND active = TRUE"),
            {"name": name.strip()}
        )
        if dup.first():
            categories = await _get_categories(conn)
            return templates.TemplateResponse("new_product.html", {
                "request": request,
                "categories": categories,
                "error": f"Produto \"{name.strip()}\" já existe! Verifique a lista de produtos.",
            }, status_code=400)

        await conn.execute(
            text("""INSERT INTO products (name, category_id, unit, sale_price, cost_price, min_stock, active)
                    VALUES (:name, :category_id, :unit, :sale_price, :cost_price, :min_stock, TRUE)"""),
            {"name": name.strip(), "category_id": category_id, "unit": unit,
             "sale_price": price, "cost_price": cost, "min_stock": min_stock},
        )
    return RedirectResponse(url="/products/new?ok=1", status_code=303)


@router.get("/products/{product_id}/edit", response_class=HTMLResponse)
async def edit_product_form(product_id: int, request: Request, _=Depends(basic_auth)):
    async with engine.connect() as conn:
        res = await conn.execute(
            text("SELECT id, name, category_id, unit, sale_price, cost_price, min_stock FROM products WHERE id = :id AND active = TRUE"),
            {"id": product_id},
        )
        product = res.mappings().first()
        if not product:
            return HTMLResponse("Produto não encontrado", status_code=404)
        categories = await _get_categories(conn)

    return templates.TemplateResponse("edit_product.html", {
        "request": request,
        "product": product,
        "categories": categories,
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
    _=Depends(basic_auth),
):
    price = _parse_price(sale_price)
    cost  = _parse_price(cost_price)
    async with engine.begin() as conn:
        # Verificar duplicata (excluindo o próprio produto)
        dup = await conn.execute(
            text("""SELECT id FROM products
                    WHERE LOWER(TRIM(name)) = LOWER(TRIM(:name))
                    AND active = TRUE AND id != :id"""),
            {"name": name.strip(), "id": product_id}
        )
        if dup.first():
            res = await conn.execute(
                text("SELECT id, name, category_id, unit, sale_price, cost_price, min_stock FROM products WHERE id = :id"),
                {"id": product_id}
            )
            product = res.mappings().first()
            categories = await _get_categories(conn)
            return templates.TemplateResponse("edit_product.html", {
                "request": request,
                "product": product,
                "categories": categories,
                "error": f"Produto \"{name.strip()}\" já existe! Escolha outro nome.",
            }, status_code=400)

        await conn.execute(
            text("""UPDATE products
                    SET name=:name, category_id=:category_id, unit=:unit,
                        sale_price=:sale_price, cost_price=:cost_price, min_stock=:min_stock
                    WHERE id=:id"""),
            {"id": product_id, "name": name.strip(), "category_id": category_id,
             "unit": unit, "sale_price": price, "cost_price": cost, "min_stock": min_stock},
        )
    return RedirectResponse(url=f"/products/{product_id}/edit?ok=1", status_code=303)


@router.post("/products/{product_id}/delete")
async def delete_product(product_id: int, _=Depends(basic_auth)):
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE products SET active = FALSE WHERE id = :id"),
            {"id": product_id},
        )
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
    sort_col = SORT_FIELDS.get(sort, SORT_FIELDS["name"])
    direction_sql = "DESC" if direction.lower() == "desc" else "ASC"
    offset = (page - 1) * per_page

    async with engine.connect() as conn:
        total = await conn.execute(text("SELECT COUNT(*) FROM products p WHERE p.active = TRUE"))
        total_count = int(total.scalar() or 0)

        rows_res = await conn.execute(
            text(f"""
                SELECT p.id, p.name, p.sale_price, p.cost_price, p.unit, p.min_stock,
                       c.name as category_name,
                       COALESCE(SUM(sm.qty), 0) as current_stock
                FROM products p
                LEFT JOIN categories c ON c.id = p.category_id
                LEFT JOIN stock_movements sm ON sm.product_id = p.id
                WHERE p.active = TRUE
                GROUP BY p.id, p.name, p.sale_price, p.cost_price, p.unit, p.min_stock, c.name
                ORDER BY COALESCE(c.name, 'Outro') ASC, {sort_col} {direction_sql}
                LIMIT :limit OFFSET :offset
            """),
            {"limit": per_page, "offset": offset},
        )
        rows = rows_res.mappings().all()

    total_pages = max(1, ceil(total_count / per_page))
    page = min(page, total_pages)

    return templates.TemplateResponse(
        "products_list.html",
        {
            "request": request,
            "rows": rows,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
            "total_count": total_count,
            "sort": sort if sort in SORT_FIELDS else "name",
            "direction": "desc" if direction.lower() == "desc" else "asc",
            "deleted": request.query_params.get("deleted") == "1",
        },
    )
