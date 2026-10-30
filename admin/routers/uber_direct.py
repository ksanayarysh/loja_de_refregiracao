"""
Cliente Uber Direct (DaaS API) para MTF Refrigeração.
Mesma "cara" do lalamove.py (get_quote / place_order / get_order / cancel_order),
para o site, o checkout e a admin usarem os dois do mesmo jeito.

Variáveis de ambiente (Railway → Variables, no catálogo E na admin):
  UBER_CLIENT_ID       direct.uber.com → Developer → Client ID
  UBER_CLIENT_SECRET   direct.uber.com → Developer → Client Secret
  UBER_CUSTOMER_ID     direct.uber.com → Developer → Customer ID
  (usa também MTF_LAT, MTF_LNG, MTF_PHONE e MTF_CEP — os mesmos da Lalamove / Melhor Envio)
  UBER_ATIVO           "0" desliga a Uber no site sem apagar as chaves (padrão: ligado se as 3 chaves existem)

Teste: as chaves de TESTE do painel geram entregas com "robô" (nenhum motoboy de verdade vem).
Depois troque pelas chaves de produção.
"""
import json
import os
import time

import httpx

API = "https://api.uber.com/v1"
TOKEN_URL = "https://auth.uber.com/oauth/v2/token"
STORE_NAME = "MTF Refrigeração"
STORE_STREET = "Avenida Brasil, 6818 - Bonsucesso"

# status da Uber -> mesmos nomes da Lalamove (a admin já sabe mostrar, avisar por push e liberar para chamar de novo)
STATUS_MAP = {
    "pending": "ASSIGNING_DRIVER",
    "pickup": "ON_GOING",
    "pickup_complete": "PICKED_UP",
    "dropoff": "PICKED_UP",
    "delivered": "COMPLETED",
    "canceled": "CANCELED",
    "returned": "CANCELED",
}

_token: dict = {"value": "", "exp": 0.0}


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def ativo() -> bool:
    if _env("UBER_ATIVO", "1") == "0":
        return False
    return bool(_env("UBER_CLIENT_ID") and _env("UBER_CLIENT_SECRET") and _env("UBER_CUSTOMER_ID"))


class UberError(Exception):
    def __init__(self, status: int, payload):
        self.status = status
        self.payload = payload
        super().__init__(f"Uber {status}: {payload}")


async def _get_token() -> str:
    # token vale 30 dias e a Uber limita 100 pedidos de token/hora: guarda em memória
    if _token["value"] and time.time() < _token["exp"] - 3600:
        return _token["value"]
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(TOKEN_URL, data={
            "client_id": _env("UBER_CLIENT_ID"),
            "client_secret": _env("UBER_CLIENT_SECRET"),
            "grant_type": "client_credentials",
            "scope": "eats.deliveries",
        })
    data = r.json() if r.content else {}
    if r.status_code >= 400 or "access_token" not in data:
        raise UberError(r.status_code, data)
    _token["value"] = data["access_token"]
    _token["exp"] = time.time() + int(data.get("expires_in", 2592000))
    return _token["value"]


async def _request(method: str, path: str, payload: dict | None = None) -> dict:
    if not ativo():
        raise UberError(0, "UBER_CLIENT_ID / UBER_CLIENT_SECRET / UBER_CUSTOMER_ID não configurados")
    url = f"{API}/customers/{_env('UBER_CUSTOMER_ID')}{path}"
    for tentativa in (1, 2):
        headers = {"Authorization": f"Bearer {await _get_token()}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.request(method, url, json=payload, headers=headers)
        if r.status_code == 401 and tentativa == 1:      # token revogado/expirado: pega outro e tenta de novo
            _token["value"] = ""
            continue
        break
    data = r.json() if r.content else {}
    if r.status_code >= 400:
        raise UberError(r.status_code, data)
    return data


def _addr(street: str, cidade: str, uf: str, cep: str) -> str:
    cep = "".join(c for c in (cep or "") if c.isdigit())
    return json.dumps({
        "street_address": [street[:200]],
        "city": cidade or "Rio de Janeiro",
        "state": uf or "RJ",
        "zip_code": f"{cep[:5]}-{cep[5:]}" if len(cep) == 8 else cep,
        "country": "BR",
    }, ensure_ascii=False)


def _pickup() -> dict:
    lat, lng = _env("MTF_LAT"), _env("MTF_LNG")
    if not lat or not lng:
        raise UberError(0, "MTF_LAT / MTF_LNG não configurados")
    return {
        "pickup_address": _addr(STORE_STREET, "Rio de Janeiro", "RJ", _env("MTF_CEP")),
        "pickup_latitude": float(lat),
        "pickup_longitude": float(lng),
    }


async def get_quote(lat: float, lng: float, address: str, service: str = "UBER", *,
                    cep: str = "", cidade: str = "", uf: str = "", valor: float = 0) -> dict:
    """Cotação. Vale poucos minutos (campo expires). Preço em reais."""
    dropoff = _addr(address, cidade, uf, cep)
    body = dict(_pickup(), dropoff_address=dropoff,
                dropoff_latitude=float(lat), dropoff_longitude=float(lng))
    if _env("MTF_PHONE"):
        body["pickup_phone_number"] = _env("MTF_PHONE")
    if valor:
        body["manifest_total_value"] = int(round(valor * 100))
    data = await _request("POST", "/delivery_quotes", body)
    return {
        "quotation_id": data["id"],
        "expires_at": data.get("expires"),
        "price": round(int(data["fee"]) / 100, 2),          # Uber devolve em centavos
        "currency": (data.get("currency_type") or data.get("currency") or "BRL").upper(),
        "distance_m": None,                                  # a Uber não informa distância
        "minutos": data.get("duration"),                     # até a entrega, a partir de agora
        "dropoff_eta": data.get("dropoff_eta"),
        "_dropoff": dropoff, "_lat": float(lat), "_lng": float(lng),
    }


async def place_order(quote: dict, client_name: str, client_phone: str, remarks: str = "",
                      sale_id: int | None = None) -> dict:
    """Chama o entregador. client_phone no formato +55XXXXXXXXXXX."""
    body = dict(
        _pickup(),
        quote_id=quote["quotation_id"],
        pickup_name=STORE_NAME,
        pickup_phone_number=_env("MTF_PHONE"),
        pickup_notes=f"Pedido MTF #{int(sale_id) + int(_env('PEDIDO_OFFSET', '1000') or 0)}" if sale_id else "",
        dropoff_name=client_name[:80] or "Cliente",
        dropoff_phone_number=client_phone,
        dropoff_address=quote["_dropoff"],
        dropoff_latitude=quote["_lat"],
        dropoff_longitude=quote["_lng"],
        dropoff_notes=remarks[:280],
        manifest_items=[{"name": "Peças de refrigeração", "quantity": 1, "size": "small"}],
        external_id=f"mtf-{sale_id}" if sale_id else None,
    )
    body = {k: v for k, v in body.items() if v not in (None, "")}
    data = await _request("POST", "/deliveries", body)
    return {"order_id": data["id"], "share_link": data.get("tracking_url"),
            "status": STATUS_MAP.get(data.get("status", ""), "ASSIGNING_DRIVER"),
            "price": round(int(data.get("fee") or 0) / 100, 2)}


async def get_order(order_id: str) -> dict:
    """Formato igual ao da Lalamove: {"status": ..., "shareLink": ...}."""
    data = await _request("GET", f"/deliveries/{order_id}")
    return {"status": STATUS_MAP.get(data.get("status", ""), (data.get("status") or "?").upper()),
            "shareLink": data.get("tracking_url"), "_raw_status": data.get("status")}


async def cancel_order(order_id: str) -> None:
    await _request("POST", f"/deliveries/{order_id}/cancel", {})


def is_uber_id(order_id: str | None) -> bool:
    return str(order_id or "").startswith("del_")
