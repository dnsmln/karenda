#!/usr/bin/env python3
"""Build karenda.ics: Montreal theatre runs + home games, from public sources.

Sources
  victoire      PWHL season ICS files linked from thepwhl.com (home games only)
  cfmontreal    ESPN public schedule API (home games only)
  placedesarts  placedesarts.com/en/programming listing
  centaur       centaurtheatre.com WordPress REST API + show pages
  rideauvert    rideauvert.qc.ca/programmation
  rocket        AHL schedule feed (HockeyTech) behind theahl.com (home games only)
  cinemamoderne cinemamoderne.com screenings

Each source is independent. If one fails, its events from the previous
karenda.ics are kept so the feed never loses a venue because of one bad day.
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "docs"
DEBUG = ROOT / "debug"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36 karenda/1.0"}
TODAY = date.today()
KEEP_FROM = TODAY - timedelta(days=1)
DEBUG_ON = os.environ.get("KARENDA_DEBUG") == "1"

session = requests.Session()
session.headers.update(UA)


def get(url: str, name: str | None = None, retries: int = 3, **kw) -> requests.Response:
    """GET with backoff on 429/5xx. Saves the body under debug/ when KARENDA_DEBUG=1."""
    delay = 5.0
    for attempt in range(retries):
        r = session.get(url, timeout=40, **kw)
        if r.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
            wait = float(r.headers.get("Retry-After") or delay)
            print(f"  {r.status_code} on {url}; retrying in {wait:.0f}s")
            time.sleep(min(wait, 90))
            delay *= 3
            continue
        break
    r.raise_for_status()
    if DEBUG_ON and name:
        DEBUG.mkdir(exist_ok=True)
        (DEBUG / name).write_bytes(r.content)
    return r


def save_debug(name: str, data: bytes | str) -> None:
    if DEBUG_ON:
        DEBUG.mkdir(exist_ok=True)
        (DEBUG / name).write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))


# --------------------------------------------------------------------------- model
@dataclass
class Event:
    source: str
    uid: str
    summary: str
    start: datetime | date
    end: datetime | date          # exclusive for all-day
    location: str = ""
    url: str = ""
    description: str = ""
    categories: list[str] = field(default_factory=list)

    @property
    def all_day(self) -> bool:
        return not isinstance(self.start, datetime)

    def start_date(self) -> date:
        return self.start.date() if isinstance(self.start, datetime) else self.start

    def end_date(self) -> date:
        return self.end.date() if isinstance(self.end, datetime) else self.end


def ics_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line: str) -> str:
    b = line.encode("utf-8")
    if len(b) <= 72:
        return line
    out, cur = [], b""
    for ch in line:
        cb = ch.encode("utf-8")
        if len(cur) + len(cb) > 72:
            out.append(cur.decode("utf-8"))
            cur = b" " + cb
        else:
            cur += cb
    out.append(cur.decode("utf-8"))
    return "\r\n".join(out)


def fmt_dt(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_ics(path: Path, name: str, events: list[Event]) -> None:
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//karenda//montreal-events//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_escape(name)}",
        "X-WR-TIMEZONE:America/Toronto",
        "X-PUBLISHED-TTL:PT12H",
        "REFRESH-INTERVAL;VALUE=DURATION:PT12H",
    ]
    for e in sorted(events, key=lambda e: (e.start_date(), e.summary)):
        lines += ["BEGIN:VEVENT", f"UID:{e.uid}", f"DTSTAMP:{now}"]
        if e.all_day:
            lines += [f"DTSTART;VALUE=DATE:{e.start:%Y%m%d}", f"DTEND;VALUE=DATE:{e.end:%Y%m%d}"]
        else:
            lines += [f"DTSTART:{fmt_dt(e.start)}", f"DTEND:{fmt_dt(e.end)}"]
        lines.append(f"SUMMARY:{ics_escape(e.summary)}")
        if e.location:
            lines.append(f"LOCATION:{ics_escape(e.location)}")
        if e.url:
            lines.append(f"URL:{e.url}")
        desc = e.description
        if e.url:
            desc = (desc + "\n" if desc else "") + e.url
        if desc:
            lines.append(f"DESCRIPTION:{ics_escape(desc)}")
        if e.categories:
            lines.append("CATEGORIES:" + ",".join(ics_escape(c) for c in e.categories))
        lines.append(f"X-KARENDA-SOURCE:{e.source}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\r\n".join(fold(l) for l in lines) + "\r\n", encoding="utf-8")


def read_previous(path: Path) -> dict[str, list[Event]]:
    """Parse our own previous output (only the subset of ICS we write)."""
    out: dict[str, list[Event]] = {}
    if not path.exists():
        return out
    text = path.read_text(encoding="utf-8").replace("\r\n ", "").replace("\n ", "")
    for block in re.findall(r"BEGIN:VEVENT\r?\n(.*?)\r?\nEND:VEVENT", text, re.S):
        props: dict[str, str] = {}
        for line in block.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                props[k.split(";")[0]] = v
        src = props.get("X-KARENDA-SOURCE")
        if not src:
            continue

        def unesc(s: str) -> str:
            return s.replace("\\n", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")

        ds, de = props.get("DTSTART", ""), props.get("DTEND", "")
        try:
            if ds.endswith("Z"):
                start = datetime.strptime(ds, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                end = datetime.strptime(de, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            else:
                start = datetime.strptime(ds, "%Y%m%d").date()
                end = datetime.strptime(de, "%Y%m%d").date()
        except ValueError:
            continue
        url = props.get("URL", "")
        desc = unesc(props.get("DESCRIPTION", ""))
        if url and desc.endswith(url):
            desc = desc[: -len(url)].rstrip("\n")
        out.setdefault(src, []).append(Event(
            source=src, uid=props.get("UID", ""), summary=unesc(props.get("SUMMARY", "")),
            start=start, end=end, location=unesc(props.get("LOCATION", "")), url=url,
            description=desc, categories=[unesc(c) for c in props.get("CATEGORIES", "").split(",") if c],
        ))
    return out


# --------------------------------------------------------------------------- helpers
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}
MONTHS.update({k[:3]: v for k, v in MONTHS.items()})
MONTHS.update({"sept": 9})
MONTHS_FR = {"janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
             "juillet": 7, "août": 8, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11,
             "décembre": 12, "decembre": 12}
MONTH_RE = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"


def month_num(s: str) -> int | None:
    s = s.lower().rstrip(".")
    return MONTHS.get(s) or MONTHS.get(s[:3]) or MONTHS_FR.get(s)


def parse_en_range(text: str) -> tuple[date, date] | None:
    """Parse English date text as used by Place des Arts and Centaur.

    Handles: "October 9, 2026", "October 8 and 9, 2026", "October 8 to 10, 2026",
    "October 21 to November 19, 2026", "September 25, 2026 to January 30, 2027",
    "October 13 - November 1, 2026", "Oct. 13 – Nov. 1, 2026".
    Returns (first_day, last_day) inclusive.
    """
    t = html.unescape(text).replace("\u2013", "-").replace("\u2014", "-").replace("\xa0", " ")
    t = re.sub(r"\s+", " ", t).strip()
    sep = r"\s*(?:to|-|–|and|&|au|et)\s*"
    M, D, Y = MONTH_RE, r"(\d{1,2})(?:st|nd|rd|th)?", r"(\d{4})"
    m = re.search(rf"({M}) {D}, {Y}{sep}({M}) {D}, {Y}", t, re.I)       # M D, Y to M D, Y
    if m:
        a = date(int(m[3]), month_num(m[1]), int(m[2])); b = date(int(m[6]), month_num(m[4]), int(m[5]))
        return (a, b) if b >= a else None
    m = re.search(rf"({M}) {D}{sep}({M}) {D}, {Y}", t, re.I)             # M D to M D, Y
    if m:
        y = int(m[5]); a = date(y, month_num(m[1]), int(m[2])); b = date(y, month_num(m[3]), int(m[4]))
        if b < a:
            a = a.replace(year=y - 1)
        return a, b
    m = re.search(rf"({M}) {D}{sep}{D}, {Y}", t, re.I)                   # M D to D, Y
    if m:
        y = int(m[4]); mo = month_num(m[1])
        return date(y, mo, int(m[2])), date(y, mo, int(m[3]))
    m = re.search(rf"({M}) {D}, {Y}", t, re.I)                           # M D, Y
    if m:
        d = date(int(m[3]), month_num(m[1]), int(m[2]))
        return d, d
    return None


def span_event(source: str, uid: str, title: str, first: date, last: date, **kw) -> Event:
    return Event(source=source, uid=uid, summary=title, start=first, end=last + timedelta(days=1), **kw)


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:80]


def text_of(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)) if el else ""


MTL = ZoneInfo("America/Toronto")


# --------------------------------------------------------------------------- sources
def src_victoire() -> list[Event]:
    schedule = "https://www.thepwhl.com/en/schedule/"
    known = [
        "https://assets.contentstack.io/v3/assets/bltebdb4296e05d53db/bltf9cbf8a71653fae4/FULL_Regular%20Season.ics",
        "https://assets.contentstack.io/v3/assets/bltebdb4296e05d53db/blt6cd64228c3ea8504/FULL_Preaseason.ics",
    ]
    urls: list[str] = []
    try:
        page = get(schedule, "pwhl_schedule.html").text
        urls = [u.replace(" ", "%20") for u in re.findall(r'https?://[^"\'\s<>]+\.ics', html.unescape(page))]
    except Exception as e:  # noqa: BLE001
        print("victoire: schedule page failed, using known ICS urls:", e)
    urls = list(dict.fromkeys(urls + known))
    events: list[Event] = []
    seen: set[str] = set()
    ok = 0
    for u in urls:
        try:
            text = get(u).text.replace("\r\n ", "").replace("\n ", "")
        except Exception as e:  # noqa: BLE001
            print("victoire: ics failed", u, e)
            continue
        ok += 1
        for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.S):
            p = {k.split(";")[0]: v.strip() for k, v in
                 (l.split(":", 1) for l in block.strip().splitlines() if ":" in l)}
            summ = p.get("SUMMARY", "")
            m = re.match(r"(.+?)\s*@\s*(.+)", summ)
            if not m or "montr" not in m[2].lower():
                continue
            uid = p.get("UID") or f"{p.get('DTSTART')}@pwhl"
            if uid in seen:
                continue
            seen.add(uid)
            try:
                start = datetime.strptime(p["DTSTART"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                end = datetime.strptime(p.get("DTEND", p["DTSTART"]), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            except (KeyError, ValueError):
                continue
            if end <= start:
                end = start + timedelta(hours=3)
            loc = p.get("LOCATION", "").replace(" | ", ", ")
            pre = "preseason" in u.lower()
            events.append(Event(
                source="victoire", uid=f"{uid}@karenda", summary=f"Victoire vs {m[1].strip()}" + (" (preseason)" if pre else ""),
                start=start, end=end, location=loc or "Place Bell, Laval",
                url="https://www.thepwhl.com/en/schedule/", categories=["Sports", "PWHL"],
            ))
    if not ok:
        raise RuntimeError("no PWHL ICS could be fetched")
    return events


def src_cfmontreal() -> list[Event]:
    base = "https://site.api.espn.com/apis/site/v2/sports/soccer/usa.1/teams/9720/schedule"
    events: list[Event] = []
    seen: set[str] = set()
    ok = 0
    # ESPN splits a team's schedule: default = results so far, fixture=true = upcoming.
    for q in ("?fixture=true", "", "?fixture=true&seasontype=3"):
        try:
            data = get(base + q, "espn" + re.sub(r"\W+", "_", q) + ".json").json()
        except Exception as e:  # noqa: BLE001
            print("cfmontreal: fetch failed", q, e)
            continue
        ok += 1
        for ev in data.get("events", []):
            if ev.get("id") in seen:
                continue
            comp = (ev.get("competitions") or [{}])[0]
            home = next((c for c in comp.get("competitors", []) if c.get("homeAway") == "home"), None)
            away = next((c for c in comp.get("competitors", []) if c.get("homeAway") == "away"), None)
            if not home or str(home.get("id")) != "9720":
                continue
            seen.add(ev.get("id"))
            ds = ev.get("date", "")
            try:
                start = datetime.strptime(ds, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
            except ValueError:
                try:
                    start = datetime.fromisoformat(ds.replace("Z", "+00:00"))
                except ValueError:
                    continue
            opp = (away or {}).get("team", {}).get("displayName", "TBD")
            venue = comp.get("venue", {}).get("fullName") or "Stade Saputo"
            stype = (ev.get("seasonType") or ev.get("season", {}).get("type") or {})
            label = ""
            if isinstance(stype, dict) and "playoff" in (stype.get("name", "") or "").lower():
                label = " (playoffs)"
            events.append(Event(
                source="cfmontreal", uid=f"espn-{ev.get('id')}@karenda", summary=f"CF Montréal vs {opp}{label}",
                start=start, end=start + timedelta(hours=2), location=f"{venue}, Montréal",
                url=f"https://www.espn.com/soccer/match/_/gameId/{ev.get('id')}", categories=["Sports", "MLS"],
            ))
    if not ok:
        raise RuntimeError("ESPN schedule unavailable")
    return events


HALLS = ["Salle Wilfrid-Pelletier", "Théâtre Maisonneuve", "Maison symphonique", "Théâtre Jean-Duceppe",
         "Salle Claude-Léveillée", "Cinquième Salle", "Esplanade", "Salon urbain", "Salle d'exposition",
         "Espace culturel Georges-Émile-Lapalme", "Multiple venues", "Piano nobile", "Studio-théâtre"]


def _pda_event(href: str, title: str, first: date, last: date, hall: str, desc: str = "") -> Event:
    full = href if href.startswith("http") else "https://www.placedesarts.com" + href
    return span_event(
        "placedesarts", f"pda-{slug(full.rstrip('/').rsplit('/', 1)[-1])}@karenda", f"{title} · PdA",
        first, last, location=(hall + ", " if hall else "") + "Place des Arts, Montréal",
        url=full, description=desc, categories=["Theatre", "Place des Arts"],
    )


def _first_str(d: dict, *keys: str) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return html.unescape(v.strip())
        if isinstance(v, dict):
            for kk in ("title", "name", "en", "value"):
                if isinstance(v.get(kk), str) and v[kk].strip():
                    return html.unescape(v[kk].strip())
        if isinstance(v, list) and v and isinstance(v[0], str):
            return html.unescape(v[0].strip())
        if isinstance(v, list) and v and isinstance(v[0], dict):
            s = _first_str(v[0], "title", "name")
            if s:
                return s
    return ""


def _pda_algolia(page_html: str) -> list[Event]:
    """Full listing through the site's own public search index (the HTML shows a subset)."""
    app = re.search(r'algoliaId\s*=\s*"([^"]+)"', page_html)
    key = re.search(r'algoliaKey\s*=\s*"([^"]+)"', page_html)
    idx = re.search(r'algoliaIndexName\s*=\s*"([^"]+)"', page_html)
    if not (app and key and idx):
        raise RuntimeError("algolia config not found in page")
    now_ts = int(time.time())
    r = session.post(
        f"https://{app[1]}-dsn.algolia.net/1/indexes/{idx[1]}/query",
        headers={"X-Algolia-Application-Id": app[1], "X-Algolia-API-Key": key[1]},
        json={"query": "", "hitsPerPage": 1000, "numericFilters": [f"datesTimestamp>={now_ts - 86400}"]},
        timeout=40,
    )
    r.raise_for_status()
    data = r.json()
    save_debug("algolia.json", json.dumps(data, ensure_ascii=False, indent=1)[:400000])
    hits = data.get("hits", [])
    events: dict[str, Event] = {}
    for h in hits:
        href = _first_str(h, "url", "uri", "link", "permalink")
        if not href:
            s = _first_str(h, "slug")
            href = f"/en/event/{s}" if s else ""
        if not href or "/event/" not in href:
            continue
        title = _first_str(h, "title", "name")
        if not title:
            continue
        # dates: prefer explicit timestamps, else the display text
        ts = h.get("datesTimestamp") or h.get("dates_timestamp") or h.get("performanceDates")
        first = last = None
        if isinstance(ts, (int, float)):
            first = last = datetime.fromtimestamp(ts, timezone.utc).date()
        elif isinstance(ts, list) and ts and all(isinstance(x, (int, float)) for x in ts):
            ds = sorted(datetime.fromtimestamp(x, timezone.utc).date() for x in ts)
            first, last = ds[0], ds[-1]
        if first is None:
            txt = _first_str(h, "dates", "dateText", "date", "dateRange", "displayDate")
            rng = parse_en_range(txt) if txt else None
            if not rng:
                continue
            first, last = rng
        if (last - first).days > 400:
            continue
        hall = _first_str(h, "venue", "hall", "room", "salle", "venues", "location")
        n = ts if isinstance(ts, list) else None
        desc = f"{len(n)} performances" if n and len(n) > 1 else ""
        events[href] = _pda_event(href, title, first, last, hall, desc)
    return list(events.values())


def src_placedesarts() -> list[Event]:
    url = "https://www.placedesarts.com/en/programming"
    page_html = get(url, "placedesarts.html").text
    html_events = _pda_html(page_html)
    try:
        alg = _pda_algolia(page_html)
        print(f"  placedesarts: algolia {len(alg)} vs html {len(html_events)}")
        if len(alg) >= len(html_events):
            return alg
    except Exception as e:  # noqa: BLE001
        print("  placedesarts: algolia failed, using html listing:", e)
    return html_events


def _pda_html(page_html: str) -> list[Event]:
    soup = BeautifulSoup(page_html, "html.parser")
    events: dict[str, Event] = {}
    for a in soup.select('a[href*="/en/event/"]'):
        href = a.get("href", "")
        full = href if href.startswith("http") else "https://www.placedesarts.com" + href
        if full in events:
            continue
        # the card's text is inside the anchor on the listing; fall back to the parent card
        txt = text_of(a)
        if not parse_en_range(txt):
            txt = text_of(a.parent)
        rng = parse_en_range(txt)
        if not rng:
            continue
        first, last = rng
        if (last - first).days > 400:      # permanent installations
            continue
        title = ""
        for sel in ("h2", "h3", "h4", "[class*=title]"):
            h = a.select_one(sel) or (a.parent.select_one(sel) if a.parent else None)
            if h and text_of(h):
                title = text_of(h)
                break
        if not title:
            # strip the date text and known decorations from the anchor text
            title = re.split(r"\b(?:" + MONTH_RE + r") \d{1,2}", txt, 1, flags=re.I)[0]
            title = re.sub(r"^(New date added|Limited places|Sold out|Free|Last chance)\s*", "", title, flags=re.I).strip(" -|")
        if not title:
            title = full.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").title()
        hall = next((h for h in HALLS if h.lower() in txt.lower()), "")
        perf = re.search(r"(\d+)\s+performances?", txt, re.I)
        desc = (f"{perf[1]} performances" if perf else "")
        events[full] = _pda_event(full, title, first, last, hall, desc)
    if not events:
        raise RuntimeError("no events parsed from programming page")
    return list(events.values())


def _centaur_shows() -> list[dict]:
    """[{id, link, title, guest}] — WordPress REST first, sitemap if the API rate-limits."""
    api = "https://centaurtheatre.com/wp-json/wp/v2/centaur_event?per_page=40&_fields=id,link,title,class_list"
    try:
        items = get(api, "centaur.json").json()
        return [{
            "id": it["id"], "link": it.get("link", ""),
            "title": html.unescape(BeautifulSoup(it["title"]["rendered"], "html.parser").get_text()),
            "guest": any("guest" in c for c in it.get("class_list", [])),
        } for it in items if it.get("link")]
    except Exception as e:  # noqa: BLE001
        print("  centaur: REST API failed, falling back to sitemap:", e)
    xml = get("https://centaurtheatre.com/centaur_event-sitemap.xml", "centaur_sitemap.xml").text
    rows = re.findall(r"<loc>(https://centaurtheatre\.com/shows/[^<]+)</loc>\s*<lastmod>([^<]+)</lastmod>", xml)
    cutoff = (TODAY - timedelta(days=270)).isoformat()
    recent = sorted((lm, loc) for loc, lm in rows if lm[:10] >= cutoff)[-40:]
    return [{"id": slug(loc.rstrip("/").rsplit("/", 1)[-1]), "link": loc,
             "title": loc.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").title(), "guest": False}
            for _, loc in recent]


def src_centaur() -> list[Event]:
    shows = _centaur_shows()
    events: list[Event] = []
    for i, sh in enumerate(shows):
        if i:
            time.sleep(1.5)                      # the site rate-limits bursts
        try:
            page = BeautifulSoup(get(sh["link"], f"centaur_{sh['id']}.html").text, "html.parser")
        except Exception as e:  # noqa: BLE001
            print("  centaur: page fetch failed", sh["link"], e)
            continue
        for s in page(["script", "style", "nav", "footer", "header"]):
            s.decompose()
        h1 = page.select_one("h1")
        title = text_of(h1) or sh["title"]
        main = page.select_one("main, article, .entry-content, #content") or page
        rng = parse_en_range(text_of(main))
        if not rng:
            print("  centaur: no dates for", title)
            continue
        first, last = rng
        if last < KEEP_FROM or (last - first).days > 120:
            continue
        events.append(span_event(
            "centaur", f"centaur-{sh['id']}@karenda", f"{title} · Centaur", first, last,
            location="Centaur Theatre, 453 Saint-François-Xavier, Montréal", url=sh["link"],
            description="Guest show" if sh["guest"] else "", categories=["Theatre", "Centaur"],
        ))
    if not events:
        raise RuntimeError("no events parsed")
    return events


def src_rideauvert() -> list[Event]:
    url = "https://rideauvert.qc.ca/programmation/"
    soup = BeautifulSoup(get(url, "rideauvert.html").text, "html.parser")
    date_re = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})\s*/\s*(\d{2})\.(\d{2})\.(\d{4})")
    events: dict[str, Event] = {}
    for a in soup.select('a[href*="/piece/"]'):
        href = a.get("href", "").split("?")[0]
        if href in events or not href:
            continue
        node, txt, m = a, "", None
        for _ in range(5):
            txt = text_of(node)
            m = date_re.search(txt)
            if m or node.parent is None:
                break
            node = node.parent
        if not m:
            continue
        first = date(int(m[3]), int(m[2]), int(m[1])); last = date(int(m[6]), int(m[5]), int(m[4]))
        if last < first or last < KEEP_FROM:
            continue
        title = ""
        for sel in ("h1", "h2", "h3", "h4", "[class*=title]"):
            h = node.select_one(sel)
            if h and text_of(h):
                title = text_of(h); break
        if not title:
            title = text_of(a) or href.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").title()
        title = date_re.sub("", title).strip(" -|·")
        low = txt.lower()
        on_tour = "tournée" in low or "tournee" in low or "en tournée" in low
        if on_tour and "rideau vert" not in low.replace("théâtre du rideau vert", ""):
            # tour listings are not at the theatre; keep only if the block does not say tour
            continue
        events[href] = span_event(
            "rideauvert", f"trv-{slug(href.rstrip('/').rsplit('/', 1)[-1])}@karenda", f"{title} · Rideau Vert",
            first, last, location="Théâtre du Rideau Vert, 4664 rue Saint-Denis, Montréal", url=href,
            categories=["Theatre", "Rideau Vert"],
        )
    if not events:
        raise RuntimeError("no events parsed")
    return list(events.values())


AHL_FEED = "https://lscluster.hockeytech.com/feed/index.php"
AHL_KEYS = ("ccb91f29d6744675", "50c2cd9b5e18e390")


def _ahl(view: str, name: str, **params) -> dict:
    """HockeyTech 'modulekit' JSON behind theahl.com stats. Returns the SiteKit payload."""
    err: Exception = RuntimeError("no AHL key accepted")
    for key in AHL_KEYS:
        q = {"feed": "modulekit", "view": view, "key": key, "client_code": "ahl", "lang": "en", "fmt": "json", **params}
        try:
            text = get(AHL_FEED, name, params=q).text.strip()
            data = json.loads(text[1:-1] if text.startswith("(") else text)
            kit = data.get("SiteKit") if isinstance(data, dict) else None
            if isinstance(kit, dict) and not kit.get("Error"):
                return kit
            err = RuntimeError(f"{view}: unexpected payload {text[:160]!r}")
        except Exception as e:  # noqa: BLE001
            err = e
    raise err


def _ahl_start(g: dict) -> datetime | None:
    iso = g.get("GameDateISO8601") or g.get("date_time_played")
    if isinstance(iso, str):
        try:
            d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=MTL)
        except ValueError:
            pass
    try:
        d = datetime.strptime(f"{g.get('date_played')} {g.get('schedule_time') or '19:00:00'}"[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    try:
        tz = ZoneInfo(g.get("timezone") or "America/Toronto")
    except Exception:  # noqa: BLE001
        tz = MTL
    return d.replace(tzinfo=tz)


def src_rocket() -> list[Event]:
    for u, n in (("https://www.rocketlaval.com/en/schedule/", "rocket_schedule.html"),
                 ("https://theahl.com/stats/schedule", "ahl_schedule_page.html")):
        try:
            get(u, n)
        except Exception as e:  # noqa: BLE001
            print("  rocket: probe failed", u, e)
    seasons = _ahl("seasons", "ahl_seasons.json").get("Seasons") or []
    y = TODAY.year if TODAY.month >= 7 else TODAY.year - 1
    label = f"{y}-{(y + 1) % 100:02d}"                                  # "2026-27"
    cur = [s for s in seasons if label in s.get("season_name", "") and "all-star" not in s.get("season_name", "").lower()]
    if not cur:
        cur = sorted(seasons, key=lambda s: int(s.get("season_id") or 0))[-2:]
    if not cur:
        raise RuntimeError("no AHL seasons listed")
    team_id, names = "415", {}
    for s in cur:
        try:
            teams = _ahl("teamsbyseason", "ahl_teams.json", season_id=s["season_id"]).get("Teamsbyseason") or []
        except Exception as e:  # noqa: BLE001
            print("  rocket: teams lookup failed", e)
            continue
        names = {str(t.get("id")): t.get("name") or f"{t.get('city', '')} {t.get('nickname', '')}".strip() for t in teams}
        hit = next((t for t in teams if "laval" in " ".join(str(v) for v in t.values()).lower()), None)
        if hit:
            team_id = str(hit["id"])
            break
    events: list[Event] = []
    for s in cur:
        sname = s.get("season_name", "").lower()
        tag = " (playoffs)" if s.get("playoff") == "1" or "playoff" in sname else (" (preseason)" if "pre" in sname else "")
        sched = _ahl("schedule", f"ahl_schedule_{s['season_id']}.json", season_id=s["season_id"], team_id=team_id).get("Schedule") or []
        for g in sched:
            if str(g.get("home_team")) != team_id:
                continue
            start = _ahl_start(g)
            if not start:
                continue
            vid = str(g.get("visiting_team"))
            opp = g.get("visiting_team_name") or names.get(vid) or f"{g.get('visiting_team_city', '')} {g.get('visiting_team_nickname', '')}".strip() or "TBD"
            venue = g.get("venue_name") or "Place Bell"
            city = g.get("venue_location") or ("Laval" if "bell" in venue.lower() and "centre" not in venue.lower() else "Montréal")
            events.append(Event(
                source="rocket", uid=f"ahl-{g.get('game_id') or g.get('id')}@karenda", summary=f"Rocket vs {opp}{tag}",
                start=start, end=start + timedelta(hours=3), location=f"{venue}, {city}",
                url="https://www.rocketlaval.com/en/schedule/", categories=["Sports", "AHL"],
            ))
    if not events:
        raise RuntimeError("no home games parsed")
    return events


MODERNE = "https://www.cinemamoderne.com"
MODERNE_LOC = "Cinéma Moderne, 5150 boul. Saint-Laurent, Montréal"
FR_DATE_RE = re.compile(
    r"(?:(?:lun|mar|mer|jeu|ven|sam|dim|mon|tue|wed|thu|fri|sat|sun)[a-zé]*\.?,?\s+)?"
    r"(\d{1,2})(?:er|st|nd|rd|th)?\s+(janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|septembre|"
    r"octobre|novembre|décembre|decembre|jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|january|february|"
    r"march|april|june|july|august|september|october|november|december)\.?(?:\s+(\d{4}))?", re.I)
TIME_RE = re.compile(r"\b(\d{1,2})\s*(?:h|:)\s*(\d{2})?\s*(am|pm|AM|PM)?\b")


def _near_date(text: str) -> date | None:
    m = FR_DATE_RE.search(text)
    if not m:
        return None
    mo = month_num(m[2])
    if not mo:
        return None
    y = int(m[3]) if m[3] else TODAY.year
    try:
        d = date(y, mo, int(m[1]))
    except ValueError:
        return None
    if not m[3] and d < TODAY - timedelta(days=45):
        d = d.replace(year=y + 1)
    return d


def _walk_ld(obj, out: list) -> None:
    if isinstance(obj, dict):
        t = obj.get("@type")
        if (isinstance(t, str) and t in ("Event", "ScreeningEvent", "Movie")) or (isinstance(t, list) and "Event" in t):
            out.append(obj)
        for v in obj.values():
            _walk_ld(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_ld(v, out)


def src_cinemamoderne() -> list[Event]:
    pages: dict[str, str] = {}
    for path, name in (("/", "moderne_home.html"), ("/en/", "moderne_home_en.html"),
                       ("/programmation/", "moderne_programmation.html"), ("/en/programming/", "moderne_programming.html"),
                       ("/horaire/", "moderne_horaire.html"), ("/en/schedule/", "moderne_schedule.html"),
                       ("/films/", "moderne_films.html"), ("/en/films/", "moderne_films_en.html"),
                       ("/wp-json/wp/v2/types", "moderne_types.json"), ("/wp-json/", "moderne_wpjson.json"),
                       ("/sitemap.xml", "moderne_sitemap.xml"), ("/wp-sitemap.xml", "moderne_wp_sitemap.xml")):
        try:
            pages[path] = get(MODERNE + path, name).text
        except Exception as e:  # noqa: BLE001
            print("  moderne: probe failed", path, e)
    events: dict[str, Event] = {}
    for path, page_html in pages.items():
        if not path.endswith("/"):
            continue
        soup = BeautifulSoup(page_html, "html.parser")
        ld: list = []
        for s in soup.find_all("script", type="application/ld+json"):
            try:
                _walk_ld(json.loads(s.string or ""), ld)
            except Exception:  # noqa: BLE001
                pass
        for o in ld:
            sd, title = o.get("startDate"), o.get("name")
            if not (isinstance(sd, str) and isinstance(title, str)):
                continue
            try:
                start = datetime.fromisoformat(sd.replace("Z", "+00:00"))
            except ValueError:
                continue
            if start.tzinfo is None:
                start = start.replace(tzinfo=MTL)
            url = o.get("url") if isinstance(o.get("url"), str) else ""
            key = f"{title}|{start.isoformat()}"
            events[key] = Event(
                source="cinemamoderne", uid=f"moderne-{slug(title)}-{start:%Y%m%d%H%M}@karenda",
                summary=f"{html.unescape(title)} · Moderne", start=start, end=start + timedelta(hours=2),
                location=MODERNE_LOC, url=url or MODERNE, categories=["Cinema", "Cinéma Moderne"],
            )
        for a in soup.select('a[href*="/film"], a[href*="/projection"], a[href*="/event"], a[href*="/evenement"], a[href*="/seance"]'):
            href = a.get("href", "").split("?")[0]
            if not href or href.rstrip("/") == MODERNE.rstrip("/"):
                continue
            node, txt, d = a, "", None
            for _ in range(4):
                txt = text_of(node)
                d = _near_date(txt)
                if d or node.parent is None:
                    break
                node = node.parent
            if not d:
                continue
            tm = TIME_RE.search(txt)
            if not tm:
                continue
            hh, mm = int(tm[1]), int(tm[2] or 0)
            if tm[3] and tm[3].lower() == "pm" and hh < 12:
                hh += 12
            if not (0 <= hh < 24 and 0 <= mm < 60):
                continue
            title = ""
            for sel in ("h1", "h2", "h3", "h4", "[class*=title]"):
                h = a.select_one(sel) or node.select_one(sel)
                if h and text_of(h):
                    title = text_of(h)
                    break
            title = title or text_of(a) or href.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").title()
            title = FR_DATE_RE.sub("", TIME_RE.sub("", title)).strip(" -|·,")
            if not title:
                continue
            start = datetime(d.year, d.month, d.day, hh, mm, tzinfo=MTL)
            key = f"{title}|{start.isoformat()}"
            full = href if href.startswith("http") else MODERNE + href
            events.setdefault(key, Event(
                source="cinemamoderne", uid=f"moderne-{slug(title)}-{start:%Y%m%d%H%M}@karenda",
                summary=f"{title} · Moderne", start=start, end=start + timedelta(hours=2),
                location=MODERNE_LOC, url=full, categories=["Cinema", "Cinéma Moderne"],
            ))
    if not events:
        raise RuntimeError("no screenings parsed")
    return list(events.values())


# --------------------------------------------------------------------------- html page
MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def plain_title(e: Event) -> str:
    return re.sub(r"\s*·\s*(PdA|Centaur|Rideau Vert|Moderne)$", "", e.summary)


def venue_of(e: Event) -> str:
    parts = [p.strip() for p in e.location.split(",")]
    parts = [p for p in parts if p and p != "Montréal" and not re.search(r"\d", p)]
    return ", ".join(parts[:2])


def when_of(e: Event, month: date) -> str:
    if e.all_day:
        a, b = e.start, e.end - timedelta(days=1)
        if a == b:
            return f"{DOW[a.weekday()]}, {MON[a.month - 1]} {a.day}"
        if a < month:
            return f"until {MON[b.month - 1]} {b.day}"
        if a.month == b.month:
            return f"{MON[a.month - 1]} {a.day} – {b.day}"
        return f"{MON[a.month - 1]} {a.day} – {MON[b.month - 1]} {b.day}"
    s = e.start.astimezone(MTL)
    h = s.strftime("%-I:%M %p").lower().replace(":00", "")
    return f"{DOW[s.weekday()]}, {MON[s.month - 1]} {s.day} · {h}"


def render_html(events: list[Event]) -> str:
    today = TODAY
    first_month = today.replace(day=1)
    months: dict[date, list[Event]] = {}
    now = datetime.now(timezone.utc)
    for e in events:
        if e.all_day and e.end - timedelta(days=1) < today:
            continue
        if not e.all_day and e.end < now:
            continue
        start = e.start.astimezone(MTL).date() if isinstance(e.start, datetime) else e.start
        key = max(start.replace(day=1), first_month)
        months.setdefault(key, []).append(e)

    def sort_key(e: Event):
        s = e.start.astimezone(MTL) if isinstance(e.start, datetime) else datetime.combine(e.start, datetime.min.time(), MTL)
        return (s, plain_title(e))

    out = []
    for m in sorted(months):
        out.append(f'<section class="month"><h2>{MONTHS_FULL[m.month - 1]} {m.year}</h2><ul>')
        for e in sorted(months[m], key=sort_key):
            t = html.escape(plain_title(e))
            link = f'<a href="{html.escape(e.url)}" target="_blank" rel="noopener">{t}</a>' if e.url else t
            meta = html.escape(when_of(e, m)) + (f' · {html.escape(venue_of(e))}' if venue_of(e) else "")
            out.append(f'<li>{link}<span class="meta">{meta}</span></li>')
        out.append("</ul></section>")
    body = "\n".join(out)
    updated = datetime.now(MTL).strftime("%b %-d, %Y")
    return HTML_TEMPLATE.replace("{{BODY}}", body).replace("{{UPDATED}}", updated)


MONTHS_FULL = ["January", "February", "March", "April", "May", "June", "July", "August",
               "September", "October", "November", "December"]

HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Karenda</title>
<meta name="description" content="What's on in Montréal: theatre runs and home games, by month.">
<style>
:root { color-scheme: light; --paper:#fdfdfc; --ink:#292824; --link:#080808; --muted:#77746d; --rule:#e5e2db; }
@media (prefers-color-scheme: dark) { :root { color-scheme: dark; --paper:#121213; --ink:#deddd8; --link:#ffffff; --muted:#9d9b96; --rule:#303030; } }
* { box-sizing: border-box; }
html { background: var(--paper); }
body { max-width: 480px; margin: 0 auto; padding: 72px 24px 60px; color: var(--ink);
  font-family: -apple-system, BlinkMacSystemFont, Inter, "Segoe UI", system-ui, sans-serif;
  font-size: 14px; font-weight: 400; line-height: 1.7; -webkit-font-smoothing: antialiased; overflow-wrap: break-word; }
a { color: var(--link); font-weight: 450; text-decoration: none; }
header { display: flex; justify-content: space-between; align-items: baseline; gap: 20px; margin: 0 0 46px; }
header h1 { margin: 0; font-size: 14px; font-weight: 500; letter-spacing: -0.2px; line-height: 1.4; }
header nav { display: flex; gap: 20px; font-size: 12px; }
header nav a { color: var(--muted); font-weight: 400; }
.month { display: grid; grid-template-columns: 64px minmax(0, 1fr); gap: 22px; margin: 0 0 25px; }
.month h2 { color: var(--muted); font-size: 12px; font-weight: 400; margin: 3px 0 0; line-height: 1.5; }
.month ul { list-style: none; margin: 0; padding: 0; }
.month li { margin: 0 0 13px; line-height: 1.45; }
.meta { display: block; margin-top: 3px; color: var(--muted); font-size: 11px; }
footer { margin-top: 48px; padding-top: 16px; border-top: 1px solid var(--rule); color: var(--muted); font-size: 11px; }
@media (max-width: 420px) { .month { grid-template-columns: 1fr; gap: 6px; } .month h2 { margin-bottom: 4px; } }
</style>
</head>
<body>
<header>
  <h1>Karenda</h1>
  <nav>
    <a href="karenda.ics" title="Subscribe in your calendar app">Subscribe</a>
    <a href="https://github.com/dnsmln/karenda">Source</a>
  </nav>
</header>
<main>
{{BODY}}
</main>
<footer>Updated {{UPDATED}} · Montréal theatre runs and home games. Rebuilt every morning.</footer>
</body>
</html>
"""


SOURCES = {
    "victoire": src_victoire,
    "cfmontreal": src_cfmontreal,
    "placedesarts": src_placedesarts,
    "centaur": src_centaur,
    "rideauvert": src_rideauvert,
    "rocket": src_rocket,
    "cinemamoderne": src_cinemamoderne,
}
GROUPS = {
    "karenda": list(SOURCES),
    "karenda-sports": ["victoire", "cfmontreal", "rocket"],
    "karenda-theatre": ["placedesarts", "centaur", "rideauvert"],
    "karenda-cinema": ["cinemamoderne"],
}


def main() -> int:
    previous = read_previous(OUT / "karenda.ics")
    if "--render-only" in sys.argv:          # rebuild index.html from the existing ICS, no network
        (OUT / "index.html").write_text(render_html([e for l in previous.values() for e in l]), encoding="utf-8")
        print("rendered docs/index.html from existing karenda.ics")
        return 0
    status: dict = {"built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "sources": {}}
    by_source: dict[str, list[Event]] = {}
    for name, fn in SOURCES.items():
        try:
            evs = [e for e in fn() if e.end_date() >= KEEP_FROM]
            by_source[name] = evs
            status["sources"][name] = {"ok": True, "events": len(evs)}
            print(f"{name}: {len(evs)} events")
        except Exception as e:  # noqa: BLE001
            kept = [x for x in previous.get(name, []) if x.end_date() >= KEEP_FROM]
            by_source[name] = kept
            status["sources"][name] = {"ok": False, "error": f"{type(e).__name__}: {e}", "kept_previous": len(kept)}
            print(f"{name}: FAILED ({e}); kept {len(kept)} previous events", file=sys.stderr)
            traceback.print_exc()
    for fname, srcs in GROUPS.items():
        evs = [e for s in srcs for e in by_source.get(s, [])]
        write_ics(OUT / f"{fname}.ics", {"karenda": "Karenda", "karenda-sports": "Karenda · Sports",
                                          "karenda-theatre": "Karenda · Théâtre", "karenda-cinema": "Karenda · Cinéma"}[fname], evs)
        status[fname] = len(evs)
    (OUT / "index.html").write_text(render_html([e for l in by_source.values() for e in l]), encoding="utf-8")
    (OUT / "status.json").write_text(json.dumps(status, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(status, indent=2, ensure_ascii=False))
    return 0 if any(v.get("ok") for v in status["sources"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
