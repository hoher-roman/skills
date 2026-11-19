"""Article links from a plain HTML page, the last resort for sites without RSS or a news sitemap.

Config file: html.toml. Each [[source]] needs:
  url           page to read (usually the homepage or a section)
  link_pattern  regular expression an absolute article URL must match
Optional:
  min_title_length  shortest link text taken as a headline (default 25)

Pages carry no dates, so items get the time they were first seen.
"""

import html as htmllib
import re
from urllib.parse import urljoin

from ._util import clean_text, http_get

DESCRIPTION = "HTML page (links by pattern)"

_A = re.compile(r"<a\b[^>]*?href\s*=\s*[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", re.S | re.I)


def fetch(source, ctx):
    body = http_get(source["url"], timeout=ctx["timeout"], user_agent=ctx["user_agent"])
    page = body.decode("utf-8", errors="replace")
    pattern = re.compile(source["link_pattern"])
    min_len = int(source.get("min_title_length", 25))
    seen, out = set(), []
    for href, inner in _A.findall(page):
        url = urljoin(source["url"], htmllib.unescape(href.strip()))
        if url in seen or not pattern.search(url):
            continue
        title = clean_text(inner, 300)
        if len(title) < min_len:
            continue
        seen.add(url)
        out.append({"url": url, "title": title, "description": "", "published": None, "categories": []})
    return out
