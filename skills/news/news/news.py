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
  merge                                               writes pairs of stories that may report the same event
  merge-apply <result.json>                           joins the pairs Claude confirmed as one story
  digest                                              writes the input for the summary of the day, when due
  digest-apply <result.json>                          stores a summary of the day
  render  [--open]                                    writes the HTML overview

Only GET requests to the configured sources are made. Everything stays in the local folder (~/.news,
or NEWS_HOME). Requires only Python 3.11+.
"""

import argparse
import hashlib
import itertools
import json
import math
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
DIGEST_DIR = HOME / "data" / "digests"
DIGEST_DAYS = 2           # the latest days that get a summary of the day
DIGEST_MIN_STORIES = 5    # fewer summarized stories than this: no summary yet
DIGEST_AUTO_STORIES = 8   # stories of the automatic selection
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
    areas = {k: v.get("label", k) + (f" ({v['description']})" if v.get("description") else "")
             for k, v in settings["areas"].items()}
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


# ---------------------------------------------------------------- the same event from several sources

MERGE_DIR = HOME / "merge"
MERGE_HOURS = 48          # stories further apart than this are never the same event
MERGE_THRESHOLD = 0.22    # weighted share of common summary words that makes a candidate pair
MERGE_PAIRS = 120         # candidate pairs per subagent
_MERGE_STOP = set("""ktory ktora ktore ktori ktoreho ktorej ktorych ktorym ktorom podla tento tato toto tieto
jeho jeja ich jej sme som bude budu bola bolo boli mali malo mala maju moze mozu este aj ale alebo pretoze kedze
pocas medzi okrem velmi viac menej tisic tisice milion milionov miliard miliardy prvy prva prve druhy roka roku
rokoch dni den dna tyzdna tyzden mesiac hodin hodiny ludi ludia novy nova nove podla uviedol uviedla tvrdi
povedal povedala oznamil oznamila""".split())


def summary_words(text):
    return {w for w in _WORD.findall(fold(text)) if (len(w) >= 4 or w.isdigit()) and w not in _MERGE_STOP}


def merge_candidates(items, settings):
    """Pairs of summarized stories that may report the same event, by the rare words their Slovak summaries
    share. Only pairs with a story not compared yet. Returns ([(score, id_a, id_b)], ids compared now)."""
    primary_of = stories(items, settings)
    now = now_utc()
    since = now - timedelta(hours=settings["general"]["window_hours"] + MERGE_HOURS)
    prim = [i for i in items.values()
            if primary_of[i["id"]] == i["id"] and i.get("ai") and from_iso(i["published"]) >= since]
    fresh = {i["id"] for i in prim if not i.get("merge_checked")}
    ws = {i["id"]: summary_words(i["ai"]["summary"]) for i in prim}
    df = {}
    for s in ws.values():
        for w in s:
            df[w] = df.get(w, 0) + 1
    n = max(len(prim), 2)
    idf = {w: math.log(n / c) for w, c in df.items()}
    index = {}
    for iid, s in ws.items():
        for w in s:
            if df[w] <= 30:  # words in many stories say nothing about the event
                index.setdefault(w, []).append(iid)
    shared = {}
    for ids in index.values():
        for a, b in itertools.combinations(sorted(ids), 2):
            if a in fresh or b in fresh:
                shared[(a, b)] = shared.get((a, b), 0) + 1
    out = []
    for (a, b), count in shared.items():
        if count < 2:
            continue
        ta, tb = from_iso(items[a]["published"]), from_iso(items[b]["published"])
        if abs((ta - tb).total_seconds()) > MERGE_HOURS * 3600:
            continue
        common = sum(idf[w] for w in ws[a] & ws[b])
        score = common / max(min(sum(idf[w] for w in ws[a]), sum(idf[w] for w in ws[b])), 1e-9)
        if score >= MERGE_THRESHOLD:
            out.append((round(score, 3), a, b))
    out.sort(reverse=True)
    return out, sorted(fresh)


def cmd_merge(args):
    ensure_config()
    settings = load_settings()
    store = load_store()
    items = store["items"]
    pairs, compared = merge_candidates(items, settings)
    if MERGE_DIR.exists():
        shutil.rmtree(MERGE_DIR)
    if not compared:
        print("Nothing to do.")
        return
    MERGE_DIR.mkdir(parents=True)
    chunks = [pairs[i:i + MERGE_PAIRS] for i in range(0, len(pairs), MERGE_PAIRS)] or [[]]
    print(f"Candidate pairs: {len(pairs)}  Stories compared: {len(compared)}  "
          f"Batches: {len(chunks) if pairs else 0}  Model: {settings['claude']['model']}")
    for n, chunk in enumerate(chunks, 1):
        path = MERGE_DIR / f"merge-{n:02d}.json"
        ids = sorted({i for _, a, b in chunk for i in (a, b)})
        data = {
            "result_file": str(path.with_name(f"merge-{n:02d}.result.json")),
            "compared": ids,
            "stories": [{"id": i, "source": items[i]["source_name"],
                         "time": from_iso(items[i]["published"]).astimezone().strftime("%d.%m. %H:%M"),
                         "text": items[i]["ai"]["summary"]} for i in ids],
            "pairs": [[a, b] for _, a, b in chunk],
        }
        head = json.dumps({k: v for k, v in data.items() if k not in ("stories", "pairs")}, ensure_ascii=False)[:-1]
        body = (', "stories": [\n' + ",\n".join(json.dumps(s, ensure_ascii=False) for s in data["stories"])
                + '\n], "pairs": [\n' + ",\n".join(json.dumps(p) for p in data["pairs"]) + "\n]}\n")
        path.write_text(head + body, encoding="utf-8")
        if pairs:
            print(f"Merge: {path}  ({len(chunk)} pairs)")
    # stories without a candidate pair are compared now; the others when their batch is applied
    in_pairs = {i for _, a, b in pairs for i in (a, b)}
    with Lock():
        store = load_store()
        for i in compared:
            if i not in in_pairs and i in store["items"]:
                store["items"][i]["merge_checked"] = True
        save_store(store)
    if not pairs:
        shutil.rmtree(MERGE_DIR)
        print("No candidate pairs.")


def cmd_merge_apply(args):
    ensure_config()
    settings = load_settings()
    path = Path(args.result)
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        batch = json.loads(path.with_name(path.name.replace(".result.json", ".json")).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        die(f"cannot read {path} or its batch: {e}")
    asked = {tuple(sorted(p)) for p in batch["pairs"]}
    same = [tuple(sorted(p)) for p in (result.get("same", []) if isinstance(result, dict) else result)
            if isinstance(p, (list, tuple)) and len(p) == 2]
    merged, skipped = 0, []
    with Lock():
        store = load_store()
        items = store["items"]
        primary_of = stories(items, settings)
        joined = {}  # primary -> the primary it joined in this run

        def root(i):
            r = primary_of[i]
            while r in joined:
                r = joined[r]
            return r

        for a, b in same:
            if (a, b) not in asked or a not in items or b not in items:
                skipped.append(f"{a}/{b}")
                continue
            ra, rb = root(a), root(b)
            if ra == rb:
                continue
            # the story seen later joins the one seen first
            first, later = sorted((ra, rb), key=lambda i: (items[i]["first_seen"], items[i]["published"], i))
            items[later]["dup_of"] = first
            joined[later] = first
            merged += 1
        for i in batch.get("compared", []):
            if i in items:
                items[i]["merge_checked"] = True
        save_store(store)
    print(f"Merged: {merged}  Pairs asked: {len(asked)}  Skipped: {len(skipped)}"
          + (f" ({', '.join(skipped[:10])})" if skipped else ""))


# ---------------------------------------------------------------- summary of the day

def sections(settings):
    """[(id, label, set of areas or None)] in the order of the columns; None takes all other areas."""
    out = [(sid, s.get("label", sid), set(s["areas"]) if s.get("areas") else None)
           for sid, s in settings.get("sections", {}).items()]
    if not any(a is None for _, _, a in out):
        out.insert(0, ("main", "Správy", None))
    return out


def section_of(area, secs):
    for sid, _, areas in secs:
        if areas and area in areas:
            return sid
    return next(sid for sid, _, areas in secs if areas is None)


def digest_name(day, section, secs):
    """File name stem of a summary: the day for the first section, day.section for the others."""
    return day if section == secs[0][0] else f"{day}.{section}"


def load_digest(day, section, secs):
    path = DIGEST_DIR / f"{digest_name(day, section, secs)}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def digest_due(day, section, secs, entries):
    """True when the section of the day has enough summarized stories and no summary, or grew since."""
    done = sum(1 for e in entries if e["ai"])
    if done < DIGEST_MIN_STORIES:
        return False
    old = load_digest(day, section, secs)
    if not old:
        return True
    grown = done - int(old.get("story_count", 0))
    return grown >= max(10, int(old.get("story_count", 0)) // 10)


def cmd_digest(args):
    ensure_config()
    settings = load_settings()
    secs = sections(settings)
    by_day = build_days(settings, load_store())
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    for old in DIGEST_DIR.glob("*.input.json"):
        old.unlink()
    due = [(d, sid, label, [e for e in by_day[d] if e["section"] == sid])
           for d in sorted(by_day, reverse=True)[:DIGEST_DAYS] for sid, label, _ in secs]
    due = [x for x in due if digest_due(x[0], x[1], secs, x[3])]
    if not due:
        print("Nothing to do.")
        return
    print(f"Summaries: {len(due)}  Model: {settings['claude']['model']}")
    areas = {k: v.get("label", k) for k, v in settings["areas"].items()}
    for day, sid, label, section_entries in due:
        entries = [e for e in section_entries if e["ai"]]
        stem = digest_name(day, sid, secs)
        path = DIGEST_DIR / f"{stem}.input.json"
        data = {
            "day": day,
            "section": label,
            "language": "sk",
            "areas": areas,
            "result_file": str(DIGEST_DIR / f"{stem}.result.json"),
            "stories": [{
                "id": e["id"],
                "time": from_iso(e["time"]).astimezone().strftime("%H:%M") if e["dated"] else "",
                "area": e["area"],
                "region": e["region"],
                "sources": [e["source"]] + [o["source"] for o in e["also"]],
                "text": e["text"],
            } for e in entries],
        }
        # one story per line, so the file can be read in parts
        head = json.dumps({k: v for k, v in data.items() if k != "stories"}, ensure_ascii=False)[:-1]
        lines = [json.dumps(s, ensure_ascii=False) for s in data["stories"]]
        path.write_text(head + ', "stories": [\n' + ",\n".join(lines) + "\n]}\n", encoding="utf-8")
        print(f"Digest: {path}  ({day} {label}, {len(entries)} stories, {len(lines) + 2} lines)")


def cmd_digest_apply(args):
    ensure_config()
    settings = load_settings()
    secs = sections(settings)
    path = Path(args.result)
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        die(f"cannot read {path}: {e}")
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?:\.([\w-]+))?\.result\.json", path.name)
    if not m or (m.group(2) and m.group(2) not in [s[0] for s in secs]):
        die(f"{path.name}: the file name must be <day>[.<section>].result.json")
    day, section = m.group(1), m.group(2) or secs[0][0]
    items = load_store()["items"]
    intro = clean_text(result.get("intro", ""), 1200)
    picked, skipped = [], []
    for s in result.get("stories", []):
        sid, text = s.get("id"), clean_text(s.get("text", ""), 400)
        if sid in items and text and sid not in [p["id"] for p in picked]:
            picked.append({"id": sid, "text": text})
        else:
            skipped.append(sid or "?")
    if not intro or not picked:
        die("the result needs an intro and at least one story with a known id"
            + (f" (unknown: {', '.join(skipped[:10])})" if skipped else ""))
    entries = [e for e in build_days(settings, {"items": items, "sources": {}}).get(day, []) if e["section"] == section]
    data = {"day": day, "section": section, "intro": intro, "stories": picked, "at": iso(now_utc()),
            "story_count": sum(1 for e in entries if e["ai"])}
    (DIGEST_DIR / f"{digest_name(day, section, secs)}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Day: {day}  Section: {section}  Stories: {len(picked)}  Skipped: {len(skipped)}"
          + (f" ({', '.join(skipped[:10])})" if skipped else ""))


def day_digest(day, section, secs, entries, primary_of):
    """The summary of one section of a day: Claude's when there is one, else the stories most sources report."""
    by_id = {e["id"]: e for e in entries}
    d = load_digest(day, section, secs)
    if d:
        stories_out = []
        for s in d["stories"]:
            e = by_id.get(primary_of.get(s["id"], s["id"]))
            if e and e["id"] not in [x["id"] for x in stories_out]:
                stories_out.append({**e, "digest_text": s["text"]})
        if stories_out:
            done = sum(1 for e in entries if e["ai"])
            return {"kind": "claude", "intro": d["intro"], "at": d["at"], "stories": stories_out,
                    "newer": max(0, done - int(d.get("story_count", 0)))}
    ranked = sorted(entries, key=lambda e: (len(e["also"]), e["dated"], e["time"]), reverse=True)
    return {"kind": "auto", "intro": "", "stories": ranked[:DIGEST_AUTO_STORIES]} if entries else None


# ---------------------------------------------------------------- render

def build_days(settings, store):
    """{local day: [story entries]} of the whole archive."""
    items = store["items"]
    primary_of = stories(items, settings)
    secs = sections(settings)
    groups = {}
    for iid, pid in primary_of.items():
        groups.setdefault(pid, []).append(items[iid])

    by_day = {}
    for pid, members in groups.items():
        first = items[pid]
        summarized = [m for m in members if m.get("ai")]
        # shown: the member with the fullest summary (the first one seen when none has one)
        p = max(summarized, key=lambda m: (len(m["ai"]["summary"]), m["id"] == pid)) if summarized else first
        ai = p.get("ai")
        local = from_iso(first["published"]).astimezone()
        entry = {
            "id": pid,
            "time": iso(from_iso(first["published"])),
            "dated": first.get("dated", True),
            "ai": bool(ai),
            "text": ai["summary"] if ai else script_text(p),
            "title": p["title"],
            "url": p["url"],
            "source": p["source_name"],
            "group": p["group"],
            "lang": p["lang"],
            "area": ai["area"] if ai else script_area(p, settings),
            "section": "",
            "region": (ai["region"] if ai and ai.get("region") else p["region"]),
            "also": [{"source": m["source_name"], "url": m["url"], "title": m["title"]}
                     for m in sorted(members, key=lambda m: m["published"]) if m["id"] != p["id"]],
        }
        entry["section"] = section_of(entry["area"], secs)
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
    store = load_store()
    by_day = build_days(settings, store)
    primary_of = stories(store["items"], settings)
    secs = sections(settings)
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
        entries = by_day.get(day, [])
        data = {"day": day, "days": day_list, "areas": areas, "generated": generated,
                "mode": settings["general"]["summary_mode"],
                "items": entries,
                "sections": [{"id": sid, "label": label, "areas": sorted(a) if a else None,
                              "tab": settings.get("sections", {}).get(sid, {}).get("tab") or label,
                              "digest": day_digest(day, sid, secs, [e for e in entries if e["section"] == sid],
                                                   primary_of) if day else None}
                             for sid, label, a in secs]}
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
    sub.add_parser("merge").set_defaults(fn=cmd_merge)
    p = sub.add_parser("merge-apply")
    p.add_argument("result")
    p.set_defaults(fn=cmd_merge_apply)
    sub.add_parser("digest").set_defaults(fn=cmd_digest)
    p = sub.add_parser("digest-apply")
    p.add_argument("result")
    p.set_defaults(fn=cmd_digest_apply)
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
