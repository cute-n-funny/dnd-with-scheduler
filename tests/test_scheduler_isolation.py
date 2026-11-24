"""Isolation regression tests: no Discord API or token required."""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scheduler_scope import in_test_scope, scheduler_scope, scoped_callback
from storage import Store
from availability import ensure_schema, read_windows, save_windows


class TestSchedulerIsolation(unittest.TestCase):
    def test_poll_data_isolated_by_server_and_mode(self):
        with TemporaryDirectory() as directory:
            live = Store(str(Path(directory) / 'scheduler.db'))
            test = Store(str(Path(directory) / 'scheduler.db.test'))
            ensure_schema(live)
            ensure_schema(test)
            try:
                async def run():
                    for store, guild, title in ((live, 101, 'Alpha campaign'),
                                                (live, 202, 'Beta campaign'),
                                                (test, 101, 'Alpha test'),
                                                (test, 202, 'Beta test')):
                        await store.put(guild, {'title': title}, 1, 'poll')
                    # Identical guild/cycle/user/slot keys must never collide.
                    slot = '2026-12-20T14:00'
                    for store, guild, user in ((live, 101, 11), (live, 202, 22),
                                                (test, 101, 33), (test, 202, 44)):
                        await store.replace_user_votes(guild, 1, user, [slot])
                        await save_windows(store, guild, 1, user,
                            [('2026-12-20','14:00',4)], [slot], 1, 8)
                        await store.set_initial_rsvps(guild, 1, [user], 8)
                    for store, guild, user in ((live, 101, 11), (live, 202, 22),
                                                (test, 101, 33), (test, 202, 44)):
                        self.assertEqual({v['user_id'] for v in await store.votes(guild, 1)}, {user})
                        self.assertEqual({v['user_id'] for v in await read_windows(store, guild, 1)}, {user})
                        self.assertEqual({r['user_id'] for r in await store.rsvps(guild, 1)}, {user})
                    # Replacing a test poll must not affect the live campaign.
                    await test.delete(101)
                    await test.put(101, {'title': 'Second test'}, 1, 'poll')
                    self.assertEqual((await live.get(101))['config']['title'], 'Alpha campaign')
                    self.assertEqual({v['user_id'] for v in await live.votes(101, 1)}, {11})
                    self.assertEqual(len(await live.rsvps(101, 1)), 1)
                    self.assertEqual(len(await read_windows(live, 101, 1)), 1)
                    self.assertEqual(len(await test.votes(101, 1)), 0)
                    self.assertEqual({v['user_id'] for v in await live.votes(202, 1)}, {22})
                    self.assertEqual({v['user_id'] for v in await test.votes(202, 1)}, {44})
                asyncio.run(run())
            finally:
                live.db.close()
                test.db.close()

    def test_task_local_mode_never_leaks_between_concurrent_callbacks(self):
        class View:
            def __init__(self, test_mode): self.test_mode = test_mode
            @scoped_callback
            async def execute(self):
                before = in_test_scope()
                await asyncio.sleep(0)
                return before, in_test_scope()
        async def check():
            self.assertFalse(in_test_scope())
            result = await asyncio.gather(View(True).execute(), View(False).execute())
            self.assertEqual(result, [(True, True), (False, False)])
            self.assertFalse(in_test_scope())
            with scheduler_scope(True):
                self.assertTrue(in_test_scope())
                self.assertEqual(await View(False).execute(), (False, False))
                self.assertTrue(in_test_scope())
            self.assertFalse(in_test_scope())
        asyncio.run(check())

    def test_persistent_buttons_have_distinct_custom_id_namespaces(self):
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / 'bot.py').read_text(encoding='utf-8')
        self.assertIn("scope = 'test:' if test_mode else ''", source)
        self.assertIn('dnd:{scope}poll:', source)
        self.assertIn('dnd:{scope}rsvp:', source)
        self.assertIn('dnd:{scope}ical:', source)
        self.assertIn('for test_mode in (False, True):', source)
        self.assertIn('with scheduler_scope(test_mode):', source)
        self.assertIn('self.test_store = Store(DB_PATH +', source)
        self.assertIn('scheduler_scope.py', (Path(__file__).resolve().parents[1] / 'Dockerfile').read_text())
        self.assertIn('await self.store.delete(guild.id)', source)


if __name__ == '__main__':
    unittest.main()
