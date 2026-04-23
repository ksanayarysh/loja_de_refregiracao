import os
import json
import asyncio
import httpx
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()

BRT         = ZoneInfo("America/Sao_Paulo")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID   = os.environ.get("TG_CHAT_ID", "")
CRON_SECRET  = os.environ.get("CRON_SECRET", "")
SITE_URL     = "https://www.mtfrefrigeracao.com.br/"

# JSON-ключ сервисного аккаунта — кладём в env как строку JSON
# ENV var: GSC_SERVICE_ACCOUNT_JSON
GSC_SA_JSON  = os.environ.get("GSC_SERVICE_ACCOUNT_JSON", "")


async def _send_tg(message: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "HTML"}
            )
    except Exception:
        pass


async def _get_gsc_token() -> str:
    """Получает OAuth2 токен через JWT сервисного аккаунта."""
    import time, base64, json
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    sa = json.loads(GSC_SA_JSON)
    private_key_pem = sa["private_key"]
    client_email    = sa["client_email"]

    now = int(time.time())
    header  = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "iss":   client_email,
        "scope": "https://www.googleapis.com/auth/webmasters.readonly",
        "aud":   "https://oauth2.googleapis.com/token",
        "iat":   now,
        "exp":   now + 3600,
    }

    def b64(data):
        return base64.urlsafe_b64encode(
            json.dumps(data, separators=(",", ":")).encode()
        ).rstrip(b"=").decode()

    signing_input = f"{b64(header)}.{b64(payload)}".encode()

    private_key = serialization.load_pem_private_key(
        private_key_pem.encode(), password=None
    )
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    jwt_token = f"{signing_input.decode()}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion":  jwt_token,
            }
        )
        return resp.json()["access_token"]


async def _query_gsc(token: str, start: str, end: str, dimensions: list, row_limit: int = 10) -> list:
    """Запрашивает данные из Search Console API."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"https://searchconsole.googleapis.com/webmasters/v3/sites/{SITE_URL}/searchAnalytics/query",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "startDate":   start,
                "endDate":     end,
                "dimensions":  dimensions,
                "rowLimit":    row_limit,
                "dataState":   "final",
            }
        )
        data = resp.json()
        return data.get("rows", [])


async def _get_totals(token: str, start: str, end: str) -> dict:
    """Получает суммарные метрики за период."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"https://searchconsole.googleapis.com/webmasters/v3/sites/{SITE_URL}/searchAnalytics/query",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "startDate": start,
                "endDate":   end,
                "rowLimit":  1,
                "dataState": "final",
            }
        )
        data = resp.json()
        rows = data.get("rows", [])
        # API не даёт totals напрямую — суммируем все строки
        # Делаем запрос без dimensions чтобы получить агрегат
        resp2 = await client.post(
            f"https://searchconsole.googleapis.com/webmasters/v3/sites/{SITE_URL}/searchAnalytics/query",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "startDate": start,
                "endDate":   end,
                "rowLimit":  1,
                "dataState": "final",
                "dimensions": ["date"],
                "aggregationType": "byPage",
            }
        )
        # Проще — запросим без dimensions
        resp3 = await client.post(
            f"https://searchconsole.googleapis.com/webmasters/v3/sites/{SITE_URL}/searchAnalytics/query",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "startDate": start,
                "endDate":   end,
                "rowLimit":  25000,
                "dataState": "final",
            }
        )
        d = resp3.json()
        rows = d.get("rows", [])
        if not rows:
            return {"clicks": 0, "impressions": 0, "ctr": 0.0, "position": 0.0}
        total_clicks = sum(r.get("clicks", 0) for r in rows)
        total_impr   = sum(r.get("impressions", 0) for r in rows)
        avg_ctr      = sum(r.get("ctr", 0) for r in rows) / len(rows)
        avg_pos      = sum(r.get("position", 0) for r in rows) / len(rows)
        return {
            "clicks":      total_clicks,
            "impressions": total_impr,
            "ctr":         avg_ctr * 100,
            "position":    avg_pos,
        }


def _arrow(current, previous):
    if previous == 0:
        return ""
    diff = current - previous
    pct  = diff / previous * 100
    if pct > 5:   return f" <b>▲ +{pct:.0f}%</b>"
    if pct < -5:  return f" <b>▼ {pct:.0f}%</b>"
    return f" ({pct:+.0f}%)"


# ── ЕЖЕДНЕВНЫЙ ОТЧЁТ ─────────────────────────────────────────────────────────

async def build_daily_report() -> str:
    if not GSC_SA_JSON:
        return "⚠️ GSC_SERVICE_ACCOUNT_JSON não configurado"

    today     = date.today()
    yesterday = today - timedelta(days=1)
    prev_day  = today - timedelta(days=2)

    yd_str = yesterday.isoformat()
    pd_str = prev_day.isoformat()

    token   = await _get_gsc_token()
    totals  = await _get_totals(token, yd_str, yd_str)
    prev    = await _get_totals(token, pd_str, pd_str)

    # Топ-5 запросов за вчера
    queries = await _query_gsc(token, yd_str, yd_str, ["query"], row_limit=5)
    # Топ-5 страниц за вчера
    pages   = await _query_gsc(token, yd_str, yd_str, ["page"], row_limit=5)

    lines = [
        f"📊 <b>GSC — {yesterday.strftime('%d/%m/%Y')}</b>\n",
        f"🖱 Cliques: <b>{totals['clicks']}</b>{_arrow(totals['clicks'], prev['clicks'])}",
        f"👁 Impressões: <b>{totals['impressions']}</b>{_arrow(totals['impressions'], prev['impressions'])}",
        f"📈 CTR: <b>{totals['ctr']:.1f}%</b>",
        f"🏆 Posição média: <b>{totals['position']:.1f}</b>",
    ]

    if queries:
        lines.append("\n🔍 <b>Top consultas:</b>")
        for i, r in enumerate(queries, 1):
            q = r["keys"][0]
            c = r.get("clicks", 0)
            p = r.get("position", 0)
            lines.append(f"  {i}. {q} — {c} cliques · pos {p:.1f}")

    if pages:
        lines.append("\n📄 <b>Top páginas:</b>")
        for i, r in enumerate(pages, 1):
            url = r["keys"][0].replace("https://www.mtfrefrigeracao.com.br", "")
            c   = r.get("clicks", 0)
            lines.append(f"  {i}. {url} — {c} cliques")

    return "\n".join(lines)


# ── ЕЖЕНЕДЕЛЬНЫЙ ОТЧЁТ ───────────────────────────────────────────────────────

async def build_weekly_report() -> str:
    if not GSC_SA_JSON:
        return "⚠️ GSC_SERVICE_ACCOUNT_JSON não configurado"

    today      = date.today()
    # прошлая неделя пн-вс
    last_mon   = today - timedelta(days=today.weekday() + 7)
    last_sun   = last_mon + timedelta(days=6)
    # позапрошлая неделя для сравнения
    prev_mon   = last_mon - timedelta(days=7)
    prev_sun   = last_mon - timedelta(days=1)

    token  = await _get_gsc_token()
    totals = await _get_totals(token, last_mon.isoformat(), last_sun.isoformat())
    prev   = await _get_totals(token, prev_mon.isoformat(), prev_sun.isoformat())

    queries = await _query_gsc(token, last_mon.isoformat(), last_sun.isoformat(), ["query"], row_limit=10)
    pages   = await _query_gsc(token, last_mon.isoformat(), last_sun.isoformat(), ["page"],  row_limit=7)

    week_label = f"{last_mon.strftime('%d/%m')} – {last_sun.strftime('%d/%m')}"

    lines = [
        f"📊 <b>GSC semanal — {week_label}</b>\n",
        f"🖱 Cliques: <b>{totals['clicks']}</b>{_arrow(totals['clicks'], prev['clicks'])}",
        f"👁 Impressões: <b>{totals['impressions']}</b>{_arrow(totals['impressions'], prev['impressions'])}",
        f"📈 CTR médio: <b>{totals['ctr']:.1f}%</b>",
        f"🏆 Posição média: <b>{totals['position']:.1f}</b>",
        f"\n<i>vs semana anterior: {prev['clicks']} cliques · {prev['impressions']} impressões</i>",
    ]

    if queries:
        lines.append("\n🔍 <b>Top 10 consultas:</b>")
        for i, r in enumerate(queries, 1):
            q   = r["keys"][0]
            c   = r.get("clicks", 0)
            imp = r.get("impressions", 0)
            p   = r.get("position", 0)
            lines.append(f"  {i}. {q} — {c} cliques · {imp} imp · pos {p:.1f}")

    if pages:
        lines.append("\n📄 <b>Top páginas:</b>")
        for i, r in enumerate(pages, 1):
            url = r["keys"][0].replace("https://www.mtfrefrigeracao.com.br", "")
            c   = r.get("clicks", 0)
            imp = r.get("impressions", 0)
            lines.append(f"  {i}. {url} — {c} cliques · {imp} imp")

    return "\n".join(lines)


# ── ENDPOINTS (вызываются Railway Cron или вручную) ───────────────────────────

@router.post("/api/cron/gsc-daily")
async def gsc_daily(request: Request):
    secret = request.headers.get("x-cron-secret", "")
    if CRON_SECRET and secret != CRON_SECRET:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        msg = await build_daily_report()
        await _send_tg(msg)
        return JSONResponse({"ok": True})
    except Exception as e:
        await _send_tg(f"⚠️ Erro no relatório GSC diário: {e}")
        return JSONResponse({"ok": False, "error": str(e)})


@router.post("/api/cron/gsc-weekly")
async def gsc_weekly(request: Request):
    secret = request.headers.get("x-cron-secret", "")
    if CRON_SECRET and secret != CRON_SECRET:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        msg = await build_weekly_report()
        await _send_tg(msg)
        return JSONResponse({"ok": True})
    except Exception as e:
        await _send_tg(f"⚠️ Erro no relatório GSC semanal: {e}")
        return JSONResponse({"ok": False, "error": str(e)})


# ── ФОНОВЫЙ ПЛАНИРОВЩИК (запускается при старте приложения) ───────────────────

async def _scheduler():
    """Запускает отчёты по расписанию без Railway Cron."""
    from datetime import datetime
    while True:
        now = datetime.now(BRT)
        # Ежедневно в 08:00
        if now.hour == 8 and now.minute == 0:
            try:
                msg = await build_daily_report()
                await _send_tg(msg)
            except Exception as e:
                await _send_tg(f"⚠️ Erro GSC diário: {e}")
        # Еженедельно в понедельник в 08:05
        if now.weekday() == 0 and now.hour == 8 and now.minute == 5:
            try:
                msg = await build_weekly_report()
                await _send_tg(msg)
            except Exception as e:
                await _send_tg(f"⚠️ Erro GSC semanal: {e}")
        await asyncio.sleep(60)  # проверяем каждую минуту


def start_gsc_scheduler():
    asyncio.create_task(_scheduler())
