"""Time windows in questions: "last Tuesday's meeting", "yesterday", "kal", "pichle hafte", "3 days ago", "on 12 October".

English plus common Hinglish. Times are this PC's local time. The window narrows chat retrieval to the meetings
recorded in it (rag.py); when nothing matches there, chat falls back to searching everything.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

# No short forms that are also ordinary words ("sat", "sun", "mon", "wed"): "who sat in the meeting" is not Saturday.
WEEKDAYS = {
    "monday": 0, "somvar": 0, "somwar": 0, "tuesday": 1, "tues": 1, "mangalvar": 1, "mangalwar": 1,
    "wednesday": 2, "budhvar": 2, "budhwar": 2, "thursday": 3, "thurs": 3, "guruvar": 3,
    "guruwar": 3, "brihaspativar": 3, "friday": 4, "shukravar": 4, "shukrawar": 4, "saturday": 5,
    "shanivar": 5, "shaniwar": 5, "sunday": 6, "ravivar": 6, "raviwar": 6, "itvaar": 6, "itwar": 6,
}
MONTHS = {m: i for i, names in enumerate([
    ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may",), ("jun", "june"),
    ("jul", "july"), ("aug", "august"), ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
    ("dec", "december")], 1) for m in names}
_DAY_NAMES = "|".join(sorted(WEEKDAYS, key=len, reverse=True))
_MONTH_NAMES = "|".join(sorted(MONTHS, key=len, reverse=True))


def _now() -> datetime:
    return datetime.now()


def _day(d: date, label: str | None = None) -> tuple[float, float, str]:
    start = datetime.combine(d, time())
    return start.timestamp(), (start + timedelta(days=1)).timestamp(), label or d.strftime("%A %d %B")


def _span(d0: date, d1: date, label: str) -> tuple[float, float, str]:
    """[d0, d1] inclusive."""
    return datetime.combine(d0, time()).timestamp(), datetime.combine(d1 + timedelta(days=1), time()).timestamp(), label


def parse_window(text: str, now: datetime | None = None) -> tuple[float, float, str] | None:
    """The time window a question refers to, as (start, end, label), or None if it names no time."""
    now = now or _now()
    today = now.date()
    q = " " + re.sub(r"[^\w/-]+", " ", text.lower()) + " "

    if re.search(r" this morning | aaj subah ", q):
        start = datetime.combine(today, time())
        return start.timestamp(), (start + timedelta(hours=12)).timestamp(), "this morning"
    if re.search(r" (this afternoon|this evening|tonight|aaj shaam) ", q):
        start = datetime.combine(today, time(12))
        return start.timestamp(), datetime.combine(today + timedelta(days=1), time()).timestamp(), "this afternoon"
    if re.search(r" (day before yesterday|parso|parson) ", q):
        return _day(today - timedelta(days=2))
    if re.search(r" (yesterday|kal) ", q):
        return _day(today - timedelta(days=1), "yesterday")
    if re.search(r" (today|aaj|earlier today) ", q):
        return _day(today, "today")

    m = re.search(r" (\d{1,2}) (days?|din) (ago|pehle|pahle) ", q)
    if m:
        return _day(today - timedelta(days=int(m.group(1))))
    monday = today - timedelta(days=today.weekday())
    if re.search(r" (last week|pichle hafte|pichhle hafte|previous week) ", q):
        return _span(monday - timedelta(days=7), monday - timedelta(days=1), "last week")
    if re.search(r" (this week|is hafte) ", q):
        return _span(monday, today, "this week")
    if re.search(r" (last month|pichle mahine|previous month) ", q):
        first = today.replace(day=1)
        prev_last = first - timedelta(days=1)
        return _span(prev_last.replace(day=1), prev_last, "last month")
    if re.search(r" (this month|is mahine) ", q):
        return _span(today.replace(day=1), today, "this month")

    m = re.search(rf" (last|pichle|pichhle|previous)? ?({_DAY_NAMES})(s)? ", q)
    if m:
        wd = WEEKDAYS[m.group(2)]
        back = (today.weekday() - wd) % 7
        if m.group(1) and back == 0:
            back = 7  # "last Tuesday" on a Tuesday means a week ago
        return _day(today - timedelta(days=back))

    m = (re.search(r" (\d{4})-(\d{1,2})-(\d{1,2}) ", q))
    if m:
        try:
            return _day(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            return None
    m = re.search(rf" (\d{{1,2}})(st|nd|rd|th)? ({_MONTH_NAMES}) ", q) or re.search(rf" ({_MONTH_NAMES}) (\d{{1,2}})(st|nd|rd|th)? ", q)
    if m:
        groups = m.groups()
        day_s, month_s = (groups[0], groups[2]) if groups[0].isdigit() else (groups[1], groups[0])
        try:
            d = date(today.year, MONTHS[month_s], int(day_s))
        except ValueError:
            return None
        if d > today:  # "12 December" asked in October means last year
            d = d.replace(year=today.year - 1)
        return _day(d)
    m = re.search(r" (\d{1,2})/(\d{1,2}) ", q)  # day/month, as written in India
    if m:
        try:
            d = date(today.year, int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
        return _day(d if d <= today else d.replace(year=today.year - 1))
    return None
