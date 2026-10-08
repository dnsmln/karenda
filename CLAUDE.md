# Instructions for agents

## Branch
- Unless the request names a branch, work on `main`: fetch it first, commit there, push there.
- The build bot commits to `main` every morning ("Update calendar YYYY-MM-DD"), so fetch `main` right before pushing. Fast-forward; don't rewrite history.

## What this is
One script, `build.py`, scrapes seven Montréal sources and writes `docs/`: four `.ics` feeds, `index.html`, `status.json`.
`docs/` is published as-is on Vercel and consumed by calendar subscriptions. No framework, no build step beyond the script.

## Generated files
- Never hand-edit anything in `docs/` or `debug/`. Change `build.py` and regenerate.
- Page-only change (HTML template, CSS, JS, `PLACES` labels): `python build.py --render-only` rebuilds `docs/index.html` from the existing `docs/karenda.ics` with no network. Commit the regenerated file with the code.
- Scraper change: `python build.py` hits the live sites. Only run it when the scraper itself changed.
- A push to `main` that touches anything outside `docs/` and `debug/` triggers the GitHub Action, which rebuilds and commits `docs/` on top of your commit.

## Code conventions
- Keep everything in `build.py`. Each source is one `src_*` function returning `list[Event]`; a source that raises keeps its previous events, so raise on empty results rather than returning `[]`.
- Event identity is `uid`; keep UIDs stable across runs or subscribers get duplicates.
- The web page is `render_html` plus `HTML_TEMPLATE`. Keep it minimal: system font, 14px, one column, light and dark via the CSS variables already there, no dependencies.
- Place filter pills come from `PLACES` (source key → label). Adding a source means adding it to `SOURCES`, `GROUPS`, and `PLACES`.

## Checking a page change
Open `docs/index.html` in headless Chromium (Playwright is installed) and check light, dark, and 375px wide. Count visible items rather than trusting a screenshot.
