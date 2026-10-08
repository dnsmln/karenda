# Instructions for agents

## Branch
- Unless the request names a branch, work on `main`: fetch it first, commit there, push there.
- The build bot commits to `main` every morning ("Update calendar YYYY-MM-DD"), so fetch `main` right before pushing. Fast-forward; don't rewrite history.

## What this is
One script, `build.py`, scrapes four Montréal sources and writes `docs/`: three `.ics` feeds, `index.html`, `status.json`.
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
- Place filter pills come from `PLACES` (source key → label). Adding a source means adding it to `SOURCES`, `GROUPS`, and `PLACES`, plus `HOME` if its usual venue should disappear while its pill is active.
- All HTTP goes through `get()`: it retries on 429/5xx and saves the body to `debug/` under `KARENDA_DEBUG=1`. Never call `requests.get` directly. Dependencies stay at `requests` and `beautifulsoup4`.
- Dates: a theatre run is an all-day `date` span with an exclusive end (build it with `span_event`); a game is a timezone-aware UTC `datetime`. The page converts to America/Toronto. Reuse `parse_en_range`, `month_num` and `MONTHS_FR` before writing a new date parser.
- Theatre summaries end with a venue suffix for the calendar feed (`· PdA`, `· Rideau Vert`); `plain_title` strips it on the page. A new theatre source adds its suffix to that regex.
- Removing a source means the reverse of adding one: `SOURCES`, `GROUPS`, `PLACES`, `plain_title`, the README table, and its captures in `debug/`.

## Working on a scraper
- `docs/status.json` says which source failed and why. A failed source keeps its previous events, so the feed looks fine while the scraper is broken; check the file rather than the feed.
- `debug/` holds the latest raw page or JSON for each source. Write and test the parser against those files first; hit the live site only to confirm.
- Before pushing a scraper fix, run `python build.py` and confirm the source shows `ok: true` with a plausible count. The sites rate-limit; don't loop on them.

## Scope
- Do what was asked and stop. No extra features, options, or restyling. Keep the page quiet.
- Say what you verified and how. If something could not be checked, say so instead of guessing.

## Checking a page change
Open `docs/index.html` in headless Chromium (Playwright is installed) and check light, dark, and 375px wide. Count visible items rather than trusting a screenshot.
