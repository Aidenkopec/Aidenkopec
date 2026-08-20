#!/usr/bin/env python3
"""Render a live neofetch-style profile card to dark_mode.svg and light_mode.svg.

Static content comes from config.json. Everything else is measured at run time:
service health is probed over HTTPS, and repo counts, star counts, commit totals
and the language breakdown all come from one paged GraphQL query.

Stdlib only.

    ACCESS_TOKEN=<pat> python3 today.py     # the real thing, overwrites the SVGs
    python3 today.py --offline              # no token, no network, writes preview/
"""

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo  # stdlib since 3.9; needs system tzdata
except ImportError:  # pragma: no cover - only on a build without tzdata
    ZoneInfo = None

HERE = os.path.dirname(os.path.abspath(__file__))
PREVIEW = os.path.join(HERE, "preview")

TOKEN = os.environ.get("ACCESS_TOKEN", "")
GRAPHQL = "https://api.github.com/graphql"

# Layout. The card is a fixed character grid; every x is column * CHAR_W.
# CHAR_W is the advance width of the rendered font at FONT_SIZE: Menlo and DejaVu
# Sans Mono both advance 1233/2048 em (9.63px at 16px), and the size-adjust in the
# @font-face below is what pulls Consolas' narrower 1126/2048 em up to match.
CHAR_W = 9.63
LINE_H = 20
PAD = 15
RADIUS = 15
FONT_SIZE = 16
ASCII_COLS = 42
GUTTER_COLS = 2
PANEL_COLS = 60
INDENT = " "  # matches MARGIN in tools/asciify.py, so the bar blocks line up with the art

# Glacier Sapphire, lifted from aidenkopec-portfolio/app/globals.css. #60A5FA is the
# accent of the theme layout.tsx actually boots with, so the card and the site share
# a literal hex. add/del borrow the Aurora Jade and Obsidian accents from the same
# theme switcher, and mark0/mark1 are the monogram's gradient stops.
THEMES = {
    "dark_mode": {
        "bg": "#07121A", "edge": "#12304A", "fg": "#E6F0FA", "key": "#60A5FA",
        "value": "#A8C7E8", "dim": "#37607C", "add": "#2DD4BF", "del": "#FF6B6B",
        "mark0": "#7DD3FC", "mark1": "#3B82F6",
    },
    "light_mode": {
        "bg": "#F7FAFD", "edge": "#DCE7F2", "fg": "#0B2740", "key": "#1D4ED8",
        "value": "#1E4A72", "dim": "#8FA6BC", "add": "#0F766E", "del": "#C2410C",
        "mark0": "#3B82F6", "mark1": "#0B2740",
    },
}

# The bar blocks that fill the left column under the monogram. Both use the same
# grid so their bars line up as one column: an indent, a label, the bar, then a
# right-aligned value.
LANG_COUNT = 6            # named languages before everything else becomes "other"
BAR_LABEL_COLS = 12
BAR_COLS = 16
BAR_VALUE_COLS = 5

# Text baselines sit this far above the bottom of their row band, so descenders
# clear the card edge.
BASELINE_LIFT = 4

# Grown onto every art rectangle's right and bottom edge, so merged blocks
# overlap by a hair instead of abutting and letting the background through.
OVERLAP = 0.5

# ascii.txt is authored and committed as text, but the monogram is *drawn* as
# rectangles rather than typeset. Two reasons, both measured rather than assumed:
# a full block is 1.027em tall, so at FONT_SIZE it stands 16.4px inside a 20px
# row and the mark renders as venetian blinds; and abutting glyphs leave a
# hairline seam at every cell boundary because ink width and advance width are
# not the same number. Drawing the quadrants lands them exactly on the grid, in
# every viewer, and sidesteps the fact that ▘▝▖▗ have thinner font coverage than
# the half-blocks they replaced. The panel stays real text -- it is terminal
# output, and it should be selectable and searchable.
GLYPH_BITS = {
    " ": (0, 0, 0, 0), "▘": (1, 0, 0, 0), "▝": (0, 1, 0, 0), "▀": (1, 1, 0, 0),
    "▖": (0, 0, 1, 0), "▌": (1, 0, 1, 0), "▞": (0, 1, 1, 0), "▛": (1, 1, 1, 0),
    "▗": (0, 0, 0, 1), "▚": (1, 0, 0, 1), "▐": (0, 1, 0, 1), "▜": (1, 1, 0, 1),
    "▄": (0, 0, 1, 1), "▙": (1, 0, 1, 1), "▟": (0, 1, 1, 1), "█": (1, 1, 1, 1),
}

# Ink coverage digit (see tools/asciify.py) -> css class. Softening the partly
# covered cells is what stops the letterforms reading as a staircase.
COVER_TIERS = ((7, None), (3, "aa2"), (-1, "aa1"))


# ----------------------------------------------------------------- http

def _request(url, headers=None, data=None, timeout=15):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    ctx = ssl.create_default_context()
    return urllib.request.urlopen(req, timeout=timeout, context=ctx)


def gh_headers():
    if not TOKEN:
        sys.exit("ACCESS_TOKEN is not set. Create a PAT with read:user and repo scope.")
    return {
        "Authorization": "bearer " + TOKEN,
        "Accept": "application/vnd.github+json",
        "User-Agent": "profile-card",
    }


def graphql(query, variables, attempts=3, required=True):
    """POST the query, retrying transport failures.

    probe() already retries; this did not, so a single 502 from GitHub -- which
    happens -- would fail an unattended cron and leave the card stale for six
    hours. A GraphQL `errors` payload is not retried: that means the query is
    wrong, and repeating it will not make it right.

    `required=False` reports the failure and returns None instead of exiting, for
    a query that only enriches the card. Losing detail on one row beats failing
    the build and leaving every row stale until the next run.
    """
    body = json.dumps({"query": query, "variables": variables}).encode()
    headers = gh_headers()
    headers["Content-Type"] = "application/json"

    def failed(message):
        if required:
            sys.exit(message)
        sys.stderr.write(message + "\n")
        return None

    for attempt in range(attempts):
        try:
            with _request(GRAPHQL, headers, body) as resp:
                payload = json.loads(resp.read().decode())
        except Exception as exc:
            if attempt + 1 == attempts:
                return failed("GraphQL request failed after %d attempts: %s" % (attempts, exc))
            time.sleep(2 * (attempt + 1))
            continue
        if "errors" in payload:
            return failed("GraphQL error: " + json.dumps(payload["errors"]))
        return payload["data"]


# ----------------------------------------------------------------- probes

def probe(url, attempts=2):
    """Return (ok, status, latency_ms). Never raises; a dead service is data.

    Retries once: the card can stand for six hours, which is far too long to show a
    live site as down because one TCP connect happened to lose a race.
    """
    status = None
    for attempt in range(attempts):
        started = time.time()
        try:
            with _request(url, {"User-Agent": "profile-card"}, timeout=10) as resp:
                status = resp.status
                resp.read(1)
        except urllib.error.HTTPError as exc:
            status = exc.code
        except Exception:
            status = None
            if attempt + 1 < attempts:
                time.sleep(2)
            continue
        latency = int((time.time() - started) * 1000)
        return 200 <= status < 400, status, latency
    return False, status, None


# ----------------------------------------------------------------- github

USER_QUERY = """
query($login: String!, $after: String) {
  user(login: $login) {
    createdAt
    followers { totalCount }
    starredRepositories { totalCount }
    repositories(first: 100, after: $after, ownerAffiliations: OWNER, isFork: false) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        name
        isPrivate
        stargazerCount
        pushedAt
        languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
          edges { size node { name color } }
        }
      }
    }
    contributionsCollection {
      totalCommitContributions
      restrictedContributionsCount
    }
  }
}
"""

PROFILE_QUERY = """
query($login: String!) {
  repository(owner: $login, name: $login) {
    defaultBranchRef {
      target {
        ... on Commit {
          history(first: 100) { nodes { committedDate author { user { login } } } }
        }
      }
    }
  }
}
"""


def profile_repo_push(login):
    """Newest commit in the profile repo that `login` wrote themselves, or None.

    The profile repo's own pushedAt is useless as a signal: this workflow commits
    the rendered SVGs back into it on every run, so the timestamp reports the cron
    schedule rather than any work -- it read ~5h ago forever, matching the previous
    bot commit to the second. Skipping the repo outright was the first fix and
    traded one wrong answer for another: it hid real commits here, including every
    change to this file. Filtering the bot out by author keeps them visible.

    Its own request, and a non-fatal one, because this is the only part of the card
    that is an enrichment rather than a fact: if the field ever changes shape,
    "Last push" loses one repo's worth of reach instead of the cron failing and
    every row on the card going stale.

    Only the most recent commits are searched. If the user's last commit here is
    older than those, some other repo is newer anyway and this could never win.
    """
    data = graphql(PROFILE_QUERY, {"login": login}, required=False)
    ref = ((data or {}).get("repository") or {}).get("defaultBranchRef") or {}
    history = (ref.get("target") or {}).get("history") or {}
    for node in history.get("nodes") or []:
        author = (node.get("author") or {}).get("user") or {}
        if (author.get("login") or "").lower() == login.lower():
            return node["committedDate"]
    return None


def github_stats(login, exclude=()):
    """Everything the panel needs, from one paged query.

    Forks are filtered server-side with `isFork: false`. `exclude` drops repos
    that are owned but not authored from the **language bytes only**: GitHub
    reports a repo's whole language breakdown regardless of who wrote it, so one
    vendored project can dominate the bars with code that is not yours.

    It deliberately does not touch the repo count or the star sum. Those have to
    agree with what a visitor sees on the profile, and an excluded repo is still
    listed there and can still be starred. Filtering them as well is what made
    the card report 6 public against a profile showing 7.

    "Last push" is the newest pushedAt across every repo but the profile repo,
    plus the newest commit in that one the user wrote themselves -- see
    profile_repo_push for why it cannot speak for itself.
    """
    after, pushed = None, None
    stars, languages = 0, {}
    public = private = 0
    user = None
    skip = {name.lower() for name in exclude}

    while True:
        user = graphql(USER_QUERY, {"login": login, "after": after})["user"]
        block = user["repositories"]
        for node in block["nodes"]:
            if node["isPrivate"]:
                private += 1
            else:
                public += 1
            stars += node["stargazerCount"]
            # The profile repo's pushedAt is ignored here and only here; its real
            # commits come back through profile_repo_push below.
            if node["name"].lower() != login.lower():
                if node["pushedAt"] and (pushed is None or node["pushedAt"] > pushed):
                    pushed = node["pushedAt"]
            if node["name"].lower() in skip:
                continue  # counted and starred above; only its bytes are dropped
            for edge in (node.get("languages") or {}).get("edges") or []:
                name = edge["node"]["name"]
                entry = languages.setdefault(name, {"size": 0, "color": edge["node"]["color"]})
                entry["size"] += edge["size"]
        if not block["pageInfo"]["hasNextPage"]:
            break
        after = block["pageInfo"]["endCursor"]

    authored = profile_repo_push(login)
    if authored and (pushed is None or authored > pushed):
        pushed = authored

    contrib = user["contributionsCollection"]
    return {
        "repos_public": public,
        "repos_private": private,
        "created_at": user["createdAt"],
        "stars": stars,
        "starred": user["starredRepositories"]["totalCount"],
        "followers": user["followers"]["totalCount"],
        "commits": contrib["totalCommitContributions"] + contrib["restrictedContributionsCount"],
        "pushed_at": pushed,
        "languages": languages,
    }


def language_shares(languages, count=LANG_COUNT):
    """Top `count` languages by bytes as (name, percent, color), plus an "other".

    Percentages are apportioned largest-remainder so they sum to exactly 100 and
    the bars cannot add up to more or less than a full row.
    """
    total = sum(entry["size"] for entry in languages.values())
    if not total:
        return []
    ranked = sorted(languages.items(), key=lambda kv: kv[1]["size"], reverse=True)
    head, tail = ranked[:count], ranked[count:]

    rows = [(name, entry["size"], entry["color"]) for name, entry in head]
    if tail:
        rows.append(("other", sum(entry["size"] for _, entry in tail), None))

    exact = [(name, 100.0 * size / total, color) for name, size, color in rows]
    out = [[name, int(pct), color] for name, pct, color in exact]
    # Hand the rounding remainder to whoever lost the most to it, so the column
    # always totals 100 without any single bar being visibly wrong.
    short = 100 - sum(pct for _, pct, _ in out)
    order = sorted(range(len(exact)), key=lambda i: exact[i][1] - int(exact[i][1]), reverse=True)
    for i in order[:short]:
        out[i][1] += 1
    return [(name, "%d%%" % pct, pct / 100.0, color) for name, pct, color in out]


# ----------------------------------------------------------------- time

MINUTE, HOUR, DAY, WEEK, YEAR = 60, 3600, 86400, 86400 * 7, 86400 * 365

# (limit, unit, suffix): the tier applies while the elapsed time is under `limit`,
# and is then reported in multiples of `unit`. Years are handled past the last tier.
TIME_TIERS = (
    (HOUR, MINUTE, "m"),
    (DAY, HOUR, "h"),
    (WEEK, DAY, "d"),
    (YEAR, WEEK, "w"),
)


def days_since(iso_date):
    """Whole days since an ISO date, or None if it is missing or still ahead."""
    if not iso_date:
        return None
    start = datetime.strptime(iso_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    days = (datetime.now(timezone.utc) - start).days
    return days if days >= 0 else None


def relative_time(iso_stamp, now=None):
    """"4h ago" / "3d ago" from an ISO-8601 UTC stamp, or None if it cannot be read.

    Rounds rather than floors below a year: `//` reports 5h59m as "5h ago", a full
    hour of lag always in the same direction. Years floor instead, matching
    account_age -- rounding there would read 1y7m as "2y", and half a year of
    overstatement is not a rounding artifact, it is a wrong claim.

    A stamp in the future is clock skew between GitHub and the runner rather than
    a prediction, so anything under a minute either way reads "just now".
    """
    if not iso_stamp:
        return None
    try:
        when = datetime.strptime(iso_stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        # Drop the row rather than fail the build over a stamp format change.
        return None
    seconds = ((now or datetime.now(timezone.utc)) - when).total_seconds()
    if seconds < MINUTE:
        return "just now"
    for limit, unit, suffix in TIME_TIERS:
        if seconds < limit:
            value = max(int(round(seconds / unit)), 1)
            if value * unit < limit:
                return "%d%s ago" % (value, suffix)
            # Rounding pushed it over its own tier (59m40s -> "60m"). Fall through
            # and let the next tier say "1h" instead.
    return "%dy ago" % max(int(seconds // YEAR), 1)


def account_age(created_at):
    """"4y 11m on GitHub" from the account's own createdAt.

    Deliberately account age and not years of experience. An experience figure
    on a public card is a claim that has to agree with every resume and cover
    letter in circulation; account age is a fact the API reports, and the label
    says what it measures so it cannot be read as either.
    """
    if not created_at:
        return None
    try:
        start = datetime.strptime(created_at[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    now = datetime.now(timezone.utc)
    if now < start:
        return None
    months = (now.year - start.year) * 12 + (now.month - start.month)
    if now.day < start.day:
        months -= 1
    years, months = divmod(max(months, 0), 12)
    if not (years or months):
        return None
    parts = [("%dy" % years) if years else "", ("%dm" % months) if months else ""]
    return " ".join(p for p in parts if p) + " on GitHub"


def utc_offset(config):
    """"UTC-7 · async-friendly", with the offset resolved through the tz database.

    Hardcoding the offset would be wrong for five months a year: this zone is
    UTC-6 on daylight time and UTC-7 the rest of the year. Returns None rather
    than guessing if the zone cannot be resolved, and the row is then omitted.
    """
    if not config or not ZoneInfo:
        return None
    try:
        delta = datetime.now(ZoneInfo(config["zone"])).utcoffset()
    except Exception:
        return None
    if delta is None:
        return None
    minutes = int(delta.total_seconds() // 60)
    sign = "-" if minutes < 0 else "+"
    hours, rest = divmod(abs(minutes), 60)
    label = "UTC%s%d" % (sign, hours) + (":%02d" % rest if rest else "")
    note = config.get("note")
    return "%s · %s" % (label, note) if note else label


# ----------------------------------------------------------------- rows

def esc(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def span(text, cls=None):
    if not text:
        return ""
    attr = ' class="%s"' % cls if cls else ""
    return "<tspan%s>%s</tspan>" % (attr, esc(text))


def _truncate(segments, excess):
    """Shave `excess` characters off the tail of a segment list, ending in an ellipsis.

    The canvas is a fixed width, so a value that does not fit has to be cut here or
    it runs off the card. Shaving from the right keeps the label readable.
    """
    out = list(segments)
    i = len(out) - 1
    while excess > 0 and i >= 0:
        text, cls = out[i]
        if text:
            drop = min(excess, len(text))
            out[i] = (text[:len(text) - drop], cls)
            excess -= drop
        i -= 1
    for j in range(len(out) - 1, -1, -1):
        if out[j][0]:
            out[j] = (out[j][0][:-1] + "…", out[j][1])
            break
    return out


def compose(left, right):
    """Join two segment lists with a dot leader so the row is exactly PANEL_COLS.

    A segment is (text, css_class). Widths are computed from the plain text, so
    no caller has to know how many characters its own markup occupies.
    """
    budget = PANEL_COLS - 2
    used = sum(len(text) for text, _ in left + right)
    if used > budget:
        sys.stderr.write("row too long by %d cols, truncating: %s\n" % (
            used - budget, "".join(text for text, _ in left + right)))
        right = _truncate(right, used - budget)
        used = sum(len(text) for text, _ in left + right)
    segments = left + [(" " + "." * (budget - used) + " ", "dim")] + right
    return "".join(span(text, cls) for text, cls in segments)


def rule(title, cols=PANEL_COLS):
    """A section header: ─ Title ──────────────

    `cols` because the left column is ASCII_COLS wide, not PANEL_COLS: a rule
    sized for the panel would run straight across the gutter and strike through
    whatever the panel has on that row.
    """
    fill = max(cols - len(title) - 3, 0)
    return span("─ ", "dim") + span(title, "key") + span(" " + "─" * fill, "dim")


def row(label, value, value_cls="value"):
    """· Label: ......... Value   (value right-aligned to the panel edge)"""
    return compose([("· ", "dim"), (label + ":", "key")], [(value, value_cls)])


def service_row(name, ok, status, latency, uptime_days):
    bits = []
    if uptime_days is not None:
        bits.append("%dd" % uptime_days)
    if ok:
        bits.append("%s · %sms" % (status, latency))
    elif status:
        bits.append("HTTP %s" % status)
    else:
        bits.append("unreachable")
    return compose(
        [("· ", "dim"), (name, "value")],
        [("● ", "add" if ok else "del"), ("   ".join(bits), "fg")],
    )


def identity_rows(cfg, stats):
    """The identity block, with @-prefixed values resolved to computed ones.

    Keeping the order in config.json means a new row is still a config edit, and
    a computed value that cannot be resolved drops its row instead of printing a
    placeholder or a guess.
    """
    computed = {
        "@uptime": account_age(stats.get("created_at")),
        "@timezone": utc_offset(cfg.get("timezone")),
    }
    rows = []
    for label, value in cfg["identity"]:
        if value.startswith("@"):
            value = computed.get(value)
            if not value:
                continue
        rows.append(row(label, value))
    return rows


def build_rows(cfg, stats, services):
    rows = []
    header = cfg["header"]
    rows.append(span(header + " ", "key") + span("─" * (PANEL_COLS - len(header) - 1), "dim"))
    rows.append("")

    rows.extend(identity_rows(cfg, stats))
    rows.append("")

    rows.append(rule("Services"))
    for svc, result in zip(cfg["services"], services):
        ok, status, latency = result
        rows.append(service_row(svc["name"], ok, status, latency, days_since(svc["since"])))
    rows.append("")

    rows.append(rule("In Production"))
    for label, value in cfg["stack"]:
        rows.append(row(label, value))
    rows.append("")

    rows.append(rule("GitHub"))
    # Split, because the Repositories tab a reader clicks through to shows only
    # the public ones. A single combined total reads as inflated the moment they
    # compare, and takes every other number on the card down with it.
    public, private = stats["repos_public"], stats["repos_private"]
    if private:
        rows.append(compose(
            [("· ", "dim"), ("Repos:", "key")],
            [("%d public" % public, "value"), (" · ", "dim"),
             ("%d private" % private, "value")],
        ))
    else:
        rows.append(row("Repos", str(public)))
    # Both directions, because the two numbers differ and the profile page shows
    # the other one: 6 stars earned is not the 51 repos he has starred.
    rows.append(compose(
        [("· ", "dim"), ("Stars:", "key")],
        [("%d received" % stats["stars"], "value"), (" · ", "dim"),
         ("%d given" % stats["starred"], "value")],
    ))
    # The timeframe is in the label because contributionsCollection is already
    # scoped to the past year, and "Commits" alone reads as all-time. Commits and
    # not contributions: the contribution graph is right below this card on the
    # profile, so restating its total says nothing, and that total counts opening
    # an issue the same as writing code.
    rows.append(row("Commits (1y)", "{:,}".format(stats["commits"])))
    rows.append(row("Followers", str(stats["followers"])))
    pushed = relative_time(stats.get("pushed_at"))
    if pushed:
        # Time only, never the repo name: this card is public and the query
        # covers private repos.
        rows.append(row("Last push", pushed))
    rows.append("")

    rows.append(rule("Contact"))
    for label, value in cfg["contact"]:
        rows.append(row(label, value))
    rows.append("")

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rows.append(span("Last checked: " + stamp, "dim"))
    return rows


# ----------------------------------------------------------------- sidebar

def read_art():
    """Return (glyph_lines, coverage_lines) from ascii.txt and its ascii.cov sibling.

    ascii.cov is optional and only ever an enhancement: if it is missing, or has
    drifted out of shape with ascii.txt, every cell renders at full opacity.
    """
    with open(os.path.join(HERE, "ascii.txt"), encoding="utf-8") as fh:
        glyphs = [line.rstrip("\n") for line in fh]
    try:
        with open(os.path.join(HERE, "ascii.cov"), encoding="utf-8") as fh:
            covers = [line.rstrip("\n") for line in fh]
    except FileNotFoundError:
        return glyphs, None
    if len(covers) != len(glyphs) or any(len(c) != len(g) for c, g in zip(covers, glyphs)):
        sys.stderr.write("ascii.cov does not match ascii.txt, rendering art flat\n")
        return glyphs, None
    return glyphs, covers


def _tier(digit):
    for threshold, cls in COVER_TIERS:
        if digit >= threshold:
            return cls
    return None


def art_rects(glyphs, covers):
    """The monogram as <rect>s, greedily merged into the largest blocks possible.

    Two abutting shapes do not make a seamless join: each covers part of the
    boundary pixel, they composite against the background in turn, and a sliver
    of background survives both. Merging first means most of those joins never
    exist, and OVERLAP closes the ones that remain.
    """
    half_w, half_h = CHAR_W / 2, LINE_H / 2
    cols = max((len(line) for line in glyphs), default=0) * 2

    grid = []
    for i, line in enumerate(glyphs):
        cov = covers[i] if covers else None
        top, bottom = [None] * cols, [None] * cols
        for j, char in enumerate(line):
            bits = GLYPH_BITS.get(char, (0, 0, 0, 0))
            tier = _tier(int(cov[j])) if cov and j < len(cov) and cov[j].isdigit() else None
            key = tier or ""  # "" is ink at full opacity; None is no ink at all
            for side in (0, 1):
                if bits[side]:
                    top[j * 2 + side] = key
                if bits[2 + side]:
                    bottom[j * 2 + side] = key
        grid.append(top)
        grid.append(bottom)

    taken = [[False] * cols for _ in grid]
    out = []
    for r in range(len(grid)):
        for c in range(cols):
            if taken[r][c] or grid[r][c] is None:
                continue
            key = grid[r][c]
            c1 = c
            while c1 + 1 < cols and not taken[r][c1 + 1] and grid[r][c1 + 1] == key:
                c1 += 1
            r1 = r
            while r1 + 1 < len(grid) and all(
                    not taken[r1 + 1][k] and grid[r1 + 1][k] == key for k in range(c, c1 + 1)):
                r1 += 1
            for rr in range(r, r1 + 1):
                for cc in range(c, c1 + 1):
                    taken[rr][cc] = True
            out.append("<rect x='%.2f' y='%.1f' width='%.2f' height='%.1f'%s/>" % (
                PAD + c * half_w, PAD + r * half_h,
                (c1 - c + 1) * half_w + OVERLAP, (r1 - r + 1) * half_h + OVERLAP,
                ' class="%s"' % key if key else ""))
    return out


def bar_block(title, bars, first_row, palette):
    """One titled block of labelled bars.

    Returns (text_rows, rects). Labels and values are text on the normal grid;
    the bars themselves are rectangles, for the same reason the monogram is -- a
    bar is a filled region, and drawing it as one puts it exactly where the grid
    says instead of at the mercy of the viewer's font.

    `bars` is (label, value, fraction, color). Blocks share BAR_* so that two
    stacked blocks line up as a single column.
    """
    if not bars:
        return [], []

    bar_x = PAD + (len(INDENT) + BAR_LABEL_COLS) * CHAR_W
    bar_w = BAR_COLS * CHAR_W
    text, rects = [rule(title, ASCII_COLS)], []

    for i, (label, value, fraction, color) in enumerate(bars):
        cell = label[:BAR_LABEL_COLS - 1].ljust(BAR_LABEL_COLS)
        text.append(span(INDENT + cell, "value")
                    + span(" " * (BAR_COLS + 1), "dim")
                    + span(value.rjust(BAR_VALUE_COLS), "key"))

        # Inset inside the row band so the bar reads as a rule, not a filled cell.
        y = PAD + LINE_H * (first_row + 1 + i) + 6
        rects.append("<rect x='%.2f' y='%d' width='%.2f' height='8' rx='4' fill='%s' "
                     "fill-opacity='0.18'/>" % (bar_x, y, bar_w, palette["fg"]))
        if fraction > 0:
            rects.append("<rect x='%.2f' y='%d' width='%.2f' height='8' rx='4' fill='%s'/>"
                         % (bar_x, y, max(bar_w * fraction, 2.0), color or palette["dim"]))
    return text, rects


# ----------------------------------------------------------------- svg

def render(theme_name, palette, art, art_rows, blocks, rows, outdir):
    # One row band drives everything: row i owns [PAD + LINE_H*i, PAD + LINE_H*(i+1)].
    # The monogram's rectangles fill their bands exactly, and text baselines sit
    # BASELINE_LIFT above the band's bottom edge.
    # Each block costs a title row plus its bars; blank rows separate them.
    sized = [(title, bars) for title, bars in blocks if bars]
    block_rows = sum(1 + len(bars) for _, bars in sized) + max(len(sized) - 1, 0)
    n_rows = max(len(rows), art_rows + 2 + block_rows)
    height = PAD * 2 + n_rows * LINE_H
    width = int(PAD * 2 + (ASCII_COLS + PANEL_COLS + GUTTER_COLS) * CHAR_W)
    panel_x = PAD + (ASCII_COLS + GUTTER_COLS) * CHAR_W

    def baseline(i):
        return PAD + LINE_H * (i + 1) - BASELINE_LIFT

    # The bar blocks are pinned to the bottom as one stack, which keeps the
    # card's lower edge anchored on both sides however many rows the panel grows
    # to, and leaves the monogram alone at the top of its column.
    block_text, block_rects, cursor = [], [], n_rows - block_rows
    for title, bars in sized:
        text, rects = bar_block(title, bars, cursor, palette)
        block_text.append((text, cursor))
        block_rects.extend(rects)
        cursor += len(text) + 1

    out = [
        "<?xml version='1.0' encoding='UTF-8'?>",
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 %d %d' "
        "font-family='ConsolasFallback,Consolas,Menlo,monospace' "
        "width='%dpx' height='%dpx' font-size='%dpx'>" % (width, height, width, height, FONT_SIZE),
        "<title>Aiden Kopec: live profile card, regenerated at least every six hours</title>",
        "<defs>",
        # userSpaceOnUse, not objectBoundingBox: the mark is hundreds of separate
        # rects, and each would otherwise get its own copy of the whole ramp.
        "<linearGradient id='mark' gradientUnits='userSpaceOnUse' "
        "x1='0' y1='%d' x2='0' y2='%d'>" % (PAD, PAD + art_rows * LINE_H),
        "<stop offset='0' stop-color='%s'/>" % palette["mark0"],
        "<stop offset='1' stop-color='%s'/>" % palette["mark1"],
        "</linearGradient>",
        "</defs>",
        "<style>",
        "@font-face { src: local('Consolas'); font-family: 'ConsolasFallback'; "
        "font-display: swap; size-adjust: 109%; }",
        ".key {fill: %s;}" % palette["key"],
        ".value {fill: %s;}" % palette["value"],
        ".add {fill: %s;}" % palette["add"],
        ".del {fill: %s;}" % palette["del"],
        ".dim {fill: %s;}" % palette["dim"],
        ".fg {fill: %s;}" % palette["fg"],
        # fill-opacity only, never fill: these soften the partly covered cells at
        # the letterform edges, and have to compose with the gradient on the
        # parent <g> rather than replace it.
        ".aa1 {fill-opacity: 0.42;}",
        ".aa2 {fill-opacity: 0.74;}",
        "text, tspan {white-space: pre;}",
        "</style>",
        "<rect x='0.5' y='0.5' width='%d' height='%d' rx='%d' fill='%s' stroke='%s'/>"
        % (width - 1, height - 1, RADIUS, palette["bg"], palette["edge"]),
        "<g fill='url(#mark)'>",
    ] + art + [
        "</g>",
    ] + block_rects

    for text, first in block_text:
        out.append("<text x='%d' y='%d' fill='%s'>" % (PAD, baseline(first), palette["fg"]))
        for i, line in enumerate(text):
            out.append("<tspan x='%d' y='%d'>%s</tspan>" % (PAD, baseline(first + i), line))
        out.append("</text>")

    out.append("<text x='%.1f' y='%d' fill='%s'>" % (panel_x, baseline(0), palette["fg"]))
    for i, line in enumerate(rows):
        out.append("<tspan x='%.1f' y='%d'>%s</tspan>" % (panel_x, baseline(i), line))
    out.append("</text>")
    out.append("</svg>")

    if not os.path.isdir(outdir):
        os.makedirs(outdir)
    path = os.path.join(outdir, theme_name + ".svg")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
    return path


# ----------------------------------------------------------------- main

def offline_fixtures(cfg):
    """Stand-in measurements so the card can be rendered without a token or a network.

    Only the numbers are faked; every layout, color and glyph decision downstream
    is the real one, including the language colors, which are GitHub's.
    """
    stats = {
        "repos_public": 7, "repos_private": 59,
        "stars": 6, "starred": 51, "followers": 6,
        "commits": 748,
        "created_at": "2022-01-10T21:38:11Z",
        "pushed_at": (datetime.now(timezone.utc) - timedelta(hours=2)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        # Proportions mirror the real account after exclude_repos, so a preview is
        # never a flattering lie. Update these if the real distribution moves.
        "languages": {
            "TypeScript": {"size": 7_200_000, "color": "#3178c6"},
            "HTML": {"size": 1_400_000, "color": "#e34c26"},
            "JavaScript": {"size": 500_000, "color": "#f1e05a"},
            "Java": {"size": 300_000, "color": "#b07219"},
            "CSS": {"size": 300_000, "color": "#663399"},
            "Python": {"size": 100_000, "color": "#3572A5"},
            "Shell": {"size": 130_000, "color": "#89e051"},
            "Dockerfile": {"size": 70_000, "color": None},
        },
    }
    services = [(True, 200, 91 + 37 * i) for i in range(len(cfg["services"]))]
    return stats, services


def main():
    offline = "--offline" in sys.argv

    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as fh:
        cfg = json.load(fh)

    if offline:
        stats, services = offline_fixtures(cfg)
    else:
        gh_headers()  # fail on a missing token before probing anyone's live site
        services = [probe(svc["url"]) for svc in cfg["services"]]
        stats = github_stats(cfg["username"], cfg.get("exclude_repos", ()))

    glyphs, covers = read_art()
    art = art_rects(glyphs, covers)
    rows = build_rows(cfg, stats, services)

    outdir = PREVIEW if offline else HERE
    blocks = [("Languages by bytes", language_shares(stats["languages"]))]
    for name, palette in THEMES.items():
        print("wrote", render(name, palette, art, len(glyphs), blocks, rows, outdir))


if __name__ == "__main__":
    main()
