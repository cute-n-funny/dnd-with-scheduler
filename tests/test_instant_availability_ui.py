"""Native-Discord availability UI contract and save behavior, no bot token required."""
import asyncio
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


class FakeView:
    def __init__(self, timeout=None):
        self.timeout = timeout
        self.children = []
        self.stopped = False

    def add_item(self, item):
        self.children.append(item)

    def stop(self):
        self.stopped = True


class FakeSelect:
    def __init__(self, **kwargs):
        self.options = kwargs['options']
        self.placeholder = kwargs['placeholder']
        self.row = kwargs['row']
        self.min_values = kwargs['min_values']
        self.max_values = kwargs['max_values']
        self.values = []
        self.callback = None


class FakeOption:
    def __init__(self, label, value, default=False):
        self.label, self.value, self.default = label, value, default


class FakeButton:
    def __init__(self, label, style, row):
        self.label, self.style, self.row = label, style, row
        self.callback = None


class FakeEmbed:
    def __init__(self, **kwargs):
        self.description = kwargs['description']


class FakeResponse:
    def __init__(self):
        self.edits = []
        self.sent = []
        self.deferred = False

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)

    async def send_message(self, *args, **kwargs):
        self.sent.append((args,kwargs))

    async def defer(self):
        self.deferred = True


class FakeInteraction:
    def __init__(self, user_id=9):
        self.user = SimpleNamespace(id=user_id)
        self.response = FakeResponse()
        self.deleted = False

    async def delete_original_response(self):
        self.deleted = True


@contextmanager
def mocked_discord():
    discord = ModuleType('discord')
    discord.ui = SimpleNamespace(View=FakeView, Select=FakeSelect, Button=FakeButton)
    discord.ButtonStyle = SimpleNamespace(primary=1, secondary=2, success=3, danger=4)
    discord.SelectOption = FakeOption
    discord.Embed = FakeEmbed
    discord.HTTPException = type('HTTPException', (Exception,), {})
    with patch.dict(sys.modules, {'discord': discord}):
        sys.modules.pop('availability_ui',None)
        try:
            import availability_ui
            yield availability_ui
        finally:
            sys.modules.pop('availability_ui',None)


class MinimalStore:
    def __init__(self):
        self.db = sqlite3.connect(':memory:', isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('''CREATE TABLE votes (
            guild_id INTEGER, cycle INTEGER, user_id INTEGER, slot TEXT, created_at TEXT,
            PRIMARY KEY(guild_id,cycle,user_id,slot))''')
        self.lock = asyncio.Lock()
        self.record = None
        from availability import ensure_schema
        ensure_schema(self)

    async def get(self, guild_id):
        return self.record

    async def user_votes(self, guild_id, cycle, user_id):
        async with self.lock:
            return [dict(v) for v in self.db.execute(
                'SELECT slot,created_at FROM votes WHERE guild_id=? AND cycle=? AND user_id=?',
                (guild_id,cycle,user_id))]


class FakeBot:
    def __init__(self, store, slots):
        self.store = store
        self.slots = slots
        self.lock = asyncio.Lock()
        self.refreshed = 0

    def slots_for(self, record):
        return self.slots

    def lock_for(self, guild_id):
        return self.lock

    async def refresh_poll(self, guild_id):
        self.refreshed += 1


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.store = MinimalStore()
        self.days = ['2026-11-15','2026-11-16','2026-11-22']
        self.slots = [f'{d}T{t}' for d in self.days for t in ['14:00','16:00']]
        self.record = {
            'guild_id':4,'cycle':1,'state':'poll',
            'poll_deadline':(datetime.now(timezone.utc)+timedelta(days=2)).isoformat(),
            'config':{'hours':8,'timezone':'America/Chicago','event_duration':4}}
        self.store.record = self.record
        self.bot = FakeBot(self.store, self.slots)

    def tearDown(self):
        self.store.db.close()

    def make_panel(self, ui, saved_windows=None):
        return ui.AvailabilityVotePanel(self.bot,self.record,9,self.slots,
                                        saved_windows or [],[])

    def test_scheduler_hours_configuration_is_3_to_8(self):
        """Validate the real bot's config checker without importing discord.py."""
        import ast
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        source=(Path(__file__).resolve().parents[1]/'bot.py').read_text(encoding='utf-8')
        tree=ast.parse(source)
        node=next(x for x in tree.body if isinstance(x,ast.FunctionDef)
                  and x.name=='validate_config')
        ns={'ZoneInfo':ZoneInfo,'ZoneInfoNotFoundError':ZoneInfoNotFoundError}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'bot.py','exec'),ns)
        config={'weeks':2,'poll_duration':3,'poll_after_event':3,
                'reminder_before_event':6,'attendee_limit':8,'event_duration':4,
                'title':'Title','description':'Description','timezone':'America/Chicago',
                'hours':3}
        ns['validate_config'](config)
        config['hours']=8
        ns['validate_config'](config)
        for wrong in (1,2,9):
            config['hours']=wrong
            with self.assertRaises(ValueError):
                ns['validate_config'](config)

    def test_seven_controls_and_initial_defaults(self):
        with mocked_discord() as ui:
            panel = self.make_panel(ui)
            self.assertEqual(len(panel.children), 7)
            self.assertEqual([x.placeholder for x in panel.children[:4]], [
                'Select a week','Choose one or more dates',
                'Choose your earliest start time','Choose how many hours you can stay'])
            self.assertEqual([x.label for x in panel.children[4:]], [
                'Add Times','Remove Times','Close'])
            self.assertEqual([x.style for x in panel.children[4:]], [3,4,2])
            self.assertEqual(panel.target_days,set())
            self.assertFalse(any(opt.default for opt in panel.day_select.options))
            self.assertEqual(panel.selected_hours,3)
            self.assertEqual([int(o.value) for o in panel.hours_select.options],list(range(1,9)))
            self.assertEqual([o.value for o in panel.hours_select.options if o.default],['3'])
            self.assertNotIn('Press **Save**',panel.embed().description)

    def test_hours_options_one_to_configured_maximum(self):
        with mocked_discord() as ui:
            for maximum in range(3,9):
                self.record['config']['hours']=maximum
                panel = self.make_panel(ui)
                self.assertEqual([int(o.value) for o in panel.hours_select.options],
                                 list(range(1,maximum+1)))
                self.assertEqual(panel.selected_hours,3)

    def test_week_switch_clears_dates(self):
        with mocked_discord() as ui:
            panel=self.make_panel(ui)
            panel.day_select.values=['2026-11-15']
            asyncio.run(panel.choose_days(FakeInteraction()))
            self.assertTrue(panel.target_days)
            panel.week_select.values=[panel.weeks[-1].isoformat()]
            asyncio.run(panel.choose_week(FakeInteraction()))
            self.assertFalse(panel.target_days)
            self.assertFalse(any(o.default for o in panel.day_select.options))

    def test_add_and_remove_are_immediately_persisted(self):
        from availability import read_windows
        with mocked_discord() as ui:
            panel = self.make_panel(ui)
            panel.day_select.values=self.days[:2]
            asyncio.run(panel.choose_days(FakeInteraction()))
            panel.start_select.values=['14:00']
            asyncio.run(panel.choose_starts(FakeInteraction()))
            interaction=FakeInteraction()
            asyncio.run(panel.add_windows(interaction))
            self.assertIn('saved automatically',interaction.response.edits[-1]['embed'].description)
            self.assertEqual(self.bot.refreshed,1)
            windows=asyncio.run(read_windows(self.store,4,1,9))
            self.assertEqual(len(windows),2)
            self.assertEqual([w['hours'] for w in windows],[3,3])
            # 3-hour windows cannot qualify as a four-hour event.
            self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM votes').fetchone()[0],0)
            panel.hours_select.values=['4']
            asyncio.run(panel.choose_hours(FakeInteraction()))
            asyncio.run(panel.add_windows(FakeInteraction()))
            self.assertEqual(len(asyncio.run(read_windows(self.store,4,1,9))),2)
            self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM votes').fetchone()[0],2)
            asyncio.run(panel.remove_windows(FakeInteraction()))
            self.assertEqual(asyncio.run(read_windows(self.store,4,1,9)),[])
            self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM votes').fetchone()[0],0)
            self.assertEqual(self.bot.refreshed,3)

    def test_other_panel_edits_not_erased(self):
        from availability import read_windows
        with mocked_discord() as ui:
            panel_a=self.make_panel(ui)
            panel_b=self.make_panel(ui)
            panel_a.target_days={'2026-11-15'}
            panel_a.target_starts={'14:00'}
            panel_b.target_days={'2026-11-16'}
            panel_b.target_starts={'16:00'}
            asyncio.run(panel_a.add_windows(FakeInteraction()))
            asyncio.run(panel_b.add_windows(FakeInteraction()))
            self.assertEqual({(x['day'],x['start']) for x in
                              asyncio.run(read_windows(self.store,4,1,9))},
                             {('2026-11-15','14:00'),('2026-11-16','16:00')})

    def test_requires_dates_and_starts(self):
        from availability import read_windows
        with mocked_discord() as ui:
            panel=self.make_panel(ui)
            response=FakeInteraction()
            asyncio.run(panel.add_windows(response))
            self.assertIn('Select one or more dates first',response.response.edits[-1]['embed'].description)
            panel.target_days={'2026-11-15'}
            response2=FakeInteraction()
            asyncio.run(panel.remove_windows(response2))
            self.assertIn('Select one or more start times',response2.response.edits[-1]['embed'].description)
            self.assertEqual(asyncio.run(read_windows(self.store,4,1,9)),[])

    def test_rejects_wrong_user_or_closed_poll(self):
        with mocked_discord() as ui:
            panel=self.make_panel(ui)
            panel.target_days={'2026-11-15'}
            panel.target_starts={'14:00'}
            wrong=FakeInteraction(user_id=999)
            asyncio.run(panel.add_windows(wrong))
            self.assertTrue(wrong.response.sent)
            self.assertEqual(self.bot.refreshed,0)
            self.record['state']='finished'
            closed=FakeInteraction()
            asyncio.run(panel.add_windows(closed))
            self.assertEqual(closed.response.edits[-1]['view'],None)
            self.assertEqual(self.bot.refreshed,0)

    def test_close_removes_ephemeral_panel(self):
        with mocked_discord() as ui:
            panel=self.make_panel(ui)
            inter=FakeInteraction()
            asyncio.run(panel.close(inter))
            self.assertTrue(inter.response.deferred)
            self.assertTrue(inter.deleted)
            self.assertTrue(panel.stopped)


if __name__ == '__main__':
    unittest.main()
