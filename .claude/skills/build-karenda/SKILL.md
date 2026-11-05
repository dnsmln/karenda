---
name: build-karenda
description: Set up a new karenda (a self-updating calendar of local events) for someone's own city, venues and teams, starting from this repo. Use when someone asks to build, copy, fork or adapt karenda. Asks about their preferences and setup before changing any code.
---

# Build a karenda

Karenda is one script, `build.py`, with one `src_*` scraper per source, plus a GitHub Action that runs it every morning and commits `.ics` feeds and a one-page listing to `docs/`. The sources in this repo are Montréal venues and teams. A new karenda swaps them for the user's.

Read `CLAUDE.md` before writing code. Its conventions (one `src_*` per source, stable UIDs, `get()` for all HTTP, `span_event` for runs) apply to everything below.

## 1. Ask first

Ask these before touching code, in one round if you can, two at most. Use a question tool if your harness has one; otherwise one short message. Don't ask about the page's look: it stays as it is.

Preferences
1. City or area. This sets the timezone.
2. What to follow: venues, teams, festivals, series. Names are enough, links are better. If they're unsure, offer categories (theatre, music, sports, cinema, museums, family) and suggest well-known local options for the ones they pick.
3. Narrowing: home games only or away games too? Only part of a venue's programme (a kids' series, one hall)? Anything to leave out?
4. Feeds: one calendar with everything, or also one per category (sports, theatre, and so on)?

Setup
5. GitHub: which account, what repo name, public or private. The daily build runs on GitHub Actions in that repo.
6. Hosting and calendar: where `docs/` gets published (Vercel, GitHub Pages, or nowhere and subscribe to raw GitHub URLs, which needs a public repo) and which calendar app they'll subscribe from (Apple, Google, Outlook).

If they skip a question, use these defaults and say so: home games only, one feed per category plus one with everything, public repo, Vercel, Apple Calendar, build at 6 am local.

Then reply with the plan: each source, what data it will come from, and which feed it lands in. Wait for a yes.

## 2. Find each source's data

Prefer the most stable format a source offers, in this order. Each has a working example in `build.py`:

| Format | Example |
|---|---|
| A published `.ics` (team season calendars) | `src_victoire` |
| A JSON API the site's own pages call (watch the browser's network tab) | `src_cfmontreal` (ESPN), `src_rocket` (HockeyTech), `_pda_algolia` |
| schema.org `Event` JSON-LD on detail pages | `src_tohu` via `_ld_event` |
| An HTML listing parsed with BeautifulSoup | `src_rideauvert`, `src_mbam` |

Fetch each page once with `KARENDA_DEBUG=1` so it lands in `debug/`, then write and test the parser against that file. Sites rate-limit; don't loop on them.

Keys: never commit one. Read it from an environment variable and pass it in the workflow from an Actions secret, as `AHL_KEY` does in `_ahl` and `.github/workflows/build.yml`. A key the site embeds in its public page can be read from that page at runtime instead (`_pda_algolia`).

## 3. Change the code

- Remove the sources they don't keep, following "Removing a source" in `CLAUDE.md`, and delete their `debug/` captures.
- Add each new source to `SOURCES`, `GROUPS`, `PLACES`, and `HOME` if it has a usual venue. Theatre-style sources get a venue suffix in the summary and in the `plain_title` regex.
- Timezone: `MTL` in the html-page section, `X-WR-TIMEZONE` in `write_ics`, and `HOME_AREA` if they keep team sources.
- Names: the calendar names in `main()`, `PRODID` in `write_ics`, `<title>` and the meta description in `HTML_TEMPLATE`, and the module docstring.
- Links: replace `https://github.com/dnsmln/karenda` with their repo in `HTML_TEMPLATE` (the dialog's View Source button and its prompt).
- Workflow: the cron in `.github/workflows/build.yml` is UTC; convert their build time. Add any secrets to the Build step's `env`.
- README: the sources table and the feed URLs.

## 4. Check

- `python build.py`, then `docs/status.json`: every source `ok: true` with a plausible count. A failed source keeps its previous events, so the feed alone won't show it.
- Open `docs/index.html` and count the listed items per source.

## 5. Publish

- Push to their repo. On a fork, GitHub turns Actions off: open the Actions tab and enable them. Add any secrets under Settings → Secrets and variables → Actions. Run "Build calendar" once from the Actions tab.
- Vercel: import the repo; `vercel.json` already sets the output directory to `docs` with no build command. GitHub Pages: Settings → Pages → deploy from branch `main`, folder `/docs`.
- Give them the exact feed URL(s) and the steps for their app:
  - Apple Calendar: File → New Calendar Subscription, auto-refresh every day. iPhone: Settings → Calendar → Accounts → Add Account → Other → Add Subscribed Calendar.
  - Google Calendar (web): Other calendars → + → From URL. Google refreshes subscriptions slowly, often once a day or less.
  - Outlook: Add calendar → Subscribe from web.
