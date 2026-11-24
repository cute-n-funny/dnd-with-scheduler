"""Three-month poll-history retention and per-guild/mode isolation tests.

No Discord token or network access required.
"""
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from storage import Store, _poll_retention_cutoff


class PollRetentionTests(unittest.TestCase):
    CUTOFF = datetime(2026, 8, 24, 16, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.live = Store(str(Path(self.temp.name) / 'scheduler.db'))
        self.test = Store(str(Path(self.temp.name) / 'scheduler.db.test'))
        for store in (self.live, self.test):
            store.db.execute('''CREATE TABLE IF NOT EXISTS availability_windows (
                guild_id INTEGER, cycle INTEGER, user_id INTEGER, day TEXT,
                start TEXT, hours INTEGER, created_at TEXT)''')

    def tearDown(self):
        self.live.db.close()
        self.test.db.close()
        self.temp.cleanup()

    def seed(self, store, guild, cycle, deadline, user):
        store.db.execute('INSERT OR REPLACE INTO poll_cycles VALUES (?,?,?)',
                         (guild, cycle, deadline))
        # Intentionally old vote timestamps: carried votes must be judged by
        # the poll cycle's deadline, never by each vote's original timestamp.
        store.db.execute('INSERT INTO votes VALUES (?,?,?,?,?)',
                         (guild, cycle, user, '2026-11-27T14:00',
                          '2020-01-01T00:00:00+00:00'))
        store.db.execute('INSERT INTO rsvps VALUES (?,?,?,?,?)',
                         (guild, cycle, user, 'confirmed', '2020-01-01T00:00:00+00:00'))
        store.db.execute('INSERT INTO availability_windows VALUES (?,?,?,?,?,?,?)',
                         (guild, cycle, user, '2026-11-27', '14:00', 4,
                          '2020-01-01T00:00:00+00:00'))

    @staticmethod
    def remaining(store, table, guild):
        return {r[0] for r in store.db.execute(
            f'SELECT DISTINCT cycle FROM {table} WHERE guild_id=?', (guild,))}

    def test_new_poll_prunes_only_expired_cycles_in_its_server(self):
        self.seed(self.live, 101, 1, '2026-08-23T16:00:00+00:00', 10)
        self.seed(self.live, 101, 2, '2026-08-24T16:00:00+00:00', 11)  # exact cutoff retained
        self.seed(self.live, 101, 3, '2026-09-01T16:00:00+00:00', 12)
        self.seed(self.live, 101, 4, '2026-10-01T16:00:00+00:00', 13)
        self.seed(self.live, 202, 1, '2026-06-01T16:00:00+00:00', 20)
        async def run():
            with patch('storage._poll_retention_cutoff', return_value=self.CUTOFF):
                await self.live.put(101, {'title': 'New live poll'}, 4, 'poll_preparing',
                                    poll_deadline='2026-11-27T16:00:00+00:00')
        asyncio.run(run())
        for table in ('votes', 'rsvps', 'availability_windows', 'poll_cycles'):
            self.assertEqual(self.remaining(self.live, table, 101), {2, 3, 4}, table)
            self.assertEqual(self.remaining(self.live, table, 202), {1}, table)
        self.assertEqual(self.live.db.execute(
            'SELECT poll_deadline FROM poll_cycles WHERE guild_id=101 AND cycle=4'
        ).fetchone()[0], '2026-11-27T16:00:00+00:00')

    def test_test_database_is_never_pruned_by_live_poll(self):
        self.seed(self.test, 101, 1, '2026-02-01T00:00:00+00:00', 30)
        self.seed(self.live, 101, 1, '2026-02-01T00:00:00+00:00', 31)
        async def run():
            with patch('storage._poll_retention_cutoff', return_value=self.CUTOFF):
                await self.live.put(101, {'title': 'Live'}, 2, 'poll_preparing',
                                    poll_deadline='2026-11-27T16:00:00+00:00')
        asyncio.run(run())
        self.assertEqual(self.remaining(self.live, 'votes', 101), set())
        self.assertEqual(self.remaining(self.test, 'votes', 101), {1})

    def test_regular_put_and_update_do_not_prune(self):
        self.seed(self.live, 101, 1, '2026-01-01T00:00:00+00:00', 7)
        async def run():
            with patch('storage._poll_retention_cutoff', return_value=self.CUTOFF):
                await self.live.put(101, {'title': 'Loaded'}, 2, 'poll',
                                    poll_deadline='2026-11-25T00:00:00+00:00')
                await self.live.update(101, state='event')
        asyncio.run(run())
        self.assertEqual(self.remaining(self.live, 'votes', 101), {1})

    def test_reset_clears_cycle_metadata_as_well_as_results(self):
        self.seed(self.live, 101, 1, '2026-01-01T00:00:00+00:00', 7)
        self.seed(self.live, 202, 1, '2026-01-01T00:00:00+00:00', 8)
        asyncio.run(self.live.delete(101))
        for table in ('votes', 'rsvps', 'poll_cycles', 'availability_windows'):
            self.assertEqual(self.remaining(self.live, table, 101), set(), table)
            self.assertEqual(self.remaining(self.live, table, 202), {1}, table)

    def test_legacy_cycles_get_conservative_initial_retention_date(self):
        db_path = str(Path(self.temp.name) / 'legacy.db')
        old = Store(db_path)
        old.db.execute('DROP TABLE poll_cycles')  # model a pre-upgrade volume
        old.db.execute('INSERT INTO votes VALUES (?,?,?,?,?)',
                       (101, 4, 30, '2026-01-01T14:00', '2025-01-01T00:00:00+00:00'))
        old.db.close()
        before = datetime.now(timezone.utc)
        upgraded = Store(db_path)
        try:
            row = upgraded.db.execute(
                'SELECT poll_deadline FROM poll_cycles WHERE guild_id=101 AND cycle=4'
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertGreaterEqual(datetime.fromisoformat(row[0]), before)
            self.assertLessEqual(datetime.fromisoformat(row[0]), datetime.now(timezone.utc))
        finally:
            upgraded.db.close()

    def test_three_calendar_months_handles_short_months(self):
        self.assertEqual(_poll_retention_cutoff(
            datetime(2026, 5, 31, 12, tzinfo=timezone.utc)),
            datetime(2026, 2, 28, 12, tzinfo=timezone.utc))
        self.assertEqual(_poll_retention_cutoff(
            datetime(2026, 11, 24, 12, tzinfo=timezone.utc)),
            datetime(2026, 8, 24, 12, tzinfo=timezone.utc))


if __name__ == '__main__':
    unittest.main()
