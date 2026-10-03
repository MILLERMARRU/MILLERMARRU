"""Fetch GitHub statistics into cache/: stats.json for the card, calendar.json
for the Pac-Man board.

Split from tools/build_card.py on purpose: this half is the slow, network-bound,
rate-limited part, so it caches aggressively and can fail without taking the
card down (build_card.py falls back to placeholders when the cache is absent).

Lines-of-code is the expensive figure -- it walks every commit you authored on
each repo's default branch. cache/loc.json memoises that per repo, keyed by the
branch head OID, so an unchanged repo costs one cheap field in the repo query
instead of a full history walk.

Requires a GITHUB_TOKEN in the environment (the Actions-provided token is
enough for public data).

Usage:
    GITHUB_TOKEN=... python tools/fetch_stats.py
"""

from __future__ import annotations

import hashlib
import json
import re
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "cache"
STATS_OUT = CACHE / "stats.json"
LOC_CACHE = CACHE / "loc.json"
CALENDAR_OUT = CACHE / "calendar.json"

LOGIN = os.environ.get("GH_LOGIN", "MILLERMARRU")
ENDPOINT = "https://api.github.com/graphql"
TOKEN = os.environ.get("GITHUB_TOKEN")

# Un commit que agrega más de esto casi nunca es código escrito a mano:
# node_modules, venvs, lockfiles o builds subidos por error. Se descarta entero
# para que Lines of Code no se infle con millones de líneas ajenas.
MAX_COMMIT_LINES = int(os.environ.get("MAX_COMMIT_LINES", "5000"))

session = requests.Session()


def query(document: str, variables: dict, attempt: int = 0) -> dict:
    """POST a GraphQL document, retrying once on a secondary rate limit."""
    resp = session.post(
        ENDPOINT,
        json={"query": document, "variables": variables},
        headers={"Authorization": f"bearer {TOKEN}"},
        timeout=30,
    )
    if resp.status_code in (403, 429) and attempt < 3:
        wait = int(resp.headers.get("Retry-After", 60))
        print(f"  rate limited, sleeping {wait}s", file=sys.stderr)
        time.sleep(wait)
        return query(document, variables, attempt + 1)

    resp.raise_for_status()
    payload = resp.json()
    if "errors" in payload:
        raise SystemExit(f"GraphQL error: {json.dumps(payload['errors'], indent=2)}")
    return payload["data"]


REPOS_Q = """
query($login:String!, $after:String) {
  user(login:$login) {
    id
    createdAt
    followers { totalCount }
    repositories(ownerAffiliations:OWNER, isFork:false, first:100, after:$after) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        nameWithOwner
        stargazerCount
        defaultBranchRef { target { ... on Commit { oid } } }
      }
    }
  }
}
"""

# Repos de organizaciones y donde soy colaborador: no suman a Repos ni a
# Stars (no son míos), pero sí a Lines of Code, que solo cuenta mis commits.
SHARED_REPOS_Q = """
query($login:String!, $after:String) {
  user(login:$login) {
    repositories(ownerAffiliations:[COLLABORATOR, ORGANIZATION_MEMBER], isFork:false,
                 first:100, after:$after) {
      pageInfo { hasNextPage endCursor }
      nodes {
        nameWithOwner
        defaultBranchRef { target { ... on Commit { oid } } }
      }
    }
  }
}
"""

# The same last-year calendar GitHub draws on the profile. Anything the
# profile hides from visitors -- private contributions, unless the owner opts
# in -- this token cannot see either, so the board always matches the graph
# rendered directly beneath it.
CALENDAR_Q = """
query($login:String!) {
  user(login:$login) {
    contributionsCollection {
      contributionCalendar {
        totalContributions
        weeks {
          contributionDays { date weekday contributionCount contributionLevel }
        }
      }
    }
  }
}
"""

LEVELS = {"NONE": 0, "FIRST_QUARTILE": 1, "SECOND_QUARTILE": 2,
          "THIRD_QUARTILE": 3, "FOURTH_QUARTILE": 4}

# Contributed: repos ajenos donde hubo commits, PRs, issues o revisiones. Se
# junta lo de cada año desde que existe la cuenta (repositoriesContributedTo
# solo mira lo reciente) con los repos compartidos donde hay commits míos, que
# GitHub cuenta como contribución privada y no lista por repositorio.
CONTRIB_RECENT_Q = """
query($login:String!, $after:String) {
  user(login:$login) {
    repositoriesContributedTo(
      first:100, after:$after,
      contributionTypes:[COMMIT, ISSUE, PULL_REQUEST, PULL_REQUEST_REVIEW, REPOSITORY]
    ) {
      pageInfo { hasNextPage endCursor }
      nodes { nameWithOwner }
    }
  }
}
"""

DISCUSSIONS_Q = """
query($login:String!, $after:String) {
  user(login:$login) {
    repositoryDiscussionComments(first:100, after:$after) {
      pageInfo { hasNextPage endCursor }
      nodes { discussion { repository { nameWithOwner } } }
    }
  }
}
"""

ACTIVITY_Q = """
query($login:String!) {
  user(login:$login) {
    pullRequests { totalCount }
    merged: pullRequests(states:MERGED) { totalCount }
    issues { totalCount }
    repositoryDiscussionComments { totalCount }
    answers: repositoryDiscussionComments(onlyAnswers:true) { totalCount }
  }
}
"""

REVIEWS_Q = """
query($login:String!, $from:DateTime!, $to:DateTime!) {
  user(login:$login) {
    contributionsCollection(from:$from, to:$to) { totalPullRequestReviewContributions }
  }
}
"""

CONTRIB_YEAR_Q = """
query($login:String!, $from:DateTime!, $to:DateTime!) {
  user(login:$login) {
    contributionsCollection(from:$from, to:$to) {
      commitContributionsByRepository(maxRepositories:100) { repository { nameWithOwner } }
      issueContributionsByRepository(maxRepositories:100) { repository { nameWithOwner } }
      pullRequestContributionsByRepository(maxRepositories:100) { repository { nameWithOwner } }
      pullRequestReviewContributionsByRepository(maxRepositories:100) { repository { nameWithOwner } }
    }
  }
}
"""

# contributionsCollection is capped at one year per call, so this gets asked
# once per year the account has existed.
COMMITS_Q = """
query($login:String!, $from:DateTime!, $to:DateTime!) {
  user(login:$login) {
    contributionsCollection(from:$from, to:$to) {
      totalCommitContributions
      restrictedContributionsCount
    }
  }
}
"""

HISTORY_Q = """
query($owner:String!, $name:String!, $authorId:ID!, $after:String) {
  repository(owner:$owner, name:$name) {
    defaultBranchRef {
      target { ... on Commit {
        history(first:100, after:$after, author:{id:$authorId}) {
          totalCount
          pageInfo { hasNextPage endCursor }
          nodes { additions deletions }
        }
      }}
    }
  }
}
"""


def fetch_repos() -> tuple[dict, list[dict]]:
    """Return (user_meta, repos). Paginates through every owned, non-fork repo."""
    repos: list[dict] = []
    meta: dict = {}
    after = None
    while True:
        data = query(REPOS_Q, {"login": LOGIN, "after": after})["user"]
        if not meta:
            meta = {
                "id": data["id"],
                "createdAt": data["createdAt"],
                "followers": data["followers"]["totalCount"],
                "repos": data["repositories"]["totalCount"],
            }
        repos.extend(data["repositories"]["nodes"])
        page = data["repositories"]["pageInfo"]
        if not page["hasNextPage"]:
            return meta, repos
        after = page["endCursor"]


def fetch_shared_repos() -> list[dict]:
    """Repos ajenos con acceso de colaborador o de miembro de la organización."""
    repos: list[dict] = []
    after = None
    while True:
        conn = query(SHARED_REPOS_Q, {"login": LOGIN, "after": after})["user"]["repositories"]
        repos.extend(conn["nodes"])
        if not conn["pageInfo"]["hasNextPage"]:
            return repos
        after = conn["pageInfo"]["endCursor"]


def year_windows(created_at: str):
    """Ventanas de un año desde la creación de la cuenta hasta hoy."""
    cursor = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    while cursor < now:
        try:
            nxt = cursor.replace(year=cursor.year + 1)
        except ValueError:
            nxt = cursor.replace(year=cursor.year + 1, day=28)  # 29 de febrero
        end = min(nxt, now)
        yield (cursor.isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z"))
        cursor = end


def fetch_contributed(created_at: str, shared_with_commits: set[str]) -> int:
    """Cuántos repos ajenos recibieron alguna contribución mía."""
    repos = set(shared_with_commits)
    after = None
    while True:
        conn = query(CONTRIB_RECENT_Q, {"login": LOGIN, "after": after})["user"]["repositoriesContributedTo"]
        repos.update(n["nameWithOwner"] for n in conn["nodes"])
        if not conn["pageInfo"]["hasNextPage"]:
            break
        after = conn["pageInfo"]["endCursor"]
    for start, end in year_windows(created_at):
        coll = query(CONTRIB_YEAR_Q, {"login": LOGIN, "from": start, "to": end})["user"]["contributionsCollection"]
        for group in coll.values():
            repos.update(item["repository"]["nameWithOwner"] for item in group)
    # Discusiones: comentarios y respuestas (Galaxy Brain) en repos ajenos.
    after = None
    while True:
        conn = query(DISCUSSIONS_Q, {"login": LOGIN, "after": after})["user"]["repositoryDiscussionComments"]
        repos.update(n["discussion"]["repository"]["nameWithOwner"]
                     for n in conn["nodes"] if n.get("discussion"))
        if not conn["pageInfo"]["hasNextPage"]:
            break
        after = conn["pageInfo"]["endCursor"]
    return len({r for r in repos if not r.lower().startswith(f"{LOGIN.lower()}/")})


def fetch_activity(created_at: str) -> dict:
    """PRs, issues, revisiones de código y discusiones de toda la cuenta."""
    u = query(ACTIVITY_Q, {"login": LOGIN})["user"]
    reviews = sum(
        query(REVIEWS_Q, {"login": LOGIN, "from": start, "to": end})
        ["user"]["contributionsCollection"]["totalPullRequestReviewContributions"]
        for start, end in year_windows(created_at)
    )
    return {
        "prs": u["pullRequests"]["totalCount"],
        "merged": u["merged"]["totalCount"],
        "issues": u["issues"]["totalCount"],
        "reviews": reviews,
        "discussions": u["repositoryDiscussionComments"]["totalCount"],
        "answers": u["answers"]["totalCount"],
    }


def fetch_commit_total(created_at: str) -> int:
    """Sum contributions year by year from account creation to now."""
    start = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    total = 0
    cursor = start
    while cursor < now:
        try:
            next_year = cursor.replace(year=cursor.year + 1)
        except ValueError:
            next_year = cursor.replace(year=cursor.year + 1, day=28)  # Feb 29
        end = min(next_year, now)
        c = query(COMMITS_Q, {
            "login": LOGIN,
            "from": cursor.isoformat().replace("+00:00", "Z"),
            "to": end.isoformat().replace("+00:00", "Z"),
        })["user"]["contributionsCollection"]
        total += c["totalCommitContributions"] + c["restrictedContributionsCount"]
        cursor = end
    return total


def cache_key(name: str) -> str:
    """La caché se sube a un repo público: se guarda el hash, nunca el nombre,
    para no exponer los repos privados."""
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]


def fetch_loc(repos: list[dict], author_id: str) -> tuple[int, int]:
    """Total additions/deletions authored across all repos, memoised by head OID."""
    cache = json.loads(LOC_CACHE.read_text(encoding="utf-8")) if LOC_CACHE.exists() else {}
    added = deleted = 0
    walked = reused = 0

    for repo in repos:
        name = repo["nameWithOwner"]
        target = (repo.get("defaultBranchRef") or {}).get("target") or {}
        head = target.get("oid")
        if not head:
            continue  # empty repo, nothing to walk

        hit = cache.get(cache_key(name))
        if hit and hit.get("oid") == head and hit.get("cap") == MAX_COMMIT_LINES:
            added += hit["added"]
            deleted += hit["deleted"]
            reused += 1
            continue

        owner, short = name.split("/", 1)
        r_add = r_del = 0
        after = None
        while True:
            target = query(HISTORY_Q, {
                "owner": owner, "name": short,
                "authorId": author_id, "after": after,
            })["repository"]["defaultBranchRef"]["target"]
            hist = target["history"]
            for node in hist["nodes"]:
                if node["additions"] + node["deletions"] > MAX_COMMIT_LINES:
                    continue
                r_add += node["additions"]
                r_del += node["deletions"]
            if not hist["pageInfo"]["hasNextPage"]:
                break
            after = hist["pageInfo"]["endCursor"]

        cache[cache_key(name)] = {"oid": head, "cap": MAX_COMMIT_LINES, "added": r_add, "deleted": r_del}
        added += r_add
        deleted += r_del
        walked += 1
        print(f"  walked {cache_key(name)}: +{r_add:,} -{r_del:,}")

    CACHE.mkdir(exist_ok=True)
    LOC_CACHE.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
    print(f"  loc cache: {walked} walked, {reused} reused")
    return added, deleted


def fetch_achievements(previous: list | None) -> list[dict]:
    """Logros del perfil con su nivel (x2, x3, x4).

    GitHub no los expone en la API, así que se leen de la pestaña pública de
    logros. Si la página cambia o falla, se conservan los de la última vez en
    vez de dejar la tarjeta sin ellos.
    """
    try:
        resp = session.get(f"https://github.com/{LOGIN}?tab=achievements",
                           headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        resp.raise_for_status()
        found: dict[str, int] = {}
        # La página repite la lista (grilla y detalle): se lee en orden y el
        # nivel que aparece después de un nombre es de ese logro.
        current = None
        for m in re.finditer(r'alt="Achievement: ([^"]+)"|achievement-tier-label[^>]*>x(\d+)', resp.text):
            if m.group(1):
                if m.group(1) in found and current is not None:
                    break
                current = m.group(1)
                found.setdefault(current, 1)
            elif current is not None:
                found[current] = int(m.group(2))
                current = None
        if not found:
            raise ValueError("no se encontró ningún logro en la página")
        return [{"name": n, "tier": t} for n, t in found.items()]
    except Exception as exc:  # la tarjeta no debe caerse por esto
        print(f"  achievements: {exc}; se conservan los anteriores", file=sys.stderr)
        return previous or []


def fetch_calendar() -> dict:
    """Weeks as seven weekday slots (0 = Sunday), None where a day doesn't exist yet."""
    cal = query(CALENDAR_Q, {"login": LOGIN})["user"]["contributionsCollection"]["contributionCalendar"]
    weeks = []
    for week in cal["weeks"]:
        slots: list[dict | None] = [None] * 7
        for day in week["contributionDays"]:
            slots[day["weekday"]] = {
                "date": day["date"],
                "count": day["contributionCount"],
                "level": LEVELS[day["contributionLevel"]],
            }
        weeks.append(slots)
    return {"total": cal["totalContributions"], "weeks": weeks}


def main() -> None:
    if not TOKEN:
        raise SystemExit("GITHUB_TOKEN is not set")

    print(f"fetching stats for {LOGIN}")
    meta, repos = fetch_repos()
    print(f"  repos: {meta['repos']} | followers: {meta['followers']}")

    stars = sum(r["stargazerCount"] for r in repos)
    commits = fetch_commit_total(meta["createdAt"])
    shared = fetch_shared_repos()
    print(f"  shared repos: {len(shared)}")
    added, deleted = fetch_loc(repos + shared, meta["id"])
    cache = json.loads(LOC_CACHE.read_text(encoding="utf-8"))
    with_commits = {r["nameWithOwner"] for r in shared
                    if (cache.get(cache_key(r["nameWithOwner"])) or {}).get("added")}
    contrib = fetch_contributed(meta["createdAt"], with_commits)
    print(f"  contributed: {contrib}")
    activity = fetch_activity(meta["createdAt"])

    previous = json.loads(STATS_OUT.read_text(encoding="utf-8")) if STATS_OUT.exists() else {}
    achievements = fetch_achievements(previous.get("achievements"))
    listed = ", ".join(f"{a['name']} x{a['tier']}" for a in achievements)
    print(f"  achievements: {listed}")

    stats = {
        "repos": meta["repos"],
        "contrib": contrib,
        "stars": stars,
        "commits": commits,
        "followers": meta["followers"],
        "loc": added - deleted,
        "added": added,
        "deleted": deleted,
        **activity,
        "achievements": achievements,
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    CACHE.mkdir(exist_ok=True)
    STATS_OUT.write_text(json.dumps(stats, indent=2), encoding="utf-8")
    print(json.dumps(stats, indent=2))

    calendar = fetch_calendar()
    CALENDAR_OUT.write_text(json.dumps(calendar, indent=1), encoding="utf-8")
    print(f"  calendar: {len(calendar['weeks'])} weeks, {calendar['total']} contributions")


if __name__ == "__main__":
    main()
