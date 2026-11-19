"""Article pages listed in a plain sitemap, for sites whose feed or homepage shows only a selection.

Config file: pages.toml. Each [[source]] needs:
  url           a plain sitemap (<urlset>) or a sitemap index (<sitemapindex>) of the site
  link_pattern  regular expression an article URL must match
Optional:
  max_sitemaps  how many files of a sitemap index to read, newest first (default 1)
  max_pages     most article pages downloaded in one run (default 40)

The sitemap gives the complete list of articles and their time. Only articles within the time window
that are not in the archive yet are downloaded, and from each page only its head is used: the title,
the description (perex) and the publication time from the <meta> tags. The text of the article is not read.
"""

import html as htmllib
import re
from concurrent.futures import ThreadPoolExecutor

from ._util import child_text, clean_text, http_get, parse_date, parse_xml

DESCRIPTION = "Sitemap + article page"

_META = re.compile(r"<meta\b[^>]*>", re.I)
_ATTR = re.compile(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.S)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_TIME = re.compile(r"<time\b[^>]*\bdatetime\s*=\s*[\"']([^\"']+)", re.I)
_LD_DATE = re.compile(r"\"datePublished\"\s*:\s*\"([^\"]+)\"")
_PARALLEL = 4


def fetch(source, ctx):
    pattern = re.compile(source["link_pattern"])
    inc = re.compile(source["include"]) if source.get("include") else None
    exc = re.compile(source["exclude"]) if source.get("exclude") else None
    window_start = ctx.get("window_start")
    known = ctx.get("known") or (lambda url: None)

    entries = {}
    for url, lastmod in _sitemap_urls(source, ctx):
        if not pattern.search(url) or inc and not inc.search(url) or exc and exc.search(url):
            continue
        if window_start and lastmod and lastmod < window_start:
            continue
        entries.setdefault(url, lastmod)

    out, todo = [], []
    for url, lastmod in entries.items():
        old = known(url)
        if old:
            out.append(old)
        else:
            todo.append((url, lastmod))
    todo.sort(key=lambda e: e[1].timestamp() if e[1] else 0, reverse=True)
    todo = todo[: int(source.get("max_pages", 40))]

    def page(entry):
        url, lastmod = entry
        try:
            body = http_get(url, timeout=ctx["timeout"], user_agent=ctx["user_agent"])
        except Exception:
            return None  # one missing article must not fail the source
        return _from_page(url, body.decode("utf-8", errors="replace"), lastmod)

    with ThreadPoolExecutor(max_workers=_PARALLEL) as pool:
        out.extend(it for it in pool.map(page, todo) if it)
    return out


def _sitemap_urls(source, ctx):
    root = parse_xml(http_get(source["url"], timeout=ctx["timeout"], user_agent=ctx["user_agent"]))
    if root.tag == "sitemapindex":
        maps = [(child_text(s, "loc"), parse_date(child_text(s, "lastmod"))) for s in root.findall("sitemap")]
        maps = [m for m in maps if m[0]]
        maps.sort(key=lambda m: m[1].timestamp() if m[1] else 0, reverse=True)  # stable: equal dates keep order
        for loc, _ in maps[: int(source.get("max_sitemaps", 1))]:
            sub = parse_xml(http_get(loc, timeout=ctx["timeout"], user_agent=ctx["user_agent"]))
            yield from _urls(sub)
    else:
        yield from _urls(root)


def _urls(root):
    for u in root.findall("url"):
        loc = child_text(u, "loc")
        if loc:
            yield loc.strip(), parse_date(child_text(u, "lastmod"))


def _metas(page):
    """{name or property (lowercase): content} of the <meta> tags; the first of each name wins."""
    out = {}
    for tag in _META.findall(page):
        attrs = {k.lower(): (a if a else b) for k, a, b in _ATTR.findall(tag)}
        key = (attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or "").lower()
        if key and "content" in attrs and key not in out:
            out[key] = htmllib.unescape(attrs["content"])
    return out


def _from_page(url, page, lastmod):
    meta = _metas(page)
    title = meta.get("og:title") or meta.get("twitter:title")
    if not title:
        m = _TITLE.search(page)
        title = m.group(1) if m else ""
    title = clean_text(title, 300)
    site = meta.get("og:site_name")
    if site:  # "Headline | ta3" -> "Headline"
        title = re.sub(r"\s*[|–—-]\s*" + re.escape(site) + r"\s*$", "", title, flags=re.I)
    if not title:
        return None
    published = None
    for value in (meta.get("article:published_time"), meta.get("datepublished"),
                  *_LD_DATE.findall(page)[:1], *_TIME.findall(page)[:1]):
        published = parse_date(value)
        if published:
            break
    return {
        "url": url,
        "title": title,
        "description": clean_text(meta.get("og:description") or meta.get("description") or "", 600),
        "published": published or lastmod,
        "categories": [meta["article:section"]] if meta.get("article:section") else [],
    }
