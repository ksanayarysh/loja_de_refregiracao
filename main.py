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
        """))
        await conn.execute(text("""
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

        """))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_sales_sold_at ON sales(sold_at)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_sales_product_id ON sales(product_id)"))


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

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from fastapi import Form
from sqlalchemy import text

def money2(x) -> Decimal:
    return (Decimal(str(x))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

@app.get("/sales/new", response_class=HTMLResponse)
async def sales_new(request: Request, _=Depends(basic_auth)):
    async with engine.connect() as conn:
        res = await conn.execute(text("""
            SELECT id, name, sale_price, unit
            FROM products
            WHERE active = TRUE
            ORDER BY name
        """))
        products = res.mappings().all()

    return templates.TemplateResponse(
        "new_sale.html",
        {
            "request": request,
            "today": date.today().isoformat(),
            "products": products,
            "error": None,
            "success": None,
        },
    )

@app.post("/sales/new", response_class=HTMLResponse)
async def sales_create(
    request: Request,
    sold_at: str = Form(...),
    product_id: int = Form(...),
    qty: str = Form(...),
    unit_price: str = Form(""),   # можно не вводить, подставим
    total: str = Form(""),        # можно не вводить, пересчитаем
    note: str = Form(""),
    _=Depends(basic_auth),
):
    error = None
    success = None

    try:
        qty_d = Decimal(qty.replace(",", "."))
        if qty_d <= 0:
            raise ValueError("qty must be > 0")
    except Exception:
        error = "Quantidade inválida. Ex: 1 ou 0,5"
        qty_d = None

    async with engine.begin() as conn:
        # список продуктов для повторного рендера
        res = await conn.execute(text("""
            SELECT id, name, sale_price, unit
            FROM products
            WHERE active = TRUE
            ORDER BY name
        """))
        products = res.mappings().all()

        # достанем текущую цену товара (для автоподстановки)
        p = await conn.execute(
            text("SELECT sale_price FROM products WHERE id = :pid AND active = TRUE"),
            {"pid": product_id},
        )
        row = p.first()
        if row is None:
            error = "Produto inválido (não encontrado)."

        if not error:
            default_price = Decimal(str(row[0] or 0))
            # unit_price: если пусто, берём из products
            if unit_price.strip():
                try:
                    price_d = Decimal(unit_price.replace(",", "."))
                except Exception:
                    error = "Preço inválido. Ex: 35 ou 35,50"
                    price_d = None
            else:
                price_d = default_price

            if not error:
                price_d = money2(price_d)

                # total: если пусто, считаем; если введено, берём, но нормализуем
                if total.strip():
                    try:
                        total_d = Decimal(total.replace(",", "."))
                    except Exception:
                        error = "Total inválido."
                        total_d = None
                else:
                    total_d = qty_d * price_d

                if not error:
                    total_d = money2(total_d)
                    sold_at_date = date.fromisoformat(sold_at)

                    await conn.execute(
                        text("""
                            INSERT INTO sales (sold_at, product_id, qty, unit_price, total, note)
                            VALUES :sold_at, :product_id, :qty, :unit_price, :total, :note)
                        """),
                        {
                            "sold_at": sold_at_date,
                            "product_id": product_id,
                            "qty": qty_d,
                            "unit_price": price_d,
                            "total": total_d,
                            "note": note.strip() or None,
                        },
                    )
                    success = "Venda registrada."

    return templates.TemplateResponse(
        "new_sale.html",
        {
            "request": request,
            "today": sold_at or date.today().isoformat(),
            "products": products,
            "error": error,
            "success": success,
        },
    )
