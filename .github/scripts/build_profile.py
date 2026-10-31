"""Build the assets behind the profile README.

Stars are counted on public repos the user owns, organization repos they have committed to, and other
people's repos they have added substantial code to. Writes into profile/:
  dashboard-light.svg, dashboard-dark.svg  total stars and citations over time, shown in README.md
  stars.md                                 the per-repo breakdown the dashboard links to
  star-history.json                        one date per current star, carried between runs
  citations.json                           Scholar's citation counts and OpenAlex's citing dates from the last good fetch
"""
import bisect
import json
import math
import os
import re
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html import unescape

LOGIN = os.environ.get("GITHUB_REPOSITORY_OWNER", "Kyfafyd")

# Lines the user must have added to someone else's personal repo before its stars count.
MIN_ADDITIONS = 100
# Repos to leave out of the total, written as "owner/name".
EXCLUDED = set()
SCHOLAR_URL = "https://scholar.google.com/citations?user=1kEufdwAAAAJ&hl=en&pagesize=100"
OPENALEX_URL = "https://api.openalex.org/works?"
BROWSER = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
HISTORY = "profile/star-history.json"
CITATIONS = "profile/citations.json"

REPO_FIELDS = "nameWithOwner url stargazerCount forkCount isPrivate owner { __typename login }"

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


def scholar():
    """Total and per-year citations from the Google Scholar profile, plus the titles of the papers that have any."""
    request = urllib.request.Request(SCHOLAR_URL, headers={"User-Agent": BROWSER})
    with urllib.request.urlopen(request, timeout=30) as response:
        html = response.read().decode("utf-8", "replace")
    total = int(re.findall(r"gsc_rsb_std[^>]*>([\d,]+)<", html)[0].replace(",", ""))
    years = re.findall(r'class="gsc_g_t"[^>]*>(\d{4})<', html)
    bars = re.findall(r'class="gsc_g_a"[^>]*z-index:(\d+)"><span class="gsc_g_al">([\d,]+)<', html)
    if not bars:
        raise ValueError("no citation chart on the Scholar page")
    per_year = dict.fromkeys(years, 0)
    # Bars are numbered from the latest year backwards, and years without citations get no bar.
    for z_index, count in bars:
        per_year[years[-int(z_index)]] = int(count.replace(",", ""))
    titles = []
    for row in re.findall(r'<tr class="gsc_a_tr">(.*?)</tr>', html, flags=re.S):
        title = re.search(r'class="gsc_a_at"[^>]*>(.*?)</a>', row, flags=re.S)
        if title and re.search(r'class="gsc_a_ac gs_ibl"[^>]*>\d', row):
            titles.append(unescape(re.sub(r"<[^>]+>", "", title.group(1))))
    return {"total": total, "per_year": per_year}, titles


def openalex(params):
    with urllib.request.urlopen(OPENALEX_URL + urllib.parse.urlencode(params), timeout=60) as response:
        return json.load(response)["results"]


def citing_dates(titles, works):
    """Publication dates of the works citing these papers, as recorded by OpenAlex.

    `works` maps paper titles to OpenAlex ids. Titles found by searching are added to it, so later runs skip the
    search, which costs ten times a list request out of the free daily budget OpenAlex shares per IP address.
    """
    def key(text):
        return re.sub(r"[^a-z0-9]+", "", text.lower())

    for title in titles:
        if title not in works:
            match = next((work for work in openalex({"search": title, "per-page": 5, "select": "id,display_name"})
                          if key(work["display_name"] or "") == key(title)), None)
            if match:
                works[title] = match["id"].rsplit("/", 1)[1]
    ids = [works[title] for title in titles if title in works]
    dates, page = [], 1
    while ids:
        # Plain page numbers, since OpenAlex wants an API key for cursor paging.
        works = openalex({"filter": "cites:" + "|".join(ids), "per-page": 200, "page": page, "select": "publication_date"})
        dates += [work["publication_date"] for work in works if work["publication_date"]]
        if len(works) < 200:
            break
        page += 1
    return sorted(dates)


def citations():
    """Scholar's counts and OpenAlex's citing dates, each falling back to the last good copy when its fetch fails."""
    try:
        with open(CITATIONS) as handle:
            cached = json.load(handle)
    except FileNotFoundError:
        cached = {}
    try:
        counts, titles = scholar()
    except (OSError, IndexError, ValueError):  # Scholar often answers CI runners with a CAPTCHA
        return cached
    works = dict(cached.get("works", {}))
    try:
        dates = citing_dates(titles, works)
    except (OSError, ValueError, KeyError):
        dates = cached.get("dates", [])
    data = {**counts, "works": works, "dates": dates}
    with open(CITATIONS, "w") as handle:
        handle.write(json.dumps(data, indent=1) + "\n")
    return data


THEMES = {
    "light": {"text": "#1f2328", "muted": "#59636e", "grid": "#d1d9e0", "stars": "#d4a72c", "citations": "#8250df"},
    "dark": {"text": "#f0f6fc", "muted": "#9198a1", "grid": "#3d444d", "stars": "#e3b341", "citations": "#ab7df8"},
}
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif"
WIDTH, HEIGHT = 880, 332
CHART_LEFT, CHART_RIGHT, CHART_TOP, CHART_BOTTOM = 0, 868, 112, 296


def star_points(history, times):
    dates = sorted(day for days in history.values() for day in days)
    return [(day, bisect.bisect_right(dates, day.isoformat())) for day in times]


def citation_points(cited, times, today):
    """Scholar's count for each year, spread across the year by when OpenAlex says the citing works came out.

    Citations from years Scholar leaves off its chart are the starting point, so the curve ends on Scholar's total.
    """
    per_year = cited["per_year"]
    start = date(int(min(per_year)), 1, 1)
    within = {}
    for day in cited["dates"]:
        # OpenAlex dates year-only works January 1, which says nothing about when in the year they came out.
        if not day.endswith("-01-01") and day <= today.isoformat():
            within.setdefault(day[:4], []).append(day)
    base = max(cited["total"] - sum(per_year.values()), 0)
    points = []
    for day in times:
        if day < start:
            continue
        year = str(day.year)
        begin, end = date(day.year, 1, 1), min(date(day.year + 1, 1, 1), today)
        dated = within.get(year, [])
        # Five evenly spread pseudo-citations keep a year with few dated citations from rising in steps.
        share = (bisect.bisect_right(dated, day.isoformat()) + 5 * (day - begin).days / max((end - begin).days, 1)) / (len(dated) + 5)
        earlier = sum(count for other, count in per_year.items() if other < year)
        points.append((day, base + earlier + per_year.get(year, 0) * min(share, 1)))
    return points


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


def path(segments):
    return " ".join(["M%.1f,%.1f" % segments[0][0]] + ["C" + " ".join("%.1f,%.1f" % point for point in segment[1:]) for segment in segments])


def render(theme, repos, history, cited, today):
    colors = THEMES[theme]
    first_star = min(day for days in history.values() for day in days)
    start = min(date.fromisoformat(first_star), date(int(min(cited["per_year"])), 1, 1)).replace(day=1)
    times = [start + timedelta(days=offset) for offset in range(0, (today - start).days, 7)] + [today]
    series = {"stars": star_points(history, times), "citations": citation_points(cited, times, today)}
    totals = {"stars": series["stars"][-1][1], "citations": cited["total"]}
    captions = {"stars": f"across {len(repos)} repositories", "citations": "on Google Scholar"}
    span = (today - start).days or 1
    peak = max(points[-1][1] for points in series.values())
    step = nice_step(peak)
    top = peak * 1.1

    def x(day):
        return CHART_LEFT + (day - start).days / span * (CHART_RIGHT - CHART_LEFT)

    def y(value):
        return CHART_BOTTOM - value / top * (CHART_BOTTOM - CHART_TOP)

    grid = []
    for value in range(step, int(top), step):
        grid.append(f'<line x1="{CHART_LEFT}" x2="{CHART_RIGHT}" y1="{y(value):.1f}" y2="{y(value):.1f}" stroke="{colors["grid"]}" stroke-dasharray="2 4"/>')
        grid.append(f'<text x="{CHART_LEFT}" y="{y(value) - 6:.1f}" class="muted small">{value:,}</text>')
    grid.append(f'<line x1="{CHART_LEFT}" x2="{CHART_RIGHT}" y1="{CHART_BOTTOM}" y2="{CHART_BOTTOM}" stroke="{colors["grid"]}"/>')
    for year in range(start.year + 1, today.year + 1):
        tick = x(date(year, 1, 1))
        anchor = "start" if tick < CHART_LEFT + 16 else "end" if tick > CHART_RIGHT - 16 else "middle"
        grid.append(f'<text x="{tick:.1f}" y="{CHART_BOTTOM + 24}" class="muted small" text-anchor="{anchor}">{year}</text>')

    lines, header = [], []
    for index, (name, points) in enumerate(series.items()):
        color = colors[name]
        segments = curve([(x(day), y(value)) for day, value in points])
        last_x, last_y = segments[-1][-1]
        lines.append(f'<path d="{path(segments)}" fill="none" stroke="{color}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>')
        lines.append(f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="8" fill="{color}" fill-opacity="0.25"/>'
                     f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="4" fill="{color}"/>')
        left = index * 300
        header.append(f'<circle cx="{left + 6}" cy="32" r="5" fill="{color}"/>')
        header.append(f'<text x="{left + 20}" y="48"><tspan class="hero">{totals[name]:,}</tspan><tspan class="unit" dx="8">{name}</tspan></text>')
        header.append(f'<text x="{left + 20}" y="74" class="muted body">{captions[name]}</text>')

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" role="img">
<title>{totals["stars"]:,} GitHub stars and {totals["citations"]:,} citations over time</title>
<style>
text {{ font-family: {FONT}; fill: {colors["text"]}; font-variant-numeric: tabular-nums; }}
.muted {{ fill: {colors["muted"]}; }}
.hero {{ font-size: 46px; font-weight: 650; letter-spacing: -1px; }}
.unit {{ font-size: 18px; fill: {colors["muted"]}; }}
.body {{ font-size: 14px; }}
.small {{ font-size: 11px; }}
</style>
{chr(10).join(header)}
{chr(10).join(grid)}
{chr(10).join(lines)}
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
    cited = citations()
    ranked, total = write_outputs(repos, star_history(repos, today), cited, today)
    for repo in ranked:
        print(f"{repo['stargazerCount']:>6}  {repo['nameWithOwner']}")
    print(f"{total:>6}  stars total\n{cited['total']:>6}  citations")


if __name__ == "__main__":
    main()
