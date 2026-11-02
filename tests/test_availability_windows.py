"""Window duration and screenshot-style heatmap checks (no Discord token)."""
import asyncio
import ast
from types import SimpleNamespace
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import unittest

from PIL import Image
from availability import ensure_schema, read_windows, save_windows, carry_windows, _qualifying_slots
from heatmap import _color, _week_start, LIGHT_GREEN, GREEN, RED, ORANGE, render_heatmaps
from logic import parse_days, parse_times


class DummyStore:
    def __init__(self, path):
        self.db=sqlite3.connect(path,isolation_level=None)
        self.db.row_factory=sqlite3.Row
        self.lock=asyncio.Lock()
        self.db.execute('CREATE TABLE votes (guild_id INTEGER,cycle INTEGER,user_id INTEGER,slot TEXT,created_at TEXT, PRIMARY KEY(guild_id,cycle,user_id,slot))')
        ensure_schema(self)


class AvailabilityTests(unittest.TestCase):
    def test_blank_defaults_and_7_day_support(self):
        self.assertEqual(parse_days(''),[6,0,1,2,3,4,5])
        self.assertEqual(len(parse_days('sun,mon,tue,wed,thu,fri,sat')),7)
        self.assertEqual(parse_times(''), ['14:00','14:30','15:00','15:30','16:00','16:30','17:00','17:30','18:00','18:30','19:00'])

    def test_short_window_not_mistaken_for_four_hour_attendance(self):
        slots=[f'2026-11-01T{t}' for t in ['14:00','14:30','15:00','15:30','16:00','17:00','18:00','19:00']]
        one_hour=_qualifying_slots([('2026-11-01','14:00',1)],slots,4)
        four_hour=_qualifying_slots([('2026-11-01','14:00',4)],slots,4)
        eight_hour=_qualifying_slots([('2026-11-01','14:00',8)],slots,4)
        self.assertEqual(one_hour,set())
        self.assertEqual(four_hour,{'2026-11-01T14:00'})
        self.assertEqual(eight_hour,set(slots[:7]))

    def test_atomic_save_dedupes_preserves_and_carries_windows(self):
        with TemporaryDirectory() as folder:
            store=DummyStore(str(Path(folder)/'db.sqlite'))
            try:
                async def scenario():
                    slots=['2026-11-01T14:00','2026-11-01T15:00','2026-11-08T14:00']
                    n=await save_windows(store,1,1,22,[('2026-11-01','14:00',4)]*2,slots,4,8)
                    self.assertEqual(n,1)
                    original=(await read_windows(store,1,1,22))[0]['created_at']
                    await save_windows(store,1,1,22,[('2026-11-01','14:00',8)],slots,4,8)
                    self.assertEqual((await read_windows(store,1,1,22))[0]['created_at'],original)
                    self.assertEqual(store.db.execute('SELECT count(*) FROM votes').fetchone()[0],2)
                    self.assertEqual(await carry_windows(store,1,1,2,slots),1)
                    self.assertEqual(await carry_windows(store,1,1,2,slots),0)
                    self.assertEqual((await read_windows(store,1,2,22))[0]['hours'],8)
                    await save_windows(store,1,1,22,[],slots,4,8)
                    self.assertFalse(await read_windows(store,1,1,22))
                    self.assertEqual(store.db.execute('SELECT count(*) FROM votes WHERE cycle=1').fetchone()[0],0)
                asyncio.run(scenario())
            finally:store.db.close()

    def record(self):
        start=datetime(2026,10,31,17,tzinfo=timezone.utc)
        return {'config':{'title':'D&D','weeks':2,'days':[6,0,1,2,3,4,5],
                          'times':['14:00','14:30','15:00','16:00','17:00','18:00','19:00'],
                          'poll_duration':3,'timezone':'America/Chicago','attendee_limit':8,
                          'dm_id':99},'poll_deadline':(start+timedelta(days=3)).isoformat()}

    def test_no_blank_week_images_and_sunday_first(self):
        record=self.record()
        self.assertEqual(render_heatmaps(record,[]),[])
        from logic import slot_keys
        dl=datetime.fromisoformat(record['poll_deadline'])
        slot=slot_keys(record['config'],dl-timedelta(days=3),dl)[0]
        images=render_heatmaps(record,[{'slot':slot,'user_id':1}])
        self.assertEqual(len(images),1)
        self.assertEqual(_week_start(date.fromisoformat(slot[:10])).weekday(),6)
        image=Image.open(BytesIO(images[0][1]))
        self.assertEqual(image.mode,'RGB')
        self.assertLess(image.height,600)

    def test_three_colors_dm_red_and_one_two_lightgreen(self):
        self.assertEqual(_color(0),LIGHT_GREEN)
        self.assertEqual(_color(1),LIGHT_GREEN)
        self.assertEqual(_color(2),LIGHT_GREEN)
        self.assertEqual(_color(3),GREEN)
        self.assertEqual(_color(8),GREEN)
        self.assertEqual(_color(0,True,False),RED)
        self.assertEqual(_color(12,True,False),ORANGE)
        self.assertEqual(_color(2,True,True),LIGHT_GREEN)

    def test_four_week_discord_ui_uses_valid_component_rows(self):
        """All 28 candidate dates fit without overflowing the 25-option limit."""
        source=(Path(__file__).resolve().parents[1]/'availability_ui.py').read_text(encoding='utf-8')
        nodes=[n for n in ast.parse(source).body if isinstance(n,(ast.FunctionDef,ast.ClassDef))
               and n.name in ('_sunday','_label','AvailabilityVotePanel')]
        class FakeOption:
            def __init__(self,**kw):self.__dict__.update(kw)
        class FakeSelect:
            def __init__(self,**kw):self.__dict__.update(kw);self.values=[]
        class FakeButton:
            def __init__(self,**kw):self.__dict__.update(kw)
        class FakeView:
            def __init__(self,**kw):self.children=[]
            def add_item(self,item):self.children.append(item)
        class FakeEmbed:
            def __init__(self,**kw):self.__dict__.update(kw)
        styles=SimpleNamespace(primary=1,secondary=2,success=3,danger=4)
        discord=SimpleNamespace(ui=SimpleNamespace(View=FakeView,Select=FakeSelect,Button=FakeButton),
                                SelectOption=FakeOption,ButtonStyle=styles,Embed=FakeEmbed)
        ns=dict(discord=discord,date=date,datetime=datetime,timedelta=timedelta,timezone=timezone,
                read_windows=read_windows,save_windows=save_windows)
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'availability_ui.py','exec'),ns)
        weekdays=[(date(2026,11,1)+timedelta(days=i)).isoformat() for i in range(28)]
        slots=[f'{d}T14:00' for d in weekdays]
        cfg=dict(timezone='America/Chicago',event_duration=4,hours=8)
        record=dict(guild_id=1,cycle=1,config=cfg)
        panel=ns['AvailabilityVotePanel'](None,record,10,slots,[],[])
        self.assertEqual(len(panel.weeks),4)
        self.assertEqual([child.row for child in panel.children],[0,1,2,3]+[4]*3)
        self.assertTrue(all(len(getattr(child,'options',[]))<=25 for child in panel.children))
        self.assertEqual(len(panel.day_select.options),7)
        self.assertEqual(len(panel.hour_options()),8)
        self.assertEqual(panel.selected_hours,4)

    def test_hours_validation_rejects_out_of_range_and_mismatched_dates(self):
        with TemporaryDirectory() as d:
            store=DummyStore(str(Path(d)/'db.sqlite'))
            try:
                async def scenario():
                    for day,h in [('2026-11-01',0),('2026-11-01',9),('2026-11-02',2)]:
                        with self.assertRaises(ValueError):
                            await save_windows(store,1,1,23,[(day,'14:00',h)],['2026-11-01T14:00'],4,8)
                asyncio.run(scenario())
            finally:store.db.close()

if __name__=='__main__':unittest.main()
