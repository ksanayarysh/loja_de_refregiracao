import os
import base64
from decimal import Decimal

from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text

app = FastAPI()
templates = Jinja2Templates(directory="templates")

DATABASE_URL = os.environ["DATABASE_URL"]

# asyncpg не требует системных библиотек
DATABASE_URL = DATABASE_URL.replace("postgresql+psycopg2://", "postgresql+asyncpg://")
DATABASE_URL = DATABASE_URL.replace("postgresql+psycopg://", "postgresql+asyncpg://")
DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")
DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://")

print("DB URL (masked):", DATABASE_URL.split("@")[-1])

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "change-me")

engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db():
    async with engine.begin() as conn:
        await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS products (
            id BIGSERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT,
            unit TEXT NOT NULL DEFAULT 'un',
            sale_price NUMERIC(12,2) NOT NULL DEFAULT 0,
            min_stock INTEGER NOT NULL DEFAULT 0,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS sales (
          id BIGSERIAL PRIMARY KEY,
          sold_at DATE NOT NULL DEFAULT CURRENT_DATE,
          product_id BIGINT NOT NULL REFERENCES products(id),
          qty NUMERIC(12,3) NOT NULL CHECK (qty > 0),
          unit_price NUMERIC(12,2) NOT NULL CHECK (unit_price >= 0),
          total NUMERIC(12,2) NOT NULL CHECK (total >= 0),
          note TEXT,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        
        CREATE INDEX IF NOT EXISTS idx_sales_sold_at ON sales(sold_at);
        CREATE INDEX IF NOT EXISTS idx_sales_product_id ON sales(product_id);
        """))


@app.on_event("startup")
async def _startup():
    await init_db()


def basic_auth(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        raise HTTPException(
            status_code=401,
            detail="Auth required",
            headers={"WWW-Authenticate": "Basic"},
        )
    try:
        userpass = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
        user, pwd = userpass.split(":", 1)
    except Exception:
        raise HTTPException(
            status_code=401,
            detail="Bad auth",
            headers={"WWW-Authenticate": "Basic"},
        )
    if user != ADMIN_USER or pwd != ADMIN_PASS:
        raise HTTPException(
            status_code=401,
            detail="Invalid auth",
            headers={"WWW-Authenticate": "Basic"},
        )
    return True


@app.get("/products/new", response_class=HTMLResponse)
async def new_product_form(request: Request, _=Depends(basic_auth)):
    return templates.TemplateResponse("new_product.html", {"request": request})


@app.post("/products/new")
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
    # мягкий парсинг цены: "35", "35,50", "R$ 35,50"
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

from fastapi.responses import HTMLResponse

from math import ceil
from fastapi import Query
from sqlalchemy import text

# Разрешённые поля сортировки (ключ из URL -> реальная колонка в SQL)
SORT_FIELDS = {
    "name": "p.name",
    "unit": "p.unit",
    "sale_price": "p.sale_price",
    "min_stock": "p.min_stock",
    "active": "p.active",
    "created_at": "p.created_at",
    "id": "p.id",
}

@app.get("/products", response_class=HTMLResponse)
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
        rows = rows_res.mappings().all()  # list[RowMapping]

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
