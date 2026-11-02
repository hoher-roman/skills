"""Download YouTube metadata, descriptions and transcripts into a local archive (see SPEC.md).

Usage:
  python fetch.py config [--set-output-dir PATH]
  python fetch.py survey <url> [--playlist] [--types video,short,stream,podcast]
                         [--since YYYY-MM-DD] [--refresh] [--out-dir PATH]
      prints the overview and saves the exact list as a plan file
  python fetch.py download --plan PLAN [--progress-every N]
      downloads exactly the plan (run the same command again to continue after a stop)
  python fetch.py download <url> [same filters as survey]   (no overview, for scripts)
  python fetch.py changes [--apply IDS | --apply-all | --discard IDS | --discard-all] [--out-dir PATH]
  python fetch.py reindex [--out-dir PATH]
  python fetch.py rebuild-md [--out-dir PATH]
      regenerates every transcript .md from its stored .json3 (after a format change)

Exit codes: 0 done, 1 error, 3 stopped by YouTube rate limiting, 4 output_dir not set, 130 interrupted.
"""
import argparse
import csv
import difflib
import json
import os
import random
import shutil
import stat
import sys
import time
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Shared by all yt-* skills: "output_dir" at the top level, each skill's settings in its own section.
CONFIG_PATH = Path.home() / ".yt-skills" / "config.json"
DEFAULT_FETCH_CONFIG = {"caption_fallback_languages": ["en"], "explorer_names": True}

TYPES = ["video", "short", "stream", "podcast"]
# Tab order decides the type of a video listed in several tabs (podcast episodes are also in /videos).
TABS = {"podcasts": "podcast", "streams": "stream", "shorts": "short", "videos": "video"}
UNAVAILABLE_TITLES = {"[Private video]", "[Deleted video]", "[Unavailable video]"}
INFO_FIELDS = [
    "id", "title", "description", "channel", "channel_id", "channel_url", "uploader", "uploader_id",
    "uploader_url", "upload_date", "timestamp", "release_timestamp", "duration", "view_count",
    "like_count", "comment_count", "tags", "categories", "chapters", "language", "availability",
    "live_status", "was_live", "age_limit", "media_type", "webpage_url", "thumbnail",
]
CONTENT_FIELDS = ["title", "description", "chapters", "tags"]
REGISTRY_FIELDS = [
    "video_id", "channel_id", "channel", "title", "upload_date", "duration", "type", "transcript",
    "path", "fetched_at",
]
RATE_LIMIT_MARKERS = ("HTTP Error 429", "Too Many Requests", "not a bot")
RATE_LIMIT_WAITS = [60, 120, 240]
PARAGRAPH_SECONDS = 30
SECONDS_PER_VIDEO = 4
SAMPLE_SIZE = 3
PLAN_MAX_AGE_DAYS = 7

Target = namedtuple("Target", "kind id url tab playlist_id")
Entry = namedtuple("Entry", "id title duration type tab available views", defaults=(None,))


class QuietLogger:
    """yt-dlp prints errors itself before raising them; the script reports them instead."""

    def debug(self, message):
        pass

    info = warning = error = debug


class RateLimited(Exception):
    pass


class Skip(Exception):
    pass


# ---------- helpers ----------

def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hms(seconds):
    seconds = int(seconds or 0)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def iso_date(yyyymmdd):
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}" if yyyymmdd and len(yyyymmdd) == 8 else ""


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def clear_attributes(path):
    """Windows: drop read-only/hidden/system flags (set for desktop.ini names) so files can be replaced."""
    if sys.platform != "win32" or not Path(path).exists():
        return
    import ctypes
    set_attributes = ctypes.windll.kernel32.SetFileAttributesW
    set_attributes(str(path), 0x80)
    for folder, dirs, files in os.walk(path):
        for name in dirs + files:
            set_attributes(os.path.join(folder, name), 0x80)


def remove_tree(path):
    if not Path(path).exists():
        return
    clear_attributes(path)

    def retry(func, target, _):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onerror=retry)


def set_display_name(folder, name, config):
    """Windows Explorer shows `name` for the folder; the real path stays the ID."""
    if sys.platform != "win32" or not config.get("explorer_names") or not name:
        return
    import ctypes
    kernel32 = ctypes.windll.kernel32
    ini = Path(folder) / "desktop.ini"
    if ini.exists():
        kernel32.SetFileAttributesW(str(ini), 0x80)
    name = " ".join(str(name).split())
    ini.write_text(f"[.ShellClassInfo]\nLocalizedResourceName={name}\n", encoding="utf-16")
    kernel32.SetFileAttributesW(str(ini), 0x2 | 0x4)  # hidden, system
    attributes = kernel32.GetFileAttributesW(str(folder))
    kernel32.SetFileAttributesW(str(folder), attributes | 0x1)  # read-only makes Explorer read desktop.ini


def log_error(root, video_id, message):
    with open(Path(root) / "errors.log", "a", encoding="utf-8") as f:
        f.write(f"{now()} {video_id} {message}\n")


# ---------- config ----------

def load_config():
    """The settings this script uses: shared output_dir plus the "fetch" section over its defaults."""
    stored = read_json(CONFIG_PATH, {}) or {}
    return {**DEFAULT_FETCH_CONFIG, **(stored.get("fetch") or {}), "output_dir": stored.get("output_dir")}


def archive_root(args, config):
    path = getattr(args, "out_dir", None) or config.get("output_dir")
    if not path:
        print(f"output_dir is not set in {CONFIG_PATH}. Run: fetch.py config --set-output-dir PATH")
        sys.exit(4)
    root = Path(path).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root


# ---------- URL handling ----------

def classify(url, use_playlist=False):
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":")[0]
    for prefix in ("www.", "m.", "music."):
        host = host.removeprefix(prefix)
    query = parse_qs(parsed.query)
    parts = [p for p in parsed.path.split("/") if p]
    playlist = query.get("list", [None])[0]
    real_playlist = playlist if playlist and not playlist.startswith("RD") and playlist not in ("WL", "LL") else None

    video_id = None
    if host == "youtu.be" and parts:
        video_id = parts[0]
    elif host in ("youtube.com", "youtube-nocookie.com"):
        if parts[:1] == ["watch"] and "v" in query:
            video_id = query["v"][0]
        elif len(parts) > 1 and parts[0] in ("shorts", "live", "embed", "v"):
            video_id = parts[1]
        elif parts[:1] == ["playlist"]:
            if not real_playlist:
                raise Skip("unsupported playlist (Watch later, Liked videos or a mix)")
            return Target("playlist", real_playlist, f"https://www.youtube.com/playlist?list={real_playlist}", None, None)
        elif parts and (parts[0].startswith("@") or parts[0] in ("channel", "c", "user")):
            depth = 1 if parts[0].startswith("@") else 2
            base = "https://www.youtube.com/" + "/".join(parts[:depth])
            tab = parts[depth] if len(parts) > depth and parts[depth] in TABS else None
            return Target("channel", base, base, tab, None)
    if not video_id:
        raise Skip("unsupported link (supported: video, playlist, channel)")
    if use_playlist and real_playlist:
        return Target("playlist", real_playlist, f"https://www.youtube.com/playlist?list={real_playlist}", None, None)
    return Target("video", video_id, f"https://www.youtube.com/watch?v={video_id}", None, real_playlist)


# ---------- yt-dlp ----------

def make_ydl(flat=False):
    import yt_dlp
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "ignore_no_formats_error": True,
        # YouTube requires solving JS challenges; without a runtime extraction can fail.
        "js_runtimes": {"deno": {}, "node": {}},
        "logger": QuietLogger(),
    }
    if flat:
        options["extract_flat"] = True
    return yt_dlp.YoutubeDL(options)


def is_rate_limit(error):
    return any(marker in str(error) for marker in RATE_LIMIT_MARKERS)


def flat_info(ydl, url):
    from yt_dlp.utils import DownloadError
    try:
        return ydl.extract_info(url, download=False)
    except DownloadError as e:
        if "does not have a" in str(e):
            return None
        raise


def entry_from_flat(item, video_type, tab):
    title = item.get("title")
    url = item.get("url") or ""
    if video_type is None and "/shorts/" in url:
        video_type = "short"
    available = title not in UNAVAILABLE_TITLES
    return Entry(item["id"], title, item.get("duration"), video_type, tab, available, item.get("view_count"))


def list_target(target):
    """Return (meta, entries) without downloading anything per video."""
    if target.kind == "video":
        return {"kind": "video", "id": target.id}, [Entry(target.id, None, None, None, None, True)]

    with make_ydl(flat=True) as ydl:
        if target.kind == "playlist":
            info = flat_info(ydl, target.url)
            meta = {
                "kind": "playlist", "id": info.get("id"), "title": info.get("title"),
                "channel": info.get("channel") or info.get("uploader"), "channel_id": info.get("channel_id"),
                "description": info.get("description"), "url": target.url,
            }
            entries = [entry_from_flat(e, None, None) for e in info.get("entries") or [] if e.get("id")]
            return meta, entries

        tabs = [target.tab] if target.tab else list(TABS)
        meta, entries, seen = None, [], set()
        for tab in tabs:
            info = flat_info(ydl, f"{target.url}/{tab}")
            if not info:
                continue
            if meta is None:
                meta = {
                    "kind": "channel", "id": info.get("channel_id") or info.get("id"),
                    "title": info.get("channel") or info.get("uploader") or info.get("title"),
                    "handle": info.get("uploader_id"), "description": info.get("description"),
                    "url": info.get("channel_url") or target.url,
                }
            items = info.get("entries") or []
            if tab == "podcasts":
                # The podcasts tab lists playlists; expand them into episodes.
                expanded = []
                for item in items:
                    sub = flat_info(ydl, item.get("url")) if item.get("url") else None
                    expanded += (sub or {}).get("entries") or []
                items = expanded
            for item in items:
                if item.get("id") and item["id"] not in seen:
                    seen.add(item["id"])
                    entries.append(entry_from_flat(item, TABS[tab], tab))
        if meta is None:
            if target.tab:
                raise Skip(f"the channel has no /{target.tab} tab")
            raise Skip("channel not found or it has no videos")
        return meta, entries


# ---------- captions ----------

def json3_url(formats):
    for fmt in formats or []:
        if fmt.get("ext") == "json3":
            return fmt.get("url")
    return None


def pick_caption(info, fallback_languages):
    """Return (language, kind, url) per SPEC: original language first, never machine translations."""
    manual = {k: v for k, v in (info.get("subtitles") or {}).items() if k != "live_chat"}
    automatic = info.get("automatic_captions") or {}
    spoken = [k for k in automatic if k.endswith("-orig")]
    original = info.get("language") or (spoken[0].removesuffix("-orig") if spoken else None)

    def base(code):
        return code.split("-")[0].lower()

    def find(tracks, match):
        for exact in (True, False):
            for key, formats in tracks.items():
                if match(key, exact) and (url := json3_url(formats)):
                    return key, url
        return None

    if original:
        if hit := find(manual, lambda k, exact: k == original if exact else base(k) == base(original)):
            return hit[0], "manual", hit[1]
        orig_tracks = {k: automatic[k] for k in spoken}
        if hit := find(orig_tracks, lambda k, exact: base(k) == base(original)):
            return hit[0].removesuffix("-orig"), "auto", hit[1]
    for language in fallback_languages:
        if hit := find(manual, lambda k, exact: k == language if exact else base(k) == base(language)):
            return hit[0], "manual", hit[1]
    if hit := find(manual, lambda k, exact: True):
        return hit[0], "manual", hit[1]
    return None


def available_captions(info):
    manual = sorted(k for k in (info.get("subtitles") or {}) if k != "live_chat")
    automatic = sorted(k.removesuffix("-orig") for k in (info.get("automatic_captions") or {}) if k.endswith("-orig"))
    return {"manual": manual, "auto": automatic}


def transcript_paragraphs(data):
    """Join caption pieces into paragraphs of roughly PARAGRAPH_SECONDS, each with its start time.

    YouTube marks a change of speaker with ">>"; such a paragraph starts there and has turn=True.
    Returns [(start_seconds, text, turn)].
    """
    paragraphs = []
    state = {"text": "", "start": None, "turn": False}

    def flush():
        text = " ".join(state["text"].split())
        if text:
            paragraphs.append((state["start"], text, state["turn"]))
        state.update(text="", start=None, turn=False)

    for event in data.get("events", []):
        base = event.get("tStartMs", 0)
        for seg in event.get("segs") or []:
            at = (base + seg.get("tOffsetMs", 0)) / 1000
            for number, piece in enumerate(seg.get("utf8", "").replace("\n", " ").split(">>")):
                if number:  # a ">>" was here
                    flush()
                    state.update(start=at, turn=True)
                if not piece.strip():
                    continue
                if state["start"] is None:
                    state["start"] = at
                elif state["text"].strip():
                    elapsed = at - state["start"]
                    sentence_end = state["text"].rstrip()[-1:] in ".!?…"
                    if (elapsed >= PARAGRAPH_SECONDS and sentence_end) or elapsed >= PARAGRAPH_SECONDS * 2:
                        flush()
                        state["start"] = at
                state["text"] += piece
        state["text"] += " "
    flush()
    return paragraphs


def transcript_markdown(info, language, kind, data):
    lines = [
        f"# {info.get('title')}",
        "",
        f"- URL: {info.get('webpage_url')}",
        f"- Channel: {info.get('channel') or info.get('uploader')}",
        f"- Published: {iso_date(info.get('upload_date'))}",
        f"- Duration: {hms(info.get('duration'))}",
        f"- Transcript: {language}, {'manual captions' if kind == 'manual' else 'automatic captions'}",
        "",
    ]
    # "—" marks a change of speaker (">>" in the captions; ">" would be a Markdown quote).
    lines += [f"[{hms(start)}] {'— ' if turn else ''}{text}\n" for start, text, turn in transcript_paragraphs(data)]
    return "\n".join(lines)


def rebuild_markdown(folder):
    """Regenerate transcript.<lang>.md from the stored raw json3; returns True when the file changed."""
    info = read_json(folder / "info.json", {}) or {}
    transcript = (info.get("fetch") or {}).get("transcript")
    if not transcript:
        return False
    raw = folder / f"transcript.{transcript['language']}.json3"
    if not raw.exists():
        return False
    markdown = transcript_markdown(info, transcript["language"], transcript["kind"],
                                   json.loads(raw.read_text(encoding="utf-8")))
    target = folder / f"transcript.{transcript['language']}.md"
    if target.exists() and target.read_text(encoding="utf-8") == markdown:
        return False
    target.write_text(markdown, encoding="utf-8")
    return True


# ---------- one video ----------

def video_type(info, listed_type):
    if listed_type:
        return listed_type
    if info.get("media_type") == "short" or "/shorts/" in (info.get("webpage_url") or ""):
        return "short"
    if info.get("live_status") in ("was_live", "post_live") or info.get("was_live"):
        return "stream"
    return "video"


def build_video(ydl, folder, info, vtype, config):
    remove_tree(folder)
    folder.mkdir(parents=True)
    transcript = None
    if track := pick_caption(info, config.get("caption_fallback_languages") or []):
        language, kind, url = track
        raw = ydl.urlopen(url).read()
        data = json.loads(raw)
        (folder / f"transcript.{language}.json3").write_bytes(raw)
        (folder / f"transcript.{language}.md").write_text(
            transcript_markdown(info, language, kind, data), encoding="utf-8")
        transcript = {"language": language, "kind": kind}
    (folder / "description.txt").write_text(info.get("description") or "", encoding="utf-8")
    for name in ("maxresdefault", "hqdefault"):
        try:
            image = ydl.urlopen(f"https://i.ytimg.com/vi/{info['id']}/{name}.jpg").read()
            (folder / "thumbnail.jpg").write_bytes(image)
            break
        except Exception:
            continue
    record = {k: info[k] for k in INFO_FIELDS if info.get(k) is not None}
    record["type"] = vtype
    record["fetch"] = {"fetched_at": now(), "transcript": transcript, "available_captions": available_captions(info)}
    write_json(folder / "info.json", record)
    return record


def content_of(folder):
    info = read_json(folder / "info.json", {}) or {}
    content = {k: info.get(k) for k in CONTENT_FIELDS}
    # Compare the spoken text from the raw json3, not the .md, so a change of the .md format is no "change".
    parts = []
    for raw in sorted(folder.glob("transcript.*.json3")):
        data = read_json(raw, {}) or {}
        words = "".join(seg.get("utf8", "") for event in data.get("events", []) for seg in event.get("segs") or [])
        parts.append(raw.name + "\n" + " ".join(words.split()))
    content["transcript"] = "\n".join(parts)
    return content


def describe_changes(old, new):
    changed, details = [], []
    for field in CONTENT_FIELDS + ["transcript"]:
        if old.get(field) == new.get(field):
            continue
        changed.append(field)
        if field in ("title", "description"):
            diff = difflib.unified_diff(
                str(old.get(field) or "").splitlines(), str(new.get(field) or "").splitlines(), lineterm="", n=0)
            lines = [l for l in diff if not l.startswith(("---", "+++", "@@"))]
            details.append(f"{field}:\n" + "\n".join(lines[:10]) + ("\n…" if len(lines) > 10 else ""))
        elif field == "transcript":
            details.append(f"transcript: {len(old.get(field) or '')} → {len(new.get(field) or '')} characters")
        else:
            details.append(f"{field}: changed")
    return changed, "\n".join(details)


def ensure_channel(root, info, config):
    folder = root / "channels" / info["channel_id"]
    folder.mkdir(parents=True, exist_ok=True)
    if not (folder / "channel.json").exists():
        write_json(folder / "channel.json", {
            "id": info["channel_id"], "title": info.get("channel") or info.get("uploader"),
            "handle": info.get("uploader_id"), "url": info.get("channel_url"), "checked_at": None,
        })
        set_display_name(folder, info.get("channel") or info.get("uploader"), config)
    return folder


def process_video(ydl, root, config, entry, refresh, since, types):
    """Fetch one video. Returns (status, record) where status is ok, no_transcript, unchanged,
    changed, older or filtered."""
    from yt_dlp.utils import DownloadError
    try:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={entry.id}", download=False)
    except DownloadError as e:
        if is_rate_limit(e):
            raise RateLimited(str(e)) from e
        raise Skip(str(e).replace("ERROR: ", "").split("\n")[0]) from e

    live = info.get("live_status")
    if live == "is_upcoming":
        raise Skip("not published yet (premiere or scheduled stream)")
    if live == "is_live":
        raise Skip("live stream in progress")
    if since and (info.get("upload_date") or "99999999") < since:
        return "older", None
    vtype = video_type(info, entry.type)
    if types and vtype not in types:
        return "filtered", None
    if not info.get("channel_id"):
        raise Skip("channel ID missing")

    channel_folder = ensure_channel(root, info, config)
    final = channel_folder / entry.id
    existed = final.exists()
    if existed and not refresh:
        return "unchanged", None
    tmp = channel_folder / f"{entry.id}.tmp"
    record = build_video(ydl, tmp, info, vtype, config)

    if not existed:
        tmp.rename(final)
        set_display_name(final, f"{iso_date(info.get('upload_date'))} {info.get('title')}", config)
        return ("ok" if record["fetch"]["transcript"] else "no_transcript"), record

    changed, details = describe_changes(content_of(final), content_of(tmp))
    if not changed:
        remove_tree(tmp)
        return "unchanged", record
    pending = channel_folder / f"{entry.id}.pending"
    remove_tree(pending)
    tmp.rename(pending)
    changes = read_json(root / "pending-changes.json", {}) or {}
    changes[entry.id] = {
        "channel_id": info["channel_id"], "title": info.get("title"), "changed": changed,
        "details": details, "detected_at": now(),
    }
    write_json(root / "pending-changes.json", changes)
    return "changed", record


def process_with_retry(ydl, *args):
    for attempt, wait in enumerate([0] + RATE_LIMIT_WAITS):
        if wait:
            print(f"    YouTube is rate limiting, waiting {wait} s (attempt {attempt + 1}/{len(RATE_LIMIT_WAITS) + 1}) …", flush=True)
            time.sleep(wait)
        try:
            return process_video(ydl, *args)
        except RateLimited as e:
            last = e
    raise last


# ---------- registry and indexes ----------

def load_registry(root):
    # Rebuilt from the folders every time: index.csv can be stale after a killed run.
    return {row["video_id"]: row for row in reindex(root)}


def md_cell(text):
    return str(text or "").replace("|", "\\|").replace("\n", " ")


def reindex(root):
    rows = []
    for info_path in sorted(root.glob("channels/*/*/info.json")):
        folder = info_path.parent
        if "." in folder.name:  # .tmp / .pending
            continue
        info = read_json(info_path, {}) or {}
        fetch = info.get("fetch") or {}
        transcript = fetch.get("transcript")
        rows.append({
            "video_id": info.get("id") or folder.name,
            "channel_id": folder.parent.name,
            "channel": info.get("channel") or info.get("uploader") or "",
            "title": info.get("title") or "",
            "upload_date": iso_date(info.get("upload_date")),
            "duration": info.get("duration") or "",
            "type": info.get("type") or "",
            "transcript": f"{transcript['language']} ({transcript['kind']})" if transcript else "",
            "path": folder.relative_to(root).as_posix(),
            "fetched_at": fetch.get("fetched_at") or "",
        })
    rows.sort(key=lambda r: (r["channel"].lower(), r["upload_date"]), reverse=False)

    def write_csv(path, data):
        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=REGISTRY_FIELDS)
            writer.writeheader()
            writer.writerows(data)

    write_csv(root / "index.csv", rows)
    write_csv(root / "no-transcript.csv", [r for r in rows if not r["transcript"]])
    by_id = {r["video_id"]: r for r in rows}

    def video_link(row, prefix):
        folder = root / row["path"]
        target = next(iter(sorted(folder.glob("transcript.*.md"))), folder / "description.txt")
        return f"[{md_cell(row['title'])}]({prefix}{row['path']}/{target.name})"

    channels = []
    for channel_folder in sorted((root / "channels").glob("*")):
        if not channel_folder.is_dir():
            continue
        meta = read_json(channel_folder / "channel.json", {}) or {}
        videos = sorted((r for r in rows if r["channel_id"] == channel_folder.name),
                        key=lambda r: r["upload_date"], reverse=True)
        title = meta.get("title") or channel_folder.name
        channels.append((title, channel_folder.name, videos))
        lines = [f"# {title}", "", f"- YouTube: {meta.get('url') or ''}", f"- Videos in archive: {len(videos)}",
                 f"- Last checked: {meta.get('checked_at') or '–'}", "",
                 "| Date | Title | Type | Duration | Transcript | YouTube |", "|---|---|---|---|---|---|"]
        for r in videos:
            lines.append(f"| {r['upload_date']} | {video_link(r, '../../')} | {r['type']} | {hms(r['duration'])} "
                         f"| {r['transcript'] or '–'} | [▶](https://www.youtube.com/watch?v={r['video_id']}) |")
        (channel_folder / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    playlists = []
    for playlist_folder in sorted((root / "playlists").glob("*")):
        meta = read_json(playlist_folder / "playlist.json")
        if not meta:
            continue
        playlists.append((meta.get("title") or playlist_folder.name, playlist_folder.name, len(meta.get("videos", []))))
        lines = [f"# {meta.get('title')}", "", f"- YouTube: {meta.get('url')}", f"- Author: {meta.get('channel') or ''}",
                 f"- Last checked: {meta.get('checked_at') or '–'}", "",
                 "| # | Title | Channel | Duration | Transcript |", "|---|---|---|---|---|"]
        for number, video in enumerate(meta.get("videos", []), 1):
            row = by_id.get(video["id"])
            if row:
                lines.append(f"| {number} | {video_link(row, '../../')} | {md_cell(row['channel'])} "
                             f"| {hms(row['duration'])} | {row['transcript'] or '–'} |")
            else:
                state = "unavailable" if not video.get("available", True) else "not downloaded"
                lines.append(f"| {number} | {md_cell(video.get('title'))} ({state}) | | | |")
        (playlist_folder / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    lines = ["# YouTube archive", "", f"Videos: {len(rows)} · without transcript: {sum(1 for r in rows if not r['transcript'])}",
             "", "## Channels", "", "| Channel | Videos |", "|---|---|"]
    lines += [f"| [{md_cell(t)}](channels/{cid}/index.md) | {len(v)} |" for t, cid, v in sorted(channels, key=lambda c: c[0].lower())]
    if playlists:
        lines += ["", "## Playlists", "", "| Playlist | Videos |", "|---|---|"]
        lines += [f"| [{md_cell(t)}](playlists/{pid}/index.md) | {n} |" for t, pid, n in sorted(playlists, key=lambda p: p[0].lower())]
    (root / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return rows


def cleanup_tmp(root):
    for tmp in root.glob("channels/*/*.tmp"):
        remove_tree(tmp)


# ---------- commands ----------

def cmd_config(args):
    stored = read_json(CONFIG_PATH, {}) or {}
    if args.set_output_dir:
        path = Path(args.set_output_dir).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        # Keep other skills' sections; write the fetch defaults so they are visible and editable.
        stored = {"output_dir": path.as_posix(), **{k: v for k, v in stored.items() if k != "output_dir"}}
        stored["fetch"] = {**DEFAULT_FETCH_CONFIG, **(stored.get("fetch") or {})}
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        write_json(CONFIG_PATH, stored)
    print(f"Config: {CONFIG_PATH}{'' if CONFIG_PATH.exists() else ' (does not exist, defaults apply)'}")
    print(json.dumps(load_config(), ensure_ascii=False, indent=2))
    return 0


def describe_target(meta):
    if meta["kind"] == "channel":
        handle = f" {meta['handle']}" if meta.get("handle") else ""
        return f"channel {meta['title']}{handle} ({meta['id']})"
    if meta["kind"] == "playlist":
        return f"playlist \"{meta['title']}\" by {meta.get('channel') or '?'} ({meta['id']})"
    return f"video {meta['id']}"


TYPE_LABELS = {"video": "videos", "short": "shorts", "stream": "streams", "podcast": "podcasts", "?": "unknown"}


def parse_filters(args):
    types = set(args.types.split(",")) if getattr(args, "types", None) else None
    if types and (unknown := types - set(TYPES)):
        raise Skip(f"unknown type: {', '.join(sorted(unknown))}. Allowed: {', '.join(TYPES)}")
    since = getattr(args, "since", None)
    if since:
        try:
            datetime.strptime(since, "%Y-%m-%d")
        except ValueError:
            raise Skip("--since must be YYYY-MM-DD") from None
    return types, since


def video_details(video_id, config):
    """Full metadata of one video for the overview."""
    from yt_dlp.utils import DownloadError
    with make_ydl() as ydl:
        try:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        except DownloadError as e:
            raise Skip(str(e).replace("ERROR: ", "").split("\n")[0]) from e
    track = pick_caption(info, config.get("caption_fallback_languages") or [])
    return {
        "title": info.get("title"), "channel": info.get("channel") or info.get("uploader"),
        "channel_id": info.get("channel_id"), "upload_date": info.get("upload_date"), "duration": info.get("duration"),
        "type": video_type(info, None), "live_status": info.get("live_status"),
        "transcript": {"language": track[0], "kind": track[1]} if track else None,
    }


def build_plan(args, root, config):
    """List what the link covers, apply the filters and save the exact queue as a plan file."""
    types, since = parse_filters(args)
    target = classify(args.url, args.playlist)
    meta, entries = list_target(target)
    registry = load_registry(root)
    refresh = bool(args.refresh) or target.kind == "video"
    if target.kind == "video":
        meta["video"] = video_details(target.id, config)
        meta["in_archive"] = target.id in registry
        meta["playlist_id"] = target.playlist_id
    queue = [e for e in entries if e.available and (refresh or e.id not in registry)
             and (not types or e.type is None or e.type in types)]
    plan = {
        "created_at": now(), "root": root.as_posix(), "url": args.url,
        "options": {"playlist": bool(args.playlist), "types": sorted(types) if types else None,
                    "since": since, "refresh": refresh},
        "meta": meta,
        "entries": [e._asdict() for e in entries],
        "queue": [e.id for e in queue],
        # Shown in the overview, so a "sample" confirmation means exactly these videos.
        "sample": [e.id for e in sorted(queue, key=lambda e: e.views or 0, reverse=True)[:SAMPLE_SIZE]]
                  if len(queue) > SAMPLE_SIZE else [],
        "skipped_known": sum(1 for e in entries if e.available and e.id in registry and not refresh),
    }
    plans = root / ".plans"
    plans.mkdir(exist_ok=True)
    for stale in plans.glob("plan-*.json"):  # overviews never confirmed
        if time.time() - stale.stat().st_mtime > PLAN_MAX_AGE_DAYS * 86400:
            stale.unlink(missing_ok=True)
    path = plans / f"plan-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    write_json(path, plan)
    return plan, path


def print_overview(plan, path):
    meta, options = plan["meta"], plan["options"]
    entries = [Entry(**e) for e in plan["entries"]]
    queue = set(plan["queue"])
    root = Path(plan["root"])
    print(f"Storage: {root}")
    print(f"Link: {describe_target(meta)}")
    if meta["kind"] == "channel":
        print(f"Target: {root / 'channels' / meta['id']}")
    elif meta["kind"] == "playlist":
        print(f"Target: {root / 'playlists' / meta['id']} (list and links)")
        print(f"        videos to {root / 'channels'}\\<channel ID of the video>")
    elif meta["video"].get("channel_id"):
        print(f"Target: {root / 'channels' / meta['video']['channel_id'] / meta['id']}")

    if meta["kind"] == "video":
        video = meta["video"]
        transcript = video["transcript"]
        print(f"  \"{video['title']}\"")
        print(f"  {video['channel']} · {iso_date(video['upload_date'])} · {hms(video['duration'])} · {video['type']}")
        if transcript:
            kind = "manual" if transcript["kind"] == "manual" else "automatic"
            print(f"  Transcript: {transcript['language']} ({kind} captions)")
        else:
            print("  Transcript: none, only the description and metadata are saved")
        if video["live_status"] in ("is_upcoming", "is_live"):
            print("  Status: cannot be downloaded yet (premiere or live stream in progress)")
        elif meta["in_archive"]:
            print("  Status: already in the archive, changes will be checked")
        else:
            print("  Status: new, will be downloaded")
        if meta.get("playlist_id"):
            print(f"  The link also contains playlist {meta['playlist_id']}. Whole playlist: survey with --playlist.")
        print(f"Plan: {path}")
        return

    filters = ["also videos already in the archive (check for changes)" if options["refresh"] else "only new"]
    if options["types"]:
        filters.append("types: " + ", ".join(TYPE_LABELS[t] for t in options["types"]))
    if options["since"]:
        filters.append(f"since {options['since']}")
    print(f"Selection: {' · '.join(filters)}")
    print()

    available = [e for e in entries if e.available]
    print(f"{'To download':<12}{'Count':>7}{'Total':>12}{'Average':>10}")
    total_count, total_seconds, total_missing = 0, 0, 0
    for vtype in TYPES + ["?"]:
        present = [e for e in available if (e.type or "?") == vtype]
        if not present:
            continue  # only the categories the channel or playlist actually has
        label = TYPE_LABELS[vtype]
        if options["types"] and vtype != "?" and vtype not in options["types"]:
            print(f"{label:<12}{'skipped':>7}")
            continue
        chosen = [e for e in present if e.id in queue]
        seconds = sum(e.duration or 0 for e in chosen)
        known = [e for e in chosen if e.duration]
        missing = len(chosen) - len(known)
        total_count += len(chosen)
        total_seconds += seconds
        total_missing += missing
        sum_text = "?" if chosen and not known else hms(seconds) + ("+" if missing else "")
        average_text = hms(seconds / len(known)) if known else ("?" if chosen else "–")
        print(f"{label:<12}{len(chosen):>7}{sum_text:>12}{average_text:>10}")
    print(f"{'total':<12}{total_count:>7}{hms(total_seconds) + ('+' if total_missing else ''):>12}")

    notes = []
    if plan["skipped_known"]:
        notes.append(f"Already in the archive, skipped: {plan['skipped_known']}")
    if unavailable := len(entries) - len(available):
        notes.append(f"Unavailable (private/deleted): {unavailable}")
    if total_missing:
        notes.append("\"+\" and \"?\": YouTube gives no duration for some videos (mostly shorts)")
    if any(not e.type for e in available):
        notes.append("In a playlist, type \"unknown\" is found out only when downloading")
    if options["since"]:
        notes.append("The publish date is checked only when downloading, so the counts are an upper bound")
    if total_count:
        notes.append(f"Estimated time: ~{max(1, round(total_count * SECONDS_PER_VIDEO / 60))} min")
    else:
        notes.append("Nothing to download")
    print()
    for note in notes:
        print(note)
    if plan.get("sample"):
        by_id = {e.id: e for e in entries}
        print()
        print(f"Sample ({len(plan['sample'])} most viewed):")
        for video_id in plan["sample"]:
            e = by_id[video_id]
            print(f"- {e.title} · {views_text(e.views)} · {hms(e.duration) if e.duration else '?'}")
    print(f"Plan: {path}")


def views_text(views):
    if not views:
        return "? views"
    if views >= 1_000_000:
        return f"{views / 1_000_000:.1f}".removesuffix(".0") + "M views"
    if views >= 1000:
        return f"{round(views / 1000)}K views"
    return f"{views} views"


def cmd_survey(args):
    config = load_config()
    root = archive_root(args, config)
    plan, path = build_plan(args, root, config)
    print_overview(plan, path)
    return 0


def cmd_download(args):
    config = load_config()
    if args.plan:
        path = Path(args.plan)
        plan = read_json(path)
        if not plan:
            raise Skip(f"plan {path} does not exist or is corrupt")
        root = Path(plan["root"])
    elif args.url:
        root = archive_root(args, config)
        plan, path = build_plan(args, root, config)
    else:
        raise Skip("give --plan or a URL")

    meta, options = plan["meta"], plan["options"]
    entries = [Entry(**e) for e in plan["entries"]]
    types = set(options["types"]) if options["types"] else None
    since = options["since"].replace("-", "") if options["since"] else None
    refresh = options["refresh"]
    every = max(1, args.progress_every)

    cleanup_tmp(root)
    registry = load_registry(root)
    print(f"Storage: {root}")
    print(f"Link: {describe_target(meta)}", flush=True)

    if meta["kind"] == "channel":
        folder = root / "channels" / meta["id"]
        folder.mkdir(parents=True, exist_ok=True)
        stored = read_json(folder / "channel.json", {}) or {}
        write_json(folder / "channel.json", {**stored, **{k: meta.get(k) for k in ("id", "title", "handle", "description", "url")},
                                               "checked_at": now()})
        set_display_name(folder, meta["title"], config)
    elif meta["kind"] == "playlist":
        folder = root / "playlists" / meta["id"]
        folder.mkdir(parents=True, exist_ok=True)
        write_json(folder / "playlist.json", {
            **{k: meta.get(k) for k in ("id", "title", "channel", "channel_id", "description", "url")},
            "checked_at": now(),
            "videos": [{"id": e.id, "title": e.title, "available": e.available} for e in entries],
        })
        set_display_name(folder, meta["title"], config)

    by_id = {e.id: e for e in entries}
    # A plan is run again after an interruption: skip what got archived meanwhile.
    chosen = plan.get("sample") if args.sample else plan["queue"]
    if args.sample and not chosen:
        raise Skip("the plan has no sample (it has no more videos than the sample size)")
    queue = [by_id[i] for i in chosen if i in by_id and (refresh or i not in registry)]
    print(f"To process: {len(queue)} videos{' (sample)' if args.sample else ''}", flush=True)

    results = {k: [] for k in ("ok", "no_transcript", "unchanged", "changed", "older", "filtered", "error")}
    stopped_tabs, status_code = set(), 0
    try:
        with make_ydl() as ydl:
            for number, entry in enumerate(queue, 1):
                prefix = f"[{number}/{len(queue)}]"
                if entry.tab and entry.tab in stopped_tabs:
                    results["older"].append((entry, None))
                    continue
                try:
                    status, record = process_with_retry(ydl, root, config, entry, refresh, since, types)
                except Skip as e:
                    results["error"].append((entry, str(e)))
                    log_error(root, entry.id, str(e))
                    print(f"{prefix} {entry.title or entry.id} … ERROR: {e}", flush=True)
                    continue
                results[status].append((entry, record))
                title = (record or {}).get("title") or entry.title or entry.id
                # Tabs are newest first, so the first older video ends the tab (not so in a sample).
                if status == "older" and entry.tab and meta["kind"] == "channel" and not args.sample:
                    stopped_tabs.add(entry.tab)
                    print(f"{prefix} older than {options['since']}, rest of the /{entry.tab} tab skipped", flush=True)
                elif status == "changed" or number % every == 0 or number == len(queue):
                    print(progress_line(prefix, status, record, title, results, every), flush=True)
                if number < len(queue):
                    time.sleep(random.uniform(1, 3))
    except RateLimited as e:
        print(f"\nSTOPPED: YouTube is rate limiting ({str(e).splitlines()[0]}). Run the same command later to continue.")
        status_code = 3
    except KeyboardInterrupt:
        print("\nINTERRUPTED. The same command continues with the unprocessed videos.")
        status_code = 130

    reindex(root)
    print_summary(root, results)
    print(f"\nIndex: {target_index(root, meta)}")
    print(f"Archive: {root / 'index.md'}")
    if args.sample and status_code == 0:
        rest = sum(1 for i in plan["queue"] if i not in load_registry(root))
        print(f"\nRest of the plan: {rest} videos. The same command without --sample downloads it.")
    elif status_code == 0 and args.plan:
        path.unlink(missing_ok=True)
    return status_code


def target_index(root, meta):
    """The page to open for what this run downloaded: channel or playlist index, or the video's transcript."""
    if meta["kind"] == "channel":
        return root / "channels" / meta["id"] / "index.md"
    if meta["kind"] == "playlist":
        return root / "playlists" / meta["id"] / "index.md"
    channel_id = (meta.get("video") or {}).get("channel_id")
    folder = root / "channels" / channel_id / meta["id"] if channel_id else None
    if folder and folder.exists():
        return next(iter(sorted(folder.glob("transcript.*.md"))), folder / "description.txt")
    return root / "index.md"


def progress_line(prefix, status, record, title, results, every):
    if every > 1 and status != "changed":
        counts = f"ok {len(results['ok'])} · no transcript {len(results['no_transcript'])} · errors {len(results['error'])}"
        return f"{prefix} {counts} · last: {title}"
    transcript = ((record or {}).get("fetch") or {}).get("transcript")
    text = {
        "ok": f"ok ({transcript['language']}, {transcript['kind']})" if transcript else "ok",
        "no_transcript": "no transcript",
        "unchanged": "unchanged",
        "changed": "changed, awaiting decision",
        "filtered": "other type, skipped",
        "older": "older, skipped",
    }[status]
    date = iso_date((record or {}).get("upload_date"))
    return f"{prefix} {date} {title} … {text}".replace("  ", " ")


def print_summary(root, results):
    print()
    print("Summary: "
          f"ok {len(results['ok'])} · no transcript {len(results['no_transcript'])} · "
          f"unchanged {len(results['unchanged'])} · changed {len(results['changed'])} · "
          f"errors {len(results['error'])} · skipped by filter {len(results['older']) + len(results['filtered'])}")
    if missing := results["no_transcript"]:
        total = sum((r or {}).get("duration") or 0 for _, r in missing)
        print(f"\nVideos without transcript ({len(missing)}, total {hms(total)}):")
        for _, record in missing:
            print(f"- {record.get('title')} — {record.get('channel')} — {hms(record.get('duration'))}"
                  f" — channels/{record.get('channel_id')}/{record.get('id')}")
    if errors := results["error"]:
        print(f"\nErrors ({len(errors)}), also written to {root / 'errors.log'}:")
        for entry, message in errors:
            print(f"- {entry.id} {entry.title or ''}: {message}")
    if results["changed"]:
        print("\nChanged videos await a decision: fetch.py changes")


def cmd_changes(args):
    config = load_config()
    root = archive_root(args, config)
    path = root / "pending-changes.json"
    changes = read_json(path, {}) or {}
    if not changes:
        print("No pending changes.")
        return 0

    def ids(value):
        return list(changes) if value == "all" else [i.strip() for i in value.split(",") if i.strip()]

    apply_ids = ids("all") if args.apply_all else ids(args.apply) if args.apply else []
    discard_ids = ids("all") if args.discard_all else ids(args.discard) if args.discard else []
    if not apply_ids and not discard_ids:
        print(f"Pending changes ({len(changes)}):")
        for video_id, change in changes.items():
            print(f"\n## {video_id} {change['title']}\nChanged: {', '.join(change['changed'])}\n{change['details']}")
        return 0

    for video_id in apply_ids + discard_ids:
        change = changes.pop(video_id, None)
        if not change:
            print(f"{video_id}: not among the pending changes")
            continue
        channel_folder = root / "channels" / change["channel_id"]
        final, pending = channel_folder / video_id, channel_folder / f"{video_id}.pending"
        if video_id in apply_ids and pending.exists():
            remove_tree(final)
            pending.rename(final)
            info = read_json(final / "info.json", {}) or {}
            set_display_name(final, f"{iso_date(info.get('upload_date'))} {info.get('title')}", config)
            print(f"{video_id}: overwritten")
        else:
            remove_tree(pending)
            print(f"{video_id}: kept unchanged")
    write_json(path, changes) if changes else path.unlink()
    reindex(root)
    return 0


def cmd_reindex(args):
    root = archive_root(args, load_config())
    rows = reindex(root)
    print(f"Indexes rebuilt: {len(rows)} videos in {root}")
    return 0


def cmd_rebuild_md(args):
    root = archive_root(args, load_config())
    folders = [p.parent for p in sorted(root.glob("channels/*/*/info.json")) if "." not in p.parent.name]
    changed = sum(1 for folder in folders if rebuild_markdown(folder))
    print(f"Transcript .md files regenerated from .json3: {changed} of {len(folders)} videos changed in {root}")
    return 0


def main():
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    config = commands.add_parser("config")
    config.add_argument("--set-output-dir")

    for name in ("survey", "download"):
        command = commands.add_parser(name)
        command.add_argument("url", nargs="?" if name == "download" else None)
        command.add_argument("--playlist", action="store_true", help="for watch?v=…&list=… take the whole playlist")
        command.add_argument("--types", help="comma separated: " + ",".join(TYPES))
        command.add_argument("--since", help="YYYY-MM-DD, only videos published on or after")
        command.add_argument("--refresh", action="store_true", help="re-check videos already in the archive")
        command.add_argument("--out-dir")
        if name == "download":
            command.add_argument("--plan", help="plan file saved by survey; downloads exactly that list")
            command.add_argument("--progress-every", type=int, default=1, help="print progress every N videos")
            command.add_argument("--sample", action="store_true", help="only the most viewed videos listed in the overview")

    changes = commands.add_parser("changes")
    changes.add_argument("--apply")
    changes.add_argument("--apply-all", action="store_true")
    changes.add_argument("--discard")
    changes.add_argument("--discard-all", action="store_true")
    changes.add_argument("--out-dir")

    for name in ("reindex", "rebuild-md"):
        commands.add_parser(name).add_argument("--out-dir")

    args = parser.parse_args()
    handler = {"config": cmd_config, "survey": cmd_survey, "download": cmd_download,
               "changes": cmd_changes, "reindex": cmd_reindex, "rebuild-md": cmd_rebuild_md}[args.command]
    try:
        return handler(args)
    except Skip as e:
        print(f"ERROR: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
