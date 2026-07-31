#!/usr/bin/env python3
"""
Regenerate the custom cyan-themed stat cards (stats.svg, langs.svg, trophies.svg)
from real GitHub data.

Run locally:   GH_TOKEN=ghp_xxx GH_LOGIN=your-username python generate_stats.py
In Actions:    env GH_TOKEN=${{ secrets.GITHUB_TOKEN }} GH_LOGIN=${{ github.repository_owner }}
Demo (no API): python generate_stats.py --demo      # uses sample numbers

Only standard-library + `requests` (install with: pip install requests).
"""
import os
import sys
import math
import json
import datetime
import urllib.request

API = "https://api.github.com/graphql"

# ----------------------------------------------------------------------------
# 1. Fetch data from GitHub
# ----------------------------------------------------------------------------
QUERY = """
query($login: String!) {
  user(login: $login) {
    name
    login
    createdAt
    followers { totalCount }
    pullRequests { totalCount }
    issues { totalCount }
    repositoriesContributedTo(contributionTypes: [COMMIT, PULL_REQUEST, ISSUE, REPOSITORY]) { totalCount }
    contributionsCollection {
      totalCommitContributions
      totalPullRequestReviewContributions
    }
    pinnedItems(first: 6, types: [REPOSITORY]) {
      nodes {
        ... on Repository {
          name description url stargazerCount
          primaryLanguage { name }
          repositoryTopics(first: 4) { nodes { topic { name } } }
        }
      }
    }
    repositories(first: 100, ownerAffiliations: OWNER, isFork: false, orderBy: {field: STARGAZERS, direction: DESC}) {
      totalCount
      nodes {
        name description url stargazerCount pushedAt
        primaryLanguage { name }
        repositoryTopics(first: 4) { nodes { topic { name } } }
        languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
          edges { size node { name color } }
        }
      }
    }
  }
}
"""


def gql(token, login):
    body = json.dumps({"query": QUERY, "variables": {"login": login}}).encode()
    req = urllib.request.Request(
        API, data=body,
        headers={"Authorization": f"bearer {token}",
                 "Content-Type": "application/json",
                 "User-Agent": "stat-card-generator"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        payload = json.load(r)
    if "errors" in payload:
        raise RuntimeError(payload["errors"])
    return payload["data"]["user"]


def fetch_stats(token, login):
    u = gql(token, login)
    repos = u["repositories"]["nodes"]
    stars = sum(r["stargazerCount"] for r in repos)

    # aggregate language sizes + remember each language's github color
    lang_size, lang_color = {}, {}
    for r in repos:
        for e in r["languages"]["edges"]:
            name = e["node"]["name"]
            lang_size[name] = lang_size.get(name, 0) + e["size"]
            lang_color[name] = e["node"]["color"] or "#22d3ee"

    commits = u["contributionsCollection"]["totalCommitContributions"]
    reviews = u["contributionsCollection"]["totalPullRequestReviewContributions"]

    # ---- featured projects: prefer pinned repos, else top by stars then recency ----
    def repo_to_proj(r):
        if not r:
            return None
        stack = []
        if r.get("primaryLanguage"):
            stack.append(r["primaryLanguage"]["name"])
        topics = [t["topic"]["name"] for t in r.get("repositoryTopics", {}).get("nodes", [])]
        stack += topics[:2]
        # de-dupe while preserving order
        stack = list(dict.fromkeys(stack))
        return {
            "name": r["name"],
            "desc": (r.get("description") or "").strip(),
            "url": r["url"],
            "stack": " · ".join(stack),
        }

    pinned = [p for p in (repo_to_proj(n) for n in u["pinnedItems"]["nodes"]) if p]
    ranked = sorted(repos, key=lambda r: (r.get("stargazerCount", 0), r.get("pushedAt") or ""), reverse=True)
    top = [p for p in (repo_to_proj(r) for r in ranked) if p]
    projects = pinned if pinned else top[:6]

    return {
        "name": (u["name"] or u["login"]),
        "login": u["login"],
        "stars": stars,
        "commits": commits,
        "reviews": reviews,
        "prs": u["pullRequests"]["totalCount"],
        "issues": u["issues"]["totalCount"],
        "contrib": u["repositoriesContributedTo"]["totalCount"],
        "followers": u["followers"]["totalCount"],
        "lang_size": lang_size,
        "lang_color": lang_color,
        "projects": projects,
    }


# ----------------------------------------------------------------------------
# 2. Rank calculation (ported from github-readme-stats)
# ----------------------------------------------------------------------------
def calculate_rank(commits, prs, issues, reviews, stars, followers):
    def exp_cdf(x):
        return 1 - 2 ** (-x)

    def log_normal_cdf(x):
        return x / (1 + x)

    COMMITS_MED, COMMITS_W = 250, 2
    PRS_MED, PRS_W = 50, 3
    ISSUES_MED, ISSUES_W = 25, 1
    REVIEWS_MED, REVIEWS_W = 2, 1
    STARS_MED, STARS_W = 50, 4
    FOLLOWERS_MED, FOLLOWERS_W = 10, 1
    TOTAL_W = COMMITS_W + PRS_W + ISSUES_W + REVIEWS_W + STARS_W + FOLLOWERS_W

    rank = 1 - (
        COMMITS_W * exp_cdf(commits / COMMITS_MED)
        + PRS_W * exp_cdf(prs / PRS_MED)
        + ISSUES_W * exp_cdf(issues / ISSUES_MED)
        + REVIEWS_W * exp_cdf(reviews / REVIEWS_MED)
        + STARS_W * log_normal_cdf(stars / STARS_MED)
        + FOLLOWERS_W * log_normal_cdf(followers / FOLLOWERS_MED)
    ) / TOTAL_W

    levels = ["S", "A+", "A", "A-", "B+", "B", "B-", "C+", "C"]
    thresholds = [1, 12.5, 25, 37.5, 50, 62.5, 75, 87.5, 100]
    pct = rank * 100
    level = next(levels[i] for i, t in enumerate(thresholds) if pct <= t)
    return level


def tier(value, cuts):
    """Return a letter grade for a single metric against ascending cut points."""
    grades = ["C", "B", "B+", "A", "A+", "S"]
    g = "C"
    for c, gr in zip(cuts, grades[1:]):
        if value >= c:
            g = gr
    return g


def fmt(n):
    return f"{n:,}"


# ----------------------------------------------------------------------------
# 3. SVG builders (same design as the hand-built cards)
# ----------------------------------------------------------------------------
def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build_stats_svg(s, rank):
    rows = [
        ("&#9733;  Total Stars Earned", fmt(s["stars"])),
        ("&#8593;  Commits (last year)", fmt(s["commits"])),
        ("&#10227;  Total PRs", fmt(s["prs"])),
        ("&#9432;  Total Issues", fmt(s["issues"])),
        ("&#128101;  Contributed to (last yr)", fmt(s["contrib"])),
    ]
    row_svg = ""
    for i, (label, val) in enumerate(rows):
        y = i * 28
        delay = 0.2 + i * 0.15
        row_svg += (
            f'<g class="row" style="animation-delay:{delay:.2f}s">'
            f'<text x="0" y="{y}" fill="#9fb6d6">{label}</text>'
            f'<text x="270" y="{y}" fill="#e6f0ff" font-weight="700">{esc(val)}</text></g>\n  '
        )
    name = esc(s["name"])
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 480 210" width="480" height="210" font-family="'Segoe UI','Helvetica Neue',Arial,sans-serif">
<defs>
  <linearGradient id="sBg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#0b1830"/><stop offset="1" stop-color="#071120"/></linearGradient>
  <linearGradient id="sRing" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#22d3ee"/><stop offset="1" stop-color="#8b5cf6"/></linearGradient>
  <style>
    text{{fill:#e6f0ff;font-family:'Segoe UI',Arial,sans-serif}}
    .mono{{font-family:'SFMono-Regular',Consolas,monospace}}
    @keyframes slide{{from{{opacity:0;transform:translateX(24px)}}to{{opacity:1;transform:translateX(0)}}}}
    .row{{opacity:0;animation:slide .6s ease forwards}}
    @keyframes ringdash{{from{{stroke-dashoffset:339}}to{{stroke-dashoffset:70}}}}
    .ring{{stroke-dasharray:339;stroke-dashoffset:339;animation:ringdash 1.6s ease forwards .3s}}
    @keyframes fade{{to{{opacity:1}}}}
    .rt{{opacity:0;animation:fade .8s ease forwards 1.4s}}
  </style>
</defs>
<rect x="1" y="1" width="478" height="208" rx="16" fill="url(#sBg)" stroke="#1f4b7a"/>
<text x="26" y="40" font-size="20" font-weight="700" fill="#7dd3fc">{name}'s GitHub Stats</text>
<line x1="26" y1="52" x2="454" y2="52" stroke="#1f4b7a"/>
<g transform="translate(390,130)">
  <circle r="54" fill="none" stroke="#10233d" stroke-width="9"/>
  <circle class="ring" r="54" fill="none" stroke="url(#sRing)" stroke-width="9" stroke-linecap="round" transform="rotate(-90)"/>
  <text class="rt" y="-2" text-anchor="middle" font-size="30" font-weight="800" fill="#e6f0ff">{rank}</text>
  <text class="rt mono" y="20" text-anchor="middle" font-size="11" fill="#7dd3fc">RANK</text>
</g>
<g class="mono" font-size="15" transform="translate(26,80)">
  {row_svg}</g>
</svg>
'''


def build_langs_svg(s):
    palette = ["#22d3ee", "#38bdf8", "#60a5fa", "#818cf8", "#a78bfa"]
    items = sorted(s["lang_size"].items(), key=lambda kv: kv[1], reverse=True)[:5]
    total = sum(v for _, v in items) or 1
    bars = ""
    for i, (name, size) in enumerate(items):
        pct = size / total * 100
        width = round(pct * 428 / 100)
        color = s["lang_color"].get(name) or palette[i % len(palette)]
        y_lbl = i * 36
        y_bar = y_lbl + 8
        d = 0.15 + i * 0.15
        bars += (
            f'<g class="lbl" style="animation-delay:{d:.2f}s"><text x="0" y="{y_lbl}" fill="#cfe3ff">{esc(name)}</text>'
            f'<text x="428" y="{y_lbl}" text-anchor="end" fill="#9fb6d6">{pct:.0f}%</text></g>\n  '
            f'<rect x="0" y="{y_bar}" width="428" height="10" rx="5" fill="#10233d"/>\n  '
            f'<rect class="bar" x="0" y="{y_bar}" width="{width}" height="10" rx="5" fill="{color}" style="animation-delay:{d:.2f}s"/>\n  '
        )
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 480 210" width="480" height="210" font-family="'Segoe UI','Helvetica Neue',Arial,sans-serif">
<defs>
  <linearGradient id="lBg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#0b1830"/><stop offset="1" stop-color="#071120"/></linearGradient>
  <style>
    text{{fill:#e6f0ff;font-family:'Segoe UI',Arial,sans-serif}}
    .mono{{font-family:'SFMono-Regular',Consolas,monospace}}
    @keyframes grow{{from{{width:0}}}}
    .bar{{animation:grow 1.4s cubic-bezier(.2,.8,.2,1) forwards}}
    @keyframes fu{{from{{opacity:0;transform:translateY(8px)}}to{{opacity:1;transform:translateY(0)}}}}
    .lbl{{opacity:0;animation:fu .5s ease forwards}}
  </style>
</defs>
<rect x="1" y="1" width="478" height="208" rx="16" fill="url(#lBg)" stroke="#1f4b7a"/>
<text x="26" y="40" font-size="20" font-weight="700" fill="#7dd3fc">Most Used Languages</text>
<line x1="26" y1="52" x2="454" y2="52" stroke="#1f4b7a"/>
<g transform="translate(26,74)" class="mono" font-size="14">
  {bars}</g>
</svg>
'''


def build_trophies_svg(s):
    cells = [
        ("Commits",  tier(s["commits"],  [10, 100, 500, 1000, 2000])),
        ("Stars",    tier(s["stars"],    [1, 10, 50, 200, 1000])),
        ("PRs",      tier(s["prs"],      [1, 10, 50, 100, 300])),
        ("Issues",   tier(s["issues"],   [1, 10, 50, 100, 300])),
        ("Followers",tier(s["followers"],[1, 10, 50, 200, 1000])),
        ("Contrib",  tier(s["contrib"],  [1, 5, 15, 40, 100])),
    ]
    body = ""
    for i, (label, grade) in enumerate(cells):
        x = 75 + i * 150
        d = 0.1 + i * 0.12
        body += (
            f'<g transform="translate({x},60)"><g class="cell" style="animation-delay:{d:.2f}s">'
            f'<path d="M-18 -22 h36 v10 a18 18 0 0 1 -36 0 z" fill="url(#cup)"/>'
            f'<rect x="-6" y="-4" width="12" height="10" fill="#c47f16"/>'
            f'<rect x="-14" y="6" width="28" height="6" rx="2" fill="#c47f16"/>'
            f'<text class="rk" y="34" text-anchor="middle" font-size="15" font-weight="800" fill="#ffd76a">{grade}</text>'
            f'<text y="52" text-anchor="middle" font-size="11" fill="#9fb6d6">{label}</text></g></g>\n    '
        )
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 900 150" width="900" height="150" font-family="'Segoe UI','Helvetica Neue',Arial,sans-serif">
<defs>
  <linearGradient id="tBg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#0b1830"/><stop offset="1" stop-color="#071120"/></linearGradient>
  <linearGradient id="cup" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffd76a"/><stop offset="1" stop-color="#f5a623"/></linearGradient>
  <linearGradient id="tShine" x1="0" y1="0" x2="1" y2="0">
    <stop offset="0" stop-color="#fff" stop-opacity="0"/><stop offset="0.5" stop-color="#fff" stop-opacity="0.5"/><stop offset="1" stop-color="#fff" stop-opacity="0"/>
  </linearGradient>
  <clipPath id="tClip"><rect x="0" y="0" width="900" height="150" rx="14"/></clipPath>
  <style>
    text{{fill:#e6f0ff;font-family:'Segoe UI',Arial,sans-serif;font-family:'SFMono-Regular',Consolas,monospace}}
    @keyframes pop{{0%{{opacity:0;transform:scale(.5)}}70%{{transform:scale(1.08)}}100%{{opacity:1;transform:scale(1)}}}}
    .cell{{opacity:0;transform-box:fill-box;transform-origin:center;animation:pop .5s ease forwards}}
    @keyframes rankglow{{0%,100%{{opacity:.7}}50%{{opacity:1}}}}
    .rk{{animation:rankglow 2.4s ease-in-out infinite}}
    @keyframes shineX{{0%{{transform:translateX(-960px)}}55%,100%{{transform:translateX(960px)}}}}
    #tsh{{animation:shineX 4.5s ease-in-out infinite}}
  </style>
</defs>
<g clip-path="url(#tClip)">
  <rect width="900" height="150" fill="url(#tBg)" stroke="#1f4b7a"/>
  <g font-family="'SFMono-Regular',Consolas,monospace">
    {body}</g>
  <rect id="tsh" x="-200" y="0" width="160" height="150" fill="url(#tShine)" opacity="0.5" transform="skewX(-18)"/>
</g>
</svg>
'''


# ----------------------------------------------------------------------------
# 4. Featured projects -> rewrite the README table between markers
# ----------------------------------------------------------------------------
PROJ_START = "<!-- PROJECTS:START -->"
PROJ_END = "<!-- PROJECTS:END -->"


def build_projects_md(projects):
    lines = ["| 🚀 Project | Description | Stack |", "|:--|:--|:--|"]
    if not projects:
        lines.append("| _No public repositories yet_ | Come back soon — building in progress! | — |")
        return "\n".join(lines)
    for p in projects[:6]:
        desc = (p["desc"] or "—").replace("|", "\\|")
        if len(desc) > 90:
            desc = desc[:87].rstrip() + "…"
        stack = (p["stack"] or "—").replace("|", "\\|")
        lines.append(f"| **[{p['name']}]({p['url']})** | {desc} | {stack} |")
    return "\n".join(lines)


def update_readme_projects(readme_path, projects):
    if not os.path.exists(readme_path):
        print("README.md not found, skipping projects update.")
        return
    text = open(readme_path, encoding="utf-8").read()
    if PROJ_START not in text or PROJ_END not in text:
        print("Project markers not found in README, skipping.")
        return
    table = build_projects_md(projects)
    new = re.sub(
        re.escape(PROJ_START) + r".*?" + re.escape(PROJ_END),
        f"{PROJ_START}\n{table}\n{PROJ_END}",
        text, flags=re.S,
    )
    if new != text:
        open(readme_path, "w", encoding="utf-8").write(new)
        print(f"updated {readme_path} with {len(projects)} projects")
    else:
        print("README projects already up to date.")


# ----------------------------------------------------------------------------
# 4b. Years of experience -> auto-computed from career start, patched everywhere
# ----------------------------------------------------------------------------
CAREER_START = datetime.date(2020, 1, 6)  # first role (TCS), from resume


def years_of_experience(today=None):
    today = today or datetime.date.today()
    return f"{(today - CAREER_START).days / 365.25:.1f}"


def update_years(outdir, years):
    # README badge + whoami sentence
    rp = os.path.join(outdir, "README.md")
    if os.path.exists(rp):
        t = open(rp, encoding="utf-8").read()
        o = t
        t = re.sub(r'Experience-[\d.]+%2B', f'Experience-{years}%2B', t)
        t = re.sub(r'\*\*[\d.]+\+ years\*\*', f'**{years}+ years**', t)
        if t != o:
            open(rp, "w", encoding="utf-8").write(t)
            print(f"README years -> {years}")

    # banner.svg + banner-light.svg (about line + code-card exp value)
    for bf in ("banner.svg", "banner-light.svg"):
        bp = os.path.join(outdir, bf)
        if not os.path.exists(bp):
            continue
        t = open(bp, encoding="utf-8").read()
        o = t
        t = re.sub(r'(&#8226; )[\d.]+( yrs shipping GenAI)', rf'\g<1>{years}\g<2>', t)
        t = re.sub(r'(>)[\d.]+(</tspan> \+ <tspan[^>]*>&#34;yrs&#34;)', rf'\g<1>{years}\g<2>', t)
        if t != o:
            open(bp, "w", encoding="utf-8").write(t)
            print(f"{bf} years -> {years}")

    # whoami.svg terminal card ("6.6+ yrs")
    wp = os.path.join(outdir, "whoami.svg")
    if os.path.exists(wp):
        t = open(wp, encoding="utf-8").read()
        o = t
        t = re.sub(r'[\d.]+\+ yrs', f'{years}+ yrs', t)
        if t != o:
            open(wp, "w", encoding="utf-8").write(t)
            print(f"whoami.svg years -> {years}")


# ----------------------------------------------------------------------------
# 5. Main
# ----------------------------------------------------------------------------
import re  # noqa: E402  (used by update_readme_projects)

DEMO = {
    "name": "Himanshu Verma", "login": "himanshuverma0912",
    "stars": 128, "commits": 1204, "reviews": 22, "prs": 96, "issues": 54,
    "contrib": 17, "followers": 43,
    "lang_size": {"Python": 580000, "Jupyter Notebook": 180000,
                  "TypeScript": 120000, "Shell": 80000, "SQL": 40000},
    "lang_color": {"Python": "#3572A5", "Jupyter Notebook": "#DA5B0B",
                   "TypeScript": "#3178c6", "Shell": "#89e051", "SQL": "#e38c00"},
    "projects": [
        {"name": "Real-Time LLM Cost Estimation Engine",
         "desc": "Web app comparing real-time inference pricing across 500+ LLMs",
         "url": "https://github.com/himanshuverma0912", "stack": "Next.js · TypeScript"},
        {"name": "Agentic Equity Research Command Center",
         "desc": "Multi-agent financial workstation; vectorless PageIndex + MCP over SQL & docs",
         "url": "https://github.com/himanshuverma0912", "stack": "Python · LangGraph · Gemini"},
    ],
}


def main():
    outdir = os.environ.get("OUT_DIR", ".")
    if "--demo" in sys.argv:
        s = DEMO
        print("Using demo data.")
    else:
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        login = os.environ.get("GH_LOGIN") or os.environ.get("GITHUB_REPOSITORY_OWNER")
        if not token or not login:
            sys.exit("Set GH_TOKEN and GH_LOGIN (or run with --demo).")
        s = fetch_stats(token, login)
        print(f"Fetched stats for @{s['login']}: {s['stars']} stars, {s['commits']} commits")

    rank = calculate_rank(s["commits"], s["prs"], s["issues"],
                          s["reviews"], s["stars"], s["followers"])
    files = {
        "stats.svg": build_stats_svg(s, rank),
        "langs.svg": build_langs_svg(s),
        "trophies.svg": build_trophies_svg(s),
    }
    for name, content in files.items():
        path = os.path.join(outdir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        print("wrote", path)

    # refresh the featured-projects table in the README (between markers)
    update_readme_projects(os.path.join(outdir, "README.md"), s.get("projects", []))

    # refresh auto-computed years of experience everywhere
    update_years(outdir, years_of_experience())


if __name__ == "__main__":
    main()
