"""Custo ESTIMADO para os produtos sem preço de custo (comprados há tanto tempo que ninguém sabe).

Rodar na raiz do projeto Loja, com o .venv:
    python script/estimar_custos.py

Como estima: para cada categoria, olha os produtos que TÊM custo real e calcula custo/preço.
Usa um valor conservador (o 75º percentil: custo um pouco ALTO -> margem um pouco BAIXA),
para o site não dar desconto de frete além do que a loja aguenta.
Categoria com menos de 3 produtos com custo -> usa o valor de todas as categorias juntas.

- Mostra a lista e pede confirmação (s/n) antes de gravar.
- Marca cost_estimado = TRUE: na admin aparece a etiqueta "estimado" ao lado do custo.
  Quando você digitar o custo real e salvar, a etiqueta some sozinha.
- Nunca mexe em produto que já tem custo.
"""
import os
import re
import sys
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

import psycopg2

PERCENTIL = 0.75
MIN_AMOSTRA = 3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
url = None
with open(os.path.join(ROOT, "catalog", ".env"), encoding="utf-8") as f:
    for line in f:
        if line.startswith("DATABASE_URL="):
            url = line.split("=", 1)[1].strip().strip('"').strip("'")
if not url:
    sys.exit("DATABASE_URL não encontrado em catalog/.env")
conn = psycopg2.connect(re.sub(r"^postgresql\+asyncpg://", "postgresql://", url), connect_timeout=20)
cur = conn.cursor()
cur.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS cost_estimado BOOLEAN DEFAULT FALSE")
conn.commit()

cur.execute("""
    SELECT p.id, p.name, COALESCE(c.name, '(sem categoria)'), p.sale_price, p.cost_price, COALESCE(p.cost_estimado, FALSE)
    FROM products p LEFT JOIN categories c ON c.id = p.category_id
    WHERE p.active = TRUE AND COALESCE(p.sale_price, 0) > 0""")
rows = cur.fetchall()


def pct(v, q):
    v = sorted(v)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


razoes = defaultdict(list)
todas = []
for pid, nome, cat, preco, custo, est in rows:
    if custo and float(custo) > 0 and not est and float(custo) < float(preco) * 1.5:   # ignora custo absurdo
        r = float(custo) / float(preco)
        razoes[cat].append(r)
        todas.append(r)
if not todas:
    sys.exit("Nenhum produto com custo real para servir de base.")
geral = pct(todas, PERCENTIL)

print(f"\nBase: {len(todas)} produtos com custo real · custo/preço geral (75%): {geral:.2f}\n")
print(f"{'categoria':<28}{'com custo':>10}{'custo/preço':>13}")
fator = {}
for cat in sorted({r[2] for r in rows}):
    v = razoes.get(cat, [])
    fator[cat] = pct(v, PERCENTIL) if len(v) >= MIN_AMOSTRA else geral
    print(f"{cat[:27]:<28}{len(v):>10}{fator[cat]:>13.2f}" + ("" if len(v) >= MIN_AMOSTRA else "  (usa geral)"))

novos = []
for pid, nome, cat, preco, custo, est in rows:
    if custo and float(custo) > 0:
        continue
    estimado = (Decimal(str(preco)) * Decimal(str(fator[cat]))).quantize(Decimal("0.01"), ROUND_HALF_UP)
    novos.append((pid, nome, cat, preco, estimado))

if not novos:
    sys.exit("\nTodos os produtos já têm custo. Nada a fazer.")
print(f"\n{len(novos)} produtos sem custo — custo estimado:\n")
for pid, nome, cat, preco, est in novos[:400]:
    print(f"  #{pid:<5} {nome[:52]:<52} [{cat[:18]}]  preço R$ {float(preco):>8.2f}  →  custo R$ {float(est):>8.2f}")

if input(f"\nGravar o custo estimado nesses {len(novos)} produtos? (s/n) ").strip().lower() != "s":
    sys.exit("Cancelado, nada foi gravado.")
try:
    for pid, _, _, _, est in novos:
        cur.execute("UPDATE products SET cost_price = %s, cost_estimado = TRUE "
                    "WHERE id = %s AND COALESCE(cost_price, 0) = 0", (est, pid))
    conn.commit()
    print(f"\n✅ {len(novos)} produtos com custo estimado (etiqueta 'estimado' na admin).")
except Exception as e:
    conn.rollback()
    sys.exit(f"Erro, nada foi gravado: {e}")
finally:
    conn.close()
