"""RSS 2.0, RSS 1.0 (RDF), Atom and JSON Feed.

Config file: rss.toml. Each [[source]] needs `url`.
"""

import json

from ._util import child_text, clean_text, http_get, parse_date, parse_xml

DESCRIPTION = "RSS / Atom / JSON Feed"


def fetch(source, ctx):
    body = http_get(source["url"], timeout=ctx["timeout"], user_agent=ctx["user_agent"])
    if body.lstrip()[:1] in (b"{", b"["):
        return _json_feed(json.loads(body.decode("utf-8", errors="replace")))
    return parse(body)


def parse(body):
    root = parse_xml(body)
    if root.tag == "feed":
        return [_atom_entry(e) for e in root.iter("entry")]
    return [_rss_item(i) for i in root.iter("item")]


def _categories(el):
    cats = []
    for c in el.findall("category"):
        v = (c.get("term") or c.text or "").strip()
        if v:
            cats.append(v)
    for c in el.findall("subject"):
        if c.text and c.text.strip():
            cats.append(c.text.strip())
    return cats


def _rss_item(it):
    link = child_text(it, "link")
    if not link:
        guid = it.find("guid")
        if guid is not None and guid.text and guid.get("isPermaLink", "true") != "false":
            link = guid.text.strip()
    if not link:
        link = it.get("about", "")
    return {
        "url": link,
        "title": clean_text(child_text(it, "title"), 300),
        "description": clean_text(child_text(it, "description", "summary", "encoded")),
        "published": parse_date(child_text(it, "pubDate", "date", "published", "updated", "issued")),
        "categories": _categories(it),
    }


def _atom_entry(e):
    link = ""
    for l in e.findall("link"):
        if l.get("rel", "alternate") == "alternate" and l.get("href"):
            link = l.get("href")
            break
    if not link:
        l = e.find("link")
        link = l.get("href", "") if l is not None else ""
    return {
        "url": link,
        "title": clean_text(child_text(e, "title"), 300),
        "description": clean_text(child_text(e, "summary", "content")),
        "published": parse_date(child_text(e, "published", "updated", "issued")),
        "categories": _categories(e),
    }


def _json_feed(data):
    out = []
    for it in data.get("items", []) if isinstance(data, dict) else []:
        out.append({
            "url": it.get("url") or it.get("external_url") or "",
            "title": clean_text(it.get("title", ""), 300),
            "description": clean_text(it.get("summary") or it.get("content_text") or it.get("content_html") or ""),
            "published": parse_date(it.get("date_published") or it.get("date_modified")),
            "categories": list(it.get("tags") or []),
        })
    return out
