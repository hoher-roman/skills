#!/usr/bin/env python3
"""/news: a short overview of current news from configured sources.

Commands:
  config  [--set-mode claude|script] [--set-model M]   paths and settings; creates the config on first run
  sources                                             every source with its technology and last result
  probe   <url>                                       checks an address and suggests a source entry
  fetch   [--only id,id]                              downloads all enabled sources into the archive
  pending                                             writes batches of items waiting for a Claude summary
  apply   <result.json>                               stores summaries written for a batch
  list    [--day YYYY-MM-DD] [--summarized]           stories of a day (default the latest) as text
  render  [--open]                                    writes the HTML overview

Only GET requests to the configured sources are made. Everything stays in the local folder (~/.news,
or NEWS_HOME). Requires only Python 3.11+.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
import tomllib
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

SKILL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SKILL_DIR))

from fetchers import technologies  # noqa: E402
from fetchers._util import DEFAULT_UA, clean_text, http_get  # noqa: E402

HOME = Path(os.environ.get("NEWS_HOME") or Path.home() / ".news")
CONFIG_DIR = HOME / "config"
DATA_FILE = HOME / "data" / "items.json"
BATCH_DIR = HOME / "batches"
LOCK_FILE = HOME / "data" / ".lock"

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def from_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


# ---------------------------------------------------------------- config

def ensure_config():
    """Copy default config files that are missing. Returns True when the config folder was new."""
    first = not CONFIG_DIR.exists()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    for f in sorted((SKILL_DIR / "defaults").glob("*.toml")):
        target = CONFIG_DIR / f.name
        if not target.exists():
            shutil.copyfile(f, target)
    return first


def load_toml(path):
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as e:
        die(f"{path}: {e}")


def load_settings():
    s = load_toml(CONFIG_DIR / "settings.toml")
    g = s.setdefault("general", {})
    g.setdefault("summary_mode", "claude")
    g.setdefault("window_hours", 36)
    g.setdefault("retention_days", 30)
    g.setdefault("max_items_per_source", 40)
    g.setdefault("duplicate_similarity", 0.6)
    g.setdefault("timeout", 20)
    g.setdefault("site_dir", "")
    c = s.setdefault("claude", {})
    c.setdefault("model", "sonnet")
    c.setdefault("batch_size", 40)
    c.setdefault("parallel", 3)
    areas = s.setdefault("areas", {})
    areas.setdefault("ine", {"label": "Ostatné", "color": "#7b8088", "keywords": []})
    return s


def site_dir(settings):
    d = settings["general"].get("site_dir") or ""
    return Path(os.path.expanduser(d)) if d else HOME / "site"


def load_sources():
    """All sources of all technologies: list of dicts with `tech` added. Checks required fields."""
    techs = technologies()
    out, ids = [], set()
    for name in techs:
        path = CONFIG_DIR / f"{name}.toml"
        if not path.exists():
            continue
        for src in load_toml(path).get("source", []):
            src = dict(src)
            src["tech"] = name
            missing = [k for k in ("id", "name", "url") if not src.get(k)]
            if name in ("html", "pages") and not src.get("link_pattern"):
                missing.append("link_pattern")
            if missing:
                die(f"{path.name}: source {src.get('id') or src.get('url') or '?'} is missing {', '.join(missing)}")
            if src["id"] in ids:
                die(f"{path.name}: source id '{src['id']}' is used twice")
            ids.add(src["id"])
            src.setdefault("lang", "")
            src.setdefault("region", "")
            src.setdefault("group", "")
            src.setdefault("enabled", True)
            out.append(src)
    return out


def set_setting(key, value):
    path = CONFIG_DIR / "settings.toml"
    text = path.read_text(encoding="utf-8")
    new, n = re.subn(rf'(?m)^(\s*{key}\s*=\s*)"[^"]*"', rf'\g<1>"{value}"', text, count=1)
    if not n:
        die(f"{key} not found in {path}")
    path.write_text(new, encoding="utf-8")


def cmd_config(args):
    first = ensure_config()
    if args.set_mode:
        if args.set_mode not in ("claude", "script"):
            die("mode must be claude or script")
        set_setting("summary_mode", args.set_mode)
    if args.set_model:
        set_setting("model", args.set_model)
    s = load_settings()
    sources = load_sources()
    print(f"first_run: {'true' if first else 'false'}")
    print(f"config_dir: {CONFIG_DIR}")
    print(f"site: {site_dir(s) / 'index.html'}")
    print(f"summary_mode: {s['general']['summary_mode']}")
    print(f"model: {s['claude']['model']}  batch_size: {s['claude']['batch_size']}  parallel: {s['claude']['parallel']}")
    print(f"window_hours: {s['general']['window_hours']}  retention_days: {s['general']['retention_days']}")
    for tech, mod in technologies().items():
        mine = [x for x in sources if x["tech"] == tech]
        on = sum(1 for x in mine if x["enabled"])
        print(f"{tech}.toml ({mod.DESCRIPTION}): {on} enabled, {len(mine) - on} disabled")


# ---------------------------------------------------------------- store

class Lock:
    def __enter__(self):
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + 60
        while True:
            try:
                self.fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                try:
                    if time.time() - LOCK_FILE.stat().st_mtime > 120:
                        LOCK_FILE.unlink()
                        continue
                except FileNotFoundError:
                    continue
                if time.time() > deadline:
                    die(f"the archive is locked by another run ({LOCK_FILE})")
                time.sleep(0.2)

    def __exit__(self, *exc):
        os.close(self.fd)
        try:
            LOCK_FILE.unlink()
        except FileNotFoundError:
            pass


def load_store():
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return {"items": {}, "sources": {}}


def save_store(store):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, DATA_FILE)


_TRACKING = re.compile(r"^(utm_|itm_|maca$|ref$|ns_|fbclid$|gclid$|at_|cmpid$|xtor$)", re.I)


def normalize_url(url):
    p = urlsplit(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not _TRACKING.match(k)])
    path = p.path.rstrip("/") or "/"
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, query, ""))


def item_id(url):
    return hashlib.sha1(normalize_url(url).encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- fetch

def fetch_source(src, ctx, settings):
    mod = technologies()[src["tech"]]
    raw = mod.fetch(src, ctx)
    inc = re.compile(src["include"]) if src.get("include") else None
    exc = re.compile(src["exclude"]) if src.get("exclude") else None
    window_start = now_utc() - timedelta(hours=settings["general"]["window_hours"])
    out = []
    for it in raw:
        url = (it.get("url") or "").strip()
        if not url or not it.get("title"):
            continue
        url = urljoin(src["url"], url)
        if inc and not inc.search(url) or exc and exc.search(url):
            continue
        if it["published"] and it["published"] < window_start:
            continue
        it["url"] = url
        out.append(it)
    out.sort(key=lambda i: i["published"] or now_utc(), reverse=True)
    return out[: int(src.get("max_items", settings["general"]["max_items_per_source"]))]


def cmd_fetch(args):
    ensure_config()
    settings = load_settings()
    sources = [s for s in load_sources() if s["enabled"]]
    if args.only:
        wanted = set(args.only.split(","))
        unknown = wanted - {s["id"] for s in load_sources()}
        if unknown:
            die(f"unknown source: {', '.join(sorted(unknown))}")
        sources = [s for s in load_sources() if s["id"] in wanted]
    known_items = load_store()["items"]

    def known(url):
        """The archived item of a URL as a raw item, so fetchers can skip downloading it again.
        Items without a date count as unknown, so a fetcher that can date them gets the chance."""
        it = known_items.get(item_id(url))
        if not it or not it.get("dated", True):
            return None
        return {"url": it["url"], "title": it["title"], "description": it["description"],
                "published": from_iso(it["published"]), "categories": it["categories"]}

    ctx = {"timeout": settings["general"]["timeout"], "user_agent": settings["general"].get("user_agent", DEFAULT_UA),
           "window_start": now_utc() - timedelta(hours=settings["general"]["window_hours"]), "known": known}

    def run(src):
        try:
            return src, fetch_source(src, ctx, settings), None
        except Exception as e:  # one broken source must not stop the rest
            return src, [], f"{type(e).__name__}: {e}"[:200]

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(run, sources))

    now = now_utc()
    total_new = 0
    lines = []
    with Lock():
        store = load_store()
        items = store["items"]
        for src, got, err in results:
            st = store["sources"].setdefault(src["id"], {})
            st["last_fetch"] = iso(now)
            new = 0
            if err:
                st["last_error"] = err
            else:
                st.pop("last_error", None)
                st["last_ok"] = iso(now)
                st["last_count"] = len(got)
                for it in got:
                    iid = item_id(it["url"])
                    old = items.get(iid)
                    if old:
                        if not old.get("dated", True) and it["published"]:  # a date found for an undated item
                            old["published"] = iso(it["published"])
                            old["dated"] = True
                            old["description"] = old["description"] or it["description"]
                        continue
                    items[iid] = {
                        "id": iid,
                        "url": it["url"],
                        "title": it["title"],
                        "description": it["description"],
                        "categories": it["categories"][:10],
                        "source": src["id"],
                        "source_name": src["name"],
                        "tech": src["tech"],
                        "lang": src["lang"],
                        "region": src["region"],
                        "group": src["group"],
                        "default_area": src.get("area", ""),
                        "published": iso(it["published"] or now),
                        "dated": it["published"] is not None,
                        "first_seen": iso(now),
                        "ai": None,
                    }
                    new += 1
            total_new += new
            lines.append((src["id"], src["tech"], new, len(got), err))
        cutoff = now - timedelta(days=settings["general"]["retention_days"])
        pruned = [k for k, v in items.items() if from_iso(v["published"]) < cutoff]
        for k in pruned:
            del items[k]
        save_store(store)

    w = max((len(l[0]) for l in lines), default=6)
    print(f"{'source'.ljust(w)}  tech     new / in window")
    for sid, tech, new, got, err in lines:
        print(f"{sid.ljust(w)}  {tech.ljust(7)}  " + (f"ERROR {err}" if err else f"{new:>3} / {got}"))
    errors = sum(1 for l in lines if l[4])
    print(f"Sources: {len(lines)} ({errors} with errors)  New items: {total_new}  Removed (older than "
          f"{settings['general']['retention_days']} days): {len(pruned)}")
    if settings["general"]["summary_mode"] == "claude":
        print(f"Waiting for summary: {len(pending_items(store, settings))}")


# ---------------------------------------------------------------- stories (duplicates)

_WORD = re.compile(r"\w+", re.U)


def fold(text):
    """Lowercase without diacritics."""
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))


def title_words(title):
    return {w for w in _WORD.findall(fold(title)) if len(w) >= 3 and not w.isdigit()}


def stories(items, settings):
    """{item id: primary item id}. Items of one story: marked by Claude (dup_of), the same headline words
    in the same language within 48 hours. The first one seen is the primary."""
    threshold = settings["general"]["duplicate_similarity"]
    ordered = sorted(items.values(), key=lambda i: (i["first_seen"], i["published"]))
    primary_of, primaries = {}, []
    for it in ordered:
        d = it.get("dup_of")
        if d and d in items and d != it["id"]:
            continue
        words = title_words(it["title"])
        t = from_iso(it["published"])
        found = None
        if len(words) >= 3:
            for p, pw, pt, plang in primaries:
                if plang == it["lang"] and abs((t - pt).total_seconds()) < 48 * 3600:
                    inter = len(words & pw)
                    if inter and inter / len(words | pw) >= threshold:
                        found = p
                        break
        if found:
            primary_of[it["id"]] = found
        else:
            primary_of[it["id"]] = it["id"]
            primaries.append((it["id"], words, t, it["lang"]))
    for it in ordered:  # Claude's marks, resolved to the root
        d = it.get("dup_of")
        if d and d in items and d != it["id"]:
            root, seen = d, {it["id"]}
            while items.get(root, {}).get("dup_of") in items and root not in seen:
                seen.add(root)
                root = items[root]["dup_of"]
            primary_of[it["id"]] = primary_of.get(root, root)
    return primary_of


# ---------------------------------------------------------------- script mode: text and area

_SENT = re.compile(r"(?<=[.!?…])\s+(?=[\"„“'(]?[A-ZÁÄČĎÉĚÍĹĽŇÓÔŘŔŠŤÚŮÝŽ0-9])")


def script_text(item):
    """First one or two sentences of the perex, at most about 240 characters."""
    desc = item.get("description") or ""
    title = item["title"].rstrip(" .")
    if fold(desc).startswith(fold(title)):  # perex that repeats the headline
        desc = desc[len(title):].lstrip(" .:-–—|")
    if len(desc) < 15:
        return ""
    sents = _SENT.split(desc)
    text = sents[0]
    if len(text) < 90 and len(sents) > 1:
        text += " " + sents[1]
    return clean_text(text, 240)


def script_area(item, settings):
    """Area by keywords of settings.toml in URL path segments, categories and headline."""
    areas = settings["areas"]
    path_words = [w for w in re.split(r"[/_.\-]+", fold(urlsplit(item["url"]).path)) if w][:3]
    cat_words = [w for c in item.get("categories", []) for w in _WORD.findall(fold(c))]
    head_words = _WORD.findall(fold(item["title"]))
    best, best_score = None, 0
    for aid, a in areas.items():
        kws = [fold(k) for k in a.get("keywords", [])]
        if not kws:
            continue
        score = 0
        for weight, words in ((3, path_words), (2, cat_words), (1, head_words)):
            for w in words:
                if any(w.startswith(k) for k in kws if len(k) >= 4) or w in kws:
                    score += weight
        if score > best_score:
            best, best_score = aid, score
    if best and best_score >= 2:
        return best
    if item.get("default_area") in areas:
        return item["default_area"]
    return best or "ine"


# ---------------------------------------------------------------- claude mode: batches

def pending_items(store, settings):
    items = store["items"]
    primary_of = stories(items, settings)
    since = now_utc() - timedelta(hours=settings["general"]["window_hours"])
    out = [i for i in items.values()
           if primary_of[i["id"]] == i["id"] and not i.get("ai") and from_iso(i["first_seen"]) >= since]
    out.sort(key=lambda i: i["published"], reverse=True)  # newest first, so the latest day is done first
    return out


def cmd_pending(args):
    ensure_config()
    settings = load_settings()
    store = load_store()
    todo = pending_items(store, settings)
    if BATCH_DIR.exists():
        shutil.rmtree(BATCH_DIR)
    if not todo:
        print("Nothing to summarize.")
        return
    BATCH_DIR.mkdir(parents=True)
    size = max(1, int(settings["claude"]["batch_size"]))
    areas = {k: v.get("label", k) for k, v in settings["areas"].items()}
    batches = [todo[i:i + size] for i in range(0, len(todo), size)]
    print(f"Items: {len(todo)}  Batches: {len(batches)}  Model: {settings['claude']['model']}  "
          f"Parallel: {settings['claude']['parallel']}")
    for n, chunk in enumerate(batches, 1):
        path = BATCH_DIR / f"batch-{n:02d}.json"
        data = {
            "language": "sk",
            "areas": areas,
            "result_file": str(path.with_name(f"batch-{n:02d}.result.json")),
            "items": [{
                "id": i["id"],
                "source": i["source_name"],
                "lang": i["lang"],
                "region_hint": i["region"],
                "title": i["title"],
                "perex": i["description"],
                "categories": i["categories"],
                "url": i["url"],
            } for i in chunk],
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"Batch: {path}  ({len(chunk)} items)")


def cmd_apply(args):
    ensure_config()
    settings = load_settings()
    path = Path(args.result)
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        die(f"cannot read {path}: {e}")
    if isinstance(result, dict):
        result = result.get("items", [])
    applied, skipped, merged = 0, [], 0
    with Lock():
        store = load_store()
        items = store["items"]
        for r in result:
            iid = r.get("id")
            summary = clean_text(r.get("summary", ""), 400)
            if iid not in items or not summary:
                skipped.append(iid or "?")
                continue
            area = r.get("area") if r.get("area") in settings["areas"] else "ine"
            items[iid]["ai"] = {"summary": summary, "area": area, "region": clean_text(r.get("region", ""), 60),
                                "at": iso(now_utc())}
            same = r.get("same_as")
            if same and same in items and same != iid:
                items[iid]["dup_of"] = same
                merged += 1
            applied += 1
        save_store(store)
        left = len(pending_items(store, settings))
    print(f"Applied: {applied}  Merged as the same story: {merged}  Skipped: {len(skipped)}"
          + (f" ({', '.join(skipped[:10])})" if skipped else ""))
    print(f"Still waiting for summary: {left}")


# ---------------------------------------------------------------- render

def build_days(settings, store):
    """{local day: [story entries]} of the whole archive."""
    items = store["items"]
    primary_of = stories(items, settings)
    groups = {}
    for iid, pid in primary_of.items():
        groups.setdefault(pid, []).append(items[iid])

    by_day = {}
    for pid, members in groups.items():
        p = items[pid]
        ai = p.get("ai") or next((m["ai"] for m in members if m.get("ai")), None)
        local = from_iso(p["published"]).astimezone()
        entry = {
            "id": pid,
            "time": iso(from_iso(p["published"])),
            "dated": p.get("dated", True),
            "ai": bool(ai),
            "text": ai["summary"] if ai else script_text(p),
            "title": p["title"],
            "url": p["url"],
            "source": p["source_name"],
            "group": p["group"],
            "lang": p["lang"],
            "area": ai["area"] if ai else script_area(p, settings),
            "region": (ai["region"] if ai and ai.get("region") else p["region"]),
            "also": [{"source": m["source_name"], "url": m["url"], "title": m["title"]}
                     for m in sorted(members, key=lambda m: m["published"]) if m["id"] != pid],
        }
        by_day.setdefault(local.strftime("%Y-%m-%d"), []).append(entry)
    for entries in by_day.values():
        entries.sort(key=lambda e: (e["dated"], e["time"]), reverse=True)
    return by_day


def cmd_list(args):
    ensure_config()
    settings = load_settings()
    by_day = build_days(settings, load_store())
    if not by_day:
        print("No news in the archive.")
        return
    day = args.day or max(by_day)
    entries = by_day.get(day, [])
    if args.summarized:
        entries = [e for e in entries if e["ai"]]
    print(f"Day: {day}  Stories: {len(entries)}")
    print("area | region | source | other sources | text | url")
    for e in entries:
        print(" | ".join([e["area"], e["region"], e["source"], str(len(e["also"])),
                          e["text"] if e["ai"] else e["title"], e["url"]]))


def cmd_render(args):
    ensure_config()
    settings = load_settings()
    by_day = build_days(settings, load_store())
    out = site_dir(settings)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("????-??-??.html"):
        if old.stem not in by_day:
            old.unlink()
    days = sorted(by_day, reverse=True)
    day_list = [{"day": d, "count": len(by_day[d])} for d in days]
    areas = {k: {"label": v.get("label", k), "color": v.get("color", "#7b8088")} for k, v in settings["areas"].items()}
    template = (SKILL_DIR / "template.html").read_text(encoding="utf-8")
    generated = iso(now_utc())

    def page(day):
        data = {"day": day, "days": day_list, "areas": areas, "generated": generated,
                "mode": settings["general"]["summary_mode"],
                "items": by_day.get(day, [])}
        payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
        return template.replace("/*__DATA__*/null", payload)

    for d in days:
        (out / f"{d}.html").write_text(page(d), encoding="utf-8")
    index = out / "index.html"
    index.write_text(page(days[0] if days else None), encoding="utf-8")
    ai_count = sum(1 for d in days for e in by_day[d] if e["ai"])
    total = sum(len(by_day[d]) for d in days)
    today = days[0] if days else "-"
    print(f"Days: {len(days)}  Stories: {total} (with Claude summary: {ai_count})  Latest day: {today} "
          f"({len(by_day.get(today, []))} stories)")
    print(f"Page: {index.as_uri()}")
    if args.open:
        if sys.platform == "win32":
            os.startfile(index)  # noqa: S606 - opens the local file in the default browser
        else:
            import webbrowser
            webbrowser.open(index.as_uri())


# ---------------------------------------------------------------- sources, probe

def cmd_sources(args):
    ensure_config()
    store = load_store()
    counts = {}
    for it in store["items"].values():
        counts[it["source"]] = counts.get(it["source"], 0) + 1
    for s in load_sources():
        st = store["sources"].get(s["id"], {})
        state = "off" if not s["enabled"] else ("ERROR " + st["last_error"] if st.get("last_error")
                                                 else (f"ok {st.get('last_ok', '')}" if st else "never fetched"))
        print(f"{s['id']}  [{s['tech']}]  {s['name']}  group={s['group']}  lang={s['lang']}  "
              f"archived={counts.get(s['id'], 0)}  {state}\n    {s['url']}")


_ALT = re.compile(r"<link\b[^>]*>", re.I)


def cmd_probe(args):
    ensure_config()
    from fetchers import rss, sitemap
    url = args.url
    try:
        body = http_get(url)
    except Exception as e:
        die(f"{url}: {type(e).__name__}: {e}")
    head = body.lstrip()[:500].lower()

    def show(tech, u, items):
        print(f"Works as {tech}: {u}  ({len(items)} items)")
        for it in items[:5]:
            print(f"  - {it['published'] and iso(it['published']) or 'no date'}  {it['title'][:110]}")
        print(f'  perex: {"yes" if any(i["description"] for i in items) else "no"}')

    for tech, mod in (("rss", rss), ("sitemap", sitemap)):
        try:
            items = mod.fetch({"url": url}, {"timeout": 20, "user_agent": DEFAULT_UA})
            if items:
                show(tech, url, items)
                return
        except Exception:
            pass
    if b"<urlset" in head or b"<sitemapindex" in head:
        plain_sitemap(url)
        return
    if b"<html" in head or b"<!doctype html" in head:
        page = body.decode("utf-8", errors="replace")
        feeds = []
        for tag in _ALT.findall(page):
            if re.search(r"rel=[\"']?alternate", tag, re.I) and re.search(r"(rss|atom)\+xml|feed\+json", tag, re.I):
                m = re.search(r"href=[\"']([^\"']+)", tag, re.I)
                if m:
                    feeds.append(urljoin(url, m.group(1).replace("&amp;", "&")))
        for f in dict.fromkeys(feeds):
            try:
                items = rss.fetch({"url": f}, {"timeout": 20, "user_agent": DEFAULT_UA})
                if items:
                    show("rss", f, items)
                    return
            except Exception:
                continue
        try:
            p = urlsplit(url)
            robots = http_get(f"{p.scheme}://{p.netloc}/robots.txt").decode("utf-8", errors="replace")
            all_maps = [m.strip() for m in re.findall(r"(?im)^sitemap:\s*(\S+)", robots)]
        except Exception:
            all_maps = []
        maps = [m for m in all_maps if "news" in m.lower()]
        for m in maps:
            try:
                items = sitemap.fetch({"url": m}, {"timeout": 20, "user_agent": DEFAULT_UA})
                if items:
                    show("sitemap", m, items)
                    return
            except Exception:
                continue
        print("No RSS, Atom, JSON Feed or news sitemap found.")
        for m in all_maps[:1]:
            plain_sitemap(m)
        print("Or use html.toml with a link_pattern for the article URLs of this page (only what the page shows).")
        return
    die("the address is neither a feed, a sitemap nor an HTML page")


def plain_sitemap(url):
    """Show what a plain sitemap holds, for a pages.toml source."""
    from fetchers.pages import _sitemap_urls
    try:
        urls = list(_sitemap_urls({"url": url, "max_sitemaps": 1}, {"timeout": 30, "user_agent": DEFAULT_UA}))
    except Exception as e:
        print(f"Plain sitemap {url}: {type(e).__name__}: {e}")
        return
    urls.sort(key=lambda u: u[1].timestamp() if u[1] else 0, reverse=True)
    print(f"Plain sitemap (use pages.toml with a link_pattern for the article URLs): {url}  ({len(urls)} URLs in the newest file)")
    for u, t in urls[:8]:
        print(f"  - {t and iso(t) or 'no date'}  {u}")


def main():
    ap = argparse.ArgumentParser(prog="news.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("config")
    p.add_argument("--set-mode")
    p.add_argument("--set-model")
    p.set_defaults(fn=cmd_config)
    sub.add_parser("sources").set_defaults(fn=cmd_sources)
    p = sub.add_parser("probe")
    p.add_argument("url")
    p.set_defaults(fn=cmd_probe)
    p = sub.add_parser("fetch")
    p.add_argument("--only")
    p.set_defaults(fn=cmd_fetch)
    sub.add_parser("pending").set_defaults(fn=cmd_pending)
    p = sub.add_parser("apply")
    p.add_argument("result")
    p.set_defaults(fn=cmd_apply)
    p = sub.add_parser("list")
    p.add_argument("--day")
    p.add_argument("--summarized", action="store_true")
    p.set_defaults(fn=cmd_list)
    p = sub.add_parser("render")
    p.add_argument("--open", action="store_true")
    p.set_defaults(fn=cmd_render)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
