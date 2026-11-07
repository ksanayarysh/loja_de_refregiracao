"""Programa "Preço de técnico": técnicos de refrigeração/eletrodomésticos se cadastram em /tecnico,
a loja aprova na admin (Técnicos) e o técnico recebe um código pessoal.
Com o código no checkout: TECNICO_PCT % de desconto nos produtos (kits já têm desconto próprio).
Na loja física: o vendedor procura o técnico pelo WhatsApp na admin e dá o mesmo desconto.

Variável de ambiente (catálogo): TECNICO_PCT (padrão 3)
"""
import os
import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import text

from dependencies import engine, templates
from router import _send_tg, _client_ip, _is_bot, _blog_log, GA_ID, SITE_URL, STORE_ADDRESS, STORE_PHONE, WA_OWNER_NUMBER

router = APIRouter()
_ok = False


def tecnico_pct() -> float:
    try:
        return max(0.0, min(30.0, float(os.environ.get("TECNICO_PCT", "3") or 3)))
    except ValueError:
        return 3.0


async def ensure_tecnicos_table():
    global _ok
    if _ok:
        return
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS tecnicos (
                id            SERIAL PRIMARY KEY,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                nome          VARCHAR(120) NOT NULL,
                phone         VARCHAR(20) NOT NULL,
                doc           VARCHAR(20),
                cidade        VARCHAR(120),
                especialidade VARCHAR(200),
                obs           TEXT,
                status        VARCHAR(12) NOT NULL DEFAULT 'pendente',   -- pendente / aprovado / recusado
                codigo        VARCHAR(20) UNIQUE,
                aprovado_at   TIMESTAMPTZ,
                ip            VARCHAR(64)
            )"""))
    _ok = True


async def tecnico_por_codigo(codigo: str) -> dict | None:
    """Técnico APROVADO com este código (sem diferenciar maiúsculas/espaços), ou None."""
    c = re.sub(r"[^A-Za-z0-9]", "", str(codigo or "")).upper()
    if len(c) < 4:
        return None
    await ensure_tecnicos_table()
    async with engine.connect() as conn:
        r = (await conn.execute(text(
            "SELECT id, nome, codigo FROM tecnicos WHERE status = 'aprovado' "
            "AND UPPER(REGEXP_REPLACE(codigo, '[^A-Za-z0-9]', '', 'g')) = :c"), {"c": c})).mappings().first()
    return dict(r) if r else None


def aplicar_desconto(items: list, pct: float) -> tuple[list, float, float]:
    """Desconto de técnico nos produtos avulsos (kit e item de kit ficam como estão).
    -> (itens com preço novo, subtotal novo, total de desconto)."""
    novos, sub, desc = [], 0.0, 0.0
    for it in items:
        it = dict(it)
        # promoção "2ª unidade" já é desconto: não soma com o de técnico
        if not it.get("kit") and not it.get("kit_id") and not it.get("promo2") and pct > 0:
            cheio = float(it["price"])
            preco = round(cheio * (100 - pct) / 100, 2)
            desc += (cheio - preco) * float(it["qty"])
            it.update({"price": preco, "subtotal": round(preco * float(it["qty"]), 2),
                       "tecnico_pct": pct, "full_price": it.get("full_price") or cheio})
        sub += float(it["price"]) * float(it["qty"])
        novos.append(it)
    return novos, round(sub, 2), round(desc, 2)


@router.get("/tecnico", response_class=HTMLResponse)
async def tecnico_page(request: Request):
    return templates.TemplateResponse("tecnico.html", {
        "request": request, "ga_id": GA_ID, "site_url": SITE_URL, "store_address": STORE_ADDRESS,
        "store_phone": STORE_PHONE, "wa_number": WA_OWNER_NUMBER, "pct": int(tecnico_pct()),
    })


@router.post("/api/tecnico/cadastro")
async def tecnico_cadastro(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Dados inválidos."}, status_code=400)
    from checkout import _norm_cel                      # import tardio: checkout importa router
    nome = str(body.get("nome") or "").strip()[:120]
    phone = _norm_cel(body.get("phone", ""))
    if len(nome) < 3:
        return JSONResponse({"error": "Informe seu nome completo."}, status_code=400)
    if not phone:
        return JSONResponse({"error": "Informe um WhatsApp válido com DDD, ex.: (21) 99999-9999."}, status_code=400)
    if _is_bot(request.headers.get("user-agent", "")):
        return JSONResponse({"ok": True})
    doc = re.sub(r"[^\d]", "", str(body.get("doc") or ""))[:14] or None
    cidade = str(body.get("cidade") or "").strip()[:120] or None
    esp = ", ".join(str(x)[:40] for x in (body.get("especialidade") or [])[:6]) or None
    obs = str(body.get("obs") or "").strip()[:500] or None
    await ensure_tecnicos_table()
    async with engine.begin() as conn:
        ja = (await conn.execute(text("SELECT id, status FROM tecnicos WHERE phone = :p ORDER BY id DESC LIMIT 1"),
                                 {"p": phone})).mappings().first()
        if ja and ja["status"] in ("pendente", "aprovado"):
            return JSONResponse({"ok": True, "ja": ja["status"]})
        await conn.execute(text("""
            INSERT INTO tecnicos (nome, phone, doc, cidade, especialidade, obs, ip)
            VALUES (:n, :p, :d, :c, :e, :o, :ip)"""),
            {"n": nome, "p": phone, "d": doc, "c": cidade, "e": esp, "o": obs, "ip": _client_ip(request)})
    tel = f"({phone[:2]}) {phone[2:7]}-{phone[7:]}"
    try:
        await _send_tg(f"🔧 <b>Novo cadastro de técnico</b>\n{nome} · {tel}\n"
                       f"{cidade or ''}{(' · ' + esp) if esp else ''}\nAprovar em: admin → Técnicos")
    except Exception:
        _blog_log.exception("tecnico: falha ao avisar")
    return JSONResponse({"ok": True})


@router.get("/api/tecnico/validar")
async def tecnico_validar(codigo: str = ""):
    t = await tecnico_por_codigo(codigo)
    if not t:
        return JSONResponse({"ok": False, "error": "Código não encontrado ou ainda não aprovado."})
    return JSONResponse({"ok": True, "nome": t["nome"].split(" ")[0].title(), "pct": tecnico_pct(),
                         "codigo": t["codigo"]}, headers={"Cache-Control": "no-store"})
