"""Availability windows in the existing SQLite DB; event-start votes stay separate.

Rows describe a player's *continuous* availability.  Only start slots whose
entire event duration fits a window become session votes in the existing votes
ledger, keeping attendee-limit and RSVP logic honest.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def ensure_schema(store):
    store.db.execute("""
        CREATE TABLE IF NOT EXISTS availability_windows (
            guild_id INTEGER NOT NULL, cycle INTEGER NOT NULL,
            user_id INTEGER NOT NULL, day TEXT NOT NULL,
            start TEXT NOT NULL, hours INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (guild_id,cycle,user_id,day,start)
        )
    """)


def _qualifying_slots(windows, slots, duration):
    """Only starts fully contained in a player's selected availability."""
    qualified = set()
    for day, start, hours in windows:
        low = datetime.fromisoformat(f"{day}T{start}")
        high = low + timedelta(hours=hours)
        for slot in slots:
            if not slot.startswith(day):
                continue
            candidate = datetime.fromisoformat(slot)
            if low <= candidate and candidate + timedelta(hours=duration) <= high:
                qualified.add(slot)
    return qualified


def test_window_votes(votes, windows, valid_slots):
    """Include all test-time availability, even windows shorter than a normal session.

    /test_scheduler creates a one-minute event. Its future weekly time slots are
    only *ballot choices*, not the real test event's start time. Reconstruct votes
    from saved windows so votes submitted before an upgrade still qualify.
    One user counts only once per exact slot, with the earliest saved timestamp.
    """
    valid = set(valid_slots)
    by_key = {}
    for vote in votes:
        key = (vote["user_id"], vote["slot"])
        if vote["slot"] in valid and (key not in by_key or
                                      vote["created_at"] < by_key[key]["created_at"]):
            by_key[key] = dict(vote)
    for window in windows:
        slots = _qualifying_slots([(window["day"], window["start"], window["hours"])],
                                  valid, 1 / 60)
        for slot in slots:
            key = (window["user_id"], slot)
            created = window["created_at"]
            if key not in by_key or created < by_key[key]["created_at"]:
                by_key[key] = {"user_id": window["user_id"], "slot": slot,
                               "created_at": created}
    return sorted(by_key.values(), key=lambda v: (v["created_at"], v["user_id"], v["slot"]))


async def read_windows(store, guild_id, cycle, user_id=None):
    async with store.lock:
        if user_id is None:
            sql = ("SELECT user_id,day,start,hours,created_at FROM availability_windows "
                   "WHERE guild_id=? AND cycle=? ORDER BY created_at")
            params = (guild_id, cycle)
        else:
            sql = ("SELECT user_id,day,start,hours,created_at FROM availability_windows "
                   "WHERE guild_id=? AND cycle=? AND user_id=? ORDER BY created_at")
            params = (guild_id, cycle, user_id)
        return [dict(row) for row in store.db.execute(sql, params)]


async def save_windows(store, guild_id, cycle, user_id, windows, valid_slots, event_duration, max_hours):
    """Atomically replace a user's windows and their eligible session votes."""
    windows = {(day, start, int(hours)) for day, start, hours in windows}
    allowed_pairs = {(slot[:10], slot[11:]) for slot in valid_slots}
    for day, start, hours in windows:
        if (day, start) not in allowed_pairs or not 1 <= hours <= max_hours:
            raise ValueError("An availability window is outside the current poll limits")
    qualified = sorted(_qualifying_slots(windows, valid_slots, event_duration))
    now = datetime.now(timezone.utc).isoformat()
    async with store.lock:
        store.db.execute("BEGIN IMMEDIATE")
        try:
            old = {(row['day'], row['start']):row['created_at'] for row in store.db.execute(
                "SELECT day,start,created_at FROM availability_windows "
                "WHERE guild_id=? AND cycle=? AND user_id=?", (guild_id,cycle,user_id))}
            old_votes = {row['slot']:row['created_at'] for row in store.db.execute(
                "SELECT slot,created_at FROM votes WHERE guild_id=? AND cycle=? AND user_id=?",
                (guild_id,cycle,user_id))}
            store.db.execute("DELETE FROM availability_windows WHERE guild_id=? AND cycle=? AND user_id=?",
                             (guild_id,cycle,user_id))
            store.db.executemany(
                "INSERT INTO availability_windows (guild_id,cycle,user_id,day,start,hours,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                [(guild_id,cycle,user_id,day,start,hours,old.get((day,start),now))
                 for day,start,hours in sorted(windows)])
            store.db.execute("DELETE FROM votes WHERE guild_id=? AND cycle=? AND user_id=?",
                             (guild_id,cycle,user_id))
            store.db.executemany(
                "INSERT INTO votes (guild_id,cycle,user_id,slot,created_at) VALUES (?,?,?,?,?)",
                [(guild_id,cycle,user_id,slot,old_votes.get(slot,now)) for slot in qualified])
            store.db.execute("COMMIT")
        except Exception:
            store.db.execute("ROLLBACK")
            raise
    return len(qualified)


async def carry_windows(store, guild_id, old_cycle, new_cycle, valid_slots):
    """Carry exact future windows into a recurring poll, safely on retries."""
    if old_cycle < 1 or new_cycle <= old_cycle:
        return 0
    valid_days = sorted({slot[:10] for slot in valid_slots})
    valid_times = sorted({slot[11:] for slot in valid_slots})
    if not valid_days or not valid_times:
        return 0
    day_placeholders = ','.join('?' for _ in valid_days)
    time_placeholders = ','.join('?' for _ in valid_times)
    async with store.lock:
        before = store.db.total_changes
        store.db.execute(
            "INSERT OR IGNORE INTO availability_windows "
            "(guild_id,cycle,user_id,day,start,hours,created_at) "
            "SELECT guild_id,?,user_id,day,start,hours,created_at "
            "FROM availability_windows WHERE guild_id=? AND cycle=? "
            f"AND day IN ({day_placeholders}) AND start IN ({time_placeholders})",
            (new_cycle,guild_id,old_cycle,*valid_days,*valid_times))
        return store.db.total_changes - before


def windows_for_heatmap(all_windows, votes):
    """Keep legacy poll votes visible where a user has no new-style window."""
    users_with_windows = {row['user_id'] for row in all_windows}
    return all_windows, [v for v in votes if v['user_id'] not in users_with_windows]
