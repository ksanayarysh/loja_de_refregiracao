"""Helpers do blog: contexto de produtos e JSON-LD Product para artigos.

Os produtos vêm de `_get_products_cached` (router.py): dicts com
id, name, sale_price, unit, image, description, category_name, current_stock.
O slug NÃO vem do banco — é `slugify(name)`, por isso build_article_context
recebe a função slugify do router.

Campos opcionais em articles.py:
    "products":       ["gas-r410a", "gas-r22"]  # slugs cujo preço aparece na página
    "schema_product": "capacitor-xyz"           # UM slug -> JSON-LD Product
                                                # (só para artigos dedicados a um produto)
"""


def product_jsonld(product, slug, site_url):
    """Monta o JSON-LD Product. Preço com PONTO decimal (formato exigido pelo
    schema), independente do formato brasileiro mostrado na página."""
    price = product.get("sale_price")
    if not price:
        return None

    url = f"{site_url}/product/{slug}"
    in_stock = float(product.get("current_stock") or 0) > 0

    data = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": product.get("name"),
        "url": url,
        "offers": {
            "@type": "Offer",
            "url": url,
            "price": f"{float(price):.2f}",
            "priceCurrency": "BRL",
            "availability": "https://schema.org/InStock" if in_stock
                            else "https://schema.org/OutOfStock",
            "seller": {"@type": "Organization", "name": "M.T.F Refrigeração"},
        },
    }

    # _get_products já prefixa ADMIN_URL em /static/images/...; /feed.xml usa o mesmo valor
    image = product.get("image")
    if image:
        data["image"] = image if str(image).startswith("http") else f"{site_url}{image}"
    description = (product.get("description") or "").strip()
    if description:
        data["description"] = description[:5000]
    if product.get("id") is not None:
        data["sku"] = str(product["id"])
    return data


def build_article_context(article, all_products, site_url, slugify):
    """Retorna {"products": {slug: produto}, "schema_product": dict|None}."""
    site_url = site_url.rstrip("/")
    wanted = set(article.get("products", []))
    main_slug = article.get("schema_product")
    if main_slug:
        wanted.add(main_slug)

    by_slug = {}
    for p in all_products or []:
        sl = slugify(p.get("name", ""))
        if sl in wanted and sl not in by_slug:
            by_slug[sl] = p

    schema_product = None
    if main_slug and main_slug in by_slug:
        schema_product = product_jsonld(by_slug[main_slug], main_slug, site_url)

    return {"products": by_slug, "schema_product": schema_product}
