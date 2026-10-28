from database import engine
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="templates")


def so_balcao(p) -> bool:
    """Produto que NÃO se compra pelo site, só com o vendedor:
    gás no varejo (kg) e botijas/cilindros grandes (palavra "botija" no nome)."""
    if p is None:
        return False
    get = p.get if isinstance(p, dict) else (lambda k, d=None: getattr(p, k, d))
    return (str(get("unit") or "").strip().lower() == "kg"
            or "botija" in str(get("name") or "").lower())


templates.env.globals["so_balcao"] = so_balcao
