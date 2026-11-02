"""Live-heatmap tests. No Discord server or bot token required."""
import ast
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image
from heatmap import _color, render_heatmaps, GREEN, LIGHT_GREEN, RED, ORANGE
from logic import slot_keys


class HeatmapTests(unittest.TestCase):
    def record(self, weeks=2):
        created = datetime(2026, 10, 30, 17, tzinfo=timezone.utc)
        config = dict(title='D&D', description='Availability', weeks=weeks,
                      days=[3,4,5,6], times=['14:00','16:00','18:00'],
                      poll_duration=3, timezone='America/Chicago',
                      attendee_limit=8, dm_id=111)
        return dict(config=config, poll_deadline=(created + timedelta(days=3)).isoformat())

    def test_four_week_images_are_valid_png_and_counted(self):
        record = self.record(weeks=4)
        start = datetime.fromisoformat(record['poll_deadline'])-timedelta(days=3)
        slot = slot_keys(record['config'],start,datetime.fromisoformat(record['poll_deadline']))[0]
        votes = [{'slot':slot,'user_id':u} for u in range(8)]
        pictures = render_heatmaps(record, votes, winner=slot)
        self.assertEqual(len(pictures), 1)  # only the week with votes is shown
        self.assertEqual(len(set(name for name, _ in pictures)), 1)
        for name, png in pictures:
            self.assertTrue(name.endswith('.png'))
            image = Image.open(BytesIO(png))
            self.assertEqual(image.format, 'PNG')
            self.assertEqual(image.width, 850)
            self.assertGreater(image.height, 300)
            self.assertLess(len(png), 2_000_000)

    def test_duplicate_user_does_not_inflate_heatmap(self):
        record = self.record(weeks=1)
        end = datetime.fromisoformat(record['poll_deadline'])
        slot = slot_keys(record['config'],end-timedelta(days=3),end)[0]
        vote = {'slot':slot,'user_id':9}
        self.assertEqual(render_heatmaps(record,[vote])[0][1],
                         render_heatmaps(record,[vote,vote,vote])[0][1])
        self.assertEqual(render_heatmaps(record,[]),[])

    def test_text_fallback_when_render_fails(self):
        src = (Path(__file__).resolve().parents[1]/'bot.py').read_text(encoding='utf-8')
        tree = ast.parse(src)
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                  and n.name == 'build_visual_poll')
        marker = object()
        def fail(*a, **k):
            raise RuntimeError('image encoder unavailable')
        ns = dict(build_poll_embed=lambda *a, **k: marker,
                  render_heatmaps=fail,
                  log=SimpleNamespace(exception=lambda *a: None))
        exec(compile(ast.Module(body=[fn], type_ignores=[]),'bot.py','exec'),ns)
        embeds, files = ns['build_visual_poll']({'config':{}}, [])
        self.assertEqual(embeds, [marker])
        self.assertEqual(files, [])

    def test_dynamic_legend_thresholds(self):
        self.assertEqual(_color(0), LIGHT_GREEN)
        self.assertEqual(_color(2), LIGHT_GREEN)
        self.assertEqual(_color(3), GREEN)
        self.assertEqual(_color(0, True, False), RED)
        self.assertEqual(_color(2, True, False), ORANGE)
        self.assertEqual(_color(12, True, False), ORANGE)
        self.assertEqual(_color(3, True, True), GREEN)

    def test_bot_embeds_reference_distinct_uploaded_images(self):
        """Parse only the helper, so discord.py isn't needed in the unit test."""
        src = (Path(__file__).resolve().parents[1]/'bot.py').read_text(encoding='utf-8')
        tree = ast.parse(src)
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and
                  n.name == 'build_visual_poll')
        class FakeEmbed:
            def __init__(self, **kwargs):
                self.fields=[]
                self.images=[]
            def clear_fields(self): self.fields=[]
            def add_field(self, **kwargs): self.fields.append(kwargs)
            def set_image(self, *, url): self.images.append(url)
        class FakeFile:
            def __init__(self, data, *, filename):
                self.filename=filename
                self.content=data.read()
        captured={}
        def build_poll_embed(record, votes, **kwargs): return FakeEmbed()
        def slot_keys_fake(*args): return ['2026-11-06T14:00']
        namespace = dict(
            build_poll_embed=build_poll_embed,
            render_heatmaps=lambda *a,**kw:[('dnd-availability-week-1.png',b'PNG1'),
                                            ('dnd-availability-week-2.png',b'PNG2')],
            discord=SimpleNamespace(Embed=FakeEmbed, File=FakeFile),
            io=__import__('io'), log=SimpleNamespace(exception=lambda *a:None),
            slot_keys=slot_keys_fake, poll_start=lambda r:None,
            dtread=lambda x:None, slot_label=lambda x:x)
        code = compile(ast.Module(body=[fn], type_ignores=[]),'bot.py','exec')
        exec(code,namespace)
        record={'config':{'attendee_limit':8},'poll_deadline':'x'}
        embeds,files=namespace['build_visual_poll'](record, [])
        self.assertEqual(len(embeds),3)
        self.assertEqual([f.filename for f in files],
                         ['dnd-availability-week-1.png','dnd-availability-week-2.png'])
        self.assertEqual(embeds[1].images,['attachment://dnd-availability-week-1.png'])


if __name__ == '__main__':
    unittest.main()
