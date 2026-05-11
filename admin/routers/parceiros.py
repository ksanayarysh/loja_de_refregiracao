import os
import base64
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse
from dependencies import templates

router = APIRouter(prefix="/parceiros")

FAGAL_USER = os.environ.get("FAGAL_USER", "fagal")
FAGAL_PASS = os.environ.get("FAGAL_PASS", "change-me")


def fagal_auth(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        raise HTTPException(
            status_code=401,
            detail="Auth required",
            headers={"WWW-Authenticate": 'Basic realm="FAGAL"'},
        )
    try:
        userpass = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
        user, pwd = userpass.split(":", 1)
    except Exception:
        raise HTTPException(
            status_code=401,
            detail="Bad auth",
            headers={"WWW-Authenticate": 'Basic realm="FAGAL"'},
        )
    if user != FAGAL_USER or pwd != FAGAL_PASS:
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": 'Basic realm="FAGAL"'},
        )
    return True


@router.get("/fagal", response_class=HTMLResponse)
async def fagal_calculator(request: Request, _=Depends(fagal_auth)):
    return templates.TemplateResponse("parceiros_fagal.html", {"request": request})
