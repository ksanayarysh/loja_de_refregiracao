"""Reservas para retirar na loja (feitas no site): separar, confirmar pelo WhatsApp, marcar como retirada."""
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import text

from dependencies import engine, templates, basic_auth

router = APIRouter()
BRT = ZoneInfo("America/Sao_Paulo")


@router.get("/reservas", response_class=HTMLResponse)
async def reservas_page(request: Request, ver: str = Query("abertas"), _=Depends(basic_auth)):
    rows = []
    try:
        async with engine.connect() as conn:
            where = "status = 'nova'" if ver == "abertas" else "created_at >= NOW() - INTERVAL '30 days'"
            rows = (await conn.execute(text(f"SELECT * FROM reservas WHERE {where} ORDER BY id DESC LIMIT 200"))
                    ).mappings().all()
    except Exception:
        rows = []    # tabela ainda não existe (nenhuma reserva)
    lista = []
    for r in rows:
        p = r["phone"] or ""
        primeiro = (r["name"] or "").split(" ")[0].title()
        qty = float(r["qty"] or 1)
        qtxt = f"{qty:g}"
        numero = f"R-{int(r['id']) + 1000}"      # mesmo número do site (id + 1000)
        msg = (f"Olá, {primeiro}! Aqui é da MTF Refrigeração 😊 Sua reserva {numero} está separada: {r['product_name']} × {qtxt}. "
               f"Pode retirar na Av. Brasil, 6818 – Bonsucesso (seg. a sáb., 8h30–18h). Pagamento na loja.")
        lista.append({
            "id": r["id"], "numero": numero, "status": r["status"], "nome": r["name"], "produto": r["product_name"], "qty": qtxt,
            "total": f"{float(r['price'] or 0) * qty:.2f}".replace(".", ","),
            "tel": f"({p[:2]}) {p[2:7]}-{p[7:]}" if len(p) == 11 else p,
            "quando": r["created_at"].astimezone(BRT).strftime("%d/%m %H:%M"),
            "src": r["src"] or "", "wa": f"https://wa.me/55{p}?text={quote(msg)}",
            "status_at": r["status_at"].astimezone(BRT).strftime("%d/%m %H:%M") if r["status_at"] else "",
        })
    return templates.TemplateResponse("reservas.html", {"request": request, "lista": lista, "ver": ver,
                                                         "active_page": "reservas"})


@router.post("/reservas/{rid}/{acao}")
async def reserva_status(rid: int, acao: str, _=Depends(basic_auth)):
    novo = {"retirada": "retirada", "cancelar": "cancelada", "reabrir": "nova"}.get(acao)
    if novo:
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE reservas SET status = :s, status_at = NOW() WHERE id = :id"),
                               {"s": novo, "id": rid})
    return RedirectResponse(url="/reservas", status_code=303)
