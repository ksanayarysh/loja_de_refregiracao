"""Admin do programa "Preço de técnico": aprovar cadastros, mandar o código, achar técnico pelo WhatsApp (balcão)."""
import os
import random
import re
import unicodedata
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import text

from dependencies import engine, templates, basic_auth

router = APIRouter()
SITE_URL = os.environ.get("SITE_URL", "https://www.mtfrefrigeracao.com.br").rstrip("/")


def _pct() -> float:
    try:
        return float(os.environ.get("TECNICO_PCT", "3") or 3)
    except ValueError:
        return 3.0


async def _ensure():
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS tecnicos (
                id SERIAL PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                nome VARCHAR(120) NOT NULL, phone VARCHAR(20) NOT NULL, doc VARCHAR(20), cidade VARCHAR(120),
                especialidade VARCHAR(200), obs TEXT, status VARCHAR(12) NOT NULL DEFAULT 'pendente',
                codigo VARCHAR(20) UNIQUE, aprovado_at TIMESTAMPTZ, ip VARCHAR(64))"""))


def _tel(p: str) -> str:
    p = p or ""
    return f"({p[:2]}) {p[2:7]}-{p[7:]}" if len(p) == 11 else p


def _msg_codigo(nome: str, codigo: str, pct: float) -> str:
    primeiro = (nome or "").split(" ")[0].title()
    return (f"Olá, {primeiro}! Aqui é da MTF Refrigeração 😊 Seu cadastro de técnico foi aprovado!\n\n"
            f"Seu código: *{codigo}*\n"
            f"• No site: digite o código no checkout (\"Sou técnico cadastrado\") e ganha {pct:g}% de desconto nas peças.\n"
            f"• Na loja (Av. Brasil, 6818 – Bonsucesso): é só falar seu WhatsApp no balcão.\n\n"
            f"Catálogo: {SITE_URL}/catalogo")


@router.get("/tecnicos", response_class=HTMLResponse)
async def tecnicos_page(request: Request, q: str = Query(""), _=Depends(basic_auth)):
    await _ensure()
    dig = re.sub(r"\D", "", q)
    async with engine.connect() as conn:
        if dig:
            rows = (await conn.execute(text("SELECT * FROM tecnicos WHERE phone LIKE :p ORDER BY id DESC LIMIT 20"),
                                       {"p": f"%{dig[-9:]}%"})).mappings().all()
        else:
            rows = (await conn.execute(text("""SELECT * FROM tecnicos ORDER BY
                     CASE status WHEN 'pendente' THEN 0 WHEN 'aprovado' THEN 1 ELSE 2 END, id DESC LIMIT 300"""))
                    ).mappings().all()
    pct = _pct()
    lista = []
    for r in rows:
        d = dict(r)
        d["tel"] = _tel(r["phone"])
        d["quando"] = r["created_at"].strftime("%d/%m/%Y") if r["created_at"] else ""
        if r["status"] == "aprovado" and r["codigo"]:
            d["wa"] = f"https://wa.me/55{r['phone']}?text={quote(_msg_codigo(r['nome'], r['codigo'], pct))}"
        lista.append(d)
    return templates.TemplateResponse("tecnicos.html", {"request": request, "lista": lista, "q": q, "pct": pct,
                                                         "link": f"{SITE_URL}/tecnico"})


@router.post("/tecnicos/{tid}/aprovar")
async def tecnico_aprovar(tid: int, _=Depends(basic_auth)):
    await _ensure()
    async with engine.begin() as conn:
        r = (await conn.execute(text("SELECT nome, codigo FROM tecnicos WHERE id = :id"), {"id": tid})).mappings().first()
        if r:
            codigo = r["codigo"]
            if not codigo:
                base = unicodedata.normalize("NFD", (r["nome"] or "TEC").split(" ")[0]).encode("ascii", "ignore").decode()
                base = re.sub(r"[^A-Za-z]", "", base).upper()[:8] or "TEC"
                for _i in range(30):
                    cand = f"{base}{random.randint(100, 999)}"
                    if not (await conn.execute(text("SELECT 1 FROM tecnicos WHERE codigo = :c"), {"c": cand})).first():
                        codigo = cand
                        break
            await conn.execute(text("UPDATE tecnicos SET status = 'aprovado', codigo = :c, "
                                    "aprovado_at = COALESCE(aprovado_at, NOW()) WHERE id = :id"), {"c": codigo, "id": tid})
    return RedirectResponse(url="/tecnicos", status_code=303)


@router.post("/tecnicos/{tid}/recusar")
async def tecnico_recusar(tid: int, _=Depends(basic_auth)):
    await _ensure()
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE tecnicos SET status = 'recusado' WHERE id = :id"), {"id": tid})
    return RedirectResponse(url="/tecnicos", status_code=303)
