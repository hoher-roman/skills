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
