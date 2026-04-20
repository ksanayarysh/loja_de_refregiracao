from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from router import router as catalog_router

app = FastAPI(title="M.T.F Refrigeração — Catálogo")

app.add_middleware(GZipMiddleware, minimum_size=500)

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

    response = await call_next(request)

    # Cache for static files (30 days)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=2592000, immutable"

    # Block Kaspersky and other injected third-party scripts
    response.headers["Content-Security-Policy"] = (
        "script-src 'self' 'unsafe-inline' https://www.googletagmanager.com "
        "https://connect.facebook.net https://www.google-analytics.com "
        "https://www.gstatic.com https://cdn.jsdelivr.net; "
        "object-src 'none';"
    )

    return response