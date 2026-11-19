"""Google News sitemaps (<urlset> with <news:news>), for sites without RSS.

Config file: sitemap.toml. Each [[source]] needs `url` of the news sitemap.
A sitemap index (<sitemapindex>) is followed one level down, at most `max_sitemaps` files (default 3).
Sitemaps carry only titles, so items have no description.
"""

from ._util import child_text, clean_text, http_get, parse_date, parse_xml

DESCRIPTION = "Google News sitemap"


def fetch(source, ctx):
    root = parse_xml(http_get(source["url"], timeout=ctx["timeout"], user_agent=ctx["user_agent"]))
    if root.tag == "sitemapindex":
        items = []
        locs = [child_text(s, "loc") for s in root.findall("sitemap")][: int(source.get("max_sitemaps", 3))]
        for loc in locs:
            if loc:
                sub = parse_xml(http_get(loc, timeout=ctx["timeout"], user_agent=ctx["user_agent"]))
                items.extend(_urls(sub))
        return items
    return _urls(root)


def _urls(root):
    out = []
    for u in root.findall("url"):
        news = u.find("news")
        title = child_text(news, "title") if news is not None else ""
        if not title:
            continue
        keywords = child_text(news, "keywords")
        out.append({
            "url": child_text(u, "loc"),
            "title": clean_text(title, 300),
            "description": "",
            "published": parse_date(child_text(news, "publication_date") or child_text(u, "lastmod")),
            "categories": [k.strip() for k in keywords.split(",") if k.strip()] if keywords else [],
        })
    return out
