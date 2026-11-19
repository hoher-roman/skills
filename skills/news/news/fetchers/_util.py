"""Shared helpers for fetchers: HTTP GET, XML parsing, dates, text cleanup."""

import gzip
import html
import re
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) news-skill/1.0"


def http_get(url, timeout=20, user_agent=DEFAULT_UA):
    """GET a URL and return the body as bytes. Nothing is ever sent but the request itself."""
    req = urllib.request.Request(url, headers={
        "User-Agent": user_agent,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, application/json, text/html;q=0.9, */*;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        enc = (resp.headers.get("Content-Encoding") or "").lower()
    if enc == "gzip":
        body = gzip.decompress(body)
    elif enc == "deflate":
        body = zlib.decompress(body)
    return body


_ENTITY = re.compile(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)([A-Za-z][A-Za-z0-9]*);?")


def _fix_entities(text):
    def repl(m):
        name = m.group(1)
        cp = html.entities.name2codepoint.get(name)
        return f"&#{cp};" if cp else "&amp;" + m.group(0)[1:]
    return _ENTITY.sub(repl, text)


def parse_xml(body):
    """Parse XML bytes, tolerating HTML entities and stray ampersands. Namespaces are stripped from tags."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        text = body.decode("utf-8", errors="replace")
        text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)
        root = ET.fromstring(_fix_entities(text))
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
        for k in list(el.attrib):
            if "}" in k:
                el.attrib[k.split("}", 1)[1]] = el.attrib.pop(k)
    return root


_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def clean_text(value, limit=600):
    """Strip HTML tags and entities, collapse whitespace, cut to `limit` characters."""
    if not value:
        return ""
    text = html.unescape(_TAG.sub(" ", value))
    text = html.unescape(text)  # double-escaped feeds
    text = _WS.sub(" ", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def parse_date(value):
    """Parse RFC 822 or ISO 8601 dates into an aware UTC datetime, or None."""
    if not value:
        return None
    value = value.strip()
    dt = None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            m = re.match(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?", value)
            if m:
                try:
                    dt = datetime.fromisoformat(m.group(0))
                except ValueError:
                    dt = None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def child_text(el, *names):
    """Text of the first non-empty direct child among `names`."""
    for name in names:
        for c in el.findall(name):
            if c.text and c.text.strip():
                return c.text.strip()
    return ""
