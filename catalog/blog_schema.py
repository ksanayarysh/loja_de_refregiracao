"""Helpers do blog: contexto de produtos e JSON-LD Product para artigos.

Os produtos vêm de `_get_products_cached` (router.py): dicts com
id, name, sale_price, unit, image, description, category_name, current_stock.
O slug NÃO vem do banco — é `slugify(name)`, por isso build_article_context
recebe a função slugify do router.

Campos opcionais em articles.py:
    "products":       ["gas-r410a", "gas-r22"]  # só para conferência: slug que sumir vai para o log
                                                # (o template pode usar qualquer produto ativo)
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


def build_article_context(article, all_products, site_url, slugify, aliases=None):
    """Retorna {"products": {slug: produto}, "schema_product": dict|None, "missing": [...]}.

    `products` traz TODOS os produtos ativos (o template pega só os que cita),
    indexados pelo slug atual e também pelos slugs antigos (`aliases`: {slug_antigo: id}),
    para o artigo não perder o produto quando ele é renomeado na admin.
    Cada produto ganha: slug (atual), in_stock, regular_price e sale_price = preço EFETIVO (promoção).
    """
    site_url = site_url.rstrip("/")
    wanted = set(article.get("products", []))
    main_slug = article.get("schema_product")
    if main_slug:
        wanted.add(main_slug)

    by_slug, by_id = {}, {}
    for p in all_products or []:
        sl = slugify(p.get("name", ""))
        if not sl or sl in by_slug:
            continue
        q = dict(p)
        q["slug"] = sl
        q["in_stock"] = float(p.get("current_stock") or 0) > 0
        # Promoção: sale_price passa a ser o preço EFETIVO (templates e JSON-LD já usam esse campo);
        # o preço normal fica em regular_price (para mostrar riscado) e on_promo indica a promoção.
        q["regular_price"] = p.get("sale_price")
        if p.get("on_promo") and p.get("price"):
            q["sale_price"] = p["price"]
        by_slug[sl] = q
        if p.get("id") is not None:
            by_id[p["id"]] = q

    for old, pid in (aliases or {}).items():
        if old not in by_slug and pid in by_id:
            by_slug[old] = by_id[pid]

    schema_product = None
    if main_slug and main_slug in by_slug:
        sp = by_slug[main_slug]
        schema_product = product_jsonld(sp, sp["slug"], site_url)

    return {"products": by_slug, "schema_product": schema_product,
            "missing": sorted(wanted - set(by_slug))}
