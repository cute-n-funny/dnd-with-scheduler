"""Offline integration tests for durable independent forum keepalives.

Run with `python -m unittest discover -s tests -v`; discord.py is not required.
"""
import ast
import __future__
import asyncio
import importlib
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import forum_poker as poker

DATE = datetime(2026, 11, 1, 18, tzinfo=timezone.utc)


class Store:
    def __init__(self, name):
        self.db = sqlite3.connect(name, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = asyncio.Lock()
        poker.ensure_schema(self)
    def close(self):self.db.close()


class FakeText:
    def __init__(self, cid):
        self.id=cid
        self.messages=[]
    async def send(self, text, **kw):
        self.messages.append(text)


class FakeForum:
    def __init__(self,cid):
        self.id=cid
        self.name='Rule Reference Forum'


class FakeMessage:
    def __init__(self, mid, content, author=999, thread=None):
        self.id=mid
        self.content=content
        self.author=SimpleNamespace(id=author)
        self.thread=thread
        self.deleted=False
        self.delete_fails=False
    async def delete(self):
        if self.delete_fails:
            raise FakeHTTP('Cannot delete temporary poke message')
        self.deleted=True
        if self.thread and self in self.thread.posts:
            self.thread.posts.remove(self)


class FakeThread:
    def __init__(self,tid,guild,parent_id,archived=False, archive_minutes=4320):
        self.id=tid;self.guild=guild;self.parent_id=parent_id
        self.name=f'Forum thread {tid}'
        self.archived=archived;self.auto_archive_duration=archive_minutes
        self.edits=[];self.posts=[];self.sent=[]
        self.history_reads=0;self.fail=False
    async def edit(self, **kwargs):
        if self.fail:raise FakeHTTP('Missing Manage Threads permission')
        self.edits.append(kwargs)
        if 'archived' in kwargs:self.archived=kwargs['archived']
        if 'auto_archive_duration' in kwargs:self.auto_archive_duration=kwargs['auto_archive_duration']
        return self
    async def fetch_message(self, mid):
        for msg in self.posts:
            if msg.id==mid:
                msg.thread=self
                return msg
        raise FakeNotFound('message deleted')
    async def history(self,limit):
        self.history_reads+=1
        for msg in list(reversed(self.posts))[:limit]:yield msg
    async def send(self,content,**kwargs):
        assert kwargs['silent'] is True
        msg=FakeMessage(len(self.sent)+1111,content,thread=self)
        self.posts.append(msg)
        self.sent.append(msg)
        return msg


class FakeHTTP(Exception):pass
class FakeNotFound(FakeHTTP):pass
FAKE_DISCORD=SimpleNamespace(Thread=FakeThread, ForumChannel=FakeForum,TextChannel=FakeText,
    HTTPException=FakeHTTP, NotFound=FakeNotFound,
    AllowedMentions=SimpleNamespace(none=lambda:object()))


class FakeGuild:
    def __init__(self,gid):
        self.id=gid;self.threads={};self.channels={}
    def get_thread(self,tid):return self.threads.get(tid)
    def get_channel(self,cid):return self.channels.get(cid)


class FakeBot:
    def __init__(self,store,guild):
        self.store=store;self.guild=guild;self.user=SimpleNamespace(id=999)
        self.settings={'command_channel_ids':[]}
        self.cached={}
    def get_channel(self,cid):return self.cached.get(cid)
    async def fetch_channel(self,cid):
        if cid in self.guild.threads:return self.guild.threads[cid]
        raise FakeNotFound('Unknown thread')
    def preferences_for(self,g):return self.settings


class ForumPokeTests(unittest.TestCase):
    def setUp(self):
        self.dir=TemporaryDirectory()
        self.path=str(Path(self.dir.name)/'pokes.db')
        self.store=Store(self.path)
        self.guild=FakeGuild(10)
        self.forum=FakeForum(42)
        self.guild.channels[self.forum.id]=self.forum
        self.thread=FakeThread(77,self.guild,self.forum.id)
        self.guild.threads[77]=self.thread
        self.bot=FakeBot(self.store,self.guild)
        self.patcher=patch.dict(sys.modules, {'discord':FAKE_DISCORD})
        self.patcher.start()
    def tearDown(self):
        self.patcher.stop()
        self.store.close()
        self.dir.cleanup()

    def test_parse_one_many_partial_bad_duplicates_and_limit(self):
        ids,errors=poker.parse_ids('77, 88, abc,,88,99')
        self.assertEqual(ids,[77,88,99]);self.assertEqual(len(errors),2)
        self.assertEqual(poker.parse_ids('   '),([],[]))
        ids,errors=poker.parse_ids(','.join(str(n) for n in range(1,44)))
        self.assertEqual(len(ids),poker.MAX_BATCH_IDS)
        self.assertTrue(errors)

    def test_database_persists_across_restart_and_is_per_guild(self):
        async def run():
            self.assertEqual(await poker.add_targets(self.store,10,[77],now=DATE),1)
            self.assertEqual(await poker.add_targets(self.store,10,[77],now=DATE),0)
            self.assertEqual(await poker.add_targets(self.store,11,[77],now=DATE),1)
            self.assertEqual(len(await poker.list_targets(self.store,10)),1)
            self.assertEqual(await poker.due_targets(self.store,DATE+timedelta(days=2)),[])
            self.assertEqual(len(await poker.due_targets(self.store,DATE+timedelta(days=3))),2)
        asyncio.run(run())
        self.store.close()
        self.store=Store(self.path)
        self.bot.store=self.store
        async def after():
            self.assertEqual(len(await poker.list_targets(self.store,10)),1)
            self.assertEqual(await poker.remove_targets(self.store,10,[77]),1)
            self.assertEqual(len(await poker.list_targets(self.store,11)),1)
        asyncio.run(after())

    def test_active_thread_refreshes_archive_timer_and_deletes_temporary_message(self):
        async def run():
            await poker.add_targets(self.store,10,[77],now=DATE)
            target=(await poker.list_targets(self.store,10))[0]
            with patch.object(poker,'utcnow',return_value=DATE):
                self.assertIsNone(await poker.poke_one(self.bot,self.guild,target))
                await poker.record_success(self.store,10,77,now=DATE)
            self.assertEqual([v['auto_archive_duration'] for v in self.thread.edits],[1440,4320])
            self.assertEqual(self.thread.auto_archive_duration,4320)
            self.assertEqual(self.thread.posts,[])
            self.assertEqual(len(self.thread.sent),1)
            self.assertTrue(self.thread.sent[0].deleted)
            self.assertEqual(self.thread.sent[0].content,'Poked on: **11/01/2026**')
            target=(await poker.list_targets(self.store,10))[0]
            self.assertIsNone(target['message_id'])
            self.assertEqual(target['next_poke_at'],(DATE+timedelta(days=3)).isoformat())
            with patch.object(poker,'utcnow',return_value=DATE+timedelta(days=3)):
                self.assertIsNone(await poker.poke_one(self.bot,self.guild,target))
            self.assertEqual(self.thread.posts,[])  # Never accumulate poke messages.
            self.assertEqual(len(self.thread.sent),2)
            self.assertTrue(all(m.deleted for m in self.thread.sent))
        asyncio.run(run())

    def test_archived_thread_unarchives_without_duration_churn(self):
        self.thread.archived=True
        async def run():
            await poker.add_targets(self.store,10,[77],now=DATE)
            with patch.object(poker,'utcnow',return_value=DATE):
                await poker.poke_one(self.bot,self.guild,(await poker.list_targets(self.store,10))[0])
        asyncio.run(run())
        self.assertFalse(self.thread.archived)
        self.assertEqual(len(self.thread.edits),1)
        self.assertEqual(self.thread.edits[0]['archived'],False)

    def test_previous_saved_poke_is_deleted_without_touching_unrelated_messages(self):
        previous=FakeMessage(2222,'Poked on: **10/29/2026**',999,thread=self.thread)
        unrelated=FakeMessage(3333,'Do not delete campaign rules',999,thread=self.thread)
        self.thread.posts=[previous,unrelated]
        async def run():
            await poker.add_targets(self.store,10,[77],now=DATE)
            await poker.record_sent(self.store,10,77,2222)
            target=(await poker.list_targets(self.store,10))[0]
            with patch.object(poker,'utcnow',return_value=DATE):
                await poker.run_one(self.bot,self.guild,target)
            self.assertIsNone((await poker.list_targets(self.store,10))[0]['message_id'])
        asyncio.run(run())
        self.assertTrue(previous.deleted)
        self.assertFalse(unrelated.deleted)
        self.assertEqual(self.thread.posts,[unrelated])
        self.assertEqual(len(self.thread.sent),1)
        self.assertTrue(self.thread.sent[0].deleted)

    def test_delete_failure_retries_and_cleans_up_stale_message(self):
        channel=FakeText(50)
        self.guild.channels[50]=channel
        self.bot.settings['command_channel_ids']=[50]
        self.thread.name='House Rules'
        original_send=self.thread.send
        async def send_failing_delete(content,**kwargs):
            message=await original_send(content,**kwargs)
            message.delete_fails=True
            return message
        self.thread.send=send_failing_delete
        async def run():
            await poker.add_targets(self.store,10,[77],now=DATE)
            with patch.object(poker,'utcnow',return_value=DATE):
                ok,error=await poker.run_one(self.bot,self.guild,(await poker.list_targets(self.store,10))[0])
            self.assertFalse(ok)
            self.assertIn('Cannot delete',error)
            self.assertIn('House Rules',channel.messages[0])
            self.assertIn('Rule Reference Forum',channel.messages[0])
            self.assertIn('77',channel.messages[0])
            row=(await poker.list_targets(self.store,10))[0]
            self.assertEqual(row['message_id'],1111)
            self.assertEqual(row['next_poke_at'],(DATE+timedelta(hours=1)).isoformat())
            self.assertEqual(len(self.thread.posts),1)
            self.thread.sent[0].delete_fails=False
            self.thread.send=original_send
            with patch.object(poker,'utcnow',return_value=DATE+timedelta(hours=1)):
                ok,error=await poker.run_one(self.bot,self.guild,row)
            self.assertTrue(ok)
            self.assertIsNone(error)
            self.assertEqual(self.thread.posts,[])
            self.assertTrue(all(m.deleted for m in self.thread.sent))
            self.assertIsNone((await poker.list_targets(self.store,10))[0]['message_id'])
        with self.assertLogs('dnd_scheduler.forum_poker',level='ERROR'):
            asyncio.run(run())

    def test_error_for_deleted_thread_uses_id_fallback(self):
        channel=FakeText(50)
        self.guild.channels[50]=channel
        self.bot.settings['command_channel_ids']=[50]
        self.guild.threads.pop(77)
        async def run():
            await poker.add_targets(self.store,10,[77],now=DATE)
            await poker.run_one(self.bot,self.guild,(await poker.list_targets(self.store,10))[0])
        with self.assertLogs('dnd_scheduler.forum_poker',level='ERROR'):
            asyncio.run(run())
        self.assertIn('77',channel.messages[0])
        self.assertIn('<#77>',channel.messages[0])

    def test_reject_other_guild_and_nonforum_thread(self):
        async def run():
            self.thread.parent_id=99
            with self.assertRaisesRegex(ValueError,'not a post'):
                await poker.resolve_forum_thread(self.bot,self.guild,77)
            self.thread.parent_id=42
            self.thread.guild=FakeGuild(20)
            with self.assertRaisesRegex(ValueError,'belonging'):
                await poker.resolve_forum_thread(self.bot,self.guild,77)
        asyncio.run(run())

    def test_fail_retries_one_hour_and_alerts_first_valid_configured_channel(self):
        channel=FakeText(50)
        self.guild.channels[50]=channel
        self.bot.settings['command_channel_ids']=[99,50]
        self.thread.fail=True
        async def run():
            await poker.add_targets(self.store,10,[77],now=DATE)
            with patch.object(poker,'utcnow',return_value=DATE):
                ok,error=await poker.run_one(self.bot,self.guild,(await poker.list_targets(self.store,10))[0])
            self.assertFalse(ok)
            self.assertIn('permission',error)
            self.assertEqual(len(channel.messages),1)
            self.assertIn('Forum thread 77',channel.messages[0])
            self.assertIn('Rule Reference Forum',channel.messages[0])
            self.assertIn('77',channel.messages[0])
            row=(await poker.list_targets(self.store,10))[0]
            self.assertEqual(row['next_poke_at'],(DATE+timedelta(hours=1)).isoformat())
            with patch.object(poker,'utcnow',return_value=DATE):
                await poker.run_one(self.bot,self.guild,row)
            self.assertEqual(len(channel.messages),1) # Repeated error is not spammed.
        with self.assertLogs('dnd_scheduler.forum_poker', level='ERROR'):
            asyncio.run(run())

    def test_command_partial_add_immediate_remove_and_no_changes(self):
        # Execute the real undecorated slash command with fake Discord objects.
        root=ast.parse((Path(__file__).resolve().parents[1]/'bot.py').read_text(encoding='utf-8'))
        import copy
        node=copy.deepcopy(next(n for n in root.body if isinstance(n,ast.AsyncFunctionDef)
                                and n.name=='poke_forums'))
        node.decorator_list=[]
        class Response:
            def __init__(self):self.deferred=[]
            async def defer(self,**kw):self.deferred.append(kw)
        class Followup:
            def __init__(self):self.messages=[]
            async def send(self,text,**kw):self.messages.append(text)
        class Inter:
            def __init__(self,guild):
                self.guild=guild
                self.response=Response()
                self.followup=Followup()
        async def gate(interaction,require_preferences=True):
            self.assertFalse(require_preferences)
            return True
        self.bot.poke_lock_for=lambda _:asyncio.Lock()
        namespace=dict(
            admin_gate=gate,bot=self.bot,discord=FAKE_DISCORD,
            parse_poke_ids=poker.parse_ids,resolve_forum_thread=poker.resolve_forum_thread,
            remove_poke_targets=poker.remove_targets,add_poke_targets=poker.add_targets,
            list_poke_targets=poker.list_targets,poke_one_target=poker.run_one)
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])),
                     'bot.py','exec',flags=__future__.annotations.compiler_flag),namespace)
        command=namespace['poke_forums']
        another=FakeThread(88,self.guild,self.forum.id)
        self.thread.name='Campaign Rules'
        another.name='Character Creation'
        self.guild.threads[88]=another
        async def run():
            interaction=Inter(self.guild)
            await command(interaction,add='77,garbage,88',ping_now=True)
            self.assertIn('2 added',interaction.followup.messages[-1])
            self.assertIn('Invalid thread ID',interaction.followup.messages[-1])
            self.assertIn('Campaign Rules (ID: `77`)', interaction.followup.messages[-1])
            self.assertIn('Character Creation (ID: `88`)', interaction.followup.messages[-1])
            self.assertEqual(len(await poker.list_targets(self.store,10)),2)
            self.assertEqual(len(self.thread.posts),0)
            self.assertEqual(len(another.posts),0)
            self.assertTrue(self.thread.sent[0].deleted)
            self.assertTrue(another.sent[0].deleted)
            interaction=Inter(self.guild)
            await command(interaction)
            self.assertIn('No changes made.',interaction.followup.messages[-1])
            self.assertIn('Campaign Rules',interaction.followup.messages[-1])
            interaction=Inter(self.guild)
            await command(interaction,remove='77')
            self.assertIn('1 removed',interaction.followup.messages[-1])
            self.assertIn('Character Creation',interaction.followup.messages[-1])
            self.assertNotIn('Campaign Rules',interaction.followup.messages[-1])
            self.assertEqual([r['thread_id'] for r in await poker.list_targets(self.store,10)],[88])
            # A deleted or inaccessible thread should not break the status response.
            self.guild.threads.pop(88)
            interaction=Inter(self.guild)
            await command(interaction)
            self.assertIn('Unavailable or inaccessible (ID: `88`)',interaction.followup.messages[-1])
        asyncio.run(run())

    def test_command_and_worker_are_separate_from_forum_preferences(self):
        source=(Path(__file__).resolve().parents[1]/'bot.py').read_text(encoding='utf-8')
        tree=ast.parse(source)
        commands={n.name:n for n in tree.body if isinstance(n,ast.AsyncFunctionDef)}
        self.assertIn('poke_forums',commands)
        command=commands['poke_forums']
        argnames=[a.arg for a in command.args.args]
        self.assertIn('add',argnames)
        self.assertIn('remove',argnames)
        self.assertIn('ping_now',argnames)
        text=ast.unparse(command)
        self.assertIn('require_preferences=False',text)
        self.assertNotIn('forum_id',text)
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='SchedulerBot')
        self.assertTrue(any(isinstance(n,ast.AsyncFunctionDef) and n.name=='poke_worker' for n in cls.body))
        docker=(Path(__file__).resolve().parents[1]/'Dockerfile').read_text(encoding='utf-8')
        self.assertIn('forum_poker.py',docker)


if __name__=='__main__':unittest.main()
