"""Heatmap DM-priority colors, location option, and UI copy regression tests.

Tests operate without a Discord token or network connection.
"""
import ast
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from heatmap import _color, LIGHT_GREEN, GREEN, RED, ORANGE

ROOT = Path(__file__).resolve().parents[1]
BOT = ast.parse((ROOT / 'bot.py').read_text(encoding='utf-8'))
HEATMAP = ast.parse((ROOT / 'heatmap.py').read_text(encoding='utf-8'))


def extract(name, *, source=BOT, **globals_):
    node = next(n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == name)
    namespace = dict(globals_)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
                 '<bot.py>', 'exec'), namespace)
    return namespace[name]


class FakeEmbed:
    def __init__(self, title=None, description='', color=None):
        self.title = title
        self.description = description
        self.fields = []
    def add_field(self, name, value, inline=False):
        self.fields.append((name, value))
    def set_image(self, url):
        self.url = url


class LocationHeatmapTests(unittest.TestCase):
    def test_color_precedence_and_dm_abstention(self):
        self.assertEqual(_color(0), LIGHT_GREEN)
        self.assertEqual(_color(1), LIGHT_GREEN)
        self.assertEqual(_color(2), LIGHT_GREEN)
        self.assertEqual(_color(3), GREEN)
        self.assertEqual(_color(0, True, False), RED)
        self.assertEqual(_color(1, True, False), ORANGE)
        self.assertEqual(_color(9, True, False), ORANGE)  # Orange overrides green
        self.assertEqual(_color(9, True, True), GREEN)
        self.assertEqual(_color(2, False, False), LIGHT_GREEN)

    def test_heatmap_uses_ascii_hyphen_and_new_heading(self):
        fn = next(n for n in HEATMAP.body if isinstance(n, ast.FunctionDef)
                  and n.name == 'render_heatmaps')
        source = ast.get_source_segment((ROOT / 'heatmap.py').read_text(encoding='utf-8'), fn)
        self.assertIn('Group start availability -', source)
        self.assertIn('{first:%b %d}-{last:%b %d}', source)
        self.assertNotIn('Group availability ·', source)
        self.assertNotIn('–{first+', source)

    def test_new_location_defaults_and_limits(self):
        defaults = extract('config_defaults', TZ_NAME='America/Chicago')
        validate = extract('validate_config', ZoneInfo=ZoneInfo,
                           ZoneInfoNotFoundError=ZoneInfoNotFoundError)
        cfg = defaults(SimpleNamespace(id=1), 12, 45)
        self.assertEqual(cfg['location'], '')
        self.assertEqual(cfg['min_votes'], 3)
        validate(cfg)
        validate(dict(cfg, location=''))
        validate(dict(cfg, location='X'*100))
        with self.assertRaisesRegex(ValueError, 'Location'):
            validate(dict(cfg, location='X'*101))
        validate({k:v for k,v in cfg.items() if k!='location'})  # old configs

    def test_slash_command_keeps_min_votes_and_adds_location(self):
        command = next(n for n in BOT.body if isinstance(n, ast.AsyncFunctionDef)
                       and n.name == 'start_scheduler')
        args = [arg.arg for arg in command.args.args]
        self.assertIn('min_votes', args)
        self.assertIn('location', args)
        self.assertEqual(args[-1], 'location')
        self.assertEqual(command.args.defaults[-1].value, '')

    def test_availability_embed_location_optional(self):
        fn = extract('build_window_visual_poll',
                     discord=SimpleNamespace(Embed=FakeEmbed),
                     dtread=datetime.fromisoformat,
                     windows_for_heatmap=lambda w,v:(w,v),
                     render_heatmaps=lambda *a,**k: [],
                     is_test=lambda cfg:cfg.get('test_mode', False))
        record = {'config':{'title':'Campaign','dm_id':2,'timezone':'America/Chicago',
                            'location':'Table 5, Library'},
                  'poll_deadline':'2026-12-05T12:00:00+00:00'}
        embeds, files = fn(record, [], [])
        self.assertEqual(files, [])
        self.assertIn('**Location:** Table 5, Library', embeds[0].description)
        record['config']['location'] = ''
        embeds, files = fn(record, [], [])
        self.assertNotIn('Location', embeds[0].description)

    def test_session_details_location_optional(self):
        fn = extract('build_event_embed',
                     discord=SimpleNamespace(Embed=FakeEmbed),
                     dtread=datetime.fromisoformat,
                     event_title=lambda c: c['title'])
        record = dict(config=dict(title='Campaign', description='Quest',dm_id=2,
                   attendee_limit=8,location='https://example.com/table'),
                   event_start='2026-12-05T16:00:00+00:00',
                   event_end='2026-12-05T20:00:00+00:00',
                   discord_event_id=None,forum_thread_id=None)
        embed = fn(record, [])
        self.assertIn('📍 **Location:** https://example.com/table',embed.description)
        record['config'].pop('location')
        embed = fn(record, [])
        self.assertNotIn('Location', embed.description)

    def test_personal_availability_ui_omits_two_duration_lines(self):
        src = (ROOT/'availability_ui.py').read_text(encoding='utf-8')
        tree = ast.parse(src)
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                   and n.name == 'AvailabilityVotePanel')
        fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'embed')
        text = ast.get_source_segment(src, fn)
        self.assertNotIn('**Duration:**',text)
        self.assertNotIn('**Event duration:**',text)
        self.assertIn('**Saved availability',text)

    def test_native_scheduled_event_uses_location_for_external_event(self):
        cls = next(n for n in BOT.body if isinstance(n, ast.ClassDef) and n.name == 'SchedulerBot')
        fn = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef)
                  and n.name == 'create_event')
        code = ast.get_source_segment((ROOT/'bot.py').read_text(encoding='utf-8'), fn)
        self.assertIn('c.get("location", "").strip()', code)
        self.assertIn('Discord — test session', code)  # blank fallback remains


if __name__ == '__main__':
    unittest.main()
