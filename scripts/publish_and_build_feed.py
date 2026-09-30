"""Upload every mp3 waiting in pending/ as a release asset, prune anything
aged out past the 30-episode window, and rebuild feed.xml from whatever
assets remain -- so the feed can never reference an episode that doesn't
exist.

Runs inside this repo's own GitHub Actions workflow (publish-episode.yml),
never from the ai-news-pipeline routine directly -- that routine runs inside
a Claude Code cloud sandbox whose egress proxy rejects the binary POST body a
release-asset upload needs. A GitHub Actions runner has no such proxy and is
already authenticated via GITHUB_TOKEN, so this shells out to the `gh` CLI
rather than reimplementing the GitHub API over raw HTTP.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

CYPRUS = ZoneInfo("Asia/Nicosia")
RELEASE_TAG = "episodes"
KEEP = 30
FEED_TITLE = "Daily AI Brief"
FEED_LINK = "https://nikosant03.github.io/daily-ai-brief-feed/"
PENDING_DIR = Path("pending")
DATED_MP3 = re.compile(r"^\d{4}-\d{2}-\d{2}\.mp3$")


def _gh(*args: str) -> str:
    """Run `gh` and, on failure, raise something that says why.

    `check=True` with `capture_output=True` raised a CalledProcessError whose
    message is only the exit code and the argv — gh's own explanation went
    into the captured stderr and was thrown away. The 2026-09-28 failure log
    therefore showed a traceback ending in "returned non-zero exit status 1"
    and nothing about the cause. Put the stderr in the exception."""
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"`gh {' '.join(args)}` failed (exit {result.returncode}): "
            f"{result.stderr.strip() or '<no stderr>'}"
        )
    return result.stdout


def ensure_release_exists() -> None:
    result = subprocess.run(["gh", "release", "view", RELEASE_TAG], capture_output=True)
    if result.returncode != 0:
        _gh("release", "create", RELEASE_TAG, "--title", "Episodes", "--notes", "Rolling episode window.")


def upload_pending_episodes() -> tuple[list[Path], list[str]]:
    """Publish every usable mp3 waiting in pending/, and report the rest.

    This loop used to abort on the first file gh refused, which made one bad
    render block every later day's episode as well: a 0-byte 2026-09-28.mp3
    stalled the whole feed at 2026-09-25 until 2026-10-01, with the perfectly
    good 2026-09-30 episode queued behind it. A single unpublishable file must
    cost that one day only, so problems are collected and returned rather than
    raised — main() still exits non-zero afterwards so the run goes red and
    GitHub emails about it."""
    uploaded: list[Path] = []
    problems: list[str] = []
    for mp3 in sorted(PENDING_DIR.glob("*.mp3")):
        # A 0-byte file is a failed text-to-speech render, not an episode. It
        # has no business in a podcast feed even if gh would take it, so it is
        # discarded here rather than retried forever on every later push.
        if mp3.stat().st_size == 0:
            problems.append(f"{mp3.name} was empty (0 bytes) — discarded, not published")
            mp3.unlink()
            continue
        try:
            _gh("release", "upload", RELEASE_TAG, str(mp3), "--clobber")
        except RuntimeError as exc:
            problems.append(f"{mp3.name} failed to upload: {exc}")
            continue
        uploaded.append(mp3)
    return uploaded, problems


def list_dated_assets() -> list[dict]:
    release_id = json.loads(_gh("api", f"repos/{{owner}}/{{repo}}/releases/tags/{RELEASE_TAG}"))["id"]
    assets = json.loads(_gh("api", f"repos/{{owner}}/{{repo}}/releases/{release_id}/assets"))
    # Ignore anything that isn't a YYYY-MM-DD.mp3 episode (e.g. a stray
    # manually-uploaded test asset) -- it has no parseable date and must
    # never reach the feed or the aging-out sort.
    return [a for a in assets if DATED_MP3.match(a["name"])]


def delete_aged_out(assets: list[dict]) -> list[dict]:
    ordered = sorted(assets, key=lambda a: a["name"])
    stale = ordered[: max(0, len(ordered) - KEEP)]
    for asset in stale:
        _gh("api", "-X", "DELETE", f"repos/{{owner}}/{{repo}}/releases/assets/{asset['id']}")
    stale_names = {a["name"] for a in stale}
    return [a for a in assets if a["name"] not in stale_names]


def _rfc822_date(name: str) -> str:
    date_part = name.removesuffix(".mp3")
    dt = datetime.strptime(date_part, "%Y-%m-%d").replace(hour=7, tzinfo=CYPRUS)
    return dt.strftime("%a, %d %b %Y %H:%M:%S %z")


def build_feed_xml(assets: list[dict]) -> str:
    ordered = sorted(assets, key=lambda a: a["name"], reverse=True)
    items = []
    for asset in ordered:
        date_part = asset["name"].removesuffix(".mp3")
        items.append(
            f"""    <item>
      <title>AI Brief — {date_part}</title>
      <enclosure url="{asset['browser_download_url']}" length="{asset['size']}" type="audio/mpeg"/>
      <pubDate>{_rfc822_date(asset['name'])}</pubDate>
      <guid>{asset['name']}</guid>
    </item>"""
        )
    items_xml = "\n".join(items)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>{FEED_TITLE}</title>
    <link>{FEED_LINK}</link>
    <description>Daily AI news bulletin.</description>
{items_xml}
  </channel>
</rss>
"""


def main() -> int:
    ensure_release_exists()
    uploaded, problems = upload_pending_episodes()
    # The feed is rebuilt from whatever assets actually exist, so it is still
    # correct (just missing the bad day) when some file could not be published.
    remaining = delete_aged_out(list_dated_assets())
    Path("feed.xml").write_text(build_feed_xml(remaining), encoding="utf-8")
    for mp3 in uploaded:
        mp3.unlink()
    if problems:
        print("PROBLEMS THIS RUN:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
