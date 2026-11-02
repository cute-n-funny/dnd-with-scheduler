"""Test/production RSVP regression tests using production functions and SQLite.

No Discord network, bot token, or third-party test framework required.
"""
import ast
import asyncio
import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from availability import ensure_schema, read_windows, save_windows, test_window_votes
from logic import choose_slot
from storage import Store

NOW = datetime(2026, 12, 1, 12, tzinfo=timezone.utc)
A = '2026-12-12T14:00'
B = '2026-12-12T14:30'
C = '2026-12-12T16:00'


def method_from_source(name, **provided):
    source = ast.parse((Path(__file__).resolve().parents[1] / 'bot.py').read_text(encoding='utf-8'))
    cls = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == 'SchedulerBot')
    node = copy.deepcopy(next(node for node in cls.body if isinstance(node, ast.AsyncFunctionDef)
                              and node.name == name))
    node.decorator_list = []
    namespace = dict(provided)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
                 'bot.py', 'exec'), namespace)
    return namespace[name]


def announcement_from_source():
    source = ast.parse((Path(__file__).resolve().parents[1] / 'bot.py').read_text(encoding='utf-8'))
    node = copy.deepcopy(next(node for node in source.body if isinstance(node, ast.FunctionDef)
                              and node.name == 'session_announcement'))
    ns = {'is_test': lambda c: bool(c.get('test_mode')),
          'players_mention': lambda guild: '<@&900>'}
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'bot.py', 'exec'), ns)
    return ns['session_announcement']


class TestAndNormalRSVPTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.store = Store(str(Path(self.folder.name) / 'scheduler.db'))
        ensure_schema(self.store)
        self.guild = SimpleNamespace(id=12)

    def tearDown(self):
        self.store.db.close()  # Essential for Windows temporary-directory cleanup.
        self.folder.cleanup()

    def test_short_window_qualifies_for_one_minute_test_not_normal_four_hours(self):
        async def check():
            # Default duration selection is 3 hours, shorter than campaign 4h.
            self.assertEqual(await save_windows(self.store, 12, 1, 40,
                [('2026-12-12','14:00',3)], [A,B,C], 4, 8), 0)
            self.assertEqual(await self.store.votes(12,1), [])
            # The one-minute test can still get eligible RSVP ballots.
            count = await save_windows(self.store, 12, 1, 40,
                [('2026-12-12','14:00',3)], [A,B,C], 1/60, 8)
            self.assertEqual(count,3)
            self.assertEqual({v['slot'] for v in await self.store.votes(12,1)}, {A,B,C})
        asyncio.run(check())

    def test_existing_test_windows_are_recovered_without_duplicate_votes(self):
        async def check():
            await save_windows(self.store,12,1,42,[('2026-12-12','14:00',3)], [A,B,C],4,8)
            windows = await read_windows(self.store,12,1)
            ballots = test_window_votes(await self.store.votes(12,1),windows,[A,B,C])
            self.assertEqual(len(ballots),3)
            self.assertEqual({v['user_id'] for v in ballots},{42})
            self.assertEqual(len(test_window_votes(ballots,windows,[A,B,C])),3)
        asyncio.run(check())

    def test_dm_first_first_vote_order_rsvp_limit_and_message(self):
        async def check():
            # Users 10 and 20 vote earlier, the DM (42) votes last.
            await save_windows(self.store,12,1,10,[('2026-12-12','14:00',3)],[A,B,C],4,8)
            await save_windows(self.store,12,1,20,[('2026-12-12','14:00',3)],[A,B,C],4,8)
            await save_windows(self.store,12,1,42,[('2026-12-12','14:00',3)],[A,B,C],4,8)
            await save_windows(self.store,12,1,30,[('2026-12-12','16:00',1)],[A,B,C],4,8)
            ballots = test_window_votes(await self.store.votes(12,1),
                                        await read_windows(self.store,12,1),[A,B,C])
            selected, ordered = choose_slot(ballots,[A,B,C],dm_id=42,min_votes=1,attendee_limit=2)
            self.assertEqual(selected,A)
            self.assertEqual(ordered,[42,10,20])
            await self.store.set_initial_rsvps(12,1,ordered,2)
            # Force a timestamp tie; the DB must preserve DM-first insert order.
            self.store.db.execute("UPDATE rsvps SET created_at='2026-12-01T12:00:00+00:00'")
            rsvps = await self.store.rsvps(12,1)
            self.assertEqual([(v['user_id'],v['status']) for v in rsvps],
                             [(42,'confirmed'),(10,'confirmed'),(20,'waitlist')])
            text = announcement_from_source()({'test_mode':True},self.guild,rsvps)
            self.assertIn('The following users have been added after voting:',text)
            self.assertLess(text.index('<@42>'),text.index('<@10>'))
            self.assertNotIn('<@20>',text)  # Waitlisted, not auto-confirmed.
            self.assertNotIn('<@&',text)   # No @Player role ping in test.
        asyncio.run(check())

    def test_reset_removes_orphaned_windows_before_next_test(self):
        async def check():
            await save_windows(self.store,12,1,87,[('2026-12-12','14:00',3)],[A,B,C],4,8)
            self.assertEqual(len(await read_windows(self.store,12,1)),1)
            await self.store.delete(12)
            self.assertEqual(await read_windows(self.store,12,1),[])
            self.assertEqual(await self.store.votes(12,1),[])
        asyncio.run(check())

    def test_test_close_poll_rebuilds_windows_and_auto_rsvps(self):
        close = method_from_source('close_poll',
            utcnow=lambda:NOW,timedelta=timedelta,
            is_test=lambda c: bool(c.get('test_mode')),
            choose_slot=choose_slot,read_windows=read_windows,
            test_window_votes=test_window_votes,
            event_datetimes=lambda s,c:(NOW+timedelta(days=11),NOW+timedelta(days=11,hours=4)))
        async def check():
            await save_windows(self.store,12,1,10,[('2026-12-12','14:00',3)],[A,B,C],4,8)
            await save_windows(self.store,12,1,42,[('2026-12-12','14:00',3)],[A,B,C],4,8)
            cfg={'test_mode':True,'dm_id':42,'attendee_limit':1,'min_votes':12,
                 'event_duration':4,'poll_duration':2}
            await self.store.put(12,cfg,1,'poll',poll_message_id=None)
            class Bot:
                def __init__(self,store):self.store=store;self.created=[]
                def slots_for(self,r):return [A,B,C]
                async def create_event(self,guild,r):self.created.append(r)
            bot=Bot(self.store)
            await close(bot,self.guild,await self.store.get(12))
            self.assertEqual(len(bot.created),1)
            r=bot.created[0]
            self.assertEqual(r['selected_slot'],A)
            self.assertEqual(datetime.fromisoformat(r['event_start']),NOW+timedelta(minutes=1))
            self.assertEqual(datetime.fromisoformat(r['event_end'])-datetime.fromisoformat(r['event_start']),
                             timedelta(minutes=1))
            people=await self.store.rsvps(12,1)
            self.assertEqual([(r['user_id'],r['status']) for r in people],
                             [(42,'confirmed'),(10,'waitlist')])
            self.assertIn('<@42>',announcement_from_source()({'test_mode':True},self.guild,people))
        asyncio.run(check())

    def test_normal_sessions_still_require_full_event_duration(self):
        close = method_from_source('close_poll',
            utcnow=lambda:NOW,timedelta=timedelta,
            is_test=lambda c: bool(c.get('test_mode')),
            choose_slot=choose_slot,
            event_datetimes=lambda s,c:(NOW+timedelta(days=11),NOW+timedelta(days=11,hours=4)),
            log=SimpleNamespace(info=lambda *args:None))
        async def check():
            await save_windows(self.store,12,1,10,[('2026-12-12','14:00',3)],[A,B,C],4,8)
            cfg={'test_mode':False,'dm_id':42,'attendee_limit':8,'min_votes':3,
                 'event_duration':4,'poll_duration':2}
            await self.store.put(12,cfg,1,'poll',poll_message_id=None)
            class Bot:
                def __init__(self,store):self.store=store
                def slots_for(self,r):return [A,B,C]
                async def create_event(self,*args):raise AssertionError('Unexpected event')
            await close(Bot(self.store),self.guild,await self.store.get(12))
            self.assertEqual((await self.store.get(12))['state'],'waiting_retry')
            self.assertEqual(await self.store.rsvps(12,1),[])
        asyncio.run(check())

    def test_view_code_uses_test_duration_only_for_test_poll(self):
        source=(Path(__file__).resolve().parents[1]/'availability_ui.py').read_text(encoding='utf-8')
        tree=ast.parse(source)
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='AvailabilityVotePanel')
        method=next(n for n in cls.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='_change_windows')
        src=ast.unparse(method)
        self.assertIn('required_hours',src)
        self.assertIn('test_mode',src)
        self.assertIn('1 / 60',src)


if __name__=='__main__':unittest.main()
