# Profile card

`Aidenkopec/Aidenkopec` is the GitHub profile repository: `README.md` renders on the
profile page itself, so it holds the card and nothing else. This file is the
developer-facing documentation.

## What it does

`today.py` reads the static content from `config.json`, measures everything else
live, renders `dark_mode.svg` and `light_mode.svg`, and commits them back to `main`.
GitHub Actions runs it on a six-hour cron, on every push to `main`, and on demand.
`README.md` points a `<picture>` element at the two raw SVG URLs and lets
`prefers-color-scheme` pick.

On a live run every number on the card is measured or absent: a row whose
measurement is unavailable is dropped rather than defaulted, guessed or carried over
from the previous run. Two deliberate exceptions. A service that fails its probe
prints `unreachable` or the HTTP status instead of vanishing — a dead service is
data, and hiding it would be the lie. And `--offline` fabricates every measurement
by design, which is why it writes to `preview/` and never to the tracked SVGs.

## Repository map

| Path | Role |
| --- | --- |
| `today.py` | The renderer, end to end. Stdlib only, so CI needs no install step. |
| `test_today.py` | `unittest` suite. Runs in CI ahead of the render and gates it. |
| `config.json` | Every static string on the card, plus the service list. |
| `ascii.txt` / `ascii.cov` | The monogram: quadrant glyphs, and one ink-coverage digit per glyph. |
| `tools/asciify.py` | Regenerates those two from a source logo. Needs Pillow; run by hand, never in CI. |
| `.github/workflows/build.yaml` | Schedule, test gate, and the commit-if-changed step. |
| `.gitattributes` | Marks the two SVGs as generated so merges resolve instead of conflicting. |
| `dark_mode.svg` / `light_mode.svg` | Output. Committed so the profile can serve them, never hand-edited. |
| `preview/` | Output of `--offline`. Gitignored. |

## Pipeline

1. Load `config.json`.
2. Probe each service over HTTPS for status and latency (`probe`, two attempts).
3. Collect GitHub measurements (`github_stats`): one paged `USER_QUERY` for repos,
   stars, followers, commits and language bytes, plus a non-fatal `PROFILE_QUERY`
   for the profile repo's authored commits.
4. Decode the monogram (`read_art`, `art_rects`) into merged `<rect>` elements.
5. Compose the right-hand panel (`build_rows`) — one markup string per row.
6. Apportion the language bars (`language_shares`).
7. Emit one SVG per theme (`render`).

## Layout model

The card is a fixed character grid, and a single row band drives every coordinate:
row `i` owns `[PAD + LINE_H*i, PAD + LINE_H*(i+1)]`, and every `x` is a column index
times `CHAR_W`. Text baselines sit `BASELINE_LIFT` above their band's bottom edge;
the monogram's rectangles fill their bands exactly. That shared band is what keeps
the left column (monogram, then the bar blocks pinned to the bottom) aligned with
the right column (the panel) as either grows. `render` carries the arithmetic.

Two decisions behind the grid are measured rather than assumed, and `today.py`
documents the measurements at the constants themselves: why `CHAR_W` is what it is,
and why the `@font-face` needs a `size-adjust` to hold the grid across viewers, at
the `# Layout.` block; and why the monogram is drawn as rectangles while the panel
stays real text, at `GLYPH_BITS`.

## Invariants the tests pin

- No panel row is wider than `PANEL_COLS`, and the rows built through `compose`
  and `rule` are exactly that wide. Over-long values are truncated with an
  ellipsis rather than allowed to run off the card.
- Language percentages total exactly 100 (largest-remainder apportionment).
- `exclude_repos` removes language bytes only — never repo counts or stars, which
  have to agree with what a visitor sees on the profile page.
- The profile repo's own `pushedAt` never feeds "Last push"; only commits authored
  by the user do.
- Unreadable or missing input drops its row instead of failing the build or
  printing a placeholder.
- Values from `config.json` and from the API are escaped before reaching the SVG.

## Running locally

```sh
python3 today.py --offline          # fixtures, no token or network; writes preview/
python3 -m unittest -v              # the full suite; no network either
```

Use `--offline` for local work. It writes to the gitignored `preview/`, so it can
be run as often as you like without touching anything git tracks.

The live render is CI's job, and committing one from a laptop is what causes merge
conflicts: it overwrites the same two tracked files the bot rewrites every six
hours, on lines — the timestamp, the latencies, "Last push" — that differ between
any two renders. If you need to see live data locally, render it and then throw it
away:

```sh
ACCESS_TOKEN=<pat> python3 today.py   # overwrites the two tracked SVGs
git checkout -- dark_mode.svg light_mode.svg
```

The token is a PAT with `read:user` and `repo` scope — `repo` so that private
repositories are counted. In CI it comes from the `ACCESS_TOKEN` secret.

There is no dependency manifest, because the render path has no dependencies:
`today.py` and `test_today.py` are stdlib only. Python 3.9 is the floor, set by
`zoneinfo`, which also needs system tzdata; CI pins 3.11. The one tool that does
need a third-party package installs it by hand:

```sh
pip install pillow
python3 tools/asciify.py path/to/logo.png --write
```

## Making changes

- **Card content** — edit `config.json`. Row order there is row order on the card.
- **A new computed identity row** — add an `@name` value to `identity` in
  `config.json` and resolve it in `identity_rows`. Unresolved values drop their row.
  Note that the existing `Uptime` row resolves to `@uptime`, which is *account age*
  — the label is a neofetch convention, not a claim about a service.
- **Colors** — `THEMES` in `today.py` holds both palettes; the accents are shared
  with `aidenkopec.com` so the card and the site stay in step.
- **Offline fixtures** — `offline_fixtures` mirrors the real account's proportions
  so a preview is never a flattering lie. Update it when the real shape moves.

## Non-obvious behaviour

- The workflow diffs with `-I 'Last checked'` before committing, so the timestamp
  line alone cannot produce a commit on every run.
- The render is on a cron and on demand, never on `push`. Rendering on push would
  put a bot commit on the two SVGs immediately after every push, so the next local
  commit would always be merging against a changed card.
- `.gitattributes` marks both SVGs `merge=ours`, which needs `git config
  merge.ours.driver true` once per clone. Git will not run a driver it has not been
  told about, and without it these merges conflict on lines no human can reconcile.
  The local side wins and the next scheduled run re-renders both files from live
  data, so nothing is lost by taking either side.
- `graphql(required=False)` is used for exactly one query, the profile repo's
  commit history. It is the only enrichment on the card rather than a fact, so its
  failure costs "Last push" one repo's worth of reach instead of leaving every row
  stale for six hours.
- "Last push" prints a time and never a repository name, because the query covers
  private repositories and the card is public.
- The `Repos` row counts non-fork repositories the user owns, split public from
  private, which is what the profile's Repositories tab shows.
