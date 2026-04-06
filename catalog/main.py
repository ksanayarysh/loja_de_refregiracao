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

    print("HOST =", request.headers.get("host"))
    print("URL =", request.url)

    print("HOST")
    print(host)

    if host == "mtf-catalog.up.railway.app":
        return RedirectResponse(
            str(request.url).replace(request.headers.get("host", ""), "www.mtfrefrigeracao.com.br"),
            status_code=301
        )

    if host == "mtfrefrigeracao.com.br":
        return RedirectResponse(
            str(request.url).replace(request.headers.get("host", ""), "www.mtfrefrigeracao.com.br"),
            status_code=301
        )

    return await call_next(request)