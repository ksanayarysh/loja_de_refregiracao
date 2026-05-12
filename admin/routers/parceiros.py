import os
import base64
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse, FileResponse

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


@router.get("/fagal")
async def fagal_calculator(_=Depends(fagal_auth)):
    return FileResponse("app/templates/parceiros_fagal.html", media_type="text/html")
