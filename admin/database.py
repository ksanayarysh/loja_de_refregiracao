from sqlalchemy import text
from dependencies import engine


async def init_db():
    async with engine.begin() as conn:
        await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS products (
            id BIGSERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT,
            unit TEXT NOT NULL DEFAULT 'un',
            sale_price NUMERIC(12,2) NOT NULL DEFAULT 0,
            min_stock INTEGER NOT NULL DEFAULT 0,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """))
        await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS sales (
            id BIGSERIAL PRIMARY KEY,
            sold_at DATE NOT NULL DEFAULT CURRENT_DATE,
            product_id BIGINT NOT NULL REFERENCES products(id),
            qty NUMERIC(12,3) NOT NULL CHECK (qty > 0),
            unit_price NUMERIC(12,2) NOT NULL CHECK (unit_price >= 0),
            total NUMERIC(12,2) NOT NULL CHECK (total >= 0),
            note TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_sales_sold_at ON sales(sold_at)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_sales_product_id ON sales(product_id)"))
        await conn.execute(text("""
            ALTER TABLE sales ADD COLUMN IF NOT EXISTS payment_type TEXT NOT NULL DEFAULT 'dinheiro'
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS categories (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE
            )
        """))
        await conn.execute(text("""
            INSERT INTO categories (name) VALUES ('Outro') ON CONFLICT DO NOTHING
        """))
        await conn.execute(text("""
            ALTER TABLE products ADD COLUMN IF NOT EXISTS category_id INTEGER REFERENCES categories(id)
        """))

        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS stock_movements (
                id           SERIAL PRIMARY KEY,
                product_id   INTEGER NOT NULL REFERENCES products(id),
                qty          NUMERIC(12,3) NOT NULL,
                movement_type TEXT NOT NULL DEFAULT 'entrada',
                note         TEXT,
                sale_id      INTEGER REFERENCES sales(id),
                moved_at     DATE NOT NULL DEFAULT CURRENT_DATE,
                created_at   TIMESTAMPTZ DEFAULT NOW()
            )
        """))

        await conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS cost_price NUMERIC(12,2) DEFAULT 0"))
        await conn.execute(text("ALTER TABLE stock_movements ADD COLUMN IF NOT EXISTS unit_cost NUMERIC(12,2)"))

        # vendas fora dos relatórios (ex.: compras com cartão roubado de 19–20/10/2026):
        # continuam na lista de vendas e no estoque, mas os relatórios leem a view sales_rel
        await conn.execute(text("ALTER TABLE sales ADD COLUMN IF NOT EXISTS fora_relatorio BOOLEAN NOT NULL DEFAULT FALSE"))
        await conn.execute(text("CREATE TABLE IF NOT EXISTS migracoes (nome TEXT PRIMARY KEY, feita_em TIMESTAMPTZ DEFAULT NOW())"))
        feita = (await conn.execute(text(
            "INSERT INTO migracoes (nome) VALUES ('fraude_cartao_2026_10') ON CONFLICT DO NOTHING RETURNING nome"
        ))).first()
        if feita:   # só uma vez: depois a marcação é manual (editar venda)
            await conn.execute(text("""
                UPDATE sales SET fora_relatorio = TRUE
                WHERE payment_type = 'cartao_credito' AND sold_at::date IN ('2026-10-19', '2026-10-20')
            """))
        await conn.execute(text("DROP VIEW IF EXISTS sales_rel"))
        await conn.execute(text("CREATE VIEW sales_rel AS SELECT * FROM sales WHERE fora_relatorio = FALSE"))
