#!/usr/bin/env python3
"""Build karenda.ics: Montreal theatre runs + home games, from public sources.

Sources
  victoire      PWHL season ICS files linked from thepwhl.com (home games in the Montréal area only)
  cfmontreal    ESPN public schedule API (home games only)
  placedesarts  placedesarts.com/en/programming listing
  rideauvert    rideauvert.qc.ca/programmation

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
    """Parse English date text as used by Place des Arts.

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


# --------------------------------------------------------------------------- sources
# The PWHL lists a "home" team for neutral-site games too (e.g. Montréal "hosting" Boston in
# Wellesley, MA), so the home team alone is not enough: the venue must be in the Montréal area.
HOME_AREA = re.compile(r"montr|laval|place bell|centre bell|bell centre", re.I)


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
            if loc and loc.upper() != "TBD" and not HOME_AREA.search(loc):
                print("victoire: skipping neutral-site game", summ, "@", loc)
                continue
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


# --------------------------------------------------------------------------- html page
MTL = ZoneInfo("America/Toronto")
MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
PLACES = {  # source -> pill label on the web page (venue or team), in display order
    "placedesarts": "Place des Arts",
    "rideauvert": "Rideau Vert",
    "victoire": "Victoire",
    "cfmontreal": "CF Montréal",
}
HOME = {  # source -> venue text hidden while that source's pill is active (it repeats the pill)
    "placedesarts": "Place des Arts",
    "rideauvert": "Théâtre du Rideau Vert",
    "victoire": "Place Bell, Laval",
    "cfmontreal": "Stade Saputo",
}


def plain_title(e: Event) -> str:
    return re.sub(r"\s*·\s*(PdA|Rideau Vert)$", "", e.summary)


def venue_of(e: Event) -> str:
    parts = [p.strip() for p in e.location.split(",")]
    parts = [p for p in parts if p and p != "Montréal" and not re.search(r"\d", p)]
    return ", ".join(parts[:2])


def venue_parts(e: Event) -> tuple[str, str]:
    """Split the venue into (specific, home). The home part repeats the active pill, so the page hides it when filtering."""
    venue, home = venue_of(e), HOME.get(e.source, "")
    if home and venue.endswith(home):
        return venue[: len(venue) - len(home)].rstrip(", "), home
    return venue, ""


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
        out.append(f'<section class="month"><h2>{MON[m.month - 1]} {m.year}</h2><ul>')
        for e in sorted(months[m], key=sort_key):
            t = html.escape(plain_title(e))
            link = f'<a href="{html.escape(e.url)}" target="_blank" rel="noopener">{t}</a>' if e.url else t
            specific, place = venue_parts(e)
            meta = html.escape(when_of(e, m))
            if specific:
                meta += f" · {html.escape(specific)}"
            if place:
                meta += f'<span class="place">{", " if specific else " · "}{html.escape(place)}</span>'
            out.append(f'<li data-place="{html.escape(e.source)}">{link}<span class="meta">{meta}</span></li>')
        out.append("</ul></section>")
    body = "\n".join(out)
    present = {e.source for l in months.values() for e in l}
    pills = [f'<button type="button" data-place="{src}" aria-pressed="false">{html.escape(label)}</button>'
              for src, label in PLACES.items() if src in present]
    places = '<nav class="places" aria-label="Filter">' + "".join(pills) + "</nav>"
    updated = datetime.now(MTL).strftime("%b %-d, %Y")
    return (HTML_TEMPLATE.replace("{{PLACES}}", places).replace("{{BODY}}", body)
            .replace("{{UPDATED}}", updated))


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
header { display: flex; justify-content: space-between; align-items: baseline; gap: 20px; margin: 0 0 28px; }
header h1 { margin: 0; font-size: 14px; font-weight: 500; letter-spacing: -0.2px; line-height: 1.4; }
header nav { display: flex; gap: 20px; font-size: 12px; }
header nav a { color: var(--muted); font-weight: 400; }
.places { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 40px; }
.places button { appearance: none; background: none; border: 1px solid var(--rule); border-radius: 999px;
  padding: 2px 10px; color: var(--muted); font: inherit; font-size: 12px; line-height: 1.6; cursor: pointer; }
.places button[aria-pressed="true"] { border-color: var(--ink); color: var(--ink); }
[hidden] { display: none !important; }
.filtered .place { display: none; }
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
{{PLACES}}
<main>
{{BODY}}
</main>
<footer>Updated {{UPDATED}}</footer>
<script>
(function () {
  var pills = document.querySelectorAll(".places button");
  var items = document.querySelectorAll(".month li");
  var months = document.querySelectorAll(".month");
  function apply(place) {            // "" = no filter, show everything
    pills.forEach(function (b) { b.setAttribute("aria-pressed", String(b.dataset.place === place)); });
    items.forEach(function (li) { li.hidden = !!place && li.dataset.place !== place; });
    months.forEach(function (m) { m.hidden = !m.querySelector("li:not([hidden])"); });
    document.body.classList.toggle("filtered", !!place);
    history.replaceState(null, "", location.pathname + location.search + (place ? "#" + place : ""));
  }
  pills.forEach(function (b) { b.addEventListener("click", function () {
    apply(b.getAttribute("aria-pressed") === "true" ? "" : b.dataset.place);
  }); });
  var h = location.hash.slice(1);
  apply(document.querySelector('.places button[data-place="' + h + '"]') ? h : "");
})();
</script>
</body>
</html>
"""


SOURCES = {
    "victoire": src_victoire,
    "cfmontreal": src_cfmontreal,
    "placedesarts": src_placedesarts,
    "rideauvert": src_rideauvert,
}
GROUPS = {
    "karenda": list(SOURCES),
    "karenda-sports": ["victoire", "cfmontreal"],
    "karenda-theatre": ["placedesarts", "rideauvert"],
}


def main() -> int:
    previous = read_previous(OUT / "karenda.ics")
    if "--render-only" in sys.argv:          # rebuild index.html from the existing ICS, no network
        (OUT / "index.html").write_text(render_html([e for s in SOURCES for e in previous.get(s, [])]), encoding="utf-8")
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
                                          "karenda-theatre": "Karenda · Théâtre"}[fname], evs)
        status[fname] = len(evs)
    (OUT / "index.html").write_text(render_html([e for l in by_source.values() for e in l]), encoding="utf-8")
    (OUT / "status.json").write_text(json.dumps(status, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(status, indent=2, ensure_ascii=False))
    return 0 if any(v.get("ok") for v in status["sources"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
