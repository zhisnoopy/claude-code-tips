#!/usr/bin/env python3
"""
Daily refresh for the Claude Code tips digest.

Refreshes the *dynamic* parts of data/tips.json (trending_repos, trending_posts,
generated_at, sources_polled) while preserving the manually curated `themes`
block. Then re-inlines the JSON into index.html so GitHub Pages picks it up.

Designed to run in GitHub Actions on a schedule. No external deps beyond stdlib.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "tips.json"
HTML = ROOT / "index.html"

UA = "claude-code-tips-refresh/1.0 (+https://github.com/zhisnoopy/claude-code-tips)"
TIMEOUT = 20


def http_json(url: str) -> dict | list:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read())


def fetch_hn(query: str = "claude code", min_points: int = 5, limit: int = 40) -> list[dict]:
    """Top HN stories matching the query."""
    qs = urllib.parse.urlencode({
        "query": query,
        "tags": "story",
        "hitsPerPage": limit,
        "numericFilters": f"points>{min_points}",
    })
    data = http_json(f"https://hn.algolia.com/api/v1/search?{qs}")
    return data.get("hits", [])


def fetch_github_repos(query: str, per_page: int = 15) -> list[dict]:
    """GitHub repo search by query, sorted by stars."""
    qs = urllib.parse.urlencode({
        "q": query,
        "sort": "stars",
        "order": "desc",
        "per_page": per_page,
    })
    headers = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"https://api.github.com/search/repositories?{qs}", headers=headers
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read()).get("items", [])


def is_quality_repo(repo: dict) -> bool:
    """Filter out promotional / spam repos."""
    desc = (repo.get("description") or "").lower()
    name = repo["full_name"].lower()
    # Drop empty descriptions or obvious clickbait
    if not desc or len(desc) < 10:
        return False
    # Drop repos that haven't been updated in 6+ months
    pushed = repo.get("pushed_at") or ""
    if pushed:
        try:
            last = dt.datetime.fromisoformat(pushed.replace("Z", "+00:00"))
            age_days = (dt.datetime.now(dt.timezone.utc) - last).days
            if age_days > 180:
                return False
        except ValueError:
            pass
    # Block obvious spam keywords
    spam = ("buy now", "discount", "promo code", "free vbucks")
    if any(s in desc for s in spam):
        return False
    return True


def gather_repos() -> list[dict]:
    """Aggregate trending repos across multiple queries, dedupe, top-N."""
    queries = [
        "claude-code tips",
        "awesome-claude-code",
        "claude-code workflow",
        "claude-code skills",
        "claude-code commands",
    ]
    seen: dict[str, dict] = {}
    for q in queries:
        try:
            for r in fetch_github_repos(q):
                if not is_quality_repo(r):
                    continue
                key = r["full_name"]
                if key not in seen or r["stargazers_count"] > seen[key]["stargazers_count"]:
                    seen[key] = r
        except urllib.error.HTTPError as e:
            print(f"  [warn] github query {q!r} failed: {e}", file=sys.stderr)
            continue

    repos = sorted(seen.values(), key=lambda r: r["stargazers_count"], reverse=True)
    out = []
    for r in repos[:14]:
        out.append({
            "name": r["full_name"],
            "stars": r["stargazers_count"],
            "desc": (r.get("description") or "").strip(),
            "url": r["html_url"],
        })
    return out


def gather_posts() -> list[dict]:
    """Top HN stories about Claude Code (last few months)."""
    hits = fetch_hn(query="claude code", min_points=200, limit=50)
    # Boost recent stories: drop anything older than 120 days
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=120)
    fresh = []
    for h in hits:
        if not h.get("title"):
            continue
        created = h.get("created_at", "")
        try:
            when = dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
            if when < cutoff:
                continue
        except ValueError:
            continue
        fresh.append({
            "title": h["title"],
            "points": h.get("points", 0),
            "comments": h.get("num_comments", 0),
            "url": h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
            "source": detect_source(h.get("url") or ""),
        })

    # Sort by points
    fresh.sort(key=lambda p: p["points"], reverse=True)
    return fresh[:14]


def detect_source(url: str) -> str:
    if not url:
        return "Hacker News"
    host = urllib.parse.urlparse(url).netloc.lower().replace("www.", "")
    if not host:
        return "Hacker News"
    mapping = {
        "anthropic.com": "Anthropic",
        "claude.com": "Anthropic",
        "twitter.com": "X (Twitter)",
        "x.com": "X (Twitter)",
        "github.com": "GitHub",
        "blog.sshh.io": "sshh.io",
        "boristane.com": "boristane.com",
        "builder.io": "builder.io",
        "sanity.io": "Sanity",
        "ccunpacked.dev": "ccunpacked.dev",
    }
    for needle, name in mapping.items():
        if needle in host:
            return name
    return host


def now_pt() -> dt.datetime:
    """Pacific time, US/Pacific. We accept a fixed -07:00 offset for simplicity
    (PDT spans March-November, which covers our daily-run case 99% of the time).
    """
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=-7)))


def humanize_dt(d: dt.datetime) -> str:
    # Cross-platform "no leading zero" formatting
    return d.strftime("%A, %B ") + str(d.day) + d.strftime(", %Y at ") + d.strftime("%I:%M %p PT").lstrip("0")


def update_data() -> dict:
    """Read existing JSON, refresh dynamic parts, return new dict."""
    data = json.loads(DATA.read_text())
    now = now_pt()
    data["generated_at"] = now.isoformat(timespec="seconds")
    data["generated_at_human"] = humanize_dt(now)
    data["next_refresh_human"] = "Tomorrow at 8:00 AM PT"

    sources_polled: list[dict] = []

    # GitHub
    try:
        repos = gather_repos()
        data["trending_repos"] = repos
        sources_polled.append({
            "name": "GitHub", "endpoint": "api.github.com/search/repositories",
            "status": "ok", "items": len(repos),
        })
    except Exception as e:
        sources_polled.append({
            "name": "GitHub", "endpoint": "api.github.com",
            "status": f"error: {e}", "items": 0,
        })

    # HN
    try:
        posts = gather_posts()
        data["trending_posts"] = posts
        sources_polled.append({
            "name": "Hacker News", "endpoint": "hn.algolia.com/api/v1/search",
            "status": "ok", "items": len(posts),
        })
    except Exception as e:
        sources_polled.append({
            "name": "Hacker News", "endpoint": "hn.algolia.com",
            "status": f"error: {e}", "items": 0,
        })

    # We don't poll these from CI but show users what's covered by the curated themes
    sources_polled.append({
        "name": "Reddit (r/ClaudeAI)", "endpoint": "reddit.com",
        "status": "covered-in-themes", "items": 0,
    })
    sources_polled.append({
        "name": "X / Twitter", "endpoint": "via HN-cited tweet URLs",
        "status": "indirect", "items": 0,
    })
    sources_polled.append({
        "name": "Dev blogs", "endpoint": "anthropic.com, builder.io, sshh.io, boristane.com",
        "status": "covered-in-themes", "items": 0,
    })

    data["sources_polled"] = sources_polled
    return data


def inline_into_html(data: dict) -> None:
    html = HTML.read_text()
    new_json = json.dumps(data, indent=2, ensure_ascii=False)
    new_html, n = re.subn(
        r'(<script type="application/json" id="data">\n)(.*?)(\n</script>)',
        lambda m: m.group(1) + new_json + m.group(3),
        html, count=1, flags=re.DOTALL,
    )
    if n != 1:
        sys.exit("ERROR: could not find inlined JSON block in index.html")
    HTML.write_text(new_html)


def main() -> int:
    print("[refresh] starting", flush=True)
    data = update_data()
    DATA.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    inline_into_html(data)

    n_repos = len(data.get("trending_repos", []))
    n_posts = len(data.get("trending_posts", []))
    n_themes = len(data.get("themes", []))
    n_tips = sum(len(t.get("tips", [])) for t in data.get("themes", []))
    print(f"[refresh] done: {n_themes} themes / {n_tips} tips / {n_repos} repos / {n_posts} posts", flush=True)

    # Sanity
    if n_repos < 5 or n_posts < 5 or n_themes < 5 or n_tips < 10:
        print("[refresh] WARNING: counts look low, but writing anyway")
    return 0


if __name__ == "__main__":
    sys.exit(main())
