from sqlalchemy import text
from database import init_db
from routers import products, sales
from fastapi.responses import HTMLResponse
from fastapi import FastAPI, Depends, Request
from dependencies import engine, basic_auth, templates
from routers.stock import router as stock_router
from routers.reports import router as reports_router
from routers.reports_cron import router as cron_router
from routers.clients import router as clients_router

app = FastAPI()


@app.on_event("startup")
async def _startup():
    await init_db()

app.include_router(clients_router)
app.include_router(products.router)
app.include_router(sales.router)
app.include_router(reports_router)
app.include_router(stock_router)
app.include_router(cron_router)

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, _=Depends(basic_auth)):
    import os
    return templates.TemplateResponse("index.html", {
        "request": request,
        "wa_number": os.environ.get("WA_OWNER_NUMBER", ""),
    })


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

@app.post("/api/categories")
async def create_category(data: dict, _=Depends(basic_auth)):
    async with engine.begin() as conn:
        res = await conn.execute(
            text("INSERT INTO categories (name) VALUES (:name) ON CONFLICT (name) DO UPDATE SET name=EXCLUDED.name RETURNING id, name"),
            {"name": data["name"].strip()}
        )
        row = res.mappings().first()
        return dict(row)


from fastapi.staticfiles import StaticFiles
app.mount("/static", StaticFiles(directory="/app/static"), name="static")

from fastapi.middleware.httpsredirect import HTTPSRedirectMiddleware

@app.middleware("http")
async def add_cache_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=2592000, immutable"
    return response


@app.get("/debug/static")
async def debug_static():
    import os
    path = "/app/static"
    try:
        files = os.listdir(path)
        return {"path": path, "count": len(files), "files": files[:10]}
    except Exception as e:
        return {"error": str(e)}

@app.get("/debug/static/images")
async def debug_static():
    import os
    path = "/app/static/images"
    try:
        files = os.listdir(path)
        return {"path": path, "count": len(files), "files": files[:10]}
    except Exception as e:
        return {"error": str(e)}