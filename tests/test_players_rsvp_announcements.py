"""No Discord gateway or token required: validate announcement and RSVP contracts."""
import ast
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from logic import choose_slot
from storage import Store


def announcements_from_bot():
    script = (Path(__file__).resolve().parents[1] / 'bot.py').read_text(encoding='utf-8')
    nodes = ast.parse(script).body
    methods = {'players_mention', 'mention', 'session_announcement'}
    chosen = [node for node in nodes if isinstance(node, ast.FunctionDef)
              and node.name in methods]
    if len(chosen) != 3:
        raise AssertionError('Expected the three real announcement helpers in bot.py')
    namespace = {
        'is_test': lambda c: c.get('test_mode', False),
        'log': SimpleNamespace(warning=lambda *args: None),
    }
    exec(compile(ast.Module(body=chosen, type_ignores=[]), 'bot.py', 'exec'), namespace)
    return namespace


class AnnouncementTests(unittest.TestCase):
    def setUp(self):
        self.helpers = announcements_from_bot()
        self.players = SimpleNamespace(name='Players', id=777, mention='<@&777>')
        self.custom = SimpleNamespace(name='Event Alerts', id=888, mention='<@&888>')
        self.guild = SimpleNamespace(id=123, roles=[self.players, self.custom],
            get_role=lambda id_:next((r for r in [self.players, self.custom] if r.id == id_), None))

    def test_default_poll_has_no_role_ping(self):
        mention = self.helpers['mention']
        self.assertEqual(mention({'ping_role_id': 123}, self.guild), '')
        self.assertEqual(mention({'ping_role_id': None}, self.guild), '')
        self.assertEqual(mention({'ping_role_id': 123, 'test_mode': True}, self.guild), '')
        self.assertNotIn('@everyone', mention({'ping_role_id': 123}, self.guild))

    def test_explicit_custom_poll_role_is_preserved(self):
        mention = self.helpers['mention']
        self.assertEqual(mention({'ping_role_id': 888}, self.guild), '<@&888>')
        # Session announcements use the same per-server pingable-role setting.
        text = self.helpers['session_announcement']({'ping_role_id': 888}, self.guild, [])
        self.assertIn('<@&888>', text)
        self.assertNotIn('<@&777>', text)

    def test_missing_players_role_never_reverts_to_everyone(self):
        no_roles = SimpleNamespace(id=123, roles=[])
        self.assertEqual(self.helpers['mention']({'ping_role_id': 123}, no_roles), '')
        message = self.helpers['session_announcement']({}, no_roles, [])
        self.assertNotIn('@everyone', message)
        self.assertIn('iCal', message)

    def test_session_content_lists_confirmed_only_and_calendar_actions(self):
        rsvps = [
            {'user_id': 42, 'status': 'confirmed'},
            {'user_id': 15, 'status': 'confirmed'},
            {'user_id': 17, 'status': 'waitlist'},
        ]
        text = self.helpers['session_announcement']({}, self.guild, rsvps)
        self.assertNotIn('<@&777>', text)  # blank preference means no ping
        self.assertIn('The following users have been added after voting:', text)
        self.assertLess(text.index('<@42>'), text.index('<@15>'))
        self.assertNotIn('<@17>', text)
        self.assertIn('iCal', text)
        self.assertIn('Google Calendar', text)

    def test_test_scheduler_does_not_ping_roles(self):
        message = self.helpers['session_announcement'](
            {'test_mode': True}, self.guild, [{'user_id': 42, 'status': 'confirmed'}])
        self.assertNotIn('<@&', message)
        self.assertNotIn('@everyone', message)
        self.assertIn('iCal', message)

    def test_large_confirmed_roster_stays_within_discord_message_limit(self):
        rsvps = [{'user_id': 123456789012345678 + n, 'status': 'confirmed'} for n in range(99)]
        message = self.helpers['session_announcement']({}, self.guild, rsvps)
        self.assertLessEqual(len(message), 2000)
        self.assertIn('more confirmed', message)

    def test_dm_is_confirmed_first_then_first_voters_then_waitlist(self):
        slot = '2026-12-05T14:00'
        votes = [
            {'user_id': 20, 'slot': slot, 'created_at': '2026-12-01T09:00:00+00:00'},
            {'user_id': 21, 'slot': slot, 'created_at': '2026-12-01T09:02:00+00:00'},
            {'user_id': 42, 'slot': slot, 'created_at': '2026-12-01T09:05:00+00:00'},
            {'user_id': 50, 'slot': slot, 'created_at': '2026-12-01T09:06:00+00:00'},
        ]
        selected, users = choose_slot(votes, [slot], dm_id=42, attendee_limit=2)
        self.assertEqual(selected, slot)
        self.assertEqual(users, [42, 20, 21, 50])
        with TemporaryDirectory() as directory:
            store = Store(str(Path(directory) / 'schedule.db'))
            try:
                async def verify():
                    await store.set_initial_rsvps(123, 1, users, 2)
                    actual = await store.rsvps(123, 1)
                    self.assertEqual([r['user_id'] for r in actual if r['status'] == 'confirmed'], [42, 20])
                    self.assertEqual([r['user_id'] for r in actual if r['status'] == 'waitlist'], [21, 50])
                    message = self.helpers['session_announcement']({}, self.guild, actual)
                    self.assertLess(message.index('<@42>'), message.index('<@20>'))
                asyncio.run(verify())
            finally:
                store.db.close()

    def test_dm_not_added_without_matching_vote(self):
        slot = '2026-12-05T14:00'
        votes = [{'user_id': 20, 'slot': slot, 'created_at': '2026-12-01T09:00:00+00:00'}]
        chosen, users = choose_slot(votes, [slot], dm_id=42, attendee_limit=1)
        self.assertEqual(chosen, slot)
        self.assertEqual(users, [20])


if __name__ == '__main__':
    unittest.main()
