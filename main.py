from fastapi import FastAPI, Depends
from sqlalchemy import text
from database import init_db
from routers import products, sales
from fastapi.responses import HTMLResponse
from fastapi import FastAPI, Depends, Request
from dependencies import engine, basic_auth, templates


app = FastAPI()


@app.on_event("startup")
async def _startup():
    await init_db()


app.include_router(products.router)
app.include_router(sales.router)

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, _=Depends(basic_auth)):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/products")
async def api_products(search: str = "", unit: str = "", _=Depends(basic_auth)):
    async with engine.connect() as conn:
        where = ["active = TRUE"]
        params = {}
        if search:
            where.append("name ILIKE :search")
            params["search"] = f"%{search}%"
        if unit:
            where.append("unit = :unit")
            params["unit"] = unit
        res = await conn.execute(
            text(f"SELECT id, name, sale_price, unit FROM products WHERE {' AND '.join(where)} ORDER BY name"),
            params,
        )
        return [dict(r) for r in res.mappings().all()]

@app.get("/calc", response_class=HTMLResponse)
async def calc_page(request: Request, _=Depends(basic_auth)):
    return templates.TemplateResponse("calc.html", {"request": request})