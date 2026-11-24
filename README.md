# skills

AI agent skills for everyday work, invoked as slash commands.

Skills are grouped into categories under `skills/<category>/<skill>/SKILL.md`.

## Categories

### Analysis

Skills that inspect and report. They never modify data.

| Skill | What it does |
|---|---|
| `/analyze-local <path or description>` | Analyzes a file or directory on the local filesystem |
| `/analyze-web <URL or description>` | Gives a brief overview of a website and the company behind it |

### YouTube

Skills that work with a local text archive of YouTube content: transcripts, descriptions and metadata. No video or audio is ever downloaded. They share one config file, `~/.yt-skills/config.json`.

| Skill | What it does |
|---|---|
| `/yt-fetch <URL of a video, playlist or channel>` | Saves the transcript, description and metadata of each video into the local archive and keeps it up to date |
| `/yt-summary <video, channel or playlist: link, ID or name>` | Summarizes archived videos (overview, topics, main ideas with their reasoning) and shows each under the embedded video, every point linked to its time. Also opens a local viewer of the whole archive |

`yt-fetch` needs Python 3 and Node.js or Deno on PATH. On first use the skill creates its own virtual environment in `.venv` from `requirements.txt`. `yt-summary` needs only Python 3; its viewer runs as a small local web server on `127.0.0.1`.

### News

| Skill | What it does |
|---|---|
| `/news ["skript" \| "zdroje" \| "pridaj zdroj <url>"]` | Downloads current news from configured sources and shows them on a local HTML page, each story in one or two Slovak sentences with its area, region and a link to the original, in columns of general news and IT (programming, AI, cloud, security) side by side, each with a summary of the day as the first tab, filters and a daily archive. Also adds, removes and checks sources |

The config is in `~/.news/config/`: `settings.toml` (summary mode, time window, sections and areas) and one file per technology of getting news, `rss.toml` (RSS, Atom, JSON Feed), `sitemap.toml` (Google News sitemaps), `pages.toml` (all articles from a plain sitemap, with title, perex and time from the head of each article page) and `html.toml` (links on a plain page). A new technology is a module in `fetchers/` plus a config file of the same name. In `claude` mode, subagents write the summaries, join reports of the same event from different sources and languages into one story, and write the summary of the day; in `script` mode the perex of the source is used, the summary of the day is the stories most sources report, and no AI is involved. `news.cmd` refreshes the overview without Claude. Only GET requests to the configured sources are made; needs only Python 3.11+.
