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
    host = request.headers.get("host", "")

    if "railway.app" in host:
        url = str(request.url).replace(host, "mtfrefrigeracao.com.br")
        return RedirectResponse(url, status_code=301)

    return await call_next(request)