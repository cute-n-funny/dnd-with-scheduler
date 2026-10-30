"""Behavioral tests for the standalone one-minute smoke-test workflow.

The workflow's GitHub CI runs without discord.py, so load only the production
methods/functions under test from bot.py's syntax tree.
"""
import ast
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest

from logic import choose_slot

NOW = datetime(2026, 10, 30, 21, 0, tzinfo=timezone.utc)
SLOT = '2026-11-07T14:00'
SOURCE = (Path(__file__).resolve().parents[1] / 'bot.py').read_text()
TREE = ast.parse(SOURCE)


def funcs(*names, methods=False, **values):
    if methods:
        node = next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == 'SchedulerBot')
        nodes = node.body
    else:
        nodes = TREE.body
    selected = [n for n in nodes if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name in names]
    assert len(selected) == len(names), (names, [n.name for n in selected])
    # Source decorators are intentionally omitted; test the coroutine directly.
    import copy
    selected = [copy.deepcopy(n) for n in selected]
    for node in selected:
        node.decorator_list = []
    mod = ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))
    ns = dict(values)
    exec(compile(mod, 'bot.py', 'exec'), ns)
    return [ns[n] for n in names]


class Store:
    def __init__(self, record=None):
        self.record = record
        self.writes = []
        self.rsvps = None
    async def get(self, guild_id):
        return self.record
    async def all(self):
        return [self.record]
    async def votes(self, guild_id, cycle):
        return []
    async def put(self, guild_id, cfg, cycle, state, **kwargs):
        self.writes.append((state, kwargs))
    async def set_initial_rsvps(self, guild_id, cycle, users, limit):
        self.rsvps = list(users)
    async def update(self, guild_id, **kwargs):
        self.writes.append(("update", kwargs))
        if self.record:
            self.record.update(kwargs)


class OnceTests(unittest.TestCase):
    def test_poll_start_and_no_mass_pings(self):
        is_test, poll_start, mention, event_title = funcs(
            'is_test', 'poll_start', 'mention', 'event_title',
            dtread=datetime.fromisoformat, timedelta=timedelta)
        deadline = NOW + timedelta(minutes=1)
        test_cfg = {'test_mode': True, 'poll_duration': 3, 'title': 'Test Poll', 'ping_role_id': None}
        regular_cfg = {'poll_duration': 3, 'title': 'Campaign', 'ping_role_id': 123}
        self.assertEqual(poll_start({'config': test_cfg, 'poll_deadline': deadline.isoformat()}), NOW)
        self.assertEqual(poll_start({'config': regular_cfg, 'poll_deadline': deadline.isoformat()}), deadline - timedelta(days=3))
        self.assertEqual(mention(test_cfg, SimpleNamespace(id=987)), '')
        self.assertEqual(mention(regular_cfg, SimpleNamespace(id=987)), '<@&123>')
        self.assertEqual(event_title(test_cfg), 'Test Event')
        self.assertEqual(event_title(regular_cfg), 'Campaign')

    def test_prepare_poll_is_one_minute_for_test_and_days_for_regular(self):
        (prepare,) = funcs('prepare_poll', methods=True, utcnow=lambda: NOW,
                          timedelta=timedelta, is_test=lambda c: c.get('test_mode', False))
        async def check():
            store = Store()
            bot = SimpleNamespace(store=store)
            guild = SimpleNamespace(id=55)
            cfg = {'test_mode': True, 'poll_duration': 3, 'channel_id': 9}
            await prepare(bot, guild, cfg, 1)
            self.assertEqual(datetime.fromisoformat(store.writes[-1][1]['poll_deadline']), NOW + timedelta(minutes=1))
            cfg = {'poll_duration': 3, 'channel_id': 9}
            await prepare(bot, guild, cfg, 2)
            self.assertEqual(datetime.fromisoformat(store.writes[-1][1]['poll_deadline']), NOW + timedelta(days=3))
        asyncio.run(check())

    def test_no_vote_still_creates_one_minute_event(self):
        (close_poll,) = funcs('close_poll', methods=True, utcnow=lambda: NOW,
                             timedelta=timedelta, choose_slot=choose_slot,
                             event_datetimes=lambda slot, cfg: (NOW + timedelta(days=8), NOW + timedelta(days=8, hours=4)),
                             is_test=lambda c: c.get('test_mode', False),
                             dtread=datetime.fromisoformat)
        async def check():
            cfg = {'test_mode': True, 'dm_id': 11, 'attendee_limit': 12}
            record = {'config': cfg, 'cycle': 1, 'poll_message_id': None, 'guild_id': 55}
            store = Store(record)
            created = []
            class Bot:
                def __init__(self):
                    self.store = store
                def slots_for(self, record):
                    return [SLOT]
                async def create_event(self, guild, record):
                    created.append(record.copy())
            await close_poll(Bot(), SimpleNamespace(id=55), record)
            self.assertEqual(len(created), 1)
            self.assertIsNone(created[0]['selected_slot'])
            start = datetime.fromisoformat(created[0]['event_start'])
            end = datetime.fromisoformat(created[0]['event_end'])
            self.assertEqual(start, NOW + timedelta(minutes=1))
            self.assertEqual(end - start, timedelta(minutes=1))
            self.assertEqual(store.rsvps, [])
        asyncio.run(check())

    def test_regular_poll_without_votes_still_repolls(self):
        (close_poll,) = funcs('close_poll', methods=True, utcnow=lambda: NOW,
                             timedelta=timedelta, choose_slot=choose_slot,
                             event_datetimes=lambda slot, cfg: (NOW + timedelta(days=8), NOW + timedelta(days=8, hours=4)),
                             is_test=lambda c: c.get('test_mode', False),
                             dtread=datetime.fromisoformat, log=SimpleNamespace(info=lambda *args: None))
        async def check():
            cfg = {'dm_id': 11, 'attendee_limit': 12}
            record = {'config': cfg, 'cycle': 1, 'poll_message_id': None, 'guild_id': 55}
            store = Store(record)
            made = []
            class Bot:
                def __init__(self): self.store = store
                def slots_for(self, record): return [SLOT]
                async def prepare_poll(self, guild, config, cycle): made.append(('new_poll', cycle))
                async def publish_poll(self, guild, record): made.append(('publish', record['cycle']))
            await close_poll(Bot(), SimpleNamespace(id=55), record)
            self.assertEqual(made, [('new_poll', 2), ('publish', 1)])
        asyncio.run(check())

    def test_test_command_does_not_ping_or_loop_and_saves_normal_config(self):
        # The command is a top-level coroutine: our loader removes its Discord decorators.
        async def gate(interaction): return True
        (once,) = funcs('once_scheduler', admin_gate=gate,
                        config_defaults=lambda guild, user, ch: {'title': 'Original', 'days': [4], 'times': ['16:00'], 'poll_duration': 3},
                        is_test=lambda c: c.get('test_mode', False),
                        ValueError=ValueError, discord=SimpleNamespace(HTTPException=Exception, Interaction=object))
        async def check():
            old_cfg = {'title': 'Campaign', 'days': [4], 'times': ['16:00'], 'poll_duration': 3}
            store = Store({'config': old_cfg})
            seen = []
            async def replace(guild, cfg): seen.append(cfg)
            once.__globals__['bot'] = SimpleNamespace(store=store, replace_scheduler=replace)
            class Response:
                async def defer(self, **kwargs): pass
            class Followup:
                async def send(self, message, **kwargs): pass
            inter = SimpleNamespace(guild=SimpleNamespace(id=55), user=SimpleNamespace(id=11),
                                    channel_id=101, response=Response(), followup=Followup())
            await once(inter)
            cfg = seen[0]
            self.assertEqual(cfg['title'], 'Test Poll')
            self.assertEqual(cfg['previous_normal_config']['title'], 'Campaign')
            self.assertFalse(cfg['looping'])
            self.assertTrue(cfg['test_mode'])
            self.assertIsNone(cfg['ping_role_id'])
        asyncio.run(check())

    def test_worker_skips_reminder_and_finishes_without_loop(self):
        (worker,) = funcs('worker', methods=True, utcnow=lambda: NOW,
                          timedelta=timedelta, dtread=datetime.fromisoformat,
                          is_test=lambda c: c.get('test_mode', False),
                          log=SimpleNamespace(exception=lambda *args: None))
        async def check():
            record = {'guild_id': 55, 'state': 'event', 'event_start': (NOW - timedelta(minutes=2)).isoformat(),
                      'event_end': (NOW - timedelta(minutes=1)).isoformat(), 'reminded': 0,
                      'config': {'test_mode': True, 'looping': False, 'reminder_before_event': 6}}
            store = Store(record)
            guild = SimpleNamespace(id=55)
            native = []
            class Bot:
                def __init__(self):
                    self.store = store
                    self._lock = asyncio.Lock()
                def get_guild(self, id_): return guild
                def lock_for(self, guild_id): return self._lock
                async def send_reminder(self, guild, record): raise AssertionError('test reminder should not run')
                async def sync_native_event(self, guild, record, finish=False): native.append(finish)
            await worker(Bot())
            self.assertEqual(record['state'], 'finished')
            self.assertEqual(native, [True])
            self.assertNotIn('next_poll_at', record)
        asyncio.run(check())


if __name__ == '__main__':
    unittest.main()
