"""New deadline, threshold, and cross-cycle carry-over behavior."""
import ast
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from logic import choose_slot
from storage import Store


def voters(slot, people, start=0):
    return [{'user_id': i, 'slot': slot, 'created_at': f'2026-10-30T00:00:{(start+n):02d}+00:00'}
            for n, i in enumerate(people)]


class VoteRuleTests(unittest.TestCase):
    def test_earliest_qualified_slot_beats_more_popular_later_date(self):
        near = '2026-11-07T14:00'
        future = '2026-11-14T14:00'
        votes = voters(near, range(1, 9)) + voters(future, range(1, 13))
        winner, rsvp_users = choose_slot(votes, [future, near], dm_id=99, attendee_limit=8)
        self.assertEqual(winner, near)
        self.assertEqual(rsvp_users, list(range(1, 9)))

    def test_threshold_is_distinct_people_per_exact_slot_not_daily_sum(self):
        near_2 = '2026-11-07T14:00'
        near_4 = '2026-11-07T16:00'
        later = '2026-11-14T14:00'
        # 8 distinct people across the DAY, but only 5 at each time.
        votes = voters(near_2, range(1, 6)) + voters(near_4, range(4, 9)) + voters(later, range(1, 9))
        winner, _ = choose_slot(votes, [near_2, near_4, later], dm_id=99, attendee_limit=8)
        self.assertEqual(winner, later)

    def test_earliest_time_wins_when_multiple_qualify_on_same_day(self):
        early='2026-11-07T14:00'
        late='2026-11-07T18:00'
        votes=voters(early, range(1,9))+voters(late,range(1,11))
        self.assertEqual(choose_slot(votes,[early,late],dm_id=100,attendee_limit=8)[0],early)

    def test_dm_day_restriction_still_applies(self):
        near='2026-11-07T14:00'
        later='2026-11-14T14:00'
        votes=voters(near,range(1,9))+voters(later,range(1,9))
        votes += [{'user_id': 99,'slot':later,'created_at':'2026-10-30T00:01:00+00:00'}]
        self.assertEqual(choose_slot(votes,[near,later],dm_id=99,attendee_limit=8)[0],later)

    def test_fallback_most_popular_when_nobody_hits_limit(self):
        near='2026-11-07T14:00'
        later='2026-11-14T14:00'
        votes=voters(near,range(1,3))+voters(later,range(1,7))
        self.assertEqual(choose_slot(votes,[near,later],dm_id=99,attendee_limit=8)[0],later)

    def test_multiple_times_same_user_counted_once_per_day(self):
        a='2026-11-07T14:00';b='2026-11-07T16:00';c='2026-11-14T14:00'
        votes=voters(a,[1,2,3])+voters(b,[1,2,3])+voters(c,[4,5,6,7])
        self.assertEqual(choose_slot(votes,[a,b,c],99,attendee_limit=8)[0],c)

    def test_default_attendee_limit_and_renamed_test_command(self):
        src=(Path(__file__).resolve().parents[1]/'bot.py').read_text(encoding='utf-8')
        tree=ast.parse(src)
        defs={n.name:n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))}
        self.assertIn('test_scheduler',defs)
        self.assertNotIn('once_scheduler',defs)
        import copy
        cfg=copy.deepcopy(defs['config_defaults']);cfg.decorator_list=[]
        ns={"TZ_NAME":"America/Chicago"}
        exec(compile(ast.Module(body=[cfg],type_ignores=[]),'bot.py','exec'),ns)
        self.assertEqual(ns['config_defaults'](SimpleNamespace(id=99),10,20)['attendee_limit'],8)
        sig=defs['start_scheduler'].args.defaults
        self.assertIn(8, [n.value for n in sig if isinstance(n,ast.Constant)])

    def test_exact_slot_carry_over_and_idempotency(self):
        with TemporaryDirectory() as folder:
            store=Store(str(Path(folder)/'schedule.db'))
            try:
                async def run():
                    a='2026-11-14T14:00';b='2026-11-14T16:00';past='2026-11-07T14:00'
                    for userid,slot in [(11,a),(11,b),(12,a),(12,past),(13,past)]:
                        await store.toggle_vote(42,1,userid,slot)
                    saved=await store.user_votes(42,1,11)
                    allowed=[a,b,'2026-11-21T14:00']
                    self.assertEqual(await store.carry_forward_votes(42,1,2,allowed),3)
                    self.assertEqual(await store.carry_forward_votes(42,1,2,allowed),0)
                    new=await store.votes(42,2)
                    self.assertEqual(len(new),3)
                    self.assertEqual({v['slot'] for v in new},{a,b})
                    self.assertEqual((await store.user_votes(42,2,11))[0]['created_at'],saved[0]['created_at'])
                    # A second-cycle user's edited preferences take precedence.
                    await store.replace_user_votes(42,2,11,[b])
                    self.assertEqual(len(await store.user_votes(42,2,11)),1)
                    # Next cycle copies only what's still present, not deleted votes.
                    self.assertEqual(await store.carry_forward_votes(42,2,3,allowed),2)
                    self.assertEqual(len(await store.user_votes(42,3,11)),1)
                    self.assertEqual(await store.carry_forward_votes(42,3,3,allowed),0)
                asyncio.run(run())
            finally:
                store.db.close()

if __name__=='__main__':
    unittest.main()
