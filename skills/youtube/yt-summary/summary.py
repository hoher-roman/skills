"""Helpers for yt-summary: find moments in a transcript, check a summary and serve the archive viewer.

Usage:
  python summary.py config [--set-language <code>] [--set-model <model>] [--set-parallel <n>] [--set-port <port>]
      prints the summary section of the config; the options change it
  python summary.py find "<words>"
      videos, channels and playlists of the archive whose names contain all the words
  python summary.py plan <channel or playlist: link, ID, @handle or name> [--refresh]
      overview of the videos to summarize, saved as a plan (--refresh: also videos that have a summary)
  python summary.py status <plan> [--sample]
      which videos of the plan are summarized and which remain
  python summary.py info <video_id>
      paths of the video's archive files, the summary language and the viewer labels still to translate
  python summary.py locate <video_id> "<phrase>" ["<phrase>" ...]
      start time (seconds) of each phrase in the transcript, by word timing from the .json3
  python summary.py keep <video_id>
      moves an existing summary.<lang>.json aside as summary.<lang>.<date>.json, before a new analysis
  python summary.py check <video_id>
      validates summary.<lang>.json against what the viewer expects
  python summary.py start [<video_id>] [--open]
      makes sure the viewer server runs in the background and prints its address (--open: also in the browser)
  python summary.py stop
      stops the background viewer server
  python summary.py serve
      runs the viewer server in the foreground (start uses this)

The viewer (viewer.html) lives next to this script; the archive holds only data:
summary.<lang>.json per video and summary-labels.<lang>.json (viewer labels in the summary language) in its root.

Exit codes: 0 done, 1 error, 4 output_dir not set.
"""
import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CONFIG_PATH = Path.home() / ".yt-skills" / "config.json"
SERVER_LOG = Path.home() / ".yt-skills" / "viewer-server.log"
DEFAULT_SUMMARY_CONFIG = {"language": "en", "model": "inherit", "parallel": 3, "port": 8765}
SCRIPT = Path(__file__).resolve()
VIEWER = SCRIPT.with_name("viewer.html")
LEAD_SECONDS = 1  # start a moment slightly before the phrase so the sentence is not cut
VIDEO_ID = re.compile(r"[\w-]+")

# Viewer labels in English. summary-labels.<lang>.json in the archive root overrides them in the summary language.
LABELS = {
    "archive": "YouTube archive",
    "back": "Archive",
    "search": "Search videos",
    "only_summaries": "Only with a summary",
    "video_count": "Videos: {n}",
    "summary_count": "with a summary: {n}",
    "summary_badge": "Summary",
    "no_results": "No video matches.",
    "not_found": "This video is not in the archive.",
    "list_not_found": "This channel or playlist is not in the archive.",
    "playlists": "Playlists",
    "overview": "Overview",
    "topics": "Topics",
    "ideas": "Main ideas",
    "reasoning": "Reasoning",
    "moments": "In the video",
    "chapters": "Chapters",
    "description": "Description",
    "no_summary": "This video has no summary yet.",
    "no_summary_command": "You can also send Claude this command:",
    "copy": "Copy",
    "copied": "Copied",
    "summarize": "Create summary",
    "summarize_channel": "Create missing summaries ({n})",
    "summarize_confirm": "Create the missing summaries? Videos: {n}, total length {time}, model {model}. "
                         "It runs in the background on your Claude account.",
    "job_queued": "Waiting in the queue…",
    "job_running": "Creating the summary… This usually takes 1–3 minutes.",
    "job_error": "The summary could not be created: {error}",
    "jobs_active": "Summaries in progress: {n}",
    "badge_working": "In progress",
    "theme_auto": "Auto",
    "theme_light": "Light",
    "theme_dark": "Dark",
    "open_youtube": "Open on YouTube",
    "player_fallback": "The video cannot be played on this page. Times open it on YouTube in a new tab.",
    "source": "Summary of the transcript",
}


class Fail(Exception):
    pass


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def load_config():
    stored = read_json(CONFIG_PATH, {}) or {}
    return {**DEFAULT_SUMMARY_CONFIG, **(stored.get("summary") or {}), "output_dir": stored.get("output_dir")}


def archive_root(config):
    if not config.get("output_dir"):
        print(f"output_dir is not set in {CONFIG_PATH}. Run yt-fetch first.")
        sys.exit(4)
    return Path(config["output_dir"]).expanduser()


def video_folder(root, video_id):
    if VIDEO_ID.fullmatch(video_id):
        for folder in root.glob(f"channels/*/{video_id}"):
            if (folder / "info.json").exists():
                return folder
    raise Fail(f"video {video_id} is not in the archive {root}")


def transcript_file(folder):
    info = read_json(folder / "info.json", {}) or {}
    transcript = (info.get("fetch") or {}).get("transcript")
    if not transcript:
        raise Fail("the video has no transcript")
    return folder / f"transcript.{transcript['language']}.json3", transcript


def labels_file(root, language):
    return root / f"summary-labels.{language}.json"


def hms(seconds):
    seconds = int(seconds or 0)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def seconds_of(value):
    """Accept 754, "754", "12:34" or "1:02:03"."""
    if isinstance(value, (int, float)):
        return int(value)
    total = 0
    for part in str(value).split(":"):
        total = total * 60 + int(part)
    return total


# ---------- locate ----------

def normalize(word):
    return re.sub(r"[^\w]", "", word.lower())


def timed_words(json3_path):
    """[(seconds, normalized word)] for every word of the transcript."""
    data = read_json(json3_path, {}) or {}
    words = []
    for event in data.get("events", []):
        base = event.get("tStartMs", 0)
        for seg in event.get("segs") or []:
            at = (base + seg.get("tOffsetMs", 0)) / 1000
            for word in seg.get("utf8", "").replace(">>", " ").split():
                if token := normalize(word):
                    words.append((at, token))
    return words


def locate(words, phrase):
    """All start times where the phrase's words appear in a row."""
    wanted = [t for t in (normalize(w) for w in phrase.split()) if t]
    if not wanted:
        return []
    tokens = [w for _, w in words]
    hits = []
    for i in range(len(tokens) - len(wanted) + 1):
        if tokens[i:i + len(wanted)] == wanted:
            hits.append(max(0, int(words[i][0]) - LEAD_SECONDS))
    return hits


# ---------- check ----------

def summary_problems(summary, duration):
    """What in summary.json the viewer could not show, as readable sentences."""
    problems = []

    def time_ok(value, where):
        try:
            at = seconds_of(value)
        except (TypeError, ValueError):
            problems.append(f"{where}: start {value!r} is not seconds or m:ss")
            return
        if at < 0 or (duration and at > duration):
            problems.append(f"{where}: start {hms(at)} is outside the video (0:00–{hms(duration)})")

    if not str(summary.get("overview") or "").strip():
        problems.append("overview is empty")
    for name, fields in (("topics", ("start", "title")), ("ideas", ("start", "title", "summary"))):
        items = summary.get(name)
        if not isinstance(items, list) or not items:
            problems.append(f"{name} is missing or empty")
            continue
        for number, item in enumerate(items, 1):
            where = f"{name}[{number}]"
            missing = [f for f in fields if not item.get(f) and item.get(f) != 0]
            if missing:
                problems.append(f"{where}: missing {', '.join(missing)}")
            if "start" in item:
                time_ok(item["start"], where)
            for m_number, moment in enumerate(item.get("moments") or [], 1):
                if not moment.get("text"):
                    problems.append(f"{where}.moments[{m_number}]: missing text")
                time_ok(moment.get("start"), f"{where}.moments[{m_number}]")
    return problems


# ---------- viewer server ----------

def list_videos(root, language):
    """Every video of the archive (from yt-fetch's index.csv), with whether it has a summary."""
    try:
        with (root / "index.csv").open(encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
    except OSError:
        return []
    return [{
        "id": row["video_id"],
        "channel_id": row["channel_id"],
        "channel": row["channel"],
        "title": row["title"],
        "upload_date": row["upload_date"],
        "duration": int(row["duration"] or 0),
        "type": row["type"],
        "transcript": row["transcript"],
        "summary": (root / row["path"] / f"summary.{language}.json").exists(),
    } for row in rows]


def list_playlists(root):
    """Archived playlists with their videos in playlist order."""
    playlists = []
    for path in sorted(root.glob("playlists/*/playlist.json")):
        meta = read_json(path, {}) or {}
        playlists.append({
            "id": meta.get("id") or path.parent.name,
            "title": meta.get("title") or path.parent.name,
            "channel": meta.get("channel"),
            "checked_at": meta.get("checked_at"),
            "videos": [v["id"] for v in meta.get("videos", []) if v.get("id")],
        })
    return playlists


def list_channels(root):
    return [read_json(path, {}) or {} for path in sorted(root.glob("channels/*/channel.json"))]


# ---------- find and plan ----------

def plain(text):
    """Lowercase without diacritics, so "cafe" finds "Café"."""
    import unicodedata
    decomposed = unicodedata.normalize("NFKD", str(text or "").lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def matches(words, *texts):
    haystack = plain(" ".join(t or "" for t in texts))
    return all(w in haystack for w in words)


def resolve_list(root, target):
    """A channel or playlist of the archive from a link, ID, @handle or name: (kind, id, title, checked_at)."""
    channels, playlists = list_channels(root), list_playlists(root)
    parsed = urllib.parse.urlsplit(target if "://" in target else "https://" + target) if "/" in target else None
    if parsed:
        query = urllib.parse.parse_qs(parsed.query)
        if query.get("list"):
            target = query["list"][0]
        elif match := re.search(r"/channel/(UC[\w-]+)", parsed.path):
            target = match[1]
        elif match := re.search(r"/(@[\w.-]+)", parsed.path):
            target = match[1]
        elif match := re.search(r"/(?:c|user)/([^/]+)", parsed.path):
            target = urllib.parse.unquote(match[1])

    for c in channels:
        if target in (c.get("id"), c.get("handle")) or plain(target) == plain(c.get("handle")):
            return "channel", c["id"], c.get("title") or c["id"], c.get("checked_at")
    for p in playlists:
        if target == p["id"]:
            return "playlist", p["id"], p["title"], p["checked_at"]

    words = plain(target).split()
    found = [("channel", c["id"], c.get("title") or c["id"], c.get("checked_at"))
             for c in channels if matches(words, c.get("title"), c.get("handle"))]
    found += [("playlist", p["id"], p["title"], p["checked_at"]) for p in playlists if matches(words, p["title"])]
    if len(found) == 1:
        return found[0]
    if found:
        raise Fail("several channels or playlists match: "
                   + "; ".join(f"{kind} {title} ({id_})" for kind, id_, title, _ in found))
    raise Fail(f"no channel or playlist of the archive matches {target!r}; download it with yt-fetch first")


def list_page(port, kind, list_id):
    return f"http://127.0.0.1:{port}/{'c' if kind == 'channel' else 'p'}/{list_id}"


def video_payload(root, video_id, language):
    folder = video_folder(root, video_id)
    info = read_json(folder / "info.json", {}) or {}
    try:
        description = (folder / "description.txt").read_text(encoding="utf-8")
    except OSError:
        description = info.get("description") or ""
    return {
        **{key: info.get(key) for key in (
            "id", "title", "channel", "channel_id", "upload_date", "duration", "view_count", "chapters")},
        "transcript": (info.get("fetch") or {}).get("transcript"),
        "description": description,
        "summary": read_json(folder / f"summary.{language}.json"),
    }


# ---------- summaries started from the viewer ----------

ANALYSIS = SCRIPT.with_name("ANALYSIS.md")
JOBS_DIR = Path.home() / ".yt-skills" / "jobs"
CLAUDE_OVERRIDE = "YT_SUMMARY_CLAUDE"  # environment variable: another program instead of claude, for tests
JOB_TIMEOUT = 30 * 60


def claude_command():
    import shutil
    override = os.environ.get(CLAUDE_OVERRIDE)
    if override:
        return [sys.executable, override] if override.endswith(".py") else [override]
    found = shutil.which("claude")
    return [found] if found else None


def run_summary(video_id):
    """Summarize one video with a headless Claude Code run by ANALYSIS.md. Returns None, or what went wrong."""
    config = load_config()
    root = archive_root(config)
    claude = claude_command()
    if not claude:
        return "the claude command (Claude Code) is not installed"
    folder = video_folder(root, video_id)
    script = SCRIPT.as_posix()
    prompt = (f"Summarize the YouTube video `{video_id}` from the local archive. Read `{ANALYSIS.as_posix()}` "
              f"and follow it exactly. Run the script exactly as `python \"{script}\" <command> ...`. "
              "The user's wishes: none. Reply with only the result line from step 5 of ANALYSIS.md.")
    # Edits only inside the archive (its root is the working directory), commands only this script and date.
    command = claude + ["-p", prompt, "--output-format", "json", "--no-session-persistence",
                        "--permission-mode", "acceptEdits"]
    if config["model"] != "inherit":
        command += ["--model", config["model"]]
    command += ["--allowedTools", "Read", f"Bash(python \"{script}\":*)", "Bash(date:*)"]

    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = JOBS_DIR / f"{video_id}.log"
    started = time.time()
    with log_path.open("w", encoding="utf-8") as log:
        try:
            result = subprocess.run(command, cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log,
                                    text=True, encoding="utf-8", errors="replace", timeout=JOB_TIMEOUT,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except subprocess.TimeoutExpired:
            return f"no result after {JOB_TIMEOUT // 60} minutes (log: {log_path})"
        log.write(result.stdout)
    try:
        text = str(json.loads(result.stdout).get("result") or "")
    except (ValueError, AttributeError):
        text = result.stdout

    summary = folder / f"summary.{config['language']}.json"
    if summary.exists() and summary.stat().st_mtime >= started:
        duration = (read_json(folder / "info.json", {}) or {}).get("duration") or 0
        problems = summary_problems(read_json(summary, {}) or {}, duration)
        return f"the summary has problems: {problems[0]} (log: {log_path})" if problems else None
    lines = [line for line in text.strip().splitlines() if line.strip()]
    reason = lines[-1][:300] if lines else f"claude ended with code {result.returncode}"
    if "authenticate" in text.lower() or "/login" in text:
        reason += " – sign in to Claude Code: run claude in a terminal and type /login"
    return f"{reason} (log: {log_path})"


# Failures after which every further run would fail the same way: the usage limit of the subscription or a lost sign-in.
BLOCKING_ERROR = re.compile(r"usage limit|rate limit|hit your limit|limit reached|resets? at|\b429\b|authenticate|/login",
                            re.IGNORECASE)


class Jobs:
    """Summaries requested in the viewer: a queue worked off by `parallel` headless Claude runs at once."""

    def __init__(self):
        import queue
        import threading
        self.lock = threading.Lock()
        self.queue = queue.Queue()
        self.state = {}  # video id -> {"state": "queued" | "running" | "done" | "error", "error": "..."}
        self.workers = 0

    def submit(self, root, language, ids):
        queued, skipped = [], {}
        for video_id in ids:
            try:
                folder = video_folder(root, video_id)
                transcript_file(folder)
                reason = "it has a summary" if (folder / f"summary.{language}.json").exists() else None
            except Fail as e:
                reason = str(e)
            with self.lock:
                if not reason and self.state.get(video_id, {}).get("state") in ("queued", "running"):
                    reason = "it is already in progress"
                if reason:
                    skipped[video_id] = reason
                    continue
                self.state[video_id] = {"state": "queued"}
            self.queue.put(video_id)
            queued.append(video_id)
        self.start_workers()
        return {"queued": queued, "skipped": skipped}

    def start_workers(self):
        import threading
        parallel = max(1, int(load_config()["parallel"]))
        with self.lock:
            while self.workers < parallel:
                self.workers += 1
                threading.Thread(target=self.work, daemon=True).start()

    def work(self):
        while True:
            video_id = self.queue.get()
            with self.lock:
                self.state[video_id] = {"state": "running"}
            try:
                error = run_summary(video_id)
            except Exception as e:  # keep the worker alive whatever one video does
                error = str(e)
            with self.lock:
                self.state[video_id] = {"state": "error", "error": error} if error else {"state": "done"}
            if error and BLOCKING_ERROR.search(error):
                self.cancel_queued("not started, because another video failed: " + error)

    def cancel_queued(self, reason):
        """Drop the videos still waiting, so they do not fail one after another; they can be started again later."""
        import queue
        while True:
            try:
                video_id = self.queue.get_nowait()
            except queue.Empty:
                return
            with self.lock:
                self.state[video_id] = {"state": "error", "error": reason}

    def snapshot(self):
        with self.lock:
            return {video_id: dict(job) for video_id, job in self.state.items()}

    def busy(self):
        with self.lock:
            return sum(1 for job in self.state.values() if job["state"] in ("queued", "running"))


JOBS = Jobs()
# Pages the server hands out carry this key; a request to start summaries must send it back,
# so other websites open in the browser cannot start them.
TOKEN = __import__("secrets").token_urlsafe(24)


def make_handler(port):
    import hmac
    import http.server

    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            origin = self.headers.get("Origin")
            if (self.headers.get("Host") not in allowed_hosts
                    or (origin and origin.removeprefix("http://") not in allowed_hosts)
                    or not hmac.compare_digest(self.headers.get("X-Token", ""), TOKEN)):
                return self.send_error(403)
            if urllib.parse.urlsplit(self.path).path != "/api/summarize":
                return self.send_error(404)
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                ids = [i for i in body.get("ids", []) if isinstance(i, str) and VIDEO_ID.fullmatch(i)][:1000]
            except (ValueError, AttributeError):
                return self.send_error(400)
            config = load_config()
            return self.send_json(JOBS.submit(archive_root(config), config["language"], ids))

        def do_GET(self):
            # Only pages opened on this computer's own address may read the archive (no DNS rebinding).
            if self.headers.get("Host") not in allowed_hosts:
                return self.send_error(403)
            path = urllib.parse.urlsplit(self.path).path
            config = load_config()
            root = archive_root(config)
            language = config["language"]
            try:
                if path == "/" or re.fullmatch(r"/[vcp]/[\w-]+", path):
                    page = VIEWER.read_text(encoding="utf-8").replace("{{TOKEN}}", TOKEN)
                    return self.send_body(page.encode("utf-8"), "text/html; charset=utf-8")
                if path == "/api/health":
                    return self.send_json({"app": "yt-summary", "pid": os.getpid(), "root": str(root),
                                           "script": SCRIPT_STAMP, "busy": JOBS.busy()})
                if path == "/api/config":
                    labels = {**LABELS, **(read_json(labels_file(root, language), {}) or {})}
                    return self.send_json({"language": language, "labels": labels, "model": config["model"],
                                           "summarize": claude_command() is not None})
                if path == "/api/jobs":
                    return self.send_json(JOBS.snapshot())
                if path == "/api/videos":
                    return self.send_json(list_videos(root, language))
                if path == "/api/playlists":
                    return self.send_json(list_playlists(root))
                if match := re.fullmatch(r"/api/video/([\w-]+)", path):
                    return self.send_json(video_payload(root, match[1], language))
                if match := re.fullmatch(r"/api/thumb/([\w-]+)", path):
                    return self.send_body((video_folder(root, match[1]) / "thumbnail.jpg").read_bytes(), "image/jpeg",
                                          cache="max-age=86400")
            except (Fail, OSError) as e:
                return self.send_json({"error": str(e)}, 404)
            self.send_error(404)

        def send_json(self, data, status=200):
            self.send_body(json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8",
                           status)

        def send_body(self, body, content_type, status=200, cache="no-store"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return Handler


# The running server reports which version of this script it runs, so start can replace an outdated one.
SCRIPT_STAMP = f"{SCRIPT.stat().st_mtime_ns}:{VIEWER.stat().st_mtime_ns if VIEWER.exists() else 0}"


def server_status(port):
    """The health answer of a viewer server on the port, or None when none answers."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=2) as response:
            data = json.load(response)
    except (OSError, ValueError):
        return None
    return data if data.get("app") == "yt-summary" else None


def stop_server(status):
    import signal
    os.kill(status["pid"], signal.SIGTERM)
    for _ in range(20):
        if not server_status(load_config()["port"]):
            return
        time.sleep(0.25)


def spawn_server():
    """Run `serve` as a background process that outlives this command and the console."""
    python = Path(sys.executable)
    windowless = python.with_name("pythonw.exe")
    command = [str(windowless if windowless.exists() else python), str(SCRIPT), "serve"]
    SERVER_LOG.parent.mkdir(parents=True, exist_ok=True)
    log = SERVER_LOG.open("a", encoding="utf-8")
    options = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": log, "close_fds": True}
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        try:  # leave the job of the calling console, so closing it does not stop the server
            subprocess.Popen(command, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **options)
        except OSError:
            subprocess.Popen(command, creationflags=flags, **options)
    else:
        subprocess.Popen(command, start_new_session=True, **options)


# ---------- commands ----------

def cmd_config(args):
    """Show or change this skill's section of the shared config; other sections stay as they are."""
    stored = read_json(CONFIG_PATH, {}) or {}
    section = stored.setdefault("summary", {})
    changes = {"language": args.set_language and args.set_language.strip().lower(),
               "model": args.set_model and args.set_model.strip().lower(),
               "parallel": args.set_parallel, "port": args.set_port}
    changes = {key: value for key, value in changes.items() if value}
    if changes:
        section.update(changes)
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(stored, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "config": str(CONFIG_PATH),
        "output_dir": stored.get("output_dir"),
        "summary": {**DEFAULT_SUMMARY_CONFIG, **section},
        "language_set": "language" in section,
        "model_set": "model" in section,
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_find(args):
    config = load_config()
    root = archive_root(config)
    words = plain(" ".join(args.words)).split()
    channels = [c for c in list_channels(root) if matches(words, c.get("title"), c.get("handle"))]
    playlists = [p for p in list_playlists(root) if matches(words, p["title"])]
    videos = [v for v in list_videos(root, config["language"]) if matches(words, v["title"], v["channel"])]
    for c in channels:
        print(f"Channel: {c.get('title')} · {c.get('handle') or ''} · {c.get('id')}")
    for p in playlists:
        print(f"Playlist: {p['title']} · {len(p['videos'])} videos · {p['id']}")
    for v in sorted(videos, key=lambda v: v["upload_date"], reverse=True)[:30]:
        state = " · summary" if v["summary"] else ("" if v["transcript"] else " · no transcript")
        print(f"Video: {v['id']} · {v['upload_date']} · {hms(v['duration'])} · {v['type']} · {v['channel']} · "
              f"{v['title']}{state}")
    if len(videos) > 30:
        print(f"… and {len(videos) - 30} more videos; add words to narrow it down")
    print(f"Found: {len(channels)} channels, {len(playlists)} playlists, {len(videos)} videos")
    return 0


TYPE_NAMES = {"video": "videos", "short": "shorts", "stream": "streams", "podcast": "podcasts"}


def cmd_plan(args):
    config = load_config()
    root = archive_root(config)
    language = config["language"]
    kind, list_id, title, checked_at = resolve_list(root, args.target)
    videos = list_videos(root, language)
    missing = 0
    if kind == "channel":
        chosen = sorted((v for v in videos if v["channel_id"] == list_id), key=lambda v: v["upload_date"], reverse=True)
    else:
        by_id = {v["id"]: v for v in videos}
        order = next(p for p in list_playlists(root) if p["id"] == list_id)["videos"]
        chosen = [by_id[i] for i in order if i in by_id]
        missing = len(order) - len(chosen)
    todo = [v for v in chosen if v["transcript"] and (args.refresh or not v["summary"])]
    summarized = sum(1 for v in chosen if v["transcript"] and v["summary"])
    no_transcript = sum(1 for v in chosen if not v["transcript"])

    checked = f" · last checked with yt-fetch: {checked_at[:10]}" if checked_at else ""
    print(f"Target: {kind} {title} ({list_id}){checked}")
    print(f"Videos in the archive: {len(chosen)}")
    if missing:
        print(f"Not in the archive: {missing} videos of the playlist (yt-fetch downloads them)")
    if not todo:
        print("Nothing to summarize." + (f" {summarized} videos already have a summary." if summarized else ""))
        print(f"Page: {list_page(config['port'], kind, list_id)}")
        return 0

    rows = []
    for type_, name in TYPE_NAMES.items():
        of_type = [v for v in todo if v["type"] == type_]
        if of_type:
            rows.append((name, len(of_type), hms(sum(v["duration"] for v in of_type))))
    others = [v for v in todo if v["type"] not in TYPE_NAMES]
    if others:
        rows.append(("other", len(others), hms(sum(v["duration"] for v in others))))
    rows.append(("total", len(todo), hms(sum(v["duration"] for v in todo))))
    widths = [max(len(str(r[i])) for r in rows + [("To summarize", "Count", "Total")]) for i in range(3)]
    for row in [("To summarize", "Count", "Total")] + rows:
        print(" | ".join(str(cell).ljust(width) for cell, width in zip(row, widths)).rstrip())
    skipped = [f"{summarized} with a summary" if summarized and not args.refresh else "",
               f"{no_transcript} without transcript" if no_transcript else ""]
    if any(skipped):
        print("Skipped: " + ", ".join(s for s in skipped if s))

    sample = []
    if len(todo) > 3:
        def views(v):
            info = read_json(root / "channels" / v["channel_id"] / v["id"] / "info.json", {}) or {}
            return info.get("view_count") or 0
        sample = sorted(todo, key=views, reverse=True)[:3]
        print("Sample (3 most viewed):")
        for number, v in enumerate(sample, 1):
            print(f"  {number}. {v['title']} · {hms(v['duration'])}")

    created = datetime.now(timezone.utc)
    plan_path = root / ".plans" / f"summary-{created:%Y%m%d-%H%M%S}.json"
    number = 1
    while plan_path.exists():
        number += 1
        plan_path = plan_path.with_name(f"summary-{created:%Y%m%d-%H%M%S}-{number}.json")
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps({
        "created_at": created.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "created": created.timestamp(),
        "kind": kind, "id": list_id, "title": title, "language": language, "refresh": args.refresh,
        "videos": [v["id"] for v in todo],
        "sample": [v["id"] for v in sample],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Plan: {plan_path}")
    print(f"Page: {list_page(config['port'], kind, list_id)}")
    return 0


def cmd_status(args):
    """Done means the video got its summary after the plan was made."""
    plan = read_json(args.plan)
    if not isinstance(plan, dict):
        raise Fail(f"{args.plan} does not exist or is not a plan")
    root = archive_root(load_config())
    ids = plan["sample"] if args.sample and plan["sample"] else plan["videos"]
    done, remaining = [], []
    for video_id in ids:
        summary = video_folder(root, video_id) / f"summary.{plan['language']}.json"
        is_done = summary.exists() and summary.stat().st_mtime >= plan["created"]
        (done if is_done else remaining).append(video_id)
    print(f"Done: {len(done)}/{len(ids)} · remaining: {len(remaining)}")
    if remaining:
        print("Remaining: " + " ".join(remaining))
    if args.sample and plan["sample"]:
        print(f"Rest of the plan after the sample: {len(plan['videos']) - len(plan['sample'])}")
    return 0


def cmd_info(args):
    config = load_config()
    root = archive_root(config)
    folder = video_folder(root, args.video_id)
    raw, transcript = transcript_file(folder)
    language = config["language"]
    labels = labels_file(root, language)
    translated = read_json(labels, {}) or {}
    missing = {} if language == "en" else {k: v for k, v in LABELS.items() if k not in translated}
    print(json.dumps({
        "folder": str(folder),
        "info": str(folder / "info.json"),
        "description": str(folder / "description.txt"),
        "transcript_md": str(raw.with_suffix(".md")),
        "transcript_json3": str(raw),
        "transcript": transcript,
        "summary_language": language,
        "summary_json": str(folder / f"summary.{language}.json"),
        "summary_exists": (folder / f"summary.{language}.json").exists(),
        "page": f"http://127.0.0.1:{config['port']}/v/{args.video_id}",
        "labels_file": str(labels),
        "labels_to_translate": missing,
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_locate(args):
    root = archive_root(load_config())
    raw, _ = transcript_file(video_folder(root, args.video_id))
    words = timed_words(raw)
    for phrase in args.phrases:
        hits = locate(words, phrase)
        shown = ", ".join(f"{h} ({hms(h)})" for h in hits[:5]) + (" …" if len(hits) > 5 else "")
        print(f"{phrase!r}: {shown or 'not found'}")
    return 0


def cmd_keep(args):
    """Move the current summary aside under its creation date, so a new analysis never overwrites it."""
    config = load_config()
    folder = video_folder(archive_root(config), args.video_id)
    language = config["language"]
    current = folder / f"summary.{language}.json"
    if not current.exists():
        print("No summary yet, nothing to keep.")
        return 0
    created = (read_json(current, {}) or {}).get("created_at") or ""
    stamp = created[:10] or datetime.fromtimestamp(current.stat().st_mtime, timezone.utc).strftime("%Y-%m-%d")
    number = 1
    while (folder / f"summary.{language}.{stamp}{'' if number == 1 else f'-{number}'}.json").exists():
        number += 1
    stamp += "" if number == 1 else f"-{number}"
    target = folder / f"summary.{language}.{stamp}.json"
    current.rename(target)
    print(f"Kept: {target}")
    return 0


def cmd_check(args):
    config = load_config()
    folder = video_folder(archive_root(config), args.video_id)
    source = folder / f"summary.{config['language']}.json"
    summary = read_json(source)
    if not isinstance(summary, dict):
        raise Fail(f"{source} does not exist or is not valid JSON")
    duration = (read_json(folder / "info.json", {}) or {}).get("duration") or 0
    problems = summary_problems(summary, duration)
    for problem in problems:
        print(f"PROBLEM: {problem}")
    if problems:
        return 1
    print(f"OK: {len(summary['topics'])} topics, {len(summary['ideas'])} ideas")
    return 0


def cmd_start(args):
    config = load_config()
    archive_root(config)
    port = config["port"]
    status = server_status(port)
    if status and status.get("script") != SCRIPT_STAMP:
        if status.get("busy"):  # never cut off summaries being made; the update waits for the next start
            print(f"Note: the server runs an older version but is making {status['busy']} summaries; not restarted.")
        else:
            stop_server(status)  # runs an older version of the skill
            status = None
    if status:
        print(f"Server: http://127.0.0.1:{port}/ (already running)")
    else:
        spawn_server()
        for _ in range(40):
            if server_status(port):
                break
            time.sleep(0.25)
        else:
            raise Fail(f"the server did not start on port {port}; see {SERVER_LOG}")
        print(f"Server: http://127.0.0.1:{port}/ (started)")
    url = f"http://127.0.0.1:{port}/" + (f"v/{args.video_id}" if args.video_id else "")
    if args.video_id:
        print(f"Video: {url}")
    if args.open:
        import webbrowser
        webbrowser.open(url)
    return 0


def cmd_stop(args):
    status = server_status(load_config()["port"])
    if not status:
        print("Server is not running.")
        return 0
    stop_server(status)
    print("Server stopped.")
    return 0


def cmd_serve(args):
    import http.server
    config = load_config()
    archive_root(config)
    # On Windows address reuse lets a second server bind a port that is already taken; fail instead.
    http.server.ThreadingHTTPServer.allow_reuse_address = os.name != "nt"
    server = http.server.ThreadingHTTPServer(("127.0.0.1", config["port"]), make_handler(config["port"]))
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} serving http://127.0.0.1:{config['port']}/ (pid {os.getpid()})",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def main():
    if sys.stdout:  # pythonw has no console
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    config_parser = commands.add_parser("config")
    config_parser.add_argument("--set-language")
    config_parser.add_argument("--set-model")
    config_parser.add_argument("--set-parallel", type=int)
    config_parser.add_argument("--set-port", type=int)
    commands.add_parser("find").add_argument("words", nargs="+")
    plan_parser = commands.add_parser("plan")
    plan_parser.add_argument("target")
    plan_parser.add_argument("--refresh", action="store_true")
    status_parser = commands.add_parser("status")
    status_parser.add_argument("plan")
    status_parser.add_argument("--sample", action="store_true")
    for name in ("info", "keep", "check"):
        commands.add_parser(name).add_argument("video_id")
    locate_parser = commands.add_parser("locate")
    locate_parser.add_argument("video_id")
    locate_parser.add_argument("phrases", nargs="+")
    start_parser = commands.add_parser("start")
    start_parser.add_argument("video_id", nargs="?")
    start_parser.add_argument("--open", action="store_true")
    commands.add_parser("stop")
    commands.add_parser("serve")

    # Video IDs may start with "-" (e.g. -LJhQyfiFHU); argparse would take such an ID for an option.
    argv = sys.argv[1:]
    if len(argv) > 1 and argv[0] in ("info", "keep", "check", "locate", "start") \
            and argv[1].startswith("-") and argv[1] not in ("-h", "--help", "--open", "--"):
        argv.insert(1, "--")
    args = parser.parse_args(argv)
    handler = {"config": cmd_config, "find": cmd_find, "plan": cmd_plan, "status": cmd_status, "info": cmd_info, "locate": cmd_locate, "keep": cmd_keep, "check": cmd_check,
               "start": cmd_start, "stop": cmd_stop, "serve": cmd_serve}[args.command]
    try:
        return handler(args)
    except Fail as e:
        print(f"ERROR: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
