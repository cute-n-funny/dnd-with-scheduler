"""Pure scheduling, vote tally, and calendar utilities (no Discord dependency)."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo
import random
import re

DAY_NAMES = {"mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1,
             "wed": 2, "weds": 2, "wednesday": 2, "thu": 3, "thur": 3,
             "thurs": 3, "thursday": 3, "fri": 4, "friday": 4,
             "sat": 5, "saturday": 5, "sun": 6, "sunday": 6}


def parse_days(value: str) -> list[int]:
    words = [x.strip().lower() for x in value.split(",")]
    if not 1 <= len(words) <= 4 or any(x not in DAY_NAMES for x in words):
        raise ValueError("Days must be 1–4 comma-separated weekdays, such as thur,fri,sat,sun.")
    result = [DAY_NAMES[x] for x in words]
    if len(result) != len(set(result)):
        raise ValueError("Days cannot contain duplicates.")
    return result


def parse_time(value: str) -> time:
    match = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*", value.lower())
    if not match:
        raise ValueError(f"Invalid time '{value}'. Try 2pm, 4:15pm, or 18:30.")
    hour, minute, suffix = int(match[1]), int(match[2] or 0), match[3]
    if minute > 59 or (suffix and not 1 <= hour <= 12) or (not suffix and hour > 23):
        raise ValueError(f"Invalid time '{value}'.")
    if suffix:
        hour = (hour % 12) + (12 if suffix == "pm" else 0)
    return time(hour, minute)


def parse_times(value: str) -> list[str]:
    pieces = [x.strip() for x in value.split(",")]
    if not 1 <= len(pieces) <= 12:
        raise ValueError("Times must contain 1–12 comma-separated values.")
    times = [parse_time(x).strftime("%H:%M") for x in pieces]
    if len(times) != len(set(times)):
        raise ValueError("Times cannot contain duplicates.")
    return times


def slot_keys(config: dict, now: datetime, poll_end: datetime) -> list[str]:
    """Dates in successive seven-day windows starting 7 days from poll creation.

    Keys are local wall-clock YYYY-MM-DDTHH:MM; timezone is stored on scheduler.
    """
    tz = ZoneInfo(config["timezone"])
    today = now.astimezone(tz).date()
    start = today + timedelta(days=7)
    days = set(config["days"])
    result = []
    for offset in range(config["weeks"] * 7):
        day = start + timedelta(days=offset)
        if day.weekday() not in days:
            continue
        for hhmm in config["times"]:
            dt = datetime.fromisoformat(f"{day.isoformat()}T{hhmm}").replace(tzinfo=tz)
            # Reject nonexistent wall-clock DST times and dates before poll close.
            if dt.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) != dt.replace(tzinfo=None):
                continue
            if dt.astimezone(timezone.utc) > poll_end:
                result.append(f"{day.isoformat()}T{hhmm}")
    return result


def choose_slot(votes: list[dict], valid_slots: list[str], dm_id: int, rng=None,
                attendee_limit: int = 8) -> tuple[str | None, list[int]]:
    """Earliest qualified slot wins if it has >= attendee_limit distinct voters.

    DM-voted days remain a constraint. With no qualified slot, use the original
    popular-day, then popular-time method (random tie-breaks). A person voting
    for multiple times on one date is counted only once for the day tally.
    Returns exact-slot voters in earliest-vote order for automatic RSVPs.
    """
    rng = rng or random
    valid = set(valid_slots)
    filtered = [v for v in votes if v["slot"] in valid]
    if not filtered:
        return None, []
    day_voters = defaultdict(set)
    slot_voters = defaultdict(set)
    for vote in filtered:
        day_voters[vote["slot"][:10]].add(vote["user_id"])
        slot_voters[vote["slot"]].add(vote["user_id"])
    dm_days = {vote["slot"][:10] for vote in filtered if vote["user_id"] == dm_id}
    eligible_days = dm_days if dm_days else set(day_voters)
    qualified = sorted(slot for slot, users in slot_voters.items()
                       if slot[:10] in eligible_days and len(users) >= attendee_limit)
    if qualified:
        # ISO wall-clock keys sort chronologically within this calendar/timezone.
        # The soonest qualifying session wins, even when a later slot has more votes.
        selected = qualified[0]
    else:
        # Not enough players at any one time: keep the existing popularity fallback.
        best_day_count = max(len(day_voters[day]) for day in eligible_days)
        chosen_day = rng.choice(sorted(day for day in eligible_days
                                      if len(day_voters[day]) == best_day_count))
        available_slots = [slot for slot in slot_voters if slot.startswith(chosen_day)]
        best_slot_count = max(len(slot_voters[slot]) for slot in available_slots)
        selected = rng.choice(sorted(slot for slot in available_slots
                                     if len(slot_voters[slot]) == best_slot_count))
    first_vote_by_user = {}
    for vote in filtered:
        if vote["slot"] == selected:
            user_id = vote["user_id"]
            first_vote_by_user[user_id] = min(first_vote_by_user.get(user_id, vote["created_at"]), vote["created_at"])
    ordered_users = sorted(first_vote_by_user, key=lambda user: (first_vote_by_user[user], user))
    return selected, ordered_users


def event_datetimes(slot: str, config: dict) -> tuple[datetime, datetime]:
    dt = datetime.fromisoformat(slot).replace(tzinfo=ZoneInfo(config["timezone"]))
    start = dt.astimezone(timezone.utc)
    return start, start + timedelta(hours=config["event_duration"])


def google_calendar_url(title: str, description: str, start: datetime, end: datetime) -> str:
    fmt = lambda dt: dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    params = {"action": "TEMPLATE", "text": title, "details": description,
              "dates": f"{fmt(start)}/{fmt(end)}"}
    base = "https://calendar.google.com/calendar/render?"
    url = base + urlencode(params)
    # Discord link buttons impose URL-length limits. Keep the Google link usable.
    while len(url) > 500 and params["details"]:
        params["details"] = params["details"][:-1]
        url = base + urlencode(params)
    while len(url) > 500 and len(params["text"]) > 1:
        params["text"] = params["text"][:-1]
        url = base + urlencode(params)
    return url


def make_ics(title: str, description: str, start: datetime, end: datetime, uid: str) -> bytes:
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("\r", "").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")
    def stamp(dt: datetime) -> str:
        return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    data = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//DND Scheduler//EN\r\n"
            "CALSCALE:GREGORIAN\r\nBEGIN:VEVENT\r\n"
            f"UID:{esc(uid)}\r\nDTSTAMP:{stamp(datetime.now(timezone.utc))}\r\n"
            f"DTSTART:{stamp(start)}\r\nDTEND:{stamp(end)}\r\n"
            f"SUMMARY:{esc(title)}\r\nDESCRIPTION:{esc(description)}\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n")
    # RFC 5545: fold physical lines after at most 75 octets (UTF-8 aware).
    folded = []
    for line in data.split("\r\n"):
        if not line:
            continue
        part = ""
        for char in line:
            if len((part + char).encode("utf-8")) > 75:
                folded.append(part)
                part = " " + char
            else:
                part += char
        folded.append(part)
    return ("\r\n".join(folded) + "\r\n").encode("utf-8")
