"""Per-server settings persistence, partial updates, routing and command gates."""
import ast
import __future__
import asyncio
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from guild_preferences import ensure_schema, get, load_all, update, defaults

ROOT = Path(__file__).resolve().parents[1]


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = asyncio.Lock()
        self.db.execute('CREATE TABLE IF NOT EXISTS schedulers (guild_id INTEGER PRIMARY KEY)')
        ensure_schema(self)


class FakeChannel:
    def __init__(self, id, guild=None):
        self.id, self.guild = id, guild
        self.mention = f'<#{id}>'


class TextChannel(FakeChannel): pass
class ForumChannel(FakeChannel): pass
class VoiceChannel(FakeChannel): pass


def guild(gid, *channels):
    obj = SimpleNamespace(id=gid, roles=[], channels={})
    obj.get_channel = lambda x: obj.channels.get(x)
    for channel in channels:
        channel.guild = obj
        obj.channels[channel.id] = channel
    return obj


def source_function(name, *, method=False, globals_=None):
    tree = ast.parse((ROOT / 'bot.py').read_text(encoding='utf-8'))
    nodes = tree.body
    if method:
        nodes = next(n.body for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'SchedulerBot')
    node = next(n for n in nodes if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    import copy
    node = copy.deepcopy(node)
    node.decorator_list = []
    namespace = dict(globals_ or {})
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), 'bot.py','exec', flags=__future__.annotations.compiler_flag),namespace)
    return namespace[name]


class PrefTests(unittest.TestCase):
    def test_defaults_and_restart_persistence_and_isolation(self):
        with TemporaryDirectory() as folder:
            path=str(Path(folder)/'db.sqlite')
            a=Store(path)
            try:
                async def first():
                    self.assertEqual(await get(a, 101), defaults())
                    await update(a, 101, fields={'announcements_id':20,'forum_id':21,
                                                  'activate_discord_events':True,'ping_role_id':55},
                                 toggle_command_id=30)
                    await update(a, 102, fields={'announcements_id':40,'forum_id':41})
                    # Like /end_scheduler: delete the active schedule record only.
                    a.db.execute('INSERT INTO schedulers VALUES (101)')
                    a.db.execute('DELETE FROM schedulers WHERE guild_id=101')
                asyncio.run(first())
            finally:a.db.close()
            b=Store(path)
            try:
                async def second():
                    prefs=await load_all(b)
                    self.assertEqual(prefs[101]['announcements_id'],20)
                    self.assertEqual(prefs[101]['forum_id'],21)
                    self.assertEqual(prefs[101]['command_channel_ids'],[30])
                    self.assertTrue(prefs[101]['activate_discord_events'])
                    self.assertEqual(prefs[101]['ping_role_id'],55)
                    self.assertEqual(prefs[102]['announcements_id'],40)
                    self.assertIsNone(prefs[102]['ping_role_id'])
                asyncio.run(second())
            finally:b.db.close()

    def test_toggle_and_independent_partial_update(self):
        store=Store(':memory:')
        try:
            async def check():
                await update(store,3,fields={'announcements_id':101},toggle_command_id=7)
                await update(store,3,fields={'forum_id':102},toggle_command_id=8)
                await update(store,3,toggle_command_id=7)
                self.assertEqual((await get(store,3))['command_channel_ids'],[8])
                self.assertEqual((await get(store,3))['announcements_id'],101)
                self.assertEqual((await get(store,3))['forum_id'],102)
                await update(store,3,fields={'activate_discord_events':False})
                self.assertFalse((await get(store,3))['activate_discord_events'])
            asyncio.run(check())
        finally:store.db.close()

    def test_missing_required_preferences_and_deleted_channels(self):
        d=SimpleNamespace(TextChannel=TextChannel,ForumChannel=ForumChannel)
        fn=source_function('missing_preferences',globals_={'discord':d})
        g=guild(1,TextChannel(7),ForumChannel(8))
        self.assertEqual(fn(g,defaults()), ['channel-for-announcements','channel-for-forum'])
        self.assertEqual(fn(g,{**defaults(),'announcements_id':7,'forum_id':8}),[])
        self.assertEqual(fn(g,{**defaults(),'announcements_id':7,'forum_id':999}),['channel-for-forum'])
        self.assertEqual(fn(g,{**defaults(),'announcements_id':8,'forum_id':7}),['channel-for-announcements','channel-for-forum'])

    def test_channel_routing_from_per_guild_cache(self):
        ns={'discord':SimpleNamespace(TextChannel=TextChannel,ForumChannel=ForumChannel),
            'preference_defaults':defaults}
        fcns={key:source_function(key,method=True,globals_=ns) for key in
              ('scheduler_channel','text_channel','forum_channel','preferences_for')}
        from types import MethodType
        class Router:pass
        router=Router()
        for name,f in fcns.items():setattr(router,name,MethodType(f,router))
        x=guild(1,TextChannel(10),TextChannel(20),ForumChannel(30))
        router.preferences_cache={1:{**defaults(),'announcements_id':20,'forum_id':30,'command_channel_ids':[10]}}
        self.assertEqual(router.scheduler_channel(x).id,10)
        self.assertEqual(router.text_channel(x).id,20)
        self.assertEqual(router.forum_channel(x).id,30)
        router.preferences_cache[1]['announcements_id']=999
        self.assertIsNone(router.text_channel(x))
        self.assertIsNone(router.text_channel(guild(2,TextChannel(20))))

    def test_invalid_forum_does_not_block_valid_announcements(self):
        store=Store(':memory:')
        gg=guild(4,TextChannel(10),VoiceChannel(11))
        class Bot:
            def __init__(self):self.store=store;self.preferences_cache={}
        bot=Bot()
        discord=SimpleNamespace(TextChannel=TextChannel,ForumChannel=ForumChannel,
                                Role=type('Role',(object,),{}),
                                AllowedMentions=SimpleNamespace(none=lambda:None))
        gate=async_func=lambda i,require_preferences=False: asyncio.sleep(0,result=True)
        missing=source_function('missing_preferences',globals_={'discord':discord})
        fn=source_function('set_preferences',globals_={
            'bot':bot,'admin_gate':gate,'discord':discord,
            'update_preferences':update,'get_preferences':get,'missing_preferences':missing})
        class Response:
            content=None
            async def send_message(self,content,**kwargs):self.content=content
        inter=SimpleNamespace(guild=gg,response=Response())
        try:
            async def check():
                await fn(inter,channel_for_announcements=gg.channels[10],channel_for_forum=gg.channels[11])
                p=await get(store,4)
                self.assertEqual(p['announcements_id'],10)
                self.assertIsNone(p['forum_id'])
                self.assertIn('channel-for-forum is not valid',inter.response.content)
                self.assertIn('Required preferences not set',inter.response.content)
            asyncio.run(check())
        finally:store.db.close()

    def test_admin_gate_blocks_missing_prefs_and_nonwhitelisted_channel(self):
        store=Store(':memory:')
        gg=guild(22,TextChannel(10),TextChannel(11),ForumChannel(12))
        class Bot:
            def __init__(self):self.store=store;self.preferences_cache={}
        bot=Bot()
        discord=SimpleNamespace(TextChannel=TextChannel,ForumChannel=ForumChannel)
        missing=source_function('missing_preferences',globals_={'discord':discord})
        admin_gate=source_function('admin_gate',globals_={
            'is_admin':lambda u:True,'discord':discord,'bot':bot,
            'get_preferences':get,'missing_preferences':missing})
        class Response:
            message=None
            async def send_message(self,message,**kw):self.message=message
        def inter(channel):
            return SimpleNamespace(guild=gg,user=object(),channel=channel,channel_id=channel.id,
                                   response=Response())
        try:
            async def check():
                empty=inter(gg.channels[10]);self.assertFalse(await admin_gate(empty))
                self.assertIn('Use `/set_preferences`',empty.response.message)
                await update(store,22,fields={'announcements_id':10,'forum_id':12},toggle_command_id=10)
                good=inter(gg.channels[10]);self.assertTrue(await admin_gate(good))
                wrong=inter(gg.channels[11]);self.assertFalse(await admin_gate(wrong))
                self.assertIn('only be used in',wrong.response.message)
                gg.channels.pop(10)
                # Simulate valid announcements now moved; stale command channel
                # must not lock the administrator out of /set_preferences.
                await update(store,22,fields={'announcements_id':11})
                another=inter(gg.channels[11]);self.assertTrue(await admin_gate(another))
            asyncio.run(check())
        finally:store.db.close()

    def test_role_none_is_no_ping_and_events_default_disabled(self):
        text=(ROOT/'bot.py').read_text(encoding='utf-8')
        self.assertIn("self.preferences_for(guild)['activate_discord_events']",text)
        fn=source_function('mention',globals_={'is_test':lambda c:c.get('test_mode',False)})
        gg=guild(1)
        gg.get_role=lambda _id:None
        self.assertEqual(fn({'ping_role_id':None},gg),'')
        self.assertFalse(defaults()['activate_discord_events'])
        self.assertEqual(fn({'ping_role_id':7},gg),'')
        self.assertEqual(fn({'ping_role_id':7,'test_mode':True},gg),'')


if __name__ == '__main__':unittest.main()
