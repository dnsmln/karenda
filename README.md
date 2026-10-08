# karenda

Auto-updating calendar of what's on in Montréal: theatre runs and home games.
A GitHub Action rebuilds the `.ics` files every morning and commits them to `docs/`.

## Subscribe (iCloud / Apple Calendar)

Calendar → File → New Calendar Subscription, paste one of:

| Feed | URL |
|---|---|
| Everything | `https://raw.githubusercontent.com/dnsmln/karenda/main/docs/karenda.ics` |
| Sports only | `https://raw.githubusercontent.com/dnsmln/karenda/main/docs/karenda-sports.ics` |
| Theatre only | `https://raw.githubusercontent.com/dnsmln/karenda/main/docs/karenda-theatre.ics` |

Set auto-refresh to "Every day". On iPhone: Settings → Calendar → Accounts → Add Account → Other → Add Subscribed Calendar.

## Sources

| Source | What | How |
|---|---|---|
| Montréal Victoire (PWHL) | home games, timed | season `.ics` files linked from thepwhl.com |
| CF Montréal (MLS) | home games, timed | ESPN public schedule API |
| Place des Arts | each show as an all-day span over its run, hall in location | `/en/programming` listing |
| Centaur Theatre | each show as an all-day span over its run | WordPress REST API + show pages |
| Théâtre du Rideau Vert | each show as an all-day span over its run | `/programmation` |

If a source fails on a given day, its events from the previous build are kept.
`docs/status.json` shows per-source counts and errors from the last run.

## Run locally

```
pip install -r requirements.txt
python build.py            # writes docs/*.ics and docs/status.json
KARENDA_DEBUG=1 python build.py   # also saves raw source pages to debug/
```

## Web page

`docs/index.html` is regenerated with the feeds: events by month, title linked to the venue page, pills at the top to filter by place (labels in `PLACES` in `build.py`).
On Vercel, set the project's Output Directory to `docs` (no build command).
