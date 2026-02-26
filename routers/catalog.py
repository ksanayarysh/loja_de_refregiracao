from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import text

from dependencies import engine, templates

router = APIRouter()


@router.get("/catalog", response_class=HTMLResponse)
async def catalog_page(request: Request):
    return templates.TemplateResponse("catalog.html", {"request": request})


@router.get("/api/catalog")
async def catalog_api():
    """Catálogo público de produtos — sem autenticação."""
    async with engine.connect() as conn:
        res = await conn.execute(text("""
            SELECT p.id, p.name, p.sale_price, p.unit,
                   COALESCE(c.name, 'Outro') AS category_name,
                   GREATEST(0, COALESCE(SUM(sm.qty), 0)) AS current_stock
            FROM products p
            LEFT JOIN categories c ON c.id = p.category_id
            LEFT JOIN stock_movements sm ON sm.product_id = p.id
            WHERE p.active = TRUE
            GROUP BY p.id, p.name, p.sale_price, p.unit, c.name
            ORDER BY c.name NULLS LAST, p.name
        """))
        rows = res.mappings().all()

    return [
        {
            "id":         r["id"],
            "name":       r["name"],
            "sale_price": float(r["sale_price"]) if r["sale_price"] else None,
            "unit":       r["unit"],
            "category":   r["category_name"],
            "in_stock":   float(r["current_stock"]) > 0,
        }
        for r in rows
    ]
