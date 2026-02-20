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


@router.get("/products/new", response_class=HTMLResponse)
async def new_product_form(request: Request, _=Depends(basic_auth)):
    return templates.TemplateResponse("new_product.html", {"request": request})


@router.post("/products/new")
async def create_product(
    request: Request,
    name: str = Form(...),
    category: str = Form(""),
    unit: str = Form("un"),
    sale_price: str = Form("0"),
    min_stock: int = Form(0),
    active: str = Form("true"),
    _=Depends(basic_auth),
):
    cleaned = (
        sale_price.strip()
        .replace("R$", "")
        .replace(" ", "")
        .replace(".", "")
        .replace(",", ".")
    )
    try:
        price = Decimal(cleaned)
    except Exception:
        price = Decimal("0")

    is_active = active.lower() in ("true", "1", "on", "sim", "yes")

    async with engine.begin() as conn:
        await conn.execute(
            text("""INSERT INTO products (name, category, unit, sale_price, min_stock, active)
                    VALUES (:name, :category, :unit, :sale_price, :min_stock, :active)"""),
            {
                "name": name.strip(),
                "category": category.strip(),
                "unit": unit,
                "sale_price": price,
                "min_stock": min_stock,
                "active": is_active,
            },
        )

    return RedirectResponse(url="/products/new?ok=1", status_code=303)


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
                SELECT p.id, p.name, p.sale_price, p.unit
                FROM products p
                WHERE p.active = TRUE
                ORDER BY {sort_col} {direction_sql}, p.id ASC
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
        },
    )
