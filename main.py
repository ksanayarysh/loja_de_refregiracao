import os
from datetime import datetime
from decimal import Decimal

from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

app = FastAPI()
templates = Jinja2Templates(directory="templates")

DATABASE_URL = os.environ["DATABASE_URL"]

DATABASE_URL = DATABASE_URL.replace("postgresql+psycopg://", "postgresql+psycopg2://")
DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://")
DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://")

print("DB URL (masked):", DATABASE_URL.split("@")[-1])

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "change-me")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)

def init_db():
    with engine.begin() as conn:
        conn.execute(text("""
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

@app.on_event("startup")
def _startup():
    init_db()

def basic_auth(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        raise HTTPException(status_code=401, detail="Auth required", headers={"WWW-Authenticate": "Basic"})
    import base64
    try:
        userpass = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
        user, pwd = userpass.split(":", 1)
    except Exception:
        raise HTTPException(status_code=401, detail="Bad auth", headers={"WWW-Authenticate": "Basic"})
    if user != ADMIN_USER or pwd != ADMIN_PASS:
        raise HTTPException(status_code=401, detail="Invalid auth", headers={"WWW-Authenticate": "Basic"})
    return True

@app.get("/products/new", response_class=HTMLResponse)
def new_product_form(request: Request, _=Depends(basic_auth)):
    return templates.TemplateResponse("new_product.html", {"request": request})

@app.post("/products/new")
def create_product(
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
    cleaned = sale_price.strip().replace("R$", "").replace(" ", "").replace(".", "").replace(",", ".")
    try:
        price = Decimal(cleaned)
    except Exception:
        price = Decimal("0")

    is_active = active.lower() in ("true", "1", "on", "sim", "yes")

    with engine.begin() as conn:
        conn.execute(
            text("""INSERT INTO products (name, category, unit, sale_price, min_stock, active)
                    VALUES (:name, :category, :unit, :sale_price, :min_stock, :active)"""),
            {"name": name.strip(), "category": category.strip(), "unit": unit, "sale_price": price, "min_stock": min_stock, "active": is_active},
        )

    return RedirectResponse(url="/products/new?ok=1", status_code=303)
