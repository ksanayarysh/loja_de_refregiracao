"""
Envio para fora da área da Lalamove (outros estados / longe) via Melhor Envio: Correios, Jadlog etc.
Só peças: gás, cilindros, aerossóis e inflamáveis NÃO vão (Correios proíbe all gás comprimido).

Variáveis de ambiente (catálogo):
  MELHOR_ENVIO_TOKEN    token gerado no painel do Melhor Envio (sem ele, nada disso liga)
  MTF_CEP               CEP da loja (origem dos envios), ex.: 21040-361
  MELHOR_ENVIO_BASE     padrão https://melhorenvio.com.br  (teste: https://sandbox.melhorenvio.com.br)
  MELHOR_ENVIO_CONTATO  e-mail que vai no User-Agent (exigência do Melhor Envio)
  MELHOR_ENVIO_SERVICOS opcional, ids separados por vírgula (vazio = todos que o Melhor Envio devolver)
  ENVIO_DIAS_POSTAGEM   dias úteis até postar (padrão 1) — somado ao prazo da transportadora

Peso/medidas: colunas ship_* do produto (admin → editar produto); vazio = caixa padrão da categoria.
ship_bloq: NULL = regra automática pelo nome; TRUE = nunca envia; FALSE = pode enviar mesmo se a regra barrar.
"""
import logging
import os
import re
import time

import httpx
from sqlalchemy import text

from dependencies import engine, so_balcao

log = logging.getLogger("uvicorn.error")


def _token() -> str:
    return os.environ.get("MELHOR_ENVIO_TOKEN", "").strip()


def _origem() -> str:
    return re.sub(r"\D", "", os.environ.get("MTF_CEP", ""))


def ativo() -> bool:
    return bool(_token()) and len(_origem()) == 8


def _base() -> str:
    return os.environ.get("MELHOR_ENVIO_BASE", "https://melhorenvio.com.br").rstrip("/")


def dias_postagem() -> int:
    try:
        return max(0, int(os.environ.get("ENVIO_DIAS_POSTAGEM", "1")))
    except ValueError:
        return 1


# Correios/transportadoras: gás comprimido, aerossol e inflamável não podem
_PROIBIDO = re.compile(
    r"\bg[aá]s\b|botija|cilindro|nitrog[eê]nio|\bmapp\b|propano|butano|aeross|spray|wd-?40|"
    r"[aá]lcool|isopropanol|thinner|solvente|141b|air ?flush|air ?repair|gpclear|fluido de limpeza",
    re.I)

# caixa padrão por categoria: (peso kg, comprimento, largura, altura em cm)
_CAIXA = {
    "placa eletronica": (0.4, 25, 20, 8),
    "ventilador": (1.0, 20, 20, 15),
    "maquina de lavar": (0.8, 25, 20, 15),
    "ar condicionado": (1.0, 40, 20, 20),
    "geladeira": (0.5, 25, 20, 10),
    "bebedouro": (0.5, 20, 15, 10),
    "ferramentas": (1.0, 30, 20, 10),
    "suporte": (3.0, 50, 15, 10),
    "esponjoso": (0.3, 40, 15, 15),
    "limpeza": (0.6, 25, 20, 15),
    "kit": (1.0, 30, 25, 15),
}
_CAIXA_PADRAO = (0.4, 20, 15, 10)
_POR_METRO = {"tubo de cobre": 0.2, "tubo": 0.2, "cabo": 0.12, "capilar": 0.05, "esponjoso": 0.1}


def bloqueado(p: dict) -> bool:
    if p.get("ship_bloq") is True:
        return True
    if p.get("ship_bloq") is False:
        return False
    return so_balcao(p) or bool(_PROIBIDO.search(p.get("name") or ""))


def _caixa(p: dict, qty: float):
    """-> dict de produto no formato do Melhor Envio (1 linha)."""
    cat = (p.get("categoria") or "").strip().lower()
    unit = (p.get("unit") or "un").strip().lower()
    nome = (p.get("name") or "").lower()
    peso, c, l, a = _CAIXA.get(cat, _CAIXA_PADRAO)
    if nome.startswith("compressor") and "embraco" in nome:
        peso, c, l, a = 9.0, 30, 25, 25
    quantidade = qty
    if unit == "m":                      # metro: vira um rolo só, peso pelo comprimento
        peso = _POR_METRO.get(cat, 0.15) * qty
        c, l, a = 35, 35, 10
        quantidade = 1
    if p.get("ship_peso"):
        peso = float(p["ship_peso"]) * (qty if unit == "m" else 1)
    if p.get("ship_comp") and p.get("ship_larg") and p.get("ship_alt"):
        c, l, a = float(p["ship_comp"]), float(p["ship_larg"]), float(p["ship_alt"])
    preco = float(p.get("price") or 0)
    return {
        "id": str(p.get("id")),
        "width": max(11, l), "height": max(2, a), "length": max(16, c),
        "weight": round(max(0.05, peso), 3),
        "insurance_value": round(preco * (qty if quantidade == 1 and qty != 1 else 1), 2),
        "quantity": int(max(1, round(quantidade))),
    }


async def _extras(ids: list) -> dict:
    """categoria + colunas de envio dos produtos (colunas podem ainda não existir)."""
    ids = [int(i) for i in ids if i]
    if not ids:
        return {}
    async with engine.connect() as conn:
        try:
            rows = (await conn.execute(text("""
                SELECT p.id, COALESCE(c.name, '') AS categoria,
                       p.ship_peso, p.ship_comp, p.ship_larg, p.ship_alt, p.ship_bloq
                FROM products p LEFT JOIN categories c ON c.id = p.category_id
                WHERE p.id = ANY(:ids)"""), {"ids": ids})).mappings().all()
        except Exception:
            await conn.rollback()
            rows = (await conn.execute(text("""
                SELECT p.id, COALESCE(c.name, '') AS categoria
                FROM products p LEFT JOIN categories c ON c.id = p.category_id
                WHERE p.id = ANY(:ids)"""), {"ids": ids})).mappings().all()
    return {r["id"]: dict(r) for r in rows}


def _linhas(items: list) -> list:
    """Itens do checkout (_load_items) -> [(produto, qty)]; kit abre nos componentes."""
    out = []
    for it in items:
        if it.get("components"):
            n = float(it.get("qty") or 1)
            for c in it["components"]:
                q = float(c.get("qty") or 0) * n
                if q > 0:
                    preco = (float(c.get("line_total") or 0) / float(c["qty"])) if c.get("qty") else 0
                    out.append(({"id": c["id"], "name": c.get("name"), "unit": c.get("unit"), "price": preco}, q))
        else:
            out.append((it, float(it.get("qty") or 1)))
    return out


_cache: dict = {}   # (cep, itens) -> (ts, resultado)


async def cotar(cep: str, items: list) -> dict:
    """-> {"options": [...]} | {"bloqueados": [nomes]} | {"erro": "..."}"""
    cep = re.sub(r"\D", "", cep or "")
    linhas = _linhas(items)
    if not linhas:
        return {"erro": "carrinho vazio"}
    extras = await _extras([p.get("id") for p, _ in linhas])
    prods, bloq = [], []
    for p, q in linhas:
        full = dict(p, **extras.get(int(p.get("id") or 0), {}))
        if bloqueado(full):
            bloq.append(p.get("name") or "?")
            continue
        prods.append(_caixa(full, q))
    if bloq:
        return {"bloqueados": bloq}
    chave = (cep, tuple(sorted((x["id"], x["quantity"], x["weight"]) for x in prods)))
    hit = _cache.get(chave)
    if hit and time.time() - hit[0] < 600:
        return hit[1]

    body = {"from": {"postal_code": _origem()}, "to": {"postal_code": cep}, "products": prods,
            "options": {"receipt": False, "own_hand": False}}
    serv = os.environ.get("MELHOR_ENVIO_SERVICOS", "").strip()
    if serv:
        body["services"] = serv
    contato = os.environ.get("MELHOR_ENVIO_CONTATO", "contato@mtfrefrigeracao.com.br")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(f"{_base()}/api/v2/me/shipment/calculate", json=body, headers={
                "Accept": "application/json", "Content-Type": "application/json",
                "Authorization": f"Bearer {_token()}", "User-Agent": f"MTF Refrigeracao ({contato})",
            })
        data = r.json()
    except Exception as e:
        log.warning("melhor envio: falha %s: %s", cep, e)
        return {"erro": "transportadora fora do ar"}
    if r.status_code >= 400 or not isinstance(data, list):
        log.warning("melhor envio: %s %s", r.status_code, str(data)[:300])
        return {"erro": "não cotou"}

    extra = dias_postagem()
    opts = []
    for s in data:
        if s.get("error") or s.get("custom_price") in (None, ""):
            continue
        try:
            preco = float(s.get("custom_price") or s.get("price"))
        except (TypeError, ValueError):
            continue
        rng = s.get("custom_delivery_range") or s.get("delivery_range") or {}
        dias = int(rng.get("max") or s.get("custom_delivery_time") or s.get("delivery_time") or 0) + extra
        empresa = (s.get("company") or {}).get("name") or ""
        nome = s.get("name") or ""
        opts.append({"service": f"ME-{s.get('id')}", "label": f"{empresa} {nome}".strip(),
                     "hint": f"Chega em até {dias} dias úteis" if dias else "Prazo informado na postagem",
                     "price": round(preco, 2), "dias": dias})
    opts.sort(key=lambda o: o["price"])
    res = {"options": opts[:4]} if opts else {"erro": "sem opção de envio para este CEP"}
    _cache[chave] = (time.time(), res)
    return res


async def cep_info(cep: str) -> dict | None:
    """Rua/bairro/cidade/UF do CEP (sem coordenadas — para Correios não precisa)."""
    cep = re.sub(r"\D", "", cep or "")
    async with httpx.AsyncClient(timeout=8) as c:
        for url, conv in (
            (f"https://viacep.com.br/ws/{cep}/json/",
             lambda j: None if j.get("erro") else {"rua": j.get("logradouro") or "", "bairro": j.get("bairro") or "",
                                                   "cidade": j.get("localidade") or "", "uf": j.get("uf") or ""}),
            (f"https://brasilapi.com.br/api/cep/v2/{cep}",
             lambda j: {"rua": j.get("street") or "", "bairro": j.get("neighborhood") or "",
                        "cidade": j.get("city") or "", "uf": j.get("state") or ""}),
        ):
            try:
                r = await c.get(url)
                if r.status_code == 200:
                    d = conv(r.json())
                    if d and d.get("cidade"):
                        return d
            except Exception:
                continue
    return None


def doc_ok(doc: str) -> str:
    """CPF (11) ou CNPJ (14) válido -> só dígitos; senão ''."""
    d = re.sub(r"\D", "", doc or "")
    if len(d) == 11 and len(set(d)) > 1:
        for n in (9, 10):
            s = sum(int(d[i]) * (n + 1 - i) for i in range(n))
            if (s * 10 % 11) % 10 != int(d[n]):
                return ""
        return d
    if len(d) == 14 and len(set(d)) > 1:
        for n in (12, 13):
            pesos = list(range(n - 7, 1, -1)) + list(range(9, 1, -1))
            s = sum(int(d[i]) * pesos[i] for i in range(n))
            dv = 11 - s % 11
            if (0 if dv >= 10 else dv) != int(d[n]):
                return ""
        return d
    return ""
