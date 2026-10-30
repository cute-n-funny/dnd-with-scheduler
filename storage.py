"""SQLite backing store; one Fly Machine and a persistent /data volume are required."""
import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.lock = asyncio.Lock()
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS schedulers (
            guild_id INTEGER PRIMARY KEY, cycle INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL, config TEXT NOT NULL,
            poll_deadline TEXT, poll_channel_id INTEGER, poll_message_id INTEGER,
            event_channel_id INTEGER, event_message_id INTEGER, forum_thread_id INTEGER,
            discord_event_id INTEGER, selected_slot TEXT, event_start TEXT, event_end TEXT,
            next_poll_at TEXT, reminded INTEGER NOT NULL DEFAULT 0,
            native_started INTEGER NOT NULL DEFAULT 0, native_ended INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS votes (
            guild_id INTEGER NOT NULL, cycle INTEGER NOT NULL, user_id INTEGER NOT NULL,
            slot TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, cycle, user_id, slot)
        );
        CREATE TABLE IF NOT EXISTS rsvps (
            guild_id INTEGER NOT NULL, cycle INTEGER NOT NULL, user_id INTEGER NOT NULL,
            status TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, cycle, user_id)
        );
        """)
        # Safe upgrade for an existing SQLite volume from an earlier release.
        current = {r[1] for r in self.db.execute("PRAGMA table_info(schedulers)")}
        for col in ("native_started", "native_ended"):
            if col not in current:
                self.db.execute(f"ALTER TABLE schedulers ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")

    async def all(self):
        async with self.lock:
            return [self._convert(r) for r in self.db.execute("SELECT * FROM schedulers")]

    def _convert(self, row):
        if row is None: return None
        result = dict(row)
        result["config"] = json.loads(result["config"])
        return result

    async def get(self, guild_id):
        async with self.lock:
            return self._convert(self.db.execute("SELECT * FROM schedulers WHERE guild_id=?", (guild_id,)).fetchone())

    async def put(self, guild_id, config, cycle, state, **fields):
        values = {"guild_id": guild_id, "config": json.dumps(config), "cycle": cycle, "state": state, **fields}
        async with self.lock:
            cols = list(values)
            sql = f"INSERT OR REPLACE INTO schedulers ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})"
            self.db.execute(sql, tuple(values.values()))

    async def update(self, guild_id, **fields):
        if not fields: return
        async with self.lock:
            values = dict(fields)
            if "config" in values: values["config"] = json.dumps(values["config"])
            sql = f"UPDATE schedulers SET {', '.join(k+'=?' for k in values)} WHERE guild_id=?"
            self.db.execute(sql, (*values.values(), guild_id))

    async def delete(self, guild_id):
        async with self.lock:
            for table in ("votes", "rsvps", "schedulers"):
                self.db.execute(f"DELETE FROM {table} WHERE guild_id=?", (guild_id,))

    async def votes(self, guild_id, cycle):
        async with self.lock:
            return [dict(r) for r in self.db.execute(
                "SELECT user_id,slot,created_at FROM votes WHERE guild_id=? AND cycle=? ORDER BY created_at,user_id", (guild_id, cycle))]

    async def toggle_vote(self, guild_id, cycle, user_id, slot):
        async with self.lock:
            old = self.db.execute("SELECT 1 FROM votes WHERE guild_id=? AND cycle=? AND user_id=? AND slot=?",
                                  (guild_id, cycle, user_id, slot)).fetchone()
            if old:
                self.db.execute("DELETE FROM votes WHERE guild_id=? AND cycle=? AND user_id=? AND slot=?",
                                (guild_id, cycle, user_id, slot))
                return False
            self.db.execute("INSERT INTO votes VALUES (?,?,?,?,?)",
                            (guild_id, cycle, user_id, slot, datetime.now(timezone.utc).isoformat()))
            return True

    async def set_initial_rsvps(self, guild_id, cycle, users, limit):
        async with self.lock:
            self.db.execute("DELETE FROM rsvps WHERE guild_id=? AND cycle=?", (guild_id, cycle))
            for n, user in enumerate(users):
                self.db.execute("INSERT INTO rsvps VALUES (?,?,?,?,?)",
                                (guild_id, cycle, user, "confirmed" if n < limit else "waitlist",
                                 datetime.now(timezone.utc).isoformat()))

    async def rsvps(self, guild_id, cycle):
        async with self.lock:
            return [dict(r) for r in self.db.execute("SELECT user_id,status,created_at FROM rsvps "
                "WHERE guild_id=? AND cycle=? ORDER BY created_at,user_id", (guild_id, cycle))]

    async def toggle_rsvp(self, guild_id, cycle, user_id, limit):
        """Serializes counts and RSVP updates. Returns own status and promoted user if any."""
        async with self.lock:
            row = self.db.execute("SELECT status FROM rsvps WHERE guild_id=? AND cycle=? AND user_id=?",
                                  (guild_id, cycle, user_id)).fetchone()
            promoted = None
            if row:
                self.db.execute("DELETE FROM rsvps WHERE guild_id=? AND cycle=? AND user_id=?", (guild_id, cycle, user_id))
                new_status = "removed"
            else:
                count = self.db.execute("SELECT COUNT(*) FROM rsvps WHERE guild_id=? AND cycle=? AND status='confirmed'",
                                        (guild_id, cycle)).fetchone()[0]
                new_status = "confirmed" if count < limit else "waitlist"
                self.db.execute("INSERT INTO rsvps VALUES (?,?,?,?,?)",
                                (guild_id, cycle, user_id, new_status, datetime.now(timezone.utc).isoformat()))
            count = self.db.execute("SELECT COUNT(*) FROM rsvps WHERE guild_id=? AND cycle=? AND status='confirmed'",
                                    (guild_id, cycle)).fetchone()[0]
            if count < limit:
                first = self.db.execute("SELECT user_id FROM rsvps WHERE guild_id=? AND cycle=? AND status='waitlist' "
                                        "ORDER BY created_at,user_id LIMIT 1", (guild_id, cycle)).fetchone()
                if first:
                    promoted = first[0]
                    self.db.execute("UPDATE rsvps SET status='confirmed' WHERE guild_id=? AND cycle=? AND user_id=?",
                                    (guild_id, cycle, promoted))
            return new_status, promoted
