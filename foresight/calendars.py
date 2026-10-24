"""Local calendars for Daily Briefings: .ics files or subscription URLs instead of Google sign-in.

A small stdlib-only iCalendar (RFC 5545) reader. It expands the next week of VEVENTs, including
simple DAILY / WEEKLY RRULEs, EXDATEs, moved or cancelled instances (RECURRENCE-ID) and TZIDs.
"""
from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from . import db, events

log = logging.getLogger(__name__)

WINDOW_DAYS = 7
REFRESH_SECONDS = 900
MAX_ICS_BYTES = 5 * 1024 * 1024
MAX_PERIODS = 20000         # safety stop for a recurrence with a huge COUNT

_started = False

# Outlook and Exchange exports use Windows zone names; map the common ones to IANA.
_WINDOWS_ZONES = {
    "UTC": "UTC", "Coordinated Universal Time": "UTC", "GMT Standard Time": "Europe/London",
    "Greenwich Standard Time": "Atlantic/Reykjavik", "W. Europe Standard Time": "Europe/Berlin",
    "Romance Standard Time": "Europe/Paris", "Central Europe Standard Time": "Europe/Budapest",
    "Central European Standard Time": "Europe/Warsaw", "E. Europe Standard Time": "Europe/Chisinau",
    "FLE Standard Time": "Europe/Kiev", "GTB Standard Time": "Europe/Bucharest",
    "Russian Standard Time": "Europe/Moscow", "Israel Standard Time": "Asia/Jerusalem",
    "Arabian Standard Time": "Asia/Dubai", "India Standard Time": "Asia/Kolkata",
    "SE Asia Standard Time": "Asia/Bangkok", "China Standard Time": "Asia/Shanghai",
    "Singapore Standard Time": "Asia/Singapore", "Tokyo Standard Time": "Asia/Tokyo",
    "Korea Standard Time": "Asia/Seoul", "AUS Eastern Standard Time": "Australia/Sydney",
    "E. Australia Standard Time": "Australia/Brisbane", "New Zealand Standard Time": "Pacific/Auckland",
    "Hawaiian Standard Time": "Pacific/Honolulu", "Alaskan Standard Time": "America/Anchorage",
    "Pacific Standard Time": "America/Los_Angeles", "US Mountain Standard Time": "America/Phoenix",
    "Mountain Standard Time": "America/Denver", "Central Standard Time": "America/Chicago",
    "Eastern Standard Time": "America/New_York", "Atlantic Standard Time": "America/Halifax",
    "SA Eastern Standard Time": "America/Cayenne", "E. South America Standard Time": "America/Sao_Paulo",
    "South Africa Standard Time": "Africa/Johannesburg",
}
_WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
_DT = re.compile(r"^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})?(Z)?)?$")
_DURATION = re.compile(r"^([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


class CalendarError(ValueError):
    pass


@dataclass
class Event:
    uid: str
    title: str
    starts_at: float
    ends_at: float
    all_day: bool = False
    description: str = ""
    location: str = ""
    attendees: list[str] = field(default_factory=list)

    @property
    def fingerprint(self) -> str:
        data = [self.title, self.description, self.location, self.attendees, self.starts_at, self.ends_at, self.all_day]
        return hashlib.sha1(json.dumps(data, ensure_ascii=False).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- reading .ics text

def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]  # folded continuation line
        elif raw.strip():
            lines.append(raw)
    return lines


def _parse_line(line: str) -> tuple[str, dict[str, str], str] | None:
    """NAME;PARAM=a;PARAM="b:c":value → (NAME, {PARAM: ...}, value). Colons inside quotes are not separators."""
    in_quote = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_quote = not in_quote
        elif ch == ":" and not in_quote:
            head, value = line[:i], line[i + 1:]
            break
    else:
        return None
    parts = re.findall(r'(?:[^;"]|"[^"]*")+', head)
    if not parts:
        return None
    params = {}
    for p in parts[1:]:
        key, _, val = p.partition("=")
        params[key.strip().upper()] = val.strip().strip('"')
    return parts[0].strip().upper(), params, value


def _unescape(value: str) -> str:
    return re.sub(r"\\([\\,;nN])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value)


Props = dict[str, list[tuple[dict[str, str], str]]]


def _read(text: str) -> tuple[dict[str, str], list[Props]]:
    """Return the calendar-level properties and the property lists of every VEVENT (alarms skipped)."""
    if "BEGIN:VCALENDAR" not in text[:4096].upper():
        raise CalendarError("not an iCalendar (.ics) file")
    cal: dict[str, str] = {}
    vevents: list[Props] = []
    stack: list[str] = []
    current: Props | None = None
    for line in _unfold(text):
        parsed = _parse_line(line)
        if not parsed:
            continue
        name, params, value = parsed
        if name == "BEGIN":
            stack.append(value.strip().upper())
            if stack[-1] == "VEVENT":
                current = {}
        elif name == "END":
            if value.strip().upper() == "VEVENT" and current is not None:
                vevents.append(current)
                current = None
            if stack:
                stack.pop()
        elif current is not None and stack and stack[-1] == "VEVENT":
            current.setdefault(name, []).append((params, value))
        elif stack == ["VCALENDAR"]:
            cal.setdefault(name, _unescape(value).strip())
    return cal, vevents


# ---------------------------------------------------------------- dates and times

@functools.lru_cache(maxsize=64)
def _zone(tzid: str | None) -> tzinfo | None:
    """IANA or Windows zone for a TZID; None means floating (this PC's local time)."""
    if not tzid:
        return None
    name = tzid.strip().strip('"')
    candidates = [_WINDOWS_ZONES.get(name, name)]
    m = re.search(r"([A-Za-z_]+/[A-Za-z0-9_+\-]+(?:/[A-Za-z0-9_+\-]+)?)$", name)  # /mozilla.org/…/Europe/Berlin
    if m:
        candidates.append(m.group(1))
    for candidate in candidates:
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    log.info("unknown TZID %r; using local time", tzid)
    return None


def _parse_dt(value: str, params: dict[str, str]) -> datetime | date:
    m = _DT.match(value.strip())
    if not m:
        raise ValueError(f"bad date-time {value!r}")
    y, mo, d, hh, mi, ss, utc = m.groups()
    if hh is None or params.get("VALUE", "").upper() == "DATE":
        return date(int(y), int(mo), int(d))
    dt = datetime(int(y), int(mo), int(d), int(hh), int(mi), int(ss or 0))
    if utc:
        return dt.replace(tzinfo=timezone.utc)
    return dt.replace(tzinfo=_zone(params.get("TZID")))


def _ts(value: datetime | date) -> float:
    if isinstance(value, datetime):
        return value.timestamp()  # naive (floating) times are read as local time
    return datetime(value.year, value.month, value.day).timestamp()


def _parse_duration(value: str) -> timedelta | None:
    m = _DURATION.match(value.strip().upper())
    if not m:
        return None
    sign, w, d, h, mi, s = m.groups()
    delta = timedelta(weeks=int(w or 0), days=int(d or 0), hours=int(h or 0), minutes=int(mi or 0), seconds=int(s or 0))
    return -delta if sign == "-" else delta


def _duration(ve: Props, first: datetime | date) -> timedelta:
    all_day = not isinstance(first, datetime)
    if "DTEND" in ve:
        try:
            end = _parse_dt(ve["DTEND"][0][1], ve["DTEND"][0][0])
            if all_day:
                end_day = end.date() if isinstance(end, datetime) else end
                return timedelta(days=max(1, (end_day - first).days))
            return timedelta(seconds=max(0.0, _ts(end) - _ts(first)))
        except ValueError:
            pass
    if "DURATION" in ve:
        delta = _parse_duration(ve["DURATION"][0][1])
        if delta is not None and delta >= timedelta(0):
            return delta
    return timedelta(days=1) if all_day else timedelta(0)


# ---------------------------------------------------------------- recurrence

def _occurrences(first: datetime | date, rule: dict[str, str], window_start: float,
                 window_end: float) -> Iterator[datetime | date]:
    """Instance starts of a DAILY or WEEKLY rule, in order, until the window ends.

    Other frequencies (MONTHLY, YEARLY, …) are not expanded; only the first instance is used.
    Times repeat on the wall clock of the event's own zone, so they stay put across DST changes."""
    freq = rule.get("FREQ", "").upper()
    if freq not in ("DAILY", "WEEKLY"):
        yield first
        return
    interval = max(1, int(rule["INTERVAL"])) if rule.get("INTERVAL", "").isdigit() else 1
    count = int(rule["COUNT"]) if rule.get("COUNT", "").isdigit() else None
    until = None
    if rule.get("UNTIL"):
        try:
            u = _parse_dt(rule["UNTIL"], {})
            until = _ts(u) if isinstance(u, datetime) else _ts(u) + 86399  # a DATE includes that whole day
        except ValueError:
            pass
    byday = sorted({_WEEKDAYS[d.strip()[-2:].upper()] for d in rule.get("BYDAY", "").split(",")
                    if d.strip()[-2:].upper() in _WEEKDAYS})

    is_dt = isinstance(first, datetime)
    day0 = first.date() if is_dt else first

    def at(d: date) -> datetime | date:
        return datetime.combine(d, first.time(), tzinfo=first.tzinfo) if is_dt else d

    if freq == "DAILY":
        step = interval
        period = day0
        offsets = [0]
    else:
        step = 7 * interval
        wkst = _WEEKDAYS.get(rule.get("WKST", "MO").upper(), 0)
        period = day0 - timedelta(days=(day0.weekday() - wkst) % 7)  # first day of DTSTART's week
        offsets = sorted((wd - wkst) % 7 for wd in (byday or [day0.weekday()]))

    if count is None:
        # Skip whole periods that end before the window, keeping one period of slack for time zones.
        lag = (datetime.fromtimestamp(window_start).date() - period).days - step - 1
        if lag > 0:
            period += timedelta(days=lag // step * step)

    n = 0
    for _ in range(MAX_PERIODS):
        for off in offsets:
            d = period + timedelta(days=off)
            if d < day0 or (freq == "DAILY" and byday and d.weekday() not in byday):
                continue
            inst = at(d)
            ts = _ts(inst)
            if (until is not None and ts > until) or ts >= window_end:
                return
            n += 1
            if count is not None and n > count:
                return
            yield inst
        period += timedelta(days=step)


def _exdates(ve: Props) -> set[int]:
    out: set[int] = set()
    for params, value in ve.get("EXDATE", []):
        for part in value.split(","):
            try:
                out.add(round(_ts(_parse_dt(part, params))))
            except ValueError:
                continue
    return out


# ---------------------------------------------------------------- events

def _text(ve: Props, name: str) -> str:
    values = ve.get(name)
    return _unescape(values[0][1]).strip() if values else ""


def _attendees(ve: Props) -> list[str]:
    names: list[str] = []
    for prop in ("ORGANIZER", "ATTENDEE"):
        for params, value in ve.get(prop, []):
            if params.get("CUTYPE", "").upper() in ("ROOM", "RESOURCE"):
                continue
            name = params.get("CN", "").strip() or re.sub(r"(?i)^mailto:", "", value.strip())
            if name and name.lower() not in {n.lower() for n in names}:
                names.append(name)
    return names[:40]


def _instance(ve: Props, uid: str, start: datetime | date, duration: timedelta) -> Event:
    all_day = not isinstance(start, datetime)
    return Event(
        uid=uid,
        title=_text(ve, "SUMMARY") or "(no title)",
        starts_at=_ts(start),
        ends_at=_ts(start + duration),
        all_day=all_day,
        description=_text(ve, "DESCRIPTION")[:4000],
        location=_text(ve, "LOCATION")[:300],
        attendees=_attendees(ve),
    )


def _cancelled(ve: Props) -> bool:
    return _text(ve, "STATUS").upper() == "CANCELLED"


def parse_ics(text: str, *, start: float, end: float) -> list[Event]:
    """Every event instance that overlaps [start, end), sorted by start time."""
    return _expand(_read(text)[1], start=start, end=end)


def _expand(vevents: list[Props], *, start: float, end: float) -> list[Event]:
    masters: list[Props] = []
    overrides: dict[tuple[str, int], Props] = {}
    for ve in vevents:
        if "DTSTART" not in ve:
            continue
        if "RECURRENCE-ID" in ve:
            params, value = ve["RECURRENCE-ID"][0]
            try:
                overrides[(_text(ve, "UID"), round(_ts(_parse_dt(value, params))))] = ve
            except ValueError:
                pass
        else:
            masters.append(ve)

    found: list[Event] = []

    def keep(ev: Event) -> None:
        if ev.starts_at < end and (ev.ends_at > start or ev.starts_at >= start):
            found.append(ev)

    for ve in masters:
        if _cancelled(ve):
            continue
        params, value = ve["DTSTART"][0]
        try:
            first = _parse_dt(value, params)
        except ValueError:
            continue
        uid = _text(ve, "UID") or hashlib.sha1(f"{_text(ve, 'SUMMARY')}|{value}".encode()).hexdigest()
        duration = _duration(ve, first)
        rrule = ve.get("RRULE")
        if not rrule:
            keep(_instance(ve, uid, first, duration))
            continue
        rule = {k.strip().upper(): v.strip() for k, _, v in (p.partition("=") for p in rrule[0][1].split(";")) if k}
        skip = _exdates(ve)
        # Ask for instances that started up to one duration before the window, so ones in progress are kept.
        for inst in _occurrences(first, rule, start - duration.total_seconds(), end):
            key = round(_ts(inst))
            if key in skip or (uid, key) in overrides:
                continue
            keep(_instance(ve, f"{uid}#{key}", inst, duration))

    for (uid, key), ve in overrides.items():
        if _cancelled(ve):
            continue
        params, value = ve["DTSTART"][0]
        try:
            moved = _parse_dt(value, params)
        except ValueError:
            continue
        keep(_instance(ve, f"{uid}#{key}", moved, _duration(ve, moved)))

    found.sort(key=lambda e: (e.starts_at, e.title))
    return found


# ---------------------------------------------------------------- calendar sources

def _is_url(uri: str) -> bool:
    return urlparse(uri).scheme in ("http", "https")


def normalize(uri: str) -> str:
    uri = uri.strip().strip('"')
    if not uri:
        raise CalendarError("enter a .ics file path or a calendar URL")
    if uri.lower().startswith("webcal://"):
        uri = "https://" + uri[len("webcal://"):]
    if _is_url(uri):
        if not urlparse(uri).netloc:
            raise CalendarError("that URL has no host")
        return uri
    if re.match(r"^[a-z][a-z0-9+.\-]+://", uri, re.I):
        raise CalendarError("use a local .ics file or an https:// (or webcal://) calendar URL")
    path = Path(os.path.expandvars(os.path.expanduser(uri))).resolve()
    if not path.is_file():
        raise CalendarError(f"file not found: {path}")
    return str(path)


def _fetch(url: str) -> bytes:
    headers = {"User-Agent": "ForesightWing/0.1 (+calendar subscription)", "Accept": "text/calendar, */*"}
    body = bytearray()
    try:
        with httpx.stream("GET", url, headers=headers, follow_redirects=True, timeout=30) as resp:
            resp.raise_for_status()
            for part in resp.iter_bytes():
                body += part
                if len(body) > MAX_ICS_BYTES:
                    raise CalendarError("the calendar is larger than 5 MB")
    except httpx.HTTPStatusError as exc:
        raise CalendarError(f"the calendar server answered HTTP {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise CalendarError(f"could not download the calendar ({type(exc).__name__})") from exc
    return bytes(body)


def _load_text(uri: str) -> str:
    if _is_url(uri):
        raw = _fetch(uri)
    else:
        path = Path(uri)
        try:
            if path.stat().st_size > MAX_ICS_BYTES:
                raise CalendarError("the calendar is larger than 5 MB")
            raw = path.read_bytes()
        except OSError as exc:
            raise CalendarError(f"could not read {path.name}: {exc.strerror or exc}") from exc
    return raw.decode("utf-8-sig", "replace")


def _default_name(uri: str) -> str:
    if _is_url(uri):
        return urlparse(uri).netloc
    return Path(uri).stem


def _store(calendar_id: int, found: list[Event]) -> int:
    """Upsert this calendar's events by UID (ids stay stable, so cached briefings survive) and drop the rest."""
    unique: dict[str, Event] = {}
    for ev in found:
        unique.setdefault(ev.uid, ev)
    with db.tx() as conn:
        conn.executemany(
            "INSERT INTO events(calendar_id, uid, title, description, location, attendees, starts_at, ends_at, all_day, "
            "fingerprint) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(calendar_id, uid) DO UPDATE SET title=excluded.title, "
            "description=excluded.description, location=excluded.location, attendees=excluded.attendees, "
            "starts_at=excluded.starts_at, ends_at=excluded.ends_at, all_day=excluded.all_day, "
            "fingerprint=excluded.fingerprint",
            [(calendar_id, e.uid, e.title, e.description, e.location, db.dumps(e.attendees), e.starts_at, e.ends_at,
              int(e.all_day), e.fingerprint) for e in unique.values()])
        known = [r["uid"] for r in conn.execute("SELECT uid FROM events WHERE calendar_id = ?", (calendar_id,))]
        stale = [(calendar_id, uid) for uid in known if uid not in unique]
        conn.executemany("DELETE FROM events WHERE calendar_id = ? AND uid = ?", stale)
    return len(unique)


def _parse_week(text: str) -> tuple[str, list[Event]]:
    """The calendar's own name (if any) and its events from now through the next 7 days."""
    cal, vevents = _read(text)
    now = time.time()
    return cal.get("X-WR-CALNAME", ""), _expand(vevents, start=now, end=now + WINDOW_DAYS * 86400)


def _apply(calendar_id: int, name: str, found: list[Event]) -> dict:
    n = _store(calendar_id, found)
    db.execute("UPDATE calendars SET name = ?, synced_at = ?, error = NULL, n_events = ? WHERE id = ?",
               (name, db.now(), n, calendar_id))
    events.publish("calendar", id=calendar_id, status="synced", n_events=n)
    return {"id": calendar_id, "name": name, "n_events": n}


def add_calendar(uri: str) -> dict:
    uri = normalize(uri)
    name, found = _parse_week(_load_text(uri))  # raises before anything is stored if it is not iCalendar
    name = name or _default_name(uri)
    existing = db.query_one("SELECT id FROM calendars WHERE uri = ?", (uri,))
    calendar_id = existing["id"] if existing else db.execute(
        "INSERT INTO calendars(uri, name, added_at) VALUES (?,?,?)", (uri, name, db.now()))
    return {**_apply(calendar_id, name, found), "uri": uri}


def sync(calendar_id: int) -> dict:
    cal = db.query_one("SELECT * FROM calendars WHERE id = ?", (calendar_id,))
    if not cal:
        raise KeyError(calendar_id)
    try:
        name, found = _parse_week(_load_text(cal["uri"]))
        return _apply(calendar_id, name or cal["name"], found)
    except Exception as exc:
        # Keep the last good events: a flaky network should not empty the Upcoming list.
        log.warning("calendar %s sync failed: %s", calendar_id, exc)
        db.execute("UPDATE calendars SET error = ?, synced_at = ? WHERE id = ?", (str(exc)[:300], db.now(), calendar_id))
        events.publish("calendar", id=calendar_id, status="error", error=str(exc)[:200])
        return {"id": calendar_id, "name": cal["name"], "error": str(exc)[:300]}


def sync_all() -> None:
    for cal in db.query("SELECT id FROM calendars"):
        sync(cal["id"])


def list_calendars() -> list[dict]:
    return db.query("SELECT id, uri, name, added_at, synced_at, error, n_events FROM calendars ORDER BY added_at")


def remove_calendar(calendar_id: int) -> None:
    db.execute("DELETE FROM calendars WHERE id = ?", (calendar_id,))
    events.publish("calendar", id=calendar_id, status="removed")


def _refresher(on_sync: Callable[[], None] | None) -> None:
    while True:
        try:
            sync_all()
            if on_sync:
                on_sync()
        except Exception:
            log.exception("calendar refresh failed")
        time.sleep(REFRESH_SECONDS)


def start(on_sync: Callable[[], None] | None = None) -> None:
    """Sync every calendar now and then every 15 minutes; `on_sync` runs after each pass."""
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_refresher, args=(on_sync,), name="calendars", daemon=True).start()
