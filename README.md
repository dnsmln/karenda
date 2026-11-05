# karenda

A calendar of what's on near you that updates itself: shows, games, films, exhibitions. One Python script scrapes the venues and teams you follow and writes calendar feeds plus a one-page listing. A GitHub Action reruns it every morning.

This repo is set up for Montréal. Copy it and point it at your own city.

## How it works

- `build.py` has one `src_*` function per source. Each returns a list of events: a theatre run is an all-day span, a game or screening has a start time.
- It writes `docs/`: `karenda.ics` with everything, one `.ics` per group (sports, theatre, ...), `index.html`, and `status.json`.
- If a source fails, its events from the previous build are kept. `status.json` shows per-source counts and errors; the page footer names any source that failed.
- `.github/workflows/build.yml` runs the script daily and commits `docs/`.
- `docs/` is static. Host it anywhere: Vercel (`vercel.json` is ready), GitHub Pages, or straight from raw GitHub URLs.

## Build your own

1. Copy this repo into your GitHub account.
2. Open it with a coding agent such as Claude Code and ask it to build your karenda. It follows `.claude/skills/build-karenda/SKILL.md`: a few questions about your city, the venues and teams you follow, and where it should run, then it writes the scrapers.
3. Publish `docs/` and subscribe to the feed in your calendar app.

## Kinds of sources

Most event sites fall into one of these. The Montréal sources in this repo cover each:

| Data | Example source here | Notes |
|---|---|---|
| Published season `.ics` | Montréal Victoire (PWHL) | `.ics` files linked from thepwhl.com, home games only |
| Public JSON API | CF Montréal (MLS) | ESPN schedule API, home games only |
| JSON API with a key | Laval Rocket (AHL) | HockeyTech feed behind theahl.com; key in the `AHL_KEY` secret |
| Search index the site queries | Place des Arts | Algolia; app id and key read from the public page, HTML listing as fallback |
| JSON-LD on detail pages | TOHU | `/en/season/shows` listing, dates from each show's page |
| HTML listing | Théâtre du Rideau Vert, Maison Théâtre, MBAM | runs as all-day spans; MBAM exhibitions as two one-day events, opening and last day |
| HTML calendar, filtered | Cinéma Moderne | Petits Modernes screenings only, this month and the next two |

## Subscribe

Use your host's URL, e.g. `https://<your-site>/karenda.ics`.

- Apple Calendar: File → New Calendar Subscription, auto-refresh every day. iPhone: Settings → Calendar → Accounts → Add Account → Other → Add Subscribed Calendar.
- Google Calendar (web): Other calendars → + → From URL.
- Outlook: Add calendar → Subscribe from web.

## Run locally

```
pip install -r requirements.txt
python build.py                   # writes docs/*.ics, docs/index.html, docs/status.json
python build.py --render-only     # rebuilds docs/index.html from the existing feed, no network
KARENDA_DEBUG=1 python build.py   # also saves raw source pages to debug/
```

Sources that need a key read it from the environment (`AHL_KEY=... python build.py`). In GitHub, add it under Settings → Secrets and variables → Actions.
