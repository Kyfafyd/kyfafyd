"""Count stars on the public repos the user owns plus the organization repos they've committed to.

Writes profile/stars.json (a shields.io endpoint badge) and profile/stars.md (the per-repo breakdown).
"""
import json
import os
import urllib.request

LOGIN = os.environ.get("GITHUB_REPOSITORY_OWNER", "Kyfafyd")
TOKEN = os.environ["GITHUB_TOKEN"]

# Repos to leave out of the total, written as "owner/name".
EXCLUDED = set()

REPO_FIELDS = "nameWithOwner url stargazerCount isPrivate owner { __typename }"

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

CONTRIBUTION_YEARS = """
query($login: String!) {
  user(login: $login) { contributionsCollection { contributionYears } }
}
"""

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


def query(text, variables):
    request = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": text, "variables": variables}).encode(),
        headers={"Authorization": f"bearer {TOKEN}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    if payload.get("errors"):
        raise RuntimeError(json.dumps(payload["errors"], indent=2))
    return payload["data"]["user"]


def own_repos():
    after = None
    while True:
        page = query(OWN_REPOS, {"login": LOGIN, "after": after})["repositories"]
        yield from page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            return
        after = page["pageInfo"]["endCursor"]


def committed_org_repos():
    years = query(CONTRIBUTION_YEARS, {"login": LOGIN})["contributionsCollection"]["contributionYears"]
    for year in years:
        collection = query(COMMITTED_REPOS, {"login": LOGIN, "from": f"{year}-01-01T00:00:00Z"})["contributionsCollection"]
        for entry in collection["commitContributionsByRepository"]:
            if entry["repository"]["owner"]["__typename"] == "Organization":
                yield entry["repository"]


repos = {
    repo["nameWithOwner"]: repo
    for repo in [*own_repos(), *committed_org_repos()]
    if not repo["isPrivate"] and repo["nameWithOwner"] not in EXCLUDED
}
ranked = sorted(repos.values(), key=lambda repo: (-repo["stargazerCount"], repo["nameWithOwner"].lower()))
total = sum(repo["stargazerCount"] for repo in ranked)

os.makedirs("profile", exist_ok=True)
with open("profile/stars.json", "w") as handle:
    json.dump({
        "schemaVersion": 1,
        "label": "total stars",
        "message": f"{total:,}",
        "color": "yellow",
    }, handle, indent=2)

with open("profile/stars.md", "w") as handle:
    handle.write(f"# Total stars: {total:,}\n\n")
    handle.write(f"Stars on public repositories owned by @{LOGIN} and on organization repositories @{LOGIN} has committed to.\n\n")
    handle.write("| Repository | Stars |\n| --- | ---: |\n")
    for repo in ranked:
        handle.write(f"| [{repo['nameWithOwner']}]({repo['url']}) | {repo['stargazerCount']:,} |\n")

for repo in ranked:
    print(f"{repo['stargazerCount']:>6}  {repo['nameWithOwner']}")
print(f"{total:>6}  total")
