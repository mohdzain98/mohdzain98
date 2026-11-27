"""Generate GitHub profile SVG cards from the GraphQL API.

Collects every commit authored by the token owner across owned, collaborator
and organization repos (deduplicated by oid, so forks never double count), then
renders three cards: language journey, public vs private commits, active repos.
Run by the profile-cards workflow; private repo names never appear in output.
"""

from __future__ import annotations

import json
import math
import os
import sys
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

API_URL = "https://api.github.com/graphql"
ACTIVE_DAYS = 90
MAX_LANGS = 6
MAX_ACTIVE_ROWS = 6
GAP = 2  # surface gap between adjacent fills
CARD_W = 480  # matches metrics.svg so cards pair up in a 2-column README grid
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"

# Validated categorical palette; slot order is the colorblind-safety mechanism.
PALETTE_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]
PALETTE_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#0ca30c"]
OTHER_LIGHT, OTHER_DARK = "#9a9893", "#6f6e69"
# Visibility card: one violet hue, split by lightness (no clash with language slots).
PUBLIC_LIGHT, PRIVATE_LIGHT = "#4a3aa7", "#a99ff0"
PUBLIC_DARK, PRIVATE_DARK = "#9085e9", "#5a48c8"

REPOS_QUERY = """
query($cursor: String) {
  viewer {
    repositories(first: 100, after: $cursor,
      affiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER],
      ownerAffiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER]) {
      pageInfo { hasNextPage endCursor }
      nodes { nameWithOwner isPrivate isFork primaryLanguage { name } }
    }
  }
}"""

HISTORY_QUERY = """
query($owner: String!, $name: String!, $author: ID!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { target { ... on Commit {
      history(first: 100, after: $cursor, author: {id: $author}) {
        pageInfo { hasNextPage endCursor }
        nodes { oid authoredDate }
      }
    } } }
  }
}"""


@dataclass(frozen=True)
class Commit:
    """One authored commit, tagged with metadata from the repo it lives in.

    The language is the repo's primary language as computed by Linguist,
    not the languages of the files the commit touched.
    """

    oid: str
    when: datetime
    repo: str
    language: str
    private: bool


def graphql(token: str, query: str, variables: dict | None = None) -> dict:
    """POST a GraphQL query and return its `data` payload.

    Raises RuntimeError when the response carries GraphQL errors.
    """
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={"Authorization": f"bearer {token}", "User-Agent": "profile-cards"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.load(resp)
    if payload.get("errors"):
        raise RuntimeError(json.dumps(payload["errors"])[:500])
    return payload["data"]


def fetch_repos(token: str) -> list[dict]:
    """List every repo the token owner owns, collaborates on, or can see via orgs.

    Forks are sorted last so commits shared with an upstream count there first.
    """
    repos, cursor = [], None
    while True:
        page = graphql(token, REPOS_QUERY, {"cursor": cursor})["viewer"]["repositories"]
        repos.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    return sorted(repos, key=lambda r: r["isFork"])


def fetch_commits(token: str) -> list[Commit]:
    """Collect the viewer's authored commits on each repo's default branch.

    Commits are deduplicated by oid across repos (fork and upstream overlap).
    """
    author_id = graphql(token, "{ viewer { id } }")["viewer"]["id"]
    seen: set[str] = set()
    commits: list[Commit] = []
    for repo in fetch_repos(token):
        owner, name = repo["nameWithOwner"].split("/", 1)
        language = (repo["primaryLanguage"] or {}).get("name") or "Other"
        cursor = None
        while True:
            variables = {"owner": owner, "name": name, "author": author_id, "cursor": cursor}
            ref = graphql(token, HISTORY_QUERY, variables)["repository"]["defaultBranchRef"]
            if not ref or not ref["target"]:
                break  # empty repo
            history = ref["target"]["history"]
            for node in history["nodes"]:
                if node["oid"] in seen:
                    continue
                seen.add(node["oid"])
                when = datetime.fromisoformat(node["authoredDate"].replace("Z", "+00:00"))
                commits.append(Commit(node["oid"], when, repo["nameWithOwner"], language, repo["isPrivate"]))
            if not history["pageInfo"]["hasNextPage"]:
                break
            cursor = history["pageInfo"]["endCursor"]
    return commits


def rank_languages(commits: list[Commit]) -> list[str]:
    """Return the top languages by commit count, most first.

    "Other" (no detected language) is never ranked; it is folded in later.
    """
    counts = Counter(c.language for c in commits if c.language != "Other")
    return [lang for lang, _ in counts.most_common(MAX_LANGS)]


def bucket(language: str, ranked: list[str]) -> str:
    """Map a language to itself if ranked, else to "Other"."""
    return language if language in ranked else "Other"


def color(language: str, ranked: list[str]) -> str:
    """CSS fill for a language's fixed categorical slot."""
    return f"var(--s{ranked.index(language)})" if language in ranked else "var(--so)"


def nice_scale(max_value: int, ticks: int = 4) -> tuple[int, int]:
    """Pick a round tick step and axis top that cover max_value.

    Returns (step, top); both are at least 1.
    """
    raw = max(max_value, 1) / ticks
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    step = max(int(step), 1)
    return step, max(step * math.ceil(max_value / step), step)


def style_block() -> str:
    """Shared CSS with light and dark tokens; dark follows prefers-color-scheme."""
    light = "".join(f"--s{i}:{c};" for i, c in enumerate(PALETTE_LIGHT)) + f"--so:{OTHER_LIGHT};--pub:{PUBLIC_LIGHT};--pri:{PRIVATE_LIGHT};"
    dark = "".join(f"--s{i}:{c};" for i, c in enumerate(PALETTE_DARK)) + f"--so:{OTHER_DARK};--pub:{PUBLIC_DARK};--pri:{PRIVATE_DARK};"
    return (
        "<style>"
        f"svg{{--bg:#ffffff;--bd:#d0d7de;--t1:#1f2328;--t2:#59636e;--gr:#eaeef2;{light}}}"
        "@media (prefers-color-scheme:dark){"
        f"svg{{--bg:#0d1117;--bd:#30363d;--t1:#e6edf3;--t2:#9198a1;--gr:#21262d;{dark}}}}}"
        f"text{{font-family:{FONT}}}"
        ".bg{fill:var(--bg);stroke:var(--bd)}"
        ".h{font-size:16px;font-weight:600;fill:var(--t1)}"
        ".sub{font-size:11px;fill:var(--t2)}"
        ".l{font-size:12px;fill:var(--t1)}"
        ".m{font-size:11px;fill:var(--t2)}"
        ".v{font-size:11px;font-weight:600;fill:var(--t1)}"
        ".g{stroke:var(--gr);stroke-width:1}"
        "</style>"
    )


def frame(width: int, height: int, title: str, subtitle: str, body: str) -> str:
    """Wrap card body in the SVG root with background, title and subtitle."""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">'
        f"{style_block()}"
        f'<rect class="bg" x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="6"/>'
        f'<text class="h" x="20" y="32">{escape(title)}</text>'
        f'<text class="sub" x="20" y="50">{escape(subtitle)}</text>'
        f"{body}</svg>"
    )


def legend(items: list[tuple[str, str]], x: float, y: float, max_x: float) -> tuple[str, float]:
    """Render (label, fill) swatches from x, wrapping to a new line past max_x.

    Returns the SVG and the extra height used by wrapped lines.
    """
    parts, start, extra = [], x, 0.0
    for label, fill in items:
        item_w = 15 + len(label) * 6.0 + 12
        if x + item_w > max_x and x > start:
            x, extra = start, extra + 18
        parts.append(f'<rect x="{x:.1f}" y="{y + extra - 9}" width="10" height="10" rx="2" style="fill:{fill}"/>')
        parts.append(f'<text class="m" x="{x + 15:.1f}" y="{y + extra}">{escape(label)}</text>')
        x += item_w
    return "".join(parts), extra


def bar_up(x: float, y: float, w: float, h: float, fill: str, rounded: bool) -> str:
    """Vertical bar segment; rounded=True gives the 4px data-end on top."""
    r = min(4.0, w / 2, h) if rounded else 0
    if r <= 0:
        return f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" style="fill:{fill}"/>'
    d = (f"M{x:.1f},{y + h:.1f}V{y + r:.1f}Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f}"
         f"H{x + w - r:.1f}Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f}V{y + h:.1f}Z")
    return f'<path d="{d}" style="fill:{fill}"/>'


def bar_right(x: float, y: float, w: float, h: float, fill: str, rounded: bool) -> str:
    """Horizontal bar segment; rounded=True gives the 4px data-end on the right."""
    r = min(4.0, h / 2, w) if rounded else 0
    if r <= 0:
        return f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" style="fill:{fill}"/>'
    d = (f"M{x:.1f},{y:.1f}H{x + w - r:.1f}Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f}"
         f"V{y + h - r:.1f}Q{x + w:.1f},{y + h:.1f} {x + w - r:.1f},{y + h:.1f}H{x:.1f}Z")
    return f'<path d="{d}" style="fill:{fill}"/>'


def stacked_segments(values: list[tuple[int, str]], start: float, scale: float,
                     horizontal: bool, cross: float, thickness: float) -> str:
    """Draw a stack of (value, fill) segments with surface gaps and a rounded end.

    For vertical stacks `start` is the baseline y; for horizontal it is the left x.
    """
    live = [(v, f) for v, f in values if v > 0]
    parts, cursor = [], start
    for i, (value, fill) in enumerate(live):
        length = value * scale
        last = i == len(live) - 1
        drawn = max(length - (0 if last else GAP), 1.0)
        if horizontal:
            parts.append(bar_right(cursor, cross, drawn, thickness, fill, last))
            cursor += length
        else:
            parts.append(bar_up(cross, cursor - length + (0 if last else GAP), thickness, drawn, fill, last))
            cursor -= length
    return "".join(parts)


def render_journey(commits: list[Commit], ranked: list[str]) -> str:
    """Stacked column per year: commits split by repo primary language."""
    now = datetime.now(timezone.utc)
    first = min((c.when.year for c in commits), default=now.year)
    years = list(range(first, now.year + 1))
    per_year: dict[int, Counter] = defaultdict(Counter)
    for c in commits:
        per_year[c.when.year][bucket(c.language, ranked)] += 1
    order = ranked + (["Other"] if any(per_year[y]["Other"] for y in years) else [])

    width = CARD_W
    legend_svg, extra = legend([(lang, color(lang, ranked)) for lang in order], 20, 74, width - 16)
    height = 320 + int(extra)
    left, right, top, bottom = 44, width - 16, 96 + extra, height - 34
    step, axis_top = nice_scale(max((sum(per_year[y].values()) for y in years), default=1))
    scale = (bottom - top) / axis_top
    band = (right - left) / len(years)
    bar_w = min(38.0, band * 0.6)

    body = [legend_svg]
    for tick in range(0, axis_top + 1, step):
        y = bottom - tick * scale
        body.append(f'<line class="g" x1="{left}" x2="{right}" y1="{y:.1f}" y2="{y:.1f}"/>')
        body.append(f'<text class="m" x="{left - 8}" y="{y + 4:.1f}" text-anchor="end">{tick:,}</text>')
    for i, year in enumerate(years):
        cx = left + band * i + band / 2
        total = sum(per_year[year].values())
        segs = [(per_year[year][lang], color(lang, ranked)) for lang in order]
        body.append(stacked_segments(segs, bottom, scale, False, cx - bar_w / 2, bar_w))
        if total:
            body.append(f'<text class="v" x="{cx:.1f}" y="{bottom - total * scale - 6:.1f}" '
                        f'text-anchor="middle">{total:,}</text>')
        body.append(f'<text class="m" x="{cx:.1f}" y="{bottom + 18}" text-anchor="middle">{year}</text>')

    subtitle = f"{len(commits):,} commits since {first} · by repo primary language"
    return frame(width, height, "Language Journey", subtitle, "".join(body))


def render_visibility(commits: list[Commit], ranked: list[str]) -> str:
    """Horizontal stacked bars per language: public vs private commits."""
    counts: dict[str, Counter] = defaultdict(Counter)
    for c in commits:
        counts[bucket(c.language, ranked)]["private" if c.private else "public"] += 1
    rows = [lang for lang in ranked + ["Other"] if counts[lang]]
    public = sum(counts[l]["public"] for l in rows)
    private = sum(counts[l]["private"] for l in rows)

    width, row_h, top = CARD_W, 30, 90
    height = top + len(rows) * row_h + 14
    x0, x1 = 104, width - 118
    scale = (x1 - x0) / max((sum(counts[l].values()) for l in rows), default=1)
    fills = {"public": "var(--pub)", "private": "var(--pri)"}

    body = [legend([("Public", fills["public"]), ("Private", fills["private"])], 20, 74, width - 16)[0]]
    for i, lang in enumerate(rows):
        y = top + i * row_h
        pub, priv = counts[lang]["public"], counts[lang]["private"]
        body.append(f'<text class="l" x="20" y="{y + 14}">{escape(lang)}</text>')
        body.append(stacked_segments([(pub, fills["public"]), (priv, fills["private"])],
                                     x0, scale, True, y + 2, 16))
        end = x0 + (pub + priv) * scale + 8
        note = f" · {priv:,} private" if priv else ""
        body.append(f'<text class="v" x="{end:.1f}" y="{y + 14}">{pub + priv:,}'
                    f'<tspan class="m" font-weight="400">{note}</tspan></text>')

    subtitle = f"{public:,} public · {private:,} private commits · private names hidden"
    return frame(width, height, "Commits by Visibility", subtitle, "".join(body))


def render_active(commits: list[Commit], ranked: list[str]) -> str:
    """Top repos by commits in the last ACTIVE_DAYS; private repos merged unnamed."""
    since = datetime.now(timezone.utc) - timedelta(days=ACTIVE_DAYS)
    recent = [c for c in commits if c.when >= since]
    per_repo = Counter(c.repo for c in recent if not c.private)
    repo_lang = {c.repo: c.language for c in recent}
    private = [c for c in recent if c.private]

    rows = [(repo.split("/", 1)[1], n, repo_lang[repo]) for repo, n in per_repo.most_common(MAX_ACTIVE_ROWS)]
    if private:
        n_private_repos = len({c.repo for c in private})
        top_lang = Counter(c.language for c in private).most_common(1)[0][0]
        rows.append((f"Private repos ({n_private_repos})", len(private), top_lang))
        rows.sort(key=lambda r: -r[1])

    width, row_h, top = CARD_W, 30, 74
    height = top + max(len(rows), 1) * row_h + 14
    x0, x1 = 214, width - 112
    scale = (x1 - x0) / max((r[1] for r in rows), default=1)

    body = []
    if not rows:
        body.append(f'<text class="m" x="20" y="{top + 14}">No commits in the last {ACTIVE_DAYS} days</text>')
    for i, (name, n, lang) in enumerate(rows):
        y = top + i * row_h
        label = name if len(name) <= 30 else name[:29] + "…"
        body.append(f'<text class="l" x="20" y="{y + 14}">{escape(label)}</text>')
        body.append(bar_right(x0, y + 2, max(n * scale, 1.0), 16, color(lang, ranked), True))
        body.append(f'<text class="v" x="{x0 + n * scale + 8:.1f}" y="{y + 14}">{n:,}'
                    f'<tspan class="m" font-weight="400"> · {escape(lang)}</tspan></text>')

    repo_count = len(per_repo) + len({c.repo for c in private})
    subtitle = f"Last {ACTIVE_DAYS} days · {len(recent):,} commits across {repo_count} repos"
    return frame(width, height, "Most Active Repos", subtitle, "".join(body))


def main() -> None:
    """Fetch commits once and write all three cards to the output directory."""
    token = os.environ.get("GH_STATS_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("Set GH_STATS_TOKEN (a token with repo scope) to include private repos.")
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "cards")
    out_dir.mkdir(parents=True, exist_ok=True)

    commits = fetch_commits(token)
    ranked = rank_languages(commits)
    cards = {
        "language-journey.svg": render_journey(commits, ranked),
        "commit-visibility.svg": render_visibility(commits, ranked),
        "active-repos.svg": render_active(commits, ranked),
    }
    for filename, svg in cards.items():
        (out_dir / filename).write_text(svg, encoding="utf-8")
    print(f"Wrote {len(cards)} cards from {len(commits):,} commits to {out_dir}/")


if __name__ == "__main__":
    main()
