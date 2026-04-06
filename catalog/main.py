from fastapi import FastAPI
from router import router as catalog_router

app = FastAPI(title="M.T.F Refrigeração — Catálogo")

app.include_router(catalog_router)

from fastapi.staticfiles import StaticFiles

app.mount("/static", StaticFiles(directory="static"), name="static")

from fastapi import Request
from fastapi.responses import RedirectResponse

@app.middleware("http")
async def force_domain(request: Request, call_next):
    host = request.headers.get("host", "").split(":")[0].lower()

    if host == "mtf-catalog.up.railway.app" or host == "mtfrefrigeracao.com.br":
        new_url = str(request.url).replace(host, "www.mtfrefrigeracao.com.br", 1)
        return RedirectResponse(new_url, status_code=301)

    return await call_next(request)