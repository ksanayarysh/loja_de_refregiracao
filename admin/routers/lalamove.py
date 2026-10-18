"""
Клиент Lalamove API v3 для MTF Refrigeração.

Переменные окружения (Railway → Variables):
  LALAMOVE_API_KEY      pk_test_... (sandbox) / pk_prod_... (production)
  LALAMOVE_API_SECRET   sk_test_... / sk_prod_...
  LALAMOVE_BASE         https://rest.sandbox.lalamove.com  (по умолчанию)
                        https://rest.lalamove.com          (production)
  MTF_LAT, MTF_LNG      координаты магазина (взять из Google Maps)
  MTF_PHONE             телефон магазина в формате +5521XXXXXXXXX
"""
import hashlib
import hmac
import json
import os
import time
import uuid

import httpx

MARKET = "BR"
LANG = "pt_BR"
STORE_ADDRESS = "Avenida Brasil, 6818, Rio de Janeiro - RJ"
STORE_NAME = "MTF Refrigeração"


def _env(name: str, default: str = "") -> str:
    # Читаем при каждом вызове, а не при импорте — не зависит от того, когда вызван load_dotenv
    return os.getenv(name, default).strip()


class LalamoveError(Exception):
    def __init__(self, status: int, payload):
        self.status = status
        self.payload = payload
        super().__init__(f"Lalamove {status}: {payload}")


async def _request(method: str, path: str, payload: dict | None = None) -> dict:
    key, secret = _env("LALAMOVE_API_KEY"), _env("LALAMOVE_API_SECRET")
    base = _env("LALAMOVE_BASE", "https://rest.sandbox.lalamove.com")
    if not key or not secret:
        raise LalamoveError(0, "LALAMOVE_API_KEY / LALAMOVE_API_SECRET não configurados")
    # Тело подписывается и отправляется ОДНОЙ И ТОЙ ЖЕ строкой,
    # поэтому используем content=, а не json= (иначе httpx пересериализует и подпись не сойдётся)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) if payload is not None else ""
    ts = str(int(time.time() * 1000))
    raw = f"{ts}\r\n{method}\r\n{path}\r\n\r\n{body}"
    signature = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()

    headers = {
        "Authorization": f"hmac {key}:{ts}:{signature}",
        "Market": MARKET,
        "Request-ID": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.request(method, base + path, content=body.encode() if body else None, headers=headers)

    data = r.json() if r.content else {}
    if r.status_code >= 400:
        raise LalamoveError(r.status_code, data)
    return data


async def get_cities() -> dict:
    """Список городов и доступных serviceType/specialRequests. Вызвать один раз, посмотреть что есть в Рио."""
    return await _request("GET", "/v3/cities")


async def get_quote(lat: float, lng: float, address: str, service_type: str = "LALAGO") -> dict:
    """Расчёт стоимости. Котировка живёт 5 минут."""
    store_lat, store_lng = _env("MTF_LAT"), _env("MTF_LNG")
    if not store_lat or not store_lng:
        raise LalamoveError(0, "MTF_LAT / MTF_LNG não configurados")
    payload = {
        "data": {
            "serviceType": service_type,
            "language": LANG,
            "stops": [
                {"coordinates": {"lat": store_lat, "lng": store_lng}, "address": STORE_ADDRESS},
                {"coordinates": {"lat": str(lat), "lng": str(lng)}, "address": address},
            ],
        }
    }
    data = (await _request("POST", "/v3/quotations", payload))["data"]
    return {
        "quotation_id": data["quotationId"],
        "expires_at": data["expiresAt"],
        "price": data["priceBreakdown"]["total"],
        "currency": data["priceBreakdown"]["currency"],
        "distance_m": data.get("distance", {}).get("value"),
        "stops": data["stops"],  # нужны stopId для place_order
    }


async def place_order(quote: dict, client_name: str, client_phone: str, remarks: str = "", sale_id: int | None = None) -> dict:
    """Вызывает курьера. client_phone в формате +55XXXXXXXXXXX."""
    stops = quote["stops"]
    payload = {
        "data": {
            "quotationId": quote["quotation_id"],
            "sender": {"stopId": stops[0]["stopId"], "name": STORE_NAME, "phone": _env("MTF_PHONE")},
            "recipients": [
                {"stopId": stops[1]["stopId"], "name": client_name, "phone": client_phone, "remarks": remarks}
            ],
            "isPODEnabled": True,
            "metadata": {"sale_id": str(sale_id)} if sale_id else {},
        }
    }
    data = (await _request("POST", "/v3/orders", payload))["data"]
    return {"order_id": data["orderId"], "share_link": data.get("shareLink"), "status": data.get("status")}


async def get_order(order_id: str) -> dict:
    return (await _request("GET", f"/v3/orders/{order_id}"))["data"]


async def cancel_order(order_id: str) -> None:
    """Отмена возможна только в ASSIGNING_DRIVER или < 5 минут после назначения водителя."""
    await _request("DELETE", f"/v3/orders/{order_id}")
