"""Regression tests for poll minimums, retry cooldowns and test bypass.

They parse actual bot.py functions without importing Discord, so CI does not
need a bot token or an active Discord connection.
"""
import ast
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from logic import choose_slot

NOW = datetime(2026, 12, 1, 12, tzinfo=timezone.utc)
SLOT_A = "2026-12-11T14:00"
SLOT_B = "2026-12-11T16:00"
SLOT_C = "2026-12-12T14:00"


def source_function(name, method=False, **globals_):
    root = ast.parse((Path(__file__).resolve().parents[1] / "bot.py").read_text(encoding="utf-8"))
    candidates = root.body
    if method:
        parent = next(c for c in root.body if isinstance(c, ast.ClassDef) and c.name == "SchedulerBot")
        candidates = parent.body
    import copy
    func = copy.deepcopy(next(n for n in candidates if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                              and n.name == name))
    func.decorator_list = []
    ns = dict(globals_)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[func], type_ignores=[])),
                 "bot.py", "exec"), ns)
    return ns[name]


def ballots(slot, people):
    return [{"slot": slot, "user_id": person, "created_at": f"2026-12-01T10:00:{i:02d}+00:00"}
            for i, person in enumerate(people)]


class MemoryStore:
    def __init__(self, record, votes=None):
        self.record = record
        self.ballots = votes or []
        self.calls = []
    async def votes(self, guild_id, cycle):
        return self.ballots
    async def update(self, guild_id, **fields):
        self.record.update(fields)
        self.calls.append(fields)
    async def get(self, guild_id):
        return self.record
    async def set_initial_rsvps(self, guild_id, cycle, users, limit):
        self.calls.append({"rsvp_users": list(users), "limit": limit})
    async def all(self):
        return [self.record]


class MinVotesTests(unittest.TestCase):
    def test_new_defaults_and_validation(self):
        config_defaults = source_function("config_defaults", TZ_NAME="America/Chicago")
        validate_config = source_function("validate_config", ZoneInfo=ZoneInfo,
                                          ZoneInfoNotFoundError=ZoneInfoNotFoundError)
        c = config_defaults(SimpleNamespace(id=88), 42, 999)
        self.assertEqual((c["poll_duration"], c["min_votes"]), (2, 3))
        validate_config(c)
        for minimum in (3, 12):
            trial = dict(c, min_votes=minimum)
            validate_config(trial)
        for minimum in (2, 13):
            with self.assertRaisesRegex(ValueError, "min_votes"):
                validate_config(dict(c, min_votes=minimum))
        for days in (2, 6):
            validate_config(dict(c, poll_duration=days))
        for days in (1, 7):
            with self.assertRaisesRegex(ValueError, "poll_duration"):
                validate_config(dict(c, poll_duration=days))
        # Old saved schedules without the new key use the default minimum.
        c.pop("min_votes")
        validate_config(c)

    def test_slash_command_exposes_min_votes_and_two_day_default(self):
        root = ast.parse((Path(__file__).resolve().parents[1] / "bot.py").read_text(encoding="utf-8"))
        fn = next(x for x in root.body if isinstance(x, ast.AsyncFunctionDef) and x.name == "start_scheduler")
        defaults = dict(zip([arg.arg for arg in fn.args.args][-len(fn.args.defaults):],
                            [x.value for x in fn.args.defaults if isinstance(x, ast.Constant)]))
        self.assertEqual(defaults["poll_duration"], 2)
        self.assertEqual(defaults["min_votes"], 3)

    def test_same_person_three_times_does_not_meet_three_vote_minimum(self):
        votes = ballots(SLOT_A, [1]) + ballots(SLOT_B, [1])
        self.assertEqual(choose_slot(votes, [SLOT_A, SLOT_B], dm_id=99, min_votes=3), (None, []))

    def test_three_distinct_voters_on_date_qualify_even_across_times(self):
        votes = ballots(SLOT_A, [1, 2]) + ballots(SLOT_B, [2, 3])
        selected, people = choose_slot(votes, [SLOT_A, SLOT_B], dm_id=99, min_votes=3,
                                       attendee_limit=8)
        self.assertIn(selected, (SLOT_A, SLOT_B))
        self.assertGreaterEqual(len(people), 2)

    def test_dm_days_still_constrain_minimum(self):
        votes = ballots(SLOT_A, [42]) + ballots(SLOT_C, [1, 2, 3, 4])
        # DM voted only on the under-supported date; don't schedule the other.
        self.assertEqual(choose_slot(votes, [SLOT_A, SLOT_C], dm_id=42, min_votes=3), (None, []))
        # DM abstains: the three-voter date is allowed.
        selected, _ = choose_slot(votes, [SLOT_A, SLOT_C], dm_id=123, min_votes=3)
        self.assertEqual(selected, SLOT_C)

    def test_attendee_limit_priority_still_wins_among_minimum_dates(self):
        near, far = SLOT_A, SLOT_C
        votes = ballots(near, [1, 2, 3]) + ballots(far, range(10, 18))
        selected, _ = choose_slot(votes, [near, far], dm_id=99,
                                  attendee_limit=8, min_votes=3)
        self.assertEqual(selected, far)

    def close_function(self):
        return source_function('close_poll', method=True, choose_slot=choose_slot,
                               utcnow=lambda: NOW, timedelta=timedelta,
                               event_datetimes=lambda s, c: (NOW + timedelta(days=8), NOW + timedelta(days=8, hours=4)),
                               is_test=lambda c: c.get('test_mode', False),
                               log=SimpleNamespace(info=lambda *args: None))

    def test_poll_under_minimum_schedules_24_hour_retry_and_no_event(self):
        record = dict(guild_id=42, cycle=1, poll_message_id=None,
                      config=dict(dm_id=100, attendee_limit=8, poll_duration=2, min_votes=3))
        store = MemoryStore(record, ballots(SLOT_A, [1, 2]))
        class Bot:
            def __init__(self): self.store = store
            def slots_for(self, r): return [SLOT_A]
            async def create_event(self, guild, r): raise AssertionError('No event should be created')
        asyncio.run(self.close_function()(Bot(), SimpleNamespace(id=42), record))
        self.assertEqual(record['state'], 'waiting_retry')
        self.assertEqual(datetime.fromisoformat(record['next_poll_at']), NOW + timedelta(hours=24))
        self.assertIsNone(record['selected_slot'])
        self.assertFalse(any('rsvp_users' in call for call in store.calls))

    def test_three_voters_create_event_even_below_attendee_limit(self):
        record = dict(guild_id=42, cycle=1, poll_message_id=None,
                      config=dict(dm_id=100, attendee_limit=8, poll_duration=2, min_votes=3))
        store = MemoryStore(record, ballots(SLOT_A, [1, 2, 3]))
        events = []
        class Bot:
            def __init__(self): self.store = store
            def slots_for(self, r): return [SLOT_A]
            async def create_event(self, guild, r): events.append(r.copy())
        asyncio.run(self.close_function()(Bot(), SimpleNamespace(id=42), record))
        self.assertEqual(len(events), 1)
        self.assertEqual(record['selected_slot'], SLOT_A)
        self.assertEqual(record['state'], 'creating_event')

    def test_test_mode_bypasses_minimum_and_never_queues_retry(self):
        record = dict(guild_id=42, cycle=1, poll_message_id=None,
                      config=dict(dm_id=100, attendee_limit=8, poll_duration=2,
                                  min_votes=12, test_mode=True))
        store = MemoryStore(record, ballots(SLOT_A, [1]))
        events = []
        class Bot:
            def __init__(self): self.store = store
            def slots_for(self, r): return [SLOT_A]
            async def create_event(self, guild, r): events.append(r.copy())
        asyncio.run(self.close_function()(Bot(), SimpleNamespace(id=42), record))
        self.assertEqual(len(events), 1)
        self.assertNotEqual(record['state'], 'waiting_retry')
        self.assertEqual(datetime.fromisoformat(record['event_start']), NOW + timedelta(minutes=1))

    def test_retry_worker_only_opens_new_poll_after_deadline(self):
        record = dict(guild_id=42, cycle=5, state='waiting_retry',
                      next_poll_at=(NOW + timedelta(hours=24)).isoformat(),
                      config=dict(poll_duration=2))
        store = MemoryStore(record)
        when = [NOW]
        worker = source_function('worker', method=True,
                                 utcnow=lambda: when[0],
                                 dtread=datetime.fromisoformat,
                                 timedelta=timedelta,
                                 is_test=lambda c: False,
                                 log=SimpleNamespace(exception=lambda *args: None))
        class Bot:
            def __init__(self): self.store = store; self.lock = asyncio.Lock(); self.actions = []
            def get_guild(self, guild_id): return SimpleNamespace(id=42)
            def lock_for(self, guild_id): return self.lock
            async def prepare_poll(self, guild, config, cycle):
                self.actions.append(('prepare',cycle))
                record['state'] = 'poll_preparing'
            async def publish_poll(self, guild, data):
                self.actions.append(('publish',data['state']))
                record['state'] = 'poll'
        bot = Bot()
        asyncio.run(worker(bot))
        self.assertEqual(bot.actions, [])
        when[0] += timedelta(hours=24)
        asyncio.run(worker(bot))
        asyncio.run(worker(bot))
        self.assertEqual(bot.actions, [('prepare',6), ('publish','poll_preparing')])


if __name__ == '__main__':
    unittest.main()
