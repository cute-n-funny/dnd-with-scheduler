"""Regression tests for default start times and historical Discord forum posts.

Read the actual production bot.py syntax tree, so these tests run without
Discord network access or a bot token.
"""
import ast
import asyncio
import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from zoneinfo import ZoneInfo

from logic import parse_times

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ast.parse((ROOT / 'bot.py').read_text(encoding='utf-8'))
DEFAULT_TIMES = ["14:00", "14:30", "15:00", "15:30", "16:00", "16:30",
                 "17:00", "17:30", "18:00", "18:30", "19:00"]


def get_function(name, *, method=False, **bindings):
    nodes = (next(n for n in SOURCE.body if isinstance(n, ast.ClassDef)
                  and n.name == 'SchedulerBot').body if method else SOURCE.body)
    node = copy.deepcopy(next(n for n in nodes
                              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name))
    node.decorator_list = []
    ns = dict(bindings)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
                 'bot.py', 'exec'), ns)
    return ns[name]


class TestStore:
    def __init__(self, record):
        self.record = record
        self.updated = []
        self.deleted = []

    async def get(self, guild_id):
        return self.record

    async def update(self, guild_id, **data):
        self.record.update(data)
        self.updated.append(data)

    async def delete(self, guild_id):
        self.deleted.append(guild_id)

    async def rsvps(self, guild_id, cycle):
        return []


class DummyGuild:
    id = 42
    def __init__(self):
        self.events = []
        self.params = []
        self.threads = []

    async def fetch_scheduled_events(self):
        return self.events

    async def create_scheduled_event(self, **kwargs):
        self.params.append(kwargs)
        return SimpleNamespace(id=501)

    def get_thread(self, thread_id):
        raise AssertionError('Cleanup must not look up existing forum posts')


class ForumHistoryTests(unittest.TestCase):
    def test_eleven_default_times_for_both_config_and_blank_input(self):
        config_defaults = get_function('config_defaults', TZ_NAME='America/Chicago')
        c = config_defaults(SimpleNamespace(id=42), 11, 99)
        self.assertEqual(c['times'], DEFAULT_TIMES)
        self.assertEqual(parse_times(''), DEFAULT_TIMES)
        self.assertEqual(len(c['times']), 11)
        self.assertEqual(c['poll_duration'], 2)
        self.assertEqual(c['min_votes'], 3)
        self.assertEqual(parse_times('2pm,7pm'), ['14:00', '19:00'])

    def test_cleanup_never_deletes_existing_forum_thread(self):
        cleanup = get_function('cleanup', method=True,
            discord=SimpleNamespace(HTTPException=Exception, EventStatus=SimpleNamespace(active='active')),
            utcnow=lambda: datetime(2026, 12, 1, tzinfo=timezone.utc),
            log=SimpleNamespace(exception=lambda *a:None, warning=lambda *a:None))
        guild = DummyGuild()
        record = dict(poll_channel_id=2,poll_message_id=10,
                      event_channel_id=2,event_message_id=11,forum_thread_id=777)
        store = TestStore(record)
        class Bot:
            def __init__(self):
                self.store = store
                self.user = SimpleNamespace(id=999)
                self.message_deletions = []
            async def try_delete_message(self, guild, channel_id, message_id):
                self.message_deletions.append(message_id)
        bot = Bot()
        asyncio.run(cleanup(bot,guild))
        self.assertEqual(bot.message_deletions, [10,11])
        self.assertEqual(store.deleted, [42])

    def test_test_scheduler_requires_configured_forum_but_posts_no_thread(self):
        # Every scheduler command now requires valid per-server forum settings.
        # The test still skips *creating* forum threads in create_event().
        replace = get_function('replace_scheduler', method=True,
            is_test=lambda cfg:cfg.get('test_mode',False),
            validate_config=lambda cfg:None,
            timedelta=timedelta,
            utcnow=lambda:datetime(2026,12,1,tzinfo=timezone.utc))
        class Bot:
            def __init__(self):
                self.store = TestStore(dict(state='poll_preparing'))
                self.lock = asyncio.Lock()
                self.actions=[]
            def lock_for(self, guild_id):return self.lock
            def text_channel(self,guild,config):return SimpleNamespace(id=901)
            def forum_channel(self,guild):return SimpleNamespace(id=902)
            def slots_for(self,record):return ['2026-12-10T14:00']
            async def cleanup(self,guild):self.actions.append('cleanup')
            async def prepare_poll(self,guild,config,cycle):self.actions.append('prepare')
            async def publish_poll(self,guild,record):self.actions.append('publish')
        bot=Bot()
        async def run():
            await replace(bot,SimpleNamespace(id=42),dict(test_mode=True,poll_duration=2))
        asyncio.run(run())
        self.assertEqual(bot.actions,['cleanup','prepare','publish'])

    def test_production_scheduler_still_requires_forum(self):
        replace = get_function('replace_scheduler', method=True,
            is_test=lambda cfg:cfg.get('test_mode',False))
        class Bot:
            def __init__(self):self.lock=asyncio.Lock()
            def lock_for(self,guild_id):return self.lock
            def text_channel(self,guild,config):return SimpleNamespace(id=901)
            def forum_channel(self,guild):return None
        with self.assertRaisesRegex(ValueError, 'channel-for-forum is not set'):
            asyncio.run(replace(Bot(),SimpleNamespace(id=42),dict(test_mode=False)))
        # The same required preference applies to the one-minute test command.
        with self.assertRaisesRegex(ValueError, 'channel-for-forum is not set'):
            asyncio.run(replace(Bot(),SimpleNamespace(id=42),dict(test_mode=True)))

    def make_create_event(self):
        fake_discord=SimpleNamespace(
            PrivacyLevel=SimpleNamespace(guild_only=1),
            EntityType=SimpleNamespace(external=2),
            AllowedMentions=lambda **k:SimpleNamespace(**k))
        return get_function('create_event', method=True,
            dtread=datetime.fromisoformat,
            event_title=lambda c:c['title'],
            event_link=lambda gid,eid:f'https://discord.com/events/{gid}/{eid}',
            ZoneInfo=ZoneInfo,
            discord=fake_discord,
            is_test=lambda cfg:cfg.get('test_mode',False),
            build_event_embed=lambda r,rsvps:'RSVP EMBED',
            session_announcement=lambda c,g,rsvps:'Calendar reminder',
            EventMessageView=lambda bot,record:'RSVP VIEW',
            log=SimpleNamespace(info=lambda *a:None))

    def make_bot(self, guild, record, test_mode):
        store=TestStore(record)
        thread_posts=[]
        announcements=[]
        class Bot:
            def __init__(self):
                self.store=store
                self.user=SimpleNamespace(id=555)
                self.native_events_enabled=True
            def preferences_for(self,guild):
                return {'activate_discord_events': self.native_events_enabled}
            def role_config(self,guild,config):return config
            def voice_channel(self,guild):return None
            def forum_channel(self,guild):
                if test_mode:
                    raise AssertionError('Test must not touch the forum channel')
                async def create_thread(**params):
                    thread_posts.append(params)
                    return SimpleNamespace(thread=SimpleNamespace(id=777))
                return SimpleNamespace(create_thread=create_thread)
            def text_channel(self,guild,c):
                async def send(**params):
                    announcements.append(params)
                    return SimpleNamespace(id=333)
                return SimpleNamespace(id=900,send=send)
        return Bot(),thread_posts,announcements

    def base_record(self,test_mode):
        start=datetime(2026,12,12,20,tzinfo=timezone.utc)
        return dict(config=dict(title='D&D',description='Session test',dm_id=55,
                                timezone='America/Chicago',test_mode=test_mode),
                    event_start=start.isoformat(),event_end=(start+timedelta(hours=4)).isoformat(),
                    discord_event_id=None,forum_thread_id=None,event_message_id=None,cycle=1)

    def test_test_event_is_created_without_forum_post(self):
        record=self.base_record(True)
        guild=DummyGuild()
        bot,posts,announcements=self.make_bot(guild,record,True)
        asyncio.run(self.make_create_event()(bot,guild,record))
        self.assertFalse(posts)
        self.assertIsNone(record['forum_thread_id'])
        self.assertEqual(record['discord_event_id'],501)
        self.assertEqual(record['state'],'event')
        self.assertEqual(len(announcements),1)
        self.assertEqual(guild.params[0]['location'],'Discord — test session')

    def test_real_event_creates_forum_post(self):
        record=self.base_record(False)
        guild=DummyGuild()
        bot,posts,announcements=self.make_bot(guild,record,False)
        asyncio.run(self.make_create_event()(bot,guild,record))
        self.assertEqual(len(posts),1)
        self.assertEqual(record['forum_thread_id'],777)
        self.assertEqual(len(announcements),1)
        self.assertEqual(guild.params[0]['location'],'Discord — see session forum post')

    def test_real_event_with_native_events_disabled_still_posts_forum(self):
        # Native Discord events default off, but bot RSVPs and forum posts remain.
        record=self.base_record(False)
        guild=DummyGuild()
        bot,posts,announcements=self.make_bot(guild,record,False)
        bot.native_events_enabled=False
        asyncio.run(self.make_create_event()(bot,guild,record))
        self.assertEqual(guild.params,[])
        self.assertIsNone(record['discord_event_id'])
        self.assertEqual(record['forum_thread_id'],777)
        self.assertEqual(len(posts),1)
        self.assertNotIn('Discord Event:',posts[0]['content'])
        self.assertEqual(len(announcements),1)


if __name__=='__main__':
    unittest.main()
