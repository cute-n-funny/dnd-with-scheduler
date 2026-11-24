"""Test and campaign scheduler stores must never share results.

These are SQLite integration tests: no Discord token or network required.
"""
import asyncio
import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from storage import Store
from availability import ensure_schema, save_windows, read_windows
from scheduler_isolation import migrate_legacy_test_runs

ROOT = Path(__file__).resolve().parents[1]
SLOT = '2026-12-12T14:00'


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'scheduler.db')
        self.campaign = Store(self.path)
        self.test = Store(self.path + '.test')
        ensure_schema(self.campaign)
        ensure_schema(self.test)

    def tearDown(self):
        self.campaign.db.close()
        self.test.db.close()
        self.tmp.cleanup()

    def test_separate_votes_windows_rsvps_across_modes_and_servers(self):
        async def run():
            campaign = {'title':'Campaign', 'test_mode':False}
            sample = {'title':'Test Poll', 'test_mode':True}
            await self.campaign.put(101,campaign,1,'poll')
            await self.test.put(101,sample,1,'poll')
            await self.campaign.put(202,campaign,1,'poll')
            await self.test.put(202,sample,1,'poll')
            # Same guild, cycle, user, and slot are still independent.
            await save_windows(self.campaign,101,1,55,
                               [('2026-12-12','14:00',4)],[SLOT],4,8)
            await save_windows(self.test,101,1,55,
                               [('2026-12-12','14:00',1)],[SLOT],1/60,8)
            await self.campaign.set_initial_rsvps(101,1,[55],8)
            await self.test.set_initial_rsvps(101,1,[77],1)
            await self.campaign.toggle_vote(202,1,88,SLOT)
            await self.test.toggle_vote(202,1,99,SLOT)
            self.assertEqual([v['user_id'] for v in await self.campaign.votes(101,1)],[55])
            self.assertEqual([v['user_id'] for v in await self.test.votes(101,1)],[55])
            self.assertEqual((await read_windows(self.campaign,101,1))[0]['hours'],4)
            self.assertEqual((await read_windows(self.test,101,1))[0]['hours'],1)
            self.assertEqual((await self.campaign.rsvps(101,1))[0]['user_id'],55)
            self.assertEqual((await self.test.rsvps(101,1))[0]['user_id'],77)
            # Test reset/delete must not erase campaign data or another server.
            await self.test.delete(101)
            self.assertIsNone(await self.test.get(101))
            self.assertEqual(len(await self.campaign.votes(101,1)),1)
            self.assertEqual(len(await read_windows(self.campaign,101,1)),1)
            self.assertEqual((await self.campaign.rsvps(101,1))[0]['user_id'],55)
            self.assertEqual((await self.test.votes(202,1))[0]['user_id'],99)
            self.assertEqual((await self.campaign.votes(202,1))[0]['user_id'],88)
        asyncio.run(run())
        self.campaign.db.close()
        self.test.db.close()
        self.campaign=Store(self.path)
        self.test=Store(self.path+'.test')
        ensure_schema(self.campaign)
        ensure_schema(self.test)
        self.assertEqual(asyncio.run(self.campaign.votes(101,1))[0]['user_id'],55)
        self.assertIsNone(asyncio.run(self.test.get(101)))
        self.assertEqual(asyncio.run(self.test.votes(202,1))[0]['user_id'],99)

    def test_legacy_test_migrates_and_previous_campaign_is_idle(self):
        async def run():
            previous = {'title':'Original Campaign','test_mode':False,'looping':True}
            test_cfg = {'title':'Test Poll','test_mode':True,
                        'previous_normal_config': previous}
            await self.campaign.put(101,test_cfg,1,'poll')
            await save_windows(self.campaign,101,1,42,
                               [('2026-12-12','14:00',2)],[SLOT],1/60,8)
            await self.campaign.set_initial_rsvps(101,1,[42],1)
            await self.campaign.put(202,{'title':'Other Server','test_mode':False},1,'poll')
            await migrate_legacy_test_runs(self.campaign,self.test)
            test = await self.test.get(101)
            self.assertTrue(test['config']['test_mode'])
            self.assertEqual(len(await read_windows(self.test,101,1)),1)
            self.assertEqual((await self.test.votes(101,1))[0]['user_id'],42)
            self.assertEqual((await self.test.rsvps(101,1))[0]['user_id'],42)
            normal = await self.campaign.get(101)
            self.assertFalse(normal['config']['test_mode'])
            self.assertEqual(normal['config']['title'],'Original Campaign')
            self.assertEqual(normal['state'],'finished')
            self.assertEqual(normal['cycle'],0)
            self.assertEqual(await self.campaign.votes(101,1),[])
            self.assertEqual((await self.campaign.get(202))['state'],'poll')
            await migrate_legacy_test_runs(self.campaign,self.test)  # idempotent
            self.assertEqual(len(await self.test.votes(101,1)),1)
        asyncio.run(run())

    def test_legacy_without_previous_config_does_not_erase_other_guild(self):
        async def run():
            await self.campaign.put(101,{'test_mode':True,'title':'Test'},1,'poll')
            await self.campaign.put(202,{'test_mode':False,'title':'Real'},1,'poll')
            await migrate_legacy_test_runs(self.campaign,self.test)
            self.assertIsNone(await self.campaign.get(101))
            self.assertIsNotNone(await self.test.get(101))
            self.assertEqual((await self.campaign.get(202))['config']['title'],'Real')
        asyncio.run(run())

    def test_real_discord_views_select_correct_mode(self):
        script = (ROOT/'bot.py').read_text(encoding='utf-8')
        tree = ast.parse(script)
        cls = {node.name:node for node in tree.body if isinstance(node,ast.ClassDef)}
        for name in ('PollMessageView','EventMessageView','RsvpManageView','VotePanel'):
            self.assertIn(name,cls)
            code = ast.unparse(cls[name])
            self.assertIn('test_store',code)
        self.assertIn('dnd:poll:test:', script)
        self.assertIn('dnd:rsvp:test:', script)
        self.assertIn('dnd:ical:test:', script)
        ui=ast.parse((ROOT/'availability_ui.py').read_text(encoding='utf-8'))
        code=ast.unparse(next(n for n in ui.body if isinstance(n,ast.ClassDef)
                              and n.name=='AvailabilityVotePanel'))
        self.assertIn('test_store',code)
        bot_cls=cls['SchedulerBot']
        worker=next(n for n in bot_cls.body if isinstance(n,ast.AsyncFunctionDef)
                    and n.name=='worker')
        self.assertIn('test_store',ast.unparse(worker))
        migration=next(n for n in bot_cls.body if isinstance(n,ast.AsyncFunctionDef)
                       and n.name=='setup_hook')
        self.assertIn('migrate_legacy_test_runs',ast.unparse(migration))
        cleanup=next(n for n in bot_cls.body if isinstance(n,ast.AsyncFunctionDef)
                     and n.name=='cleanup')
        self.assertIn('test_mode',ast.unparse(cleanup))


    def _scheduler_method(self,name, **namespace):
        import copy
        root=ast.parse((ROOT/'bot.py').read_text(encoding='utf-8'))
        cls=next(x for x in root.body if isinstance(x,ast.ClassDef)
                 and x.name=='SchedulerBot')
        method=copy.deepcopy(next(x for x in cls.body
                       if isinstance(x,ast.AsyncFunctionDef) and x.name==name))
        method.decorator_list=[]
        bindings=dict(namespace)
        exec(compile(ast.fix_missing_locations(ast.Module(body=[method],type_ignores=[])),
                     'bot.py','exec'),bindings)
        return bindings[name]

    def test_cleanup_does_not_delete_the_other_mode_events_or_posts(self):
        deleted=[]
        event_class=SimpleNamespace(active='active')
        utcnow=lambda:datetime(2026,11,12,tzinfo=timezone.utc)
        cleanup=self._scheduler_method('cleanup',
            discord=SimpleNamespace(HTTPException=Exception,EventStatus=event_class),
            utcnow=utcnow,log=SimpleNamespace(exception=lambda *a:None))
        class Event:
            def __init__(self,id,marker):
                self.id=id;self.description=marker;self.creator_id=999
                self.status='scheduled';self.start_time=utcnow()+timedelta(days=12)
            async def delete(self,**kw):deleted.append(self.id)
        class Guild:
            id=101
            async def fetch_scheduled_events(self):
                return [Event(501,'DND-SCHEDULER:101:1'),
                        Event(502,'DND-SCHEDULER:TEST:101:1'),
                        Event(503,'DND-SCHEDULER:202:1')]
        class Bot:
            def __init__(self,normal,test):
                self.store=normal;self.test_store=test;self.user=SimpleNamespace(id=999)
                self.message_deletions=[]
            async def try_delete_message(self,guild,channel_id,message_id):
                if message_id: self.message_deletions.append(message_id)
        bot=Bot(self.campaign,self.test)
        async def run():
            await self.campaign.put(101,{'test_mode':False},1,'poll',
                                    poll_channel_id=44,poll_message_id=10101,
                                    discord_event_id=501)
            await self.test.put(101,{'test_mode':True},1,'poll',
                                poll_channel_id=44,poll_message_id=20202,
                                discord_event_id=502)
            await cleanup(bot,Guild(),test_mode=True)
            self.assertIsNotNone(await self.campaign.get(101))
            self.assertIsNone(await self.test.get(101))
            self.assertEqual(deleted,[502])
            self.assertEqual(bot.message_deletions,[20202])
            await cleanup(bot,Guild())
            self.assertIsNone(await self.campaign.get(101))
            self.assertEqual(deleted,[502,501])
        asyncio.run(run())

    def test_worker_processes_normal_and_test_runs_independently(self):
        now=datetime(2026,11,12,tzinfo=timezone.utc)
        worker=self._scheduler_method('worker',
            utcnow=lambda:now,dtread=datetime.fromisoformat,
            timedelta=timedelta,is_test=lambda c:c.get('test_mode',False),
            log=SimpleNamespace(exception=lambda *a:None))
        class Bot:
            def __init__(self,normal,test):
                self.store=normal;self.test_store=test;self.actions=[]
                self.locks={}
            def get_guild(self,guild_id):
                return SimpleNamespace(id=guild_id)
            def lock_for(self,guild_id):
                return self.locks.setdefault(guild_id,asyncio.Lock())
            async def publish_poll(self,guild,record):
                self.actions.append((guild.id,record['config']['test_mode']))
        bot=Bot(self.campaign,self.test)
        async def run():
            await self.campaign.put(101,{'test_mode':False},3,'poll_preparing')
            await self.test.put(101,{'test_mode':True},1,'poll_preparing')
            await self.campaign.put(202,{'test_mode':False},7,'poll_preparing')
            await worker(bot)
            self.assertEqual(sorted(bot.actions),[(101,False),(101,True),(202,False)])
        asyncio.run(run())


if __name__=='__main__': unittest.main()
