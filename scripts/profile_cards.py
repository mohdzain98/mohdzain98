"""Generate GitHub profile SVG cards from the GraphQL API.

Collects every commit authored by the token owner across owned, collaborator
and organization repos (deduplicated by oid, so forks never double count), then
renders cards: language journey, visibility, active repos, repo constellation.
Run by the profile-cards workflow; private repo names never appear in output.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
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


def fetch_commits(token: str, repos: list[dict], author_id: str) -> list[Commit]:
    """Collect the author's commits on each repo's default branch.

    Commits are deduplicated by oid across repos (fork and upstream overlap).
    """
    seen: set[str] = set()
    commits: list[Commit] = []
    for repo in repos:
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
        ".pn{font-size:17px;font-weight:700;fill:#ffffff;paint-order:stroke;"
        "stroke:rgba(0,0,0,.28);stroke-width:2.5px;stroke-linejoin:round}"
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


def family_key(name: str) -> str:
    """First token of a repo name, used to link related repos (surfmind-*, docschat-*)."""
    return re.split(r"[-_.]", name.lower(), maxsplit=1)[0]


def stable_unit(text: str) -> float:
    """Deterministic pseudo-random number in [0, 1) derived from text."""
    return int(hashlib.md5(text.encode()).hexdigest()[:8], 16) / 0x100000000


def node_box(n: dict) -> tuple[float, float, float, float]:
    """Bounding box (x0, y0, x1, y1) of a star plus its label, if any."""
    half = max(n["r"], 7.0)
    x0, x1 = n["x"] - n["r"], n["x"] + n["r"]
    if n["label"]:
        if n["label_left"]:
            x0 -= 5 + n["label_w"]
        else:
            x1 += 5 + n["label_w"]
    return x0, n["y"] - half, x1, n["y"] + half


def relax(nodes: list[dict], bounds: tuple[float, float, float, float]) -> None:
    """Push overlapping stars and labels apart vertically, in place.

    x stays on its timeline position so the left-to-right story holds.
    """
    _, _, top, bottom = bounds
    pad = 4.0
    for _ in range(600):
        moved = False
        for i, a in enumerate(nodes):
            for b in nodes[i + 1:]:
                ax0, ay0, ax1, ay1 = node_box(a)
                bx0, by0, bx1, by1 = node_box(b)
                if ax1 + pad <= bx0 or bx1 + pad <= ax0 or ay1 + pad <= by0 or by1 + pad <= ay0:
                    continue
                overlap = min(ay1, by1) - max(ay0, by0) + pad
                up = a["y"] < b["y"] or (a["y"] == b["y"] and stable_unit(a["repo"]) < 0.5)
                a["y"] += -overlap / 2 if up else overlap / 2
                b["y"] += overlap / 2 if up else -overlap / 2
                moved = True
        for n in nodes:
            n["y"] = min(max(n["y"], top), bottom)
        if not moved:
            break


def render_constellation(commits: list[Commit], ranked: list[str], login: str) -> str:
    """Animated night-sky card: one star per repo, lit in order of first commit.

    Star size encodes commits, color the language; related repos are linked.
    Private repos render as dim, unlabeled stars so their names never leak.
    """
    stats: dict[str, dict] = {}
    for c in commits:
        s = stats.setdefault(c.repo, {"n": 0, "first": c.when, "lang": c.language, "private": c.private})
        s["n"] += 1
        s["first"] = min(s["first"], c.when)
    repos = sorted(stats.items(), key=lambda kv: kv[1]["first"])

    roots: list[str] = []
    family: dict[str, str] = {}
    for key in sorted({family_key(r.split("/", 1)[1]) for r, _ in repos}, key=len):
        root = next((p for p in roots if len(p) >= 4 and key.startswith(p)), None)
        family[key] = root or key
        if root is None:
            roots.append(key)

    width, height = 640, 340
    left, right, top, bottom = 36, width - 36, 100, height - 46
    t0, t1 = repos[0][1]["first"], repos[-1][1]["first"]
    span = max((t1 - t0).total_seconds(), 86400)
    max_n = max(s["n"] for _, s in repos)
    public_ranked = sorted((r for r, s in repos if not s["private"]), key=lambda r: -stats[r]["n"])
    labelled = set(public_ranked[:8])

    nodes = []
    for repo, s in repos:
        fam = family[family_key(repo.split("/", 1)[1])]
        frac = (s["first"] - t0).total_seconds() / span
        nodes.append({
            "repo": repo, "fam": fam, "frac": frac,
            "x": left + (right - left) * frac,
            "y": top + (bottom - top) * (0.12 + 0.76 * stable_unit(fam)) + (stable_unit(repo) - 0.5) * 36,
            "r": 2.5 + 7.5 * math.sqrt(s["n"] / max_n),
            "label": repo in labelled, **s,
        })
    for n in nodes:
        name = n["repo"].split("/", 1)[1]
        n["name"] = name if len(name) <= 22 else name[:21] + "…"
        n["label_w"] = (len(n["name"]) + len(str(n["n"])) + 1) * 5.9
        n["label_left"] = n["x"] + n["r"] + 5 + n["label_w"] > width - 14
    relax(nodes, (left, right, top, bottom))

    play = 6.0  # seconds for the timeline to play through
    def delay(frac: float) -> float:
        return 0.4 + play * frac

    def star_fill(lang: str) -> str:
        return f"var(--s{ranked.index(lang)})" if lang in ranked else "var(--so)"

    body = []
    for i in range(50):  # faint background dust
        dx, dy = stable_unit(f"dx{i}"), stable_unit(f"dy{i}")
        body.append(f'<circle cx="{14 + dx * (width - 28):.1f}" cy="{60 + dy * (height - 74):.1f}" '
                    f'r="{0.5 + stable_unit(f"dr{i}") * 0.8:.2f}" class="dust"/>')

    for year in range(t0.year + 1, t1.year + 1):
        frac = (datetime(year, 1, 1, tzinfo=timezone.utc) - t0).total_seconds() / span
        x = left + (right - left) * frac
        d = f"animation-delay:{delay(frac):.2f}s"
        body.append(f'<line class="tick fade" x1="{x:.1f}" x2="{x:.1f}" y1="{top - 8}" y2="{height - 34}" style="{d}"/>')
        body.append(f'<text class="m fade" x="{x:.1f}" y="{height - 18}" text-anchor="middle" style="{d}">{year}</text>')

    by_family: dict[str, list[dict]] = defaultdict(list)
    for n in nodes:
        by_family[n["fam"]].append(n)
    for members in by_family.values():
        for a, b in zip(members, members[1:]):
            if math.hypot(a["x"] - b["x"], a["y"] - b["y"]) > 240:
                continue  # long cross-sky lines read as clutter
            d = f"animation-delay:{delay(max(a['frac'], b['frac'])) + 0.3:.2f}s"
            op = 0.25 if a["private"] or b["private"] else 0.45
            body.append(f'<line class="link" pathLength="1" x1="{a["x"]:.1f}" y1="{a["y"]:.1f}" '
                        f'x2="{b["x"]:.1f}" y2="{b["y"]:.1f}" opacity="{op}" style="{d}"/>')

    body.append(f'<line class="sweep" x1="{left}" x2="{left}" y1="{top - 8}" y2="{height - 34}" '
                f'style="--travel:{right - left}px;animation-duration:{play:.1f}s;animation-delay:0.4s"/>')

    for n in nodes:
        d = delay(n["frac"])
        fill = star_fill(n["lang"])
        dim = ' opacity="0.42"' if n["private"] else ""
        twinkle = f"animation-delay:{d + 0.7 + stable_unit(n['repo'] + 't') * 2:.2f}s"
        body.append(
            f'<g class="star" style="animation-delay:{d:.2f}s"><g{dim}><g class="tw" style="{twinkle}">'
            f'<circle cx="{n["x"]:.1f}" cy="{n["y"]:.1f}" r="{n["r"] * 1.9:.1f}" style="fill:{fill}" '
            f'opacity="0.35" filter="url(#glow)"/>'
            f'<circle cx="{n["x"]:.1f}" cy="{n["y"]:.1f}" r="{n["r"]:.1f}" style="fill:{fill}"/>'
            f'<circle cx="{n["x"]:.1f}" cy="{n["y"]:.1f}" r="{max(n["r"] * 0.35, 0.8):.1f}" class="core"/>'
            f"</g></g></g>"
        )
        if n["label"]:
            lx = n["x"] - n["r"] - 5 if n["label_left"] else n["x"] + n["r"] + 5
            anchor = "end" if n["label_left"] else "start"
            body.append(f'<text class="lb fade" x="{lx:.1f}" y="{n["y"] + 3.5:.1f}" text-anchor="{anchor}" '
                        f'style="animation-delay:{d + 0.2:.2f}s">{escape(n["name"])} '
                        f'<tspan class="m">{n["n"]}</tspan></text>')

    langs = [l for l in ranked if any(n["lang"] == l for n in nodes)]
    leg_items = [(l, star_fill(l)) for l in langs]
    if any(n["lang"] not in ranked for n in nodes):
        leg_items.append(("Other", "var(--so)"))
    leg, _ = legend(leg_items, 20, 76, width - 20)
    leg += (f'<circle cx="{width - 104}" cy="72" r="4" style="fill:var(--s0)" opacity="0.42"/>'
            f'<text class="m" x="{width - 95}" y="76">Private (unnamed)</text>')

    n_private = sum(1 for n in nodes if n["private"])
    n_own = sum(1 for n in nodes if n["repo"].split("/", 1)[0].lower() == login.lower())
    subtitle = (f"{len(nodes)} repos · {n_own} own, {len(nodes) - n_own} contributed · "
                f"size = commits · lit by first commit · {n_private} private, unnamed")
    css = (
        "<style>"
        + "svg{" + "".join(f"--s{i}:{c};" for i, c in enumerate(PALETTE_DARK)) + f"--so:{OTHER_DARK};}}"
        f"text{{font-family:{FONT}}}"
        ".sky{fill:#0b0f19;stroke:#30363d}"
        ".h{font-size:16px;font-weight:600;fill:#e6edf3}.sub{font-size:11px;fill:#9198a1}"
        ".m{font-size:10px;fill:#8b949e}.lb{font-size:10.5px;fill:#d0d7de}"
        ".dust{fill:#c9d1d9;opacity:.22}.core{fill:#ffffff;opacity:.85}"
        ".tick{stroke:#21262d;stroke-dasharray:2 4}"
        ".link{stroke:#c9d1d9;stroke-width:1;stroke-dasharray:1;animation:draw .9s ease-out both}"
        ".sweep{stroke:#58a6ff;stroke-width:1;opacity:0;animation:sweep linear both}"
        ".star{transform-box:fill-box;transform-origin:center;animation:pop .7s cubic-bezier(.2,.9,.3,1.3) both}"
        ".tw{animation:tw 2.6s ease-in-out infinite alternate}"
        ".fade{animation:fade .6s ease-out both}"
        "@keyframes pop{from{opacity:0;transform:scale(0)}to{opacity:1;transform:scale(1)}}"
        "@keyframes tw{from{opacity:1}to{opacity:.6}}"
        "@keyframes fade{from{opacity:0}to{opacity:1}}"
        "@keyframes draw{from{stroke-dashoffset:1}to{stroke-dashoffset:0}}"
        "@keyframes sweep{0%{opacity:.55;transform:translateX(0)}92%{opacity:.55}"
        "100%{opacity:0;transform:translateX(var(--travel))}}"
        "@media (prefers-reduced-motion:reduce){*{animation:none!important}.sweep{display:none}}"
        "</style>"
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="Repo constellation">{css}'
        '<defs><filter id="glow" x="-1" y="-1" width="3" height="3">'
        '<feGaussianBlur stdDeviation="3"/></filter></defs>'
        f'<rect class="sky" x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="6"/>'
        '<text class="h" x="20" y="32">Repo Constellation</text>'
        f'<text class="sub" x="20" y="50">{escape(subtitle)}</text>'
        f'{leg}{"".join(body)}</svg>'
    )


def pie_slice(cx: float, cy: float, r: float, start: float, end: float) -> str:
    """SVG path for a pie wedge between two angles in radians (0 = 12 o'clock)."""
    if end - start >= 2 * math.pi - 1e-6:
        return f"M{cx},{cy - r}A{r},{r} 0 1 1 {cx - 0.01},{cy - r}Z"
    x0, y0 = cx + r * math.sin(start), cy - r * math.cos(start)
    x1, y1 = cx + r * math.sin(end), cy - r * math.cos(end)
    large = 1 if end - start > math.pi else 0
    return f"M{cx},{cy}L{x0:.2f},{y0:.2f}A{r},{r} 0 {large} 1 {x1:.2f},{y1:.2f}Z"


def render_repo_languages(owned: list[dict], ranked: list[str]) -> str:
    """Big pie of the top 5 primary languages across repos the user owns (no forks).

    Each slice carries only its repo count; a compact legend sits underneath.
    """
    counts = Counter((r["primaryLanguage"] or {}).get("name") for r in owned)
    counts.pop(None, None)
    top = sorted(counts, key=lambda l: (-counts[l], l))[:5]
    total = sum(counts[l] for l in top) or 1

    width, height = 320, 340
    cx, cy, r = width / 2, 172, 104
    body, angle = [], 0.0
    for lang in top:
        sweep = 2 * math.pi * counts[lang] / total
        body.append(f'<path d="{pie_slice(cx, cy, r, angle, angle + sweep)}" '
                    f'style="fill:{color(lang, ranked)};stroke:var(--bg);stroke-width:2;stroke-linejoin:round"/>')
        mid, dist = angle + sweep / 2, r * (0.62 if sweep > 0.6 else 0.72)
        tx, ty = cx + dist * math.sin(mid), cy - dist * math.cos(mid)
        body.append(f'<text class="pn" x="{tx:.1f}" y="{ty + 6:.1f}" text-anchor="middle">{counts[lang]}</text>')
        angle += sweep

    leg_items = [(lang, color(lang, ranked)) for lang in top]
    for row, items in enumerate((leg_items[:3], leg_items[3:])):  # two centered rows
        row_w = sum(15 + len(l) * 6.0 + 12 for l, _ in items) - 12
        body.append(legend(items, (width - row_w) / 2, 298 + row * 18, width)[0])

    n_repos = sum(counts.values())
    subtitle = f"Top 5 · {n_repos} own repos · forks excluded"
    return frame(width, height, "Top Languages by Repo", subtitle, "".join(body))

def main() -> None:
    """Fetch repos and commits once, then write every card to the output directory."""
    token = os.environ.get("GH_STATS_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("Set GH_STATS_TOKEN (a token with repo scope) to include private repos.")
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "cards")
    out_dir.mkdir(parents=True, exist_ok=True)

    viewer = graphql(token, "{ viewer { id login } }")["viewer"]
    profile_repo = f"{viewer['login']}/{viewer['login']}".lower()
    repos = [r for r in fetch_repos(token) if r["nameWithOwner"].lower() != profile_repo]  # config, not a project
    owned = [r for r in repos if not r["isFork"]
             and r["nameWithOwner"].split("/", 1)[0].lower() == viewer["login"].lower()]
    commits = fetch_commits(token, repos, viewer["id"])
    ranked = rank_languages(commits)
    cards = {
        "language-journey.svg": render_journey(commits, ranked),
        "commit-visibility.svg": render_visibility(commits, ranked),
        "active-repos.svg": render_active(commits, ranked),
        "repo-constellation.svg": render_constellation(commits, ranked, viewer["login"]),
        "repo-languages.svg": render_repo_languages(owned, ranked),
    }
    for filename, svg in cards.items():
        (out_dir / filename).write_text(svg, encoding="utf-8")
    print(f"Wrote {len(cards)} cards from {len(commits):,} commits to {out_dir}/")


if __name__ == "__main__":
    main()
