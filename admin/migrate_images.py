"""
Миграция: base64 картинки → файлы в /static/images/
Запускать ОДИН РАЗ после подключения Railway Volume.

cd /app && python migrate_images.py
"""
import asyncio
import base64
import os
import uuid

from sqlalchemy import text
from dependencies import engine

STATIC_DIR = os.environ.get("STATIC_DIR", os.path.join(os.path.dirname(__file__), "static", "images"))
os.makedirs(STATIC_DIR, exist_ok=True)


async def migrate():
    async with engine.begin() as conn:
        # Все товары с base64 картинками
        rows = await conn.execute(text("""
            SELECT id, image FROM products
            WHERE image IS NOT NULL AND image LIKE 'data:image%'
        """))
        rows = rows.mappings().all()

    print(f"Найдено {len(rows)} товаров с base64 картинками")

    updated = 0
    errors  = 0

    for row in rows:
        try:
            # Извлекаем данные из data:image/jpeg;base64,....
            header, b64data = row["image"].split(",", 1)
            img_bytes = base64.b64decode(b64data)

            fname = f"{uuid.uuid4().hex}.jpg"
            fpath = os.path.join(STATIC_DIR, fname)

            with open(fpath, "wb") as f:
                f.write(img_bytes)

            url = f"/static/images/{fname}"

            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE products SET image = :url WHERE id = :id"),
                    {"url": url, "id": row["id"]}
                )

            updated += 1
            print(f"  ✅ id={row['id']} → {url}")

        except Exception as e:
            errors += 1
            print(f"  ❌ id={row['id']} ошибка: {e}")

    print(f"\nГотово: {updated} обновлено, {errors} ошибок")


if __name__ == "__main__":
    asyncio.run(migrate())
