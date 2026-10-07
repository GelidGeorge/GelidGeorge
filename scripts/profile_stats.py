#!/usr/bin/env python3
"""Generate self-hosted SVG cards for the GitHub profile README.

Fetches data straight from the GitHub GraphQL API (and the public Stack
Exchange API) and renders the SVGs locally, so the profile no longer depends
on third-party card services that go down or get rate-limited.

Usage:
    GITHUB_TOKEN=... python3 scripts/profile_stats.py --user GelidGeorge \
        --stackoverflow 12537452 --out dist

Writes <card>-dark.svg and <card>-light.svg for each card into --out.
Standard library only.
"""

import argparse
import base64
import datetime as dt
import gzip
import json
import os
import re
import sys
import urllib.request
from html import escape

GRAPHQL_URL = "https://api.github.com/graphql"

THEMES = {
    "dark": {
        "bg": "#1a1b27", "border": "#2a2e3f", "title": "#70a5fd", "text": "#c0caf5",
        "muted": "#7a88cf", "accent": "#bf91f3", "fire": "#ff9e64",
        "levels": ["#24283b", "#0e4429", "#006d32", "#26a641", "#39d353"],
    },
    "light": {
        "bg": "#ffffff", "border": "#d0d7de", "title": "#0969da", "text": "#1f2328",
        "muted": "#656d76", "accent": "#8250df", "fire": "#bc4c00",
        "levels": ["#ebedf0", "#9be9a8", "#40c463", "#30a14e", "#216e39"],
    },
}
FONT = "font-family:'Segoe UI',Ubuntu,'Helvetica Neue',Sans-Serif"


# ---------------------------------------------------------------- fetching

def graphql(token, query, variables):
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(GRAPHQL_URL, data=body, headers={
        "Authorization": f"bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "profile-stats",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.load(resp)
    if payload.get("errors"):
        raise RuntimeError(json.dumps(payload["errors"]))
    return payload["data"]


USER_QUERY = """
query($login: String!, $cursor: String) {
  user(login: $login) {
    name
    createdAt
    followers { totalCount }
    repositoriesContributedTo(contributionTypes: [COMMIT, PULL_REQUEST, ISSUE, REPOSITORY]) { totalCount }
    repositories(first: 100, after: $cursor, ownerAffiliations: OWNER, isFork: false) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        stargazerCount
        languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
          edges { size node { name color } }
        }
      }
    }
  }
}
"""

YEAR_QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions
      totalPullRequestContributions
      totalIssueContributions
      totalPullRequestReviewContributions
      restrictedContributionsCount
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date contributionCount } }
      }
    }
  }
}
"""


def fetch_github(token, login):
    user, repos, cursor = None, [], None
    while True:
        data = graphql(token, USER_QUERY, {"login": login, "cursor": cursor})["user"]
        user = user or data
        page = data["repositories"]
        repos += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]

    stars = sum(r["stargazerCount"] for r in repos)
    lang_bytes, lang_color = {}, {}
    for repo in repos:
        for edge in repo["languages"]["edges"]:
            name = edge["node"]["name"]
            lang_bytes[name] = lang_bytes.get(name, 0) + edge["size"]
            lang_color[name] = edge["node"]["color"] or "#858585"

    # contributionsCollection spans at most one year, so walk year by year
    # from account creation to collect every day for the streak.
    now = dt.datetime.now(dt.timezone.utc)
    start = dt.datetime.fromisoformat(user["createdAt"].replace("Z", "+00:00"))
    def collection(frm, to):
        return graphql(token, YEAR_QUERY, {
            "login": login, "from": frm.isoformat(), "to": to.isoformat(),
        })["user"]["contributionsCollection"]

    days = {}
    totals = {"commits": 0, "prs": 0, "issues": 0, "reviews": 0}
    cursor_start = start
    while cursor_start < now:
        cursor_end = min(cursor_start + dt.timedelta(days=365), now)
        coll = collection(cursor_start, cursor_end)
        totals["commits"] += coll["totalCommitContributions"] + coll["restrictedContributionsCount"]
        totals["prs"] += coll["totalPullRequestContributions"]
        totals["issues"] += coll["totalIssueContributions"]
        totals["reviews"] += coll["totalPullRequestReviewContributions"]
        for week in coll["contributionCalendar"]["weeks"]:
            for day in week["contributionDays"]:
                days[day["date"]] = day["contributionCount"]
        cursor_start = cursor_end
    this_year = collection(now - dt.timedelta(days=365), now)

    return {
        "name": user["name"] or login,
        "login": login,
        "stars": stars,
        "repos": page["totalCount"],
        "followers": user["followers"]["totalCount"],
        "contributed_to": user["repositoriesContributedTo"]["totalCount"],
        "commits_year": this_year["totalCommitContributions"] + this_year["restrictedContributionsCount"],
        "prs_year": this_year["totalPullRequestContributions"],
        "issues_year": this_year["totalIssueContributions"],
        "reviews_year": this_year["totalPullRequestReviewContributions"],
        "totals": totals,
        "created": start.date().isoformat(),
        "years": (now - start).days / 365.25,
        "languages": sorted(
            ({"name": n, "bytes": b, "color": lang_color[n]} for n, b in lang_bytes.items()),
            key=lambda l: -l["bytes"],
        ),
        "days": dict(sorted(days.items())),
    }


def fetch_stackoverflow(user_id):
    url = f"https://api.stackexchange.com/2.3/users/{user_id}?site=stackoverflow"
    req = urllib.request.Request(url, headers={"User-Agent": "profile-stats", "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    items = json.loads(raw)["items"]
    if not items:
        raise RuntimeError(f"Stack Overflow user {user_id} not found")
    u = items[0]
    return {
        "name": u["display_name"],
        "reputation": u["reputation"],
        "gold": u["badge_counts"]["gold"],
        "silver": u["badge_counts"]["silver"],
        "bronze": u["badge_counts"]["bronze"],
    }


def fetch_wakatime(api_key=None, user=None):
    """Coding time from WakaTime.

    With an API key, reads the key owner's stats. Without one, falls back to
    the public stats of `user` (works only if "public stats" is enabled in
    the WakaTime profile settings).
    """
    who = "current" if api_key else user
    if not who:
        raise RuntimeError("no WakaTime API key or user")
    headers = {"User-Agent": "profile-stats"}
    if api_key:
        headers["Authorization"] = "Basic " + base64.b64encode(api_key.encode()).decode()

    def get(path):
        req = urllib.request.Request(f"https://wakatime.com/api/v1/users/{who}/{path}", headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)["data"]

    week = get("stats/last_7_days")
    if not week.get("human_readable_total"):
        raise RuntimeError("WakaTime stats are not ready yet")
    try:
        all_time = get("all_time_since_today")
    except Exception:  # optional: not exposed for public-only access
        all_time = {}
    return {
        "week": week["human_readable_total"],
        "daily": week.get("human_readable_daily_average", ""),
        "all_time": all_time.get("text", ""),
        "since": (all_time.get("range") or {}).get("start_date", ""),
        "languages": [
            {"name": l["name"], "percent": l["percent"], "text": l["text"]}
            for l in week.get("languages", [])[:5]
        ],
    }


# ---------------------------------------------------------------- analysis

def streaks(days):
    """Return (total, current, longest) from a {iso_date: count} map."""
    dates = sorted(days)
    total = sum(days.values())
    longest = run = 0
    for d in dates:
        run = run + 1 if days[d] > 0 else 0
        longest = max(longest, run)
    # The current streak survives an empty "today" (the day isn't over yet).
    current = 0
    for i, d in enumerate(reversed(dates)):
        if days[d] > 0:
            current += 1
        elif i == 0:
            continue
        else:
            break
    return total, current, longest


def fmt(n):
    return f"{n/1000:.1f}k" if n >= 10000 else f"{n:,}"


# ---------------------------------------------------------------- rendering

def card(width, height, theme, title, body):
    t = THEMES[theme]
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">'
        f'<title>{escape(title)}</title>'
        f'<rect x="0.5" y="0.5" rx="6" width="{width-1}" height="{height-1}" '
        f'fill="{t["bg"]}" stroke="{t["border"]}"/>'
        f'<text x="25" y="35" style="{FONT};font-size:18px;font-weight:600" fill="{t["title"]}">'
        f'{escape(title)}</text>{body}</svg>'
    )


def stats_card(d, theme):
    t = THEMES[theme]
    year = dt.date.today().year
    rows = [
        ("Total stars earned", d["stars"]),
        ("Commits (last 12 months)", d["commits_year"]),
        ("Pull requests (last 12 months)", d["prs_year"]),
        ("Issues (last 12 months)", d["issues_year"]),
        ("Code reviews (last 12 months)", d["reviews_year"]),
        ("Contributed to", d["contributed_to"]),
        ("Public repositories", d["repos"]),
    ]
    body = ""
    for i, (label, value) in enumerate(rows):
        y = 66 + i * 20
        body += (
            f'<text x="25" y="{y}" style="{FONT};font-size:14px" fill="{t["text"]}">{escape(label)}</text>'
            f'<text x="470" y="{y}" text-anchor="end" style="{FONT};font-size:14px;font-weight:700" '
            f'fill="{t["accent"]}">{fmt(value)}</text>'
        )
    return card(495, 205, theme, f"{d['name']}'s GitHub stats", body)


def streak_card(d, theme):
    t = THEMES[theme]
    total, current, longest = streaks(d["days"])
    first = next(iter(d["days"]), "")
    cols = [
        (82, fmt(total), "Total contributions", f"since {first}" if first else ""),
        (247, str(current), "Current streak", "days"),
        (412, str(longest), "Longest streak", "days"),
    ]
    body = ""
    for x, value, label, sub in cols:
        color = t["fire"] if label == "Current streak" else t["accent"]
        body += (
            f'<text x="{x}" y="105" text-anchor="middle" style="{FONT};font-size:30px;font-weight:700" '
            f'fill="{color}">{value}</text>'
            f'<text x="{x}" y="140" text-anchor="middle" style="{FONT};font-size:14px" '
            f'fill="{t["text"]}">{label}</text>'
            f'<text x="{x}" y="160" text-anchor="middle" style="{FONT};font-size:12px" '
            f'fill="{t["muted"]}">{sub}</text>'
        )
    body += (
        f'<line x1="165" y1="70" x2="165" y2="165" stroke="{t["border"]}"/>'
        f'<line x1="330" y1="70" x2="330" y2="165" stroke="{t["border"]}"/>'
    )
    return card(495, 205, theme, "Contribution streak", body)


def languages_card(d, theme, top=8):
    t = THEMES[theme]
    langs = d["languages"][:top]
    total = sum(l["bytes"] for l in langs) or 1
    body, x = "", 25.0
    bar_w = 445
    body += f'<clipPath id="bar"><rect x="25" y="55" width="{bar_w}" height="8" rx="4"/></clipPath><g clip-path="url(#bar)">'
    for l in langs:
        w = bar_w * l["bytes"] / total
        body += f'<rect x="{x:.2f}" y="55" width="{w:.2f}" height="8" fill="{l["color"]}"/>'
        x += w
    body += "</g>"
    for i, l in enumerate(langs):
        cx = 25 + (i % 2) * 230
        cy = 90 + (i // 2) * 25
        pct = 100 * l["bytes"] / total
        body += (
            f'<circle cx="{cx+5}" cy="{cy-5}" r="5" fill="{l["color"]}"/>'
            f'<text x="{cx+16}" y="{cy}" style="{FONT};font-size:13px" fill="{t["text"]}">'
            f'{escape(l["name"])} <tspan fill="{t["muted"]}">{pct:.1f}%</tspan></text>'
        )
    return card(495, 205, theme, "Most used languages", body)


def contributions_card(d, theme):
    t = THEMES[theme]
    today = dt.date.today()
    # 53 columns ending this week, Sunday-first like GitHub's own graph.
    end = today + dt.timedelta(days=(5 - today.weekday()) % 7)
    start = end - dt.timedelta(weeks=53) + dt.timedelta(days=1)
    counts = [d["days"].get((start + dt.timedelta(days=i)).isoformat(), 0)
              for i in range((end - start).days + 1)]
    peak = max(counts) or 1
    cell, gap, ox, oy = 11, 3, 25, 70
    body, last_month = "", None
    for i, c in enumerate(counts):
        day = start + dt.timedelta(days=i)
        if day > today:
            continue
        col, row = divmod(i, 7)
        level = 0 if c == 0 else min(4, 1 + int(3 * c / peak))
        x, y = ox + col * (cell + gap), oy + row * (cell + gap)
        body += (f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" rx="2" '
                 f'fill="{t["levels"][level]}"><title>{c} on {day}</title></rect>')
        if row == 0 and day.month != last_month and day.day <= 7:
            body += (f'<text x="{x}" y="{oy-8}" style="{FONT};font-size:10px" '
                     f'fill="{t["muted"]}">{day.strftime("%b")}</text>')
            last_month = day.month
    total = sum(counts)
    body += (f'<text x="{ox}" y="{oy + 7*(cell+gap) + 18}" style="{FONT};font-size:12px" '
             f'fill="{t["muted"]}">{total:,} contributions in the last year</text>')
    width = ox * 2 + 53 * (cell + gap) - gap
    return card(width, oy + 7 * (cell + gap) + 32, theme, "Contribution graph", body)


def stackoverflow_card(so, theme):
    t = THEMES[theme]
    badges = [("#ffcc01", so["gold"]), ("#b4b8bc", so["silver"]), ("#d1a684", so["bronze"])]
    body = (
        f'<text x="25" y="80" style="{FONT};font-size:30px;font-weight:700" fill="{t["accent"]}">'
        f'{fmt(so["reputation"])}</text>'
        f'<text x="25" y="102" style="{FONT};font-size:13px" fill="{t["muted"]}">reputation</text>'
    )
    for i, (color, n) in enumerate(badges):
        x = 25 + i * 70
        body += (f'<circle cx="{x+6}" cy="150" r="6" fill="{color}"/>'
                 f'<text x="{x+18}" y="155" style="{FONT};font-size:14px" fill="{t["text"]}">{n}</text>')
    return card(495, 205, theme, "Stack Overflow", body)


def wakatime_card(w, theme):
    t = THEMES[theme]
    cols = [(25, w["week"], "last 7 days"), (190, w["daily"], "daily average")]
    if w["all_time"]:
        cols.append((330, w["all_time"], f"since {w['since'][:10]}" if w["since"] else "all time"))
    body = ""
    for x, value, label in cols:
        body += (
            f'<text x="{x}" y="68" style="{FONT};font-size:15px;font-weight:700" fill="{t["accent"]}">'
            f'{escape(value)}</text>'
            f'<text x="{x}" y="86" style="{FONT};font-size:11px" fill="{t["muted"]}">{escape(label)}</text>'
        )
    for i, l in enumerate(w["languages"]):
        y = 112 + i * 18
        bar = 200 * l["percent"] / 100
        body += (
            f'<text x="25" y="{y}" style="{FONT};font-size:12px" fill="{t["text"]}">{escape(l["name"])}</text>'
            f'<rect x="140" y="{y-9}" width="200" height="8" rx="4" fill="{t["border"]}"/>'
            f'<rect x="140" y="{y-9}" width="{bar:.1f}" height="8" rx="4" fill="{t["title"]}"/>'
            f'<text x="470" y="{y}" text-anchor="end" style="{FONT};font-size:11px" '
            f'fill="{t["muted"]}">{escape(l["text"])}</text>'
        )
    return card(495, 205, theme, "Coding time (WakaTime)", body)


# Rank ladder (C .. SSS) and the minimum value needed for each step.
RANKS = ["C", "B", "A", "AA", "AAA", "S", "SS", "SSS"]
TROPHIES = [
    # (title, unit, key, thresholds)
    ("Stars", "stars", "stars", [1, 10, 30, 50, 100, 200, 700, 2000]),
    ("Commits", "commits", "commits", [1, 10, 100, 200, 500, 1000, 2000, 4000]),
    ("Followers", "followers", "followers", [1, 10, 20, 50, 100, 200, 400, 1000]),
    ("Repositories", "repos", "repos", [1, 10, 20, 30, 40, 50, 70, 100]),
    ("Pull Requests", "PRs", "prs", [1, 10, 20, 50, 100, 200, 500, 1000]),
    ("Issues", "issues", "issues", [1, 10, 20, 50, 100, 200, 500, 1000]),
    ("Reviewer", "reviews", "reviews", [1, 10, 20, 50, 100, 200, 500, 1000]),
    ("Streak", "days", "longest", [1, 7, 14, 30, 60, 100, 200, 365]),
    ("Experience", "years", "years", [1, 2, 3, 4, 5, 7, 10, 15]),
    ("Polyglot", "languages", "langs", [1, 3, 5, 7, 10, 12, 15, 20]),
]
RANK_COLORS = {"S": "#e8b923", "A": "#a8b2bd", "B": "#c47e3b", "C": "#c47e3b"}
CUP_BODY = "M-13 -16h26v5c0 10-5 16-13 16s-13-6-13-16zM-2.5 5h5v6h6v4h-17v-4h6z"
CUP_HANDLES = "M-13 -12h-5c0 6 3 9 7 9M13 -12h5c0 6-3 9-7 9"


def trophy_metrics(d):
    _, _, longest = streaks(d["days"])
    t = d["totals"]
    return {"stars": d["stars"], "commits": t["commits"], "followers": d["followers"],
            "repos": d["repos"], "prs": t["prs"], "issues": t["issues"],
            "reviews": t["reviews"], "longest": longest, "years": int(d["years"]),
            "langs": len(d["languages"])}


def rank_of(value, thresholds):
    rank = None
    for name, minimum in zip(RANKS, thresholds):
        if value >= minimum:
            rank = name
    return rank


def trophies_card(d, theme):
    t = THEMES[theme]
    metrics = trophy_metrics(d)
    tile, gap, per_row, ox, oy = 110, 12, 5, 25, 55
    body = ""
    for i, (title, unit, key, thresholds) in enumerate(TROPHIES):
        value = metrics[key]
        rank = rank_of(value, thresholds)
        col, row = i % per_row, i // per_row
        x, y = ox + col * (tile + gap), oy + row * (tile + gap)
        color = RANK_COLORS.get(rank[0], t["muted"]) if rank else t["border"]
        body += (
            f'<rect x="{x}" y="{y}" width="{tile}" height="{tile}" rx="8" fill="none" stroke="{t["border"]}"/>'
            f'<g transform="translate({x + tile/2} {y + 38})" fill="{color}">'
            f'<path d="{CUP_BODY}"/><path d="{CUP_HANDLES}" fill="none" stroke="{color}" stroke-width="2.5"/></g>'
            f'<text x="{x + tile - 10}" y="{y + 20}" text-anchor="end" style="{FONT};font-size:13px;font-weight:800" '
            f'fill="{color if rank else t["muted"]}">{rank or "–"}</text>'
            f'<text x="{x + tile/2}" y="{y + 76}" text-anchor="middle" style="{FONT};font-size:12px;font-weight:600" '
            f'fill="{t["text"]}">{title}</text>'
            f'<text x="{x + tile/2}" y="{y + 94}" text-anchor="middle" style="{FONT};font-size:11px" '
            f'fill="{t["muted"]}">{fmt(value)} {unit}</text>'
        )
    width = ox * 2 + per_row * tile + (per_row - 1) * gap
    rows = -(-len(TROPHIES) // per_row)
    return card(width, oy + rows * (tile + gap) + 13, theme, "Trophies", body)


def badge(label, value, color="#0969da"):
    """Shields-style flat badge; widths estimated for 11px Verdana."""
    def w(text):
        return int(len(text) * 6.6) + 12
    lw, vw = w(label), w(value)
    total = lw + vw
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total}" height="20" role="img" '
        f'aria-label="{escape(label)}: {escape(value)}"><title>{escape(label)}: {escape(value)}</title>'
        f'<clipPath id="r"><rect width="{total}" height="20" rx="3"/></clipPath><g clip-path="url(#r)">'
        f'<rect width="{lw}" height="20" fill="#555"/><rect x="{lw}" width="{vw}" height="20" fill="{color}"/></g>'
        f'<g fill="#fff" text-anchor="middle" font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="11">'
        f'<text x="{lw/2}" y="14">{escape(label)}</text><text x="{lw + vw/2}" y="14">{escape(value)}</text></g></svg>'
    )


def placeholder_card(title, theme):
    return card(495, 205, theme, title, (
        f'<text x="25" y="110" style="{FONT};font-size:14px" fill="{THEMES[theme]["muted"]}">'
        'Stats will appear after the next update.</text>'))


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--user", required=True)
    ap.add_argument("--stackoverflow", help="Stack Overflow numeric user id")
    ap.add_argument("--wakatime-user", help="WakaTime username or user id (public stats fallback)")
    ap.add_argument("--out", default="dist")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("GITHUB_TOKEN is not set")
    os.makedirs(args.out, exist_ok=True)

    data = fetch_github(token, args.user)
    cards = {
        "stats": lambda th: stats_card(data, th),
        "streak": lambda th: streak_card(data, th),
        "languages": lambda th: languages_card(data, th),
        "contributions": lambda th: contributions_card(data, th),
    }
    so = waka = None
    if args.stackoverflow:
        try:
            so = fetch_stackoverflow(args.stackoverflow)
            cards["stackoverflow"] = lambda th: stackoverflow_card(so, th)
        except Exception as exc:  # keep the GitHub cards even if SO is down
            print(f"warning: skipping Stack Overflow card: {exc}", file=sys.stderr)
            if not os.path.exists(os.path.join(args.out, "stackoverflow-dark.svg")):
                cards["stackoverflow"] = lambda th: placeholder_card("Stack Overflow", th)

    if os.environ.get("WAKATIME_API_KEY") or args.wakatime_user:
        try:
            waka = fetch_wakatime(os.environ.get("WAKATIME_API_KEY"), args.wakatime_user)
            cards["wakatime"] = lambda th: wakatime_card(waka, th)
        except Exception as exc:
            print(f"warning: skipping WakaTime card: {exc}", file=sys.stderr)
            # Keep the last published card if there is one; otherwise draw a
            # neutral placeholder so the README never shows a broken image.
            if not os.path.exists(os.path.join(args.out, "wakatime-dark.svg")):
                cards["wakatime"] = lambda th: placeholder_card("Coding time (WakaTime)", th)

    badges = {
        "followers": ("followers", fmt(data["followers"]), "#0969da"),
        "stars": ("stars", fmt(data["stars"]), "#e3b341"),
        "repos": ("public repos", fmt(data["repos"]), "#2da44e"),
        "since": ("on GitHub since", data["created"][:4], "#8250df"),
    }
    if so:
        badges["stackoverflow"] = ("Stack Overflow", f"{fmt(so['reputation'])} rep", "#f48024")
    elif args.stackoverflow and not os.path.exists(os.path.join(args.out, "badge-stackoverflow.svg")):
        badges["stackoverflow"] = ("Stack Overflow", "–", "#f48024")
    if waka:
        # "1,912 hrs 7 mins" -> "1,912 hrs"; fall back to this week's total
        hours = re.match(r"[\d,]+ hrs?", waka["all_time"])
        badges["wakatime"] = ("coded", hours.group(0) if hours else f"{waka['week']} this week", "#1f2328")
    elif not os.path.exists(os.path.join(args.out, "badge-wakatime.svg")):
        badges["wakatime"] = ("coded", "–", "#1f2328")
    for name, (label, value, color) in badges.items():
        path = os.path.join(args.out, f"badge-{name}.svg")
        with open(path, "w", encoding="utf-8") as f:
            f.write(badge(label, value, color))
        print("wrote", path)
    cards["trophies"] = lambda th: trophies_card(data, th)

    for name, render in cards.items():
        for theme in THEMES:
            path = os.path.join(args.out, f"{name}-{theme}.svg")
            with open(path, "w", encoding="utf-8") as f:
                f.write(render(theme))
            print("wrote", path)


if __name__ == "__main__":
    main()
