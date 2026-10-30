"""Discord-UI contract tests, executable without a Discord token or library.

Mock only the Discord primitives. Run the real bot UI callbacks, embed builders,
and SQLite save code with deterministic interaction objects.
"""
import ast
import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from logic import event_datetimes, slot_keys
from storage import Store


class FakeEmbed:
    def __init__(self, *, title='', description='', color=None):
        self.title, self.description, self.fields = title, description, []
        self.footer = ''

    def add_field(self, *, name, value, inline=True):
        self.fields.append((name, value, inline))

    def set_footer(self, *, text):
        self.footer = text

    def __len__(self):
        return len(self.title) + len(self.description) + len(self.footer) + sum(
            len(name) + len(value) for name, value, _ in self.fields)


class FakeOption:
    def __init__(self, label, value, default=False, description=None):
        self.label, self.value, self.default, self.description = label, value, default, description


class FakeSelect:
    def __init__(self, **kw):
        self.options = kw['options']
        self.max_values = kw.get('max_values', 1)
        self.values = []
        self.callback = None
        self.row = kw.get('row')


class FakeButton:
    def __init__(self, **kw):
        self.label, self.callback = kw.get('label'), None
        self.style = kw.get('style')


class FakeView:
    def __init__(self, **kw):
        self.children = []

    def add_item(self, item):
        self.children.append(item)


def ui_from_bot():
    """Execute selected real bot definitions without gateway or token."""
    src = (Path(__file__).resolve().parents[1] / 'bot.py').read_text(encoding='utf-8')
    nodes = ast.parse(src).body
    needed = {'time_label', 'slot_label', 'build_poll_embed', 'personal_rsvp_embed',
              'VotePanel', 'RsvpManageView', 'build_scheduler_status'}
    selected = [node for node in nodes if isinstance(node, (ast.FunctionDef, ast.ClassDef))
                and node.name in needed]
    discord = SimpleNamespace(
        Embed=FakeEmbed, SelectOption=FakeOption, ui=SimpleNamespace(
            View=FakeView, Select=FakeSelect, Button=FakeButton),
        ButtonStyle=SimpleNamespace(success=1, danger=2, secondary=3, primary=4))
    from datetime import datetime as dt
    ns = {'discord': discord, 'datetime': dt, 'Counter': Counter, 'timedelta': timedelta,
          'utcnow': lambda: datetime.now(timezone.utc),
          'dtread': datetime.fromisoformat, 'slot_keys': slot_keys,
          'poll_start': lambda r: datetime.fromisoformat(r['poll_deadline']) - timedelta(days=r['config']['poll_duration']),
          'is_test': lambda c: c.get('test_mode', False),
          'event_datetimes': event_datetimes, 'event_title': lambda c: c['title'],
          'event_link': lambda g, e: f'https://discord.com/events/{g}/{e}'}
    exec(compile(ast.Module(body=selected, type_ignores=[]), '<bot.py extracted>', 'exec'), ns)
    return ns


class FakeResponse:
    def __init__(self):
        self.edit = None
        self.sent = None

    async def edit_message(self, **kw):
        self.edit = kw

    async def send_message(self, *args, **kw):
        self.sent = (args, kw)


class UXTests(unittest.TestCase):
    def setUp(self):
        self.ui = ui_from_bot()

    def test_atomic_save_preserves_original_vote_timestamp(self):
        with TemporaryDirectory() as d:
            store = Store(str(Path(d)/'scheduler.db'))
            try:
                async def test():
                    await store.toggle_vote(9, 2, 3, '2026-12-01T14:00')
                    original = (await store.user_votes(9, 2, 3))[0]['created_at']
                    await store.replace_user_votes(9, 2, 3,
                        ['2026-12-01T14:00', '2026-12-02T16:00', '2026-12-02T16:00'])
                    votes = await store.user_votes(9, 2, 3)
                    self.assertEqual([v['slot'] for v in votes],
                                     ['2026-12-01T14:00', '2026-12-02T16:00'])
                    self.assertEqual(votes[0]['created_at'], original)
                    await store.replace_user_votes(9, 2, 3, [])
                    self.assertEqual(await store.user_votes(9, 2, 3), [])
                asyncio.run(test())
            finally:
                store.db.close()

    def test_copy_previous_poll_only(self):
        with TemporaryDirectory() as d:
            store = Store(str(Path(d)/'scheduler.db'))
            try:
                async def test():
                    await store.toggle_vote(9, 1, 3, '2026-12-01T14:00')
                    await store.toggle_vote(9, 2, 3, '2026-12-02T16:00')
                    self.assertEqual(len(await store.previous_user_votes(9, 2, 3)), 1)
                    self.assertEqual((await store.previous_user_votes(9, 3, 3))[0]['slot'],
                                     '2026-12-02T16:00')
                asyncio.run(test())
            finally:
                store.db.close()

    def test_select_multiple_dates_then_save_once(self):
        with TemporaryDirectory() as d:
            store = Store(str(Path(d)/'scheduler.db'))
            try:
                days = ['2026-12-03', '2026-12-04']
                slots = [f'{day}T{t}' for day in days for t in ('14:00','16:00','18:00')]
                record = {'guild_id': 9, 'cycle': 2, 'state': 'poll',
                          'poll_deadline': (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),
                          'config': {'timezone':'America/Chicago'}}
                class Bot:
                    def __init__(self):
                        self.store = store
                        self.lock = asyncio.Lock()
                        self.refreshed = 0
                    def lock_for(self, guild): return self.lock
                    def slots_for(self, record): return slots
                    async def refresh_poll(self, guild): self.refreshed += 1
                bot = Bot()
                async def test():
                    await store.put(9, record['config'], 2, 'poll',
                                    poll_deadline=record['poll_deadline'])
                    await store.toggle_vote(9, 2, 99, '2026-12-03T14:00')
                    panel = self.ui['VotePanel'](bot, record, 99, slots,
                                                await store.user_votes(9, 2, 99))
                    self.assertEqual(panel.selected, {'2026-12-03T14:00'})
                    panel.day_select.values = ['2026-12-03', '2026-12-04']
                    await panel.choose_days(SimpleNamespace(response=FakeResponse()))
                    panel.time_select.values = ['14:00', '16:00']
                    await panel.choose_times(SimpleNamespace(response=FakeResponse()))
                    await panel.apply_times(SimpleNamespace(response=FakeResponse()))
                    self.assertEqual(len(panel.selected), 4)
                    # Both dates received both chosen times (cartesian product).
                    self.assertIn('2026-12-04T16:00', panel.selected)
                    self.assertIn('2026-12-03T16:00', panel.selected)
                    # Add the last time only for the second date.
                    panel.day_select.values = ['2026-12-04']
                    await panel.choose_days(SimpleNamespace(response=FakeResponse()))
                    panel.time_select.values = ['18:00']
                    await panel.choose_times(SimpleNamespace(response=FakeResponse()))
                    await panel.apply_times(SimpleNamespace(response=FakeResponse()))
                    self.assertEqual(len(await store.user_votes(9, 2, 99)), 1)  # still draft
                    interaction = SimpleNamespace(user=SimpleNamespace(id=99),response=FakeResponse())
                    await panel.save(interaction)
                    self.assertEqual(len(await store.user_votes(9, 2, 99)), 5)
                    self.assertIn('Saved!', interaction.response.edit['embed'].description)
                    self.assertEqual(bot.refreshed, 1)
                    panel.day_select.values = ['2026-12-03']
                    await panel.choose_days(SimpleNamespace(response=FakeResponse()))
                    await panel.clear_dates(SimpleNamespace(response=FakeResponse()))
                    await panel.save(interaction)
                    self.assertEqual(len(await store.user_votes(9, 2, 99)), 3)
                asyncio.run(test())
            finally:
                store.db.close()

    def test_public_poll_embed_stays_under_discord_limits(self):
        tz = 'America/Chicago'
        times = [f'{h:02d}:00' for h in range(10,22)]
        c = {'title':'D'*100, 'description':'a'*900, 'weeks':4,
             'days':[3,4,5,6], 'times':times, 'poll_duration':3,
             'timezone':tz, 'dm_id':111, 'attendee_limit':8}
        end = datetime.now(timezone.utc) + timedelta(days=3)
        record = {'config': c, 'poll_deadline':end.isoformat()}
        emb = self.ui['build_poll_embed'](record, [])
        self.assertLessEqual(len(emb.fields),25)
        self.assertLessEqual(len(emb),6000)
        self.assertTrue(all(len(value) <=1024 for _,value,_ in emb.fields))

    def test_personal_rsvp_status_and_action_labels(self):
        record = {'guild_id':9,'cycle':2, 'config':{'title':'Test Event','attendee_limit':2}}
        rsvps = [{'user_id':101,'status':'confirmed'},
                 {'user_id':102,'status':'confirmed'},
                 {'user_id':103,'status':'waitlist'},
                 {'user_id':104,'status':'waitlist'}]
        embed,status = self.ui['personal_rsvp_embed'](record,rsvps,104)
        self.assertEqual(status,'waitlist')
        self.assertIn('#2',embed.description)
        view = self.ui['RsvpManageView'](None,record,104,status,rsvps)
        self.assertEqual(view.children[0].label,'Cancel My RSVP')
        other_view = self.ui['RsvpManageView'](None,record,105,'none',rsvps)
        self.assertEqual(other_view.children[0].label,'Join Waitlist')
        _,status = self.ui['personal_rsvp_embed'](record,rsvps,101)
        self.assertEqual(status,'confirmed')

    def test_private_status_uses_current_state(self):
        idle = self.ui['build_scheduler_status'](None, [], [])
        self.assertIn('Idle',idle.title)
        now = datetime.now(timezone.utc)
        record = {'guild_id':9,'cycle':1,'state':'poll',
                  'poll_deadline':(now+timedelta(days=3)).isoformat(),
                  'config':{'title':'Campaign','looping':True,'test_mode':False},
                  'poll_channel_id':333,'poll_message_id':444,'discord_event_id':None,
                  'event_channel_id':None,'event_message_id':None}
        embed = self.ui['build_scheduler_status'](record,
                 [{'user_id':1,'slot':'a'},{'user_id':1,'slot':'b'},
                  {'user_id':2,'slot':'a'}],[])
        self.assertIn('2 players',embed.description)
        self.assertIn('Close poll',embed.description)
        self.assertIn('333/444',str(embed.fields))

if __name__ == '__main__':
    unittest.main()
