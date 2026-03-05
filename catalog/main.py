from fastapi import FastAPI
from router import router as catalog_router

app = FastAPI(title="M.T.F Refrigeração — Catálogo")

app.include_router(catalog_router)

from fastapi.staticfiles import StaticFiles

app.mount("/static", StaticFiles(directory="static"), name="static")
