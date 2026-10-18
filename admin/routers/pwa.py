"""
Admin como app no celular (PWA) + notificações push.

Arquivos:
  admin/routers/pwa.py          (este)
  admin/routers/push_utils.py   (o mesmo arquivo vai no catálogo)
  admin/pwa/                    manifest.webmanifest, sw.js, ícones
    -> fora de /app/static de propósito: lá é o Volume do Railway, que esconderia arquivos do repositório.

Incluir no main.py da admin:
  from routers.pwa import router as pwa_router
  app.include_router(pwa_router)

Variáveis: VAPID_PRIVATE_KEY, VAPID_PUBLIC_KEY (gerar com gen_vapid.py)
"""
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse

from dependencies import engine, basic_auth

try:
    from routers import push_utils as push
except ImportError:
    import push_utils as push

router = APIRouter()
PWA_DIR = Path(__file__).resolve().parent.parent / "pwa"   # admin/pwa


# ── arquivos do app (sem senha: o navegador busca sozinho e não há nada secreto) ──
@router.get("/sw.js", include_in_schema=False)
async def service_worker():
    return FileResponse(PWA_DIR / "sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@router.get("/manifest.webmanifest", include_in_schema=False)
async def manifest():
    return FileResponse(PWA_DIR / "manifest.webmanifest", media_type="application/manifest+json",
                        headers={"Cache-Control": "no-cache"})


@router.get("/pwa/{name}", include_in_schema=False)
async def pwa_asset(name: str):
    allowed = {"icon-192.png", "icon-512.png", "icon-maskable-512.png", "apple-touch-icon.png"}
    if name not in allowed:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(PWA_DIR / name, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=604800"})


# ── push ─────────────────────────────────────────────────────────────────────────
@router.get("/push/public-key")
async def push_public_key(_=Depends(basic_auth)):
    return JSONResponse({"key": push.public_key(), "enabled": push.enabled()})


@router.post("/push/subscribe")
async def push_subscribe(request: Request, _=Depends(basic_auth)):
    body = await request.json()
    ok = await push.save_subscription(engine, body.get("subscription"), str(body.get("label", "")))
    return JSONResponse({"ok": ok}, status_code=200 if ok else 400)


@router.post("/push/unsubscribe")
async def push_unsubscribe(request: Request, _=Depends(basic_auth)):
    body = await request.json()
    if body.get("endpoint"):
        await push.remove_subscription(engine, body["endpoint"])
    return JSONResponse({"ok": True})


@router.post("/push/test")
async def push_test(_=Depends(basic_auth)):
    res = await push.send_all(engine, "🔔 Teste M.T.F", "As notificações estão funcionando neste aparelho.",
                              url="/site-orders", tag="teste")
    return JSONResponse(res)
