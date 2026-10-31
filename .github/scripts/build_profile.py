"""Build the assets behind the profile README.

Stars are counted on public repos the user owns, organization repos they have committed to, and other
people's repos they have added substantial code to. Writes into profile/:
  dashboard-light.svg, dashboard-dark.svg  the star dashboard shown in README.md
  stars.md                                 the per-repo breakdown the dashboard links to
  star-history.json                        one date per current star, carried between runs
"""
import bisect
import json
import math
import os
import re
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html import escape

LOGIN = os.environ.get("GITHUB_REPOSITORY_OWNER", "Kyfafyd")

# Lines the user must have added to someone else's personal repo before its stars count.
MIN_ADDITIONS = 100
# Repos to leave out of the total, written as "owner/name".
EXCLUDED = set()
CITATIONS_URL = "https://raw.githubusercontent.com/Kyfafyd/kyfafyd.github.io/main/gs_data_shieldsio.json"
HISTORY = "profile/star-history.json"

REPO_FIELDS = "nameWithOwner url description stargazerCount forkCount isPrivate owner { __typename login }"

PROFILE = """
query($login: String!) {
  user(login: $login) {
    id
    contributionsCollection { contributionYears }
  }
}
"""

OWN_REPOS = """
query($login: String!, $after: String) {
  user(login: $login) {
    repositories(first: 100, after: $after, ownerAffiliations: OWNER, isFork: false) {
      pageInfo { hasNextPage endCursor }
      nodes { %s }
    }
  }
}
""" % REPO_FIELDS

# A contributions window can span at most one year, so the history is walked year by year.
COMMITTED_REPOS = """
query($login: String!, $from: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from) {
      commitContributionsByRepository(maxRepositories: 100) {
        repository { %s }
      }
    }
  }
}
""" % REPO_FIELDS

ADDITIONS = """
query($owner: String!, $name: String!, $author: ID!) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef {
      target {
        ... on Commit {
          history(first: 100, author: {id: $author}) { nodes { additions } }
        }
      }
    }
  }
}
"""


def query(text, variables):
    request = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": text, "variables": variables}).encode(),
        headers={"Authorization": f"bearer {os.environ['GITHUB_TOKEN']}"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.load(response)
    if payload.get("errors"):
        raise RuntimeError(json.dumps(payload["errors"], indent=2))
    return payload["data"]


def own_repos():
    after = None
    while True:
        page = query(OWN_REPOS, {"login": LOGIN, "after": after})["user"]["repositories"]
        yield from page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            return
        after = page["pageInfo"]["endCursor"]


def additions(repo, author):
    owner, name = repo["nameWithOwner"].split("/")
    branch = query(ADDITIONS, {"owner": owner, "name": name, "author": author})["repository"]["defaultBranchRef"]
    return sum(commit["additions"] for commit in branch["target"]["history"]["nodes"]) if branch else 0


def counted_repos():
    user = query(PROFILE, {"login": LOGIN})["user"]
    repos = {repo["nameWithOwner"]: repo for repo in own_repos()}
    checked = set(repos)
    for year in user["contributionsCollection"]["contributionYears"]:
        collection = query(COMMITTED_REPOS, {"login": LOGIN, "from": f"{year}-01-01T00:00:00Z"})["user"]["contributionsCollection"]
        for entry in collection["commitContributionsByRepository"]:
            repo = entry["repository"]
            if repo["nameWithOwner"] in checked:
                continue
            checked.add(repo["nameWithOwner"])
            owner = repo["owner"]
            if owner["__typename"] == "Organization" or (owner["login"] != LOGIN and additions(repo, user["id"]) >= MIN_ADDITIONS):
                repos[repo["nameWithOwner"]] = repo
    return [repo for name, repo in repos.items() if not repo["isPrivate"] and name not in EXCLUDED]


def star_history(repos, today):
    """One date per current star. New stars are dated the day they are first seen; unstars drop the latest dates."""
    try:
        with open(HISTORY) as handle:
            cached = json.load(handle)
    except FileNotFoundError:
        cached = {}
    history = {}
    for repo in repos:
        count = repo["stargazerCount"]
        dates = sorted(cached.get(repo["nameWithOwner"], []))[:count]
        history[repo["nameWithOwner"]] = dates + [today.isoformat()] * (count - len(dates))
    return history


def citations():
    try:
        with urllib.request.urlopen(CITATIONS_URL, timeout=30) as response:
            return int(json.load(response)["message"])
    except (OSError, ValueError, KeyError):
        return None


def project(repo):
    """Name a repo after the project in its description ("[Venue] Name: ..."), falling back to the repo name."""
    description = repo["description"] or ""
    name = re.match(r"(?:\[[^\]]*\]\s*)?([\w.-]+):", description)
    venue = re.match(r"\[([^\]]+)\]", description)
    return (name.group(1) if name else repo["nameWithOwner"].split("/")[1],
            re.sub(r"'20(\d\d)\b", r"'\1", venue.group(1)) if venue else None)


THEMES = {
    "light": {"text": "#1f2328", "muted": "#59636e", "grid": "#d1d9e0", "others": "#c9d1d9"},
    "dark": {"text": "#f0f6fc", "muted": "#9198a1", "grid": "#3d444d", "others": "#4d5560"},
}
# Assigned to the named projects from the newest down, so the latest work sits on top in the warmest color.
PALETTE = ["#f5b544", "#f27a5e", "#d6619e", "#9a6ef0", "#5f86f2"]
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif"
WIDTH, HEIGHT = 880, 370
CHART_LEFT, CHART_RIGHT, CHART_TOP, CHART_BOTTOM = 0, 600, 118, 330
LEGEND_LEFT = 648


def layers(repos, history, named=5):
    projects = {}
    for repo in repos:
        name, venue = project(repo)
        entry = projects.setdefault(name.lower(), {"name": name, "venue": venue, "dates": []})
        entry["venue"] = entry["venue"] or venue
        entry["dates"] += history[repo["nameWithOwner"]]
    ranked = sorted((p for p in projects.values() if p["dates"]), key=lambda p: -len(p["dates"]))
    top = sorted(ranked[:named], key=lambda p: min(p["dates"]))
    others = {"name": "Others", "venue": f"{len(ranked) - len(top)} more projects", "dates": [d for p in ranked[named:] for d in p["dates"]]}
    return ([others] if others["dates"] else []) + top


def nice_step(maximum, lines=4):
    raw = max(maximum / lines, 1)
    magnitude = 10 ** int(math.log10(raw))
    return next(int(step * magnitude) for step in (1, 2, 2.5, 5, 10) if step * magnitude >= raw and (step * magnitude).is_integer())


def curve(points):
    """Bezier segments through points along a monotone cubic, so the curve never overshoots the data."""
    slopes = [(y1 - y0) / (x1 - x0) for (x0, y0), (x1, y1) in zip(points, points[1:])]
    tangents = [slopes[0]] + [(a + b) / 2 if a * b > 0 else 0 for a, b in zip(slopes, slopes[1:])] + [slopes[-1]]
    for i, slope in enumerate(slopes):
        if slope == 0:
            tangents[i] = tangents[i + 1] = 0
            continue
        a, b = tangents[i] / slope, tangents[i + 1] / slope
        if a * a + b * b > 9:
            scale = 3 / math.hypot(a, b)
            tangents[i], tangents[i + 1] = scale * a * slope, scale * b * slope
    return [((x0, y0), (x0 + (x1 - x0) / 3, y0 + t0 * (x1 - x0) / 3), (x1 - (x1 - x0) / 3, y1 - t1 * (x1 - x0) / 3), (x1, y1))
            for (x0, y0), (x1, y1), t0, t1 in zip(points, points[1:], tangents, tangents[1:])]


def path(segments, reverse=False):
    if reverse:
        segments = [(end, c2, c1, begin) for begin, c1, c2, end in reversed(segments)]
    return " ".join(["%.1f,%.1f" % segments[0][0]] + ["C" + " ".join("%.1f,%.1f" % point for point in segment[1:]) for segment in segments])


def render(theme, repos, history, cited, today):
    colors = THEMES[theme]
    stack = layers(repos, history)
    for index, layer in enumerate(reversed(stack)):
        layer["color"] = PALETTE[index] if layer["name"] != "Others" else colors["others"]

    all_dates = sorted(d for layer in stack for d in layer["dates"])
    start = date.fromisoformat(all_dates[0]).replace(day=1)
    months = [date(start.year + (start.month - 1 + i) // 12, (start.month - 1 + i) % 12 + 1, 1)
              for i in range((today.year - start.year) * 12 + today.month - start.month + 1)]
    times = [m for m in months if m < today] + [today]
    span = (today - start).days or 1

    def x(day):
        return CHART_LEFT + (day - start).days / span * (CHART_RIGHT - CHART_LEFT)

    total = len(all_dates)
    step = nice_step(total)
    top_value = step * (int(total * 1.12 // step) + 1)

    def y(value):
        return CHART_BOTTOM - value / top_value * (CHART_BOTTOM - CHART_TOP)

    counts = [0] * len(times)
    lower = curve([(x(t), y(0)) for t in times])
    areas = []
    for layer in stack:
        dates = sorted(layer["dates"])
        counts = [count + bisect.bisect_right(dates, t.isoformat()) for count, t in zip(counts, times)]
        upper = curve([(x(t), y(count)) for t, count in zip(times, counts)])
        areas.append(f'<path d="M{path(upper)} L{path(lower, reverse=True)} Z" fill="{layer["color"]}" fill-opacity="0.88"/>')
        areas.append(f'<path d="M{path(upper)}" fill="none" stroke="{layer["color"]}" stroke-width="1.6"/>')
        lower = upper

    grid = []
    for value in range(step, top_value, step):
        grid.append(f'<line x1="{CHART_LEFT}" x2="{CHART_RIGHT}" y1="{y(value):.1f}" y2="{y(value):.1f}" stroke="{colors["grid"]}" stroke-dasharray="2 4"/>')
        grid.append(f'<text x="{CHART_LEFT}" y="{y(value) - 6:.1f}" class="muted small">{value:,}</text>')
    grid.append(f'<line x1="{CHART_LEFT}" x2="{CHART_RIGHT}" y1="{CHART_BOTTOM}" y2="{CHART_BOTTOM}" stroke="{colors["grid"]}"/>')
    for year in range(start.year + 1, today.year + 1):
        tick = x(date(year, 1, 1))
        anchor = "start" if tick < CHART_LEFT + 16 else "end" if tick > CHART_RIGHT - 16 else "middle"
        grid.append(f'<text x="{tick:.1f}" y="{CHART_BOTTOM + 24}" class="muted small" text-anchor="{anchor}">{year}</text>')

    end_x, end_y = x(today), y(total)
    marker = (f'<circle cx="{end_x:.1f}" cy="{end_y:.1f}" r="4" fill="{stack[-1]["color"]}"/>'
              f'<circle cx="{end_x:.1f}" cy="{end_y:.1f}" r="8" fill="{stack[-1]["color"]}" fill-opacity="0.25"/>')

    legend = []
    row_height = 36
    legend_top = (CHART_TOP + CHART_BOTTOM) / 2 - row_height * len(stack) / 2 + 12
    for row, layer in enumerate(reversed(stack)):
        row_y = legend_top + row * row_height
        legend.append(f'<rect x="{LEGEND_LEFT}" y="{row_y - 10:.1f}" width="10" height="10" rx="3" fill="{layer["color"]}"/>')
        legend.append(f'<text x="{LEGEND_LEFT + 20}" y="{row_y:.1f}" class="label">{escape(layer["name"])}</text>')
        legend.append(f'<text x="{WIDTH}" y="{row_y:.1f}" class="label strong" text-anchor="end">{len(layer["dates"]):,}</text>')
        if layer["venue"]:
            legend.append(f'<text x="{LEGEND_LEFT + 20}" y="{row_y + 16:.1f}" class="muted small">{escape(layer["venue"])}</text>')

    forks = sum(repo["forkCount"] for repo in repos)
    recent = len(all_dates) - bisect.bisect_right(all_dates, (today - timedelta(days=365)).isoformat())
    stats = [(f"+{recent:,}", "past year"), (f"{forks:,}", "forks")] + ([(f"{cited:,}", "citations")] if cited else [])
    header = [
        f'<text x="0" y="48"><tspan class="hero">{total:,}</tspan><tspan class="unit" dx="10">stars</tspan></text>',
        f'<text x="0" y="78" class="muted body">across {len(repos)} repositories I own or contribute to</text>',
    ]
    for index, (value, label) in enumerate(reversed(stats)):
        column = WIDTH - index * 130
        header.append(f'<text x="{column}" y="42" class="stat" text-anchor="end">{value}</text>')
        header.append(f'<text x="{column}" y="66" class="muted small" text-anchor="end">{label}</text>')

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" role="img">
<title>{total:,} GitHub stars across {len(repos)} repositories</title>
<style>
text {{ font-family: {FONT}; fill: {colors["text"]}; font-variant-numeric: tabular-nums; }}
.muted {{ fill: {colors["muted"]}; }}
.hero {{ font-size: 46px; font-weight: 650; letter-spacing: -1px; }}
.unit {{ font-size: 18px; fill: {colors["muted"]}; }}
.stat {{ font-size: 26px; font-weight: 600; }}
.body {{ font-size: 14px; }}
.label {{ font-size: 13px; }}
.strong {{ font-weight: 600; }}
.small {{ font-size: 11px; }}
</style>
{chr(10).join(header)}
{chr(10).join(grid)}
{chr(10).join(areas)}
{marker}
{chr(10).join(legend)}
</svg>
"""


def write_outputs(repos, history, cited, today):
    os.makedirs("profile", exist_ok=True)
    for theme in THEMES:
        with open(f"profile/dashboard-{theme}.svg", "w") as handle:
            handle.write(render(theme, repos, history, cited, today))

    with open(HISTORY, "w") as handle:
        rows = [f"  {json.dumps(name)}: {json.dumps(dates)}" for name, dates in sorted(history.items(), key=lambda item: item[0].lower()) if dates]
        handle.write("{\n" + ",\n".join(rows) + "\n}\n")

    ranked = sorted(repos, key=lambda repo: (-repo["stargazerCount"], repo["nameWithOwner"].lower()))
    total = sum(repo["stargazerCount"] for repo in ranked)
    with open("profile/stars.md", "w") as handle:
        handle.write(f"# {total:,} stars across {len(ranked)} repositories\n\n")
        handle.write(f"Public repositories @{LOGIN} owns, organization repositories @{LOGIN} has committed to, "
                     f"and other repositories @{LOGIN} has added at least {MIN_ADDITIONS} lines of code to.\n\n")
        handle.write("| Repository | Stars | Forks |\n| --- | ---: | ---: |\n")
        for repo in ranked:
            handle.write(f"| [{repo['nameWithOwner']}]({repo['url']}) | {repo['stargazerCount']:,} | {repo['forkCount']:,} |\n")
    return ranked, total


def main():
    today = datetime.now(timezone.utc).date()
    repos = counted_repos()
    ranked, total = write_outputs(repos, star_history(repos, today), citations(), today)
    for repo in ranked:
        print(f"{repo['stargazerCount']:>6}  {repo['nameWithOwner']}")
    print(f"{total:>6}  total")


if __name__ == "__main__":
    main()
