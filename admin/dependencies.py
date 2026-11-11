import os
import base64
from fastapi import Request, HTTPException
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
DATABASE_URL = DATABASE_URL.replace("postgresql+psycopg2://", "postgresql+asyncpg://")
DATABASE_URL = DATABASE_URL.replace("postgresql+psycopg://", "postgresql+asyncpg://")
DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")
DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://")

print("DB URL (masked):", DATABASE_URL.split("@")[-1])

engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

templates = Jinja2Templates(directory="templates")


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def brl(v) -> str:
    """1234.5 -> 'R$ 1.234,50' (formato brasileiro)"""
    s = f"{_num(v):,.2f}"
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


def qtd(v, unit: str = "") -> str:
    """Quantidade: unidade sem casas ('1', '12'); kg/m com até 3 casas e vírgula ('0,51', '1,250')."""
    x = _num(v)
    if abs(x - round(x)) < 1e-9:
        return str(int(round(x)))
    s = f"{x:.3f}".rstrip("0").rstrip(".")
    return s.replace(".", ",")


templates.env.filters["brl"] = brl
templates.env.filters["qtd"] = qtd

ADMIN_USER = os.environ.get("ADMIN_USER", "")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "change-me")


def basic_auth(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        raise HTTPException(
            status_code=401,
            detail="Auth required",
            headers={"WWW-Authenticate": "Basic"},
        )
    try:
        userpass = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
        user, pwd = userpass.split(":", 1)
    except Exception:
        raise HTTPException(
            status_code=401,
            detail="Bad auth",
            headers={"WWW-Authenticate": "Basic"},
        )
    if user != ADMIN_USER or pwd != ADMIN_PASS:
        raise HTTPException(
            status_code=401,
            detail="Invalid auth",
            headers={"WWW-Authenticate": "Basic"},
        )
    return True
