"""Migration tests: remove obsolete guild-scoped command copies, retain global ones.

No Discord connection or bot token is necessary.
"""
import ast
import asyncio
import copy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from storage import Store

ROOT = Path(__file__).resolve().parents[1]


def cleanup_method():
    source = ast.parse((ROOT / 'bot.py').read_text(encoding='utf-8'))
    cls = next(node for node in source.body
               if isinstance(node, ast.ClassDef) and node.name == 'SchedulerBot')
    node = copy.deepcopy(next(node for node in cls.body
                              if isinstance(node, ast.AsyncFunctionDef)
                              and node.name == 'cleanup_legacy_guild_commands'))
    node.decorator_list = []
    class FakeHTTPException(Exception):
        pass
    logs = []
    ns = {'discord': SimpleNamespace(Object=lambda id: SimpleNamespace(id=id),
                                     HTTPException=FakeHTTPException),
          'log': SimpleNamespace(info=lambda *x: logs.append(('info', x)),
                                 warning=lambda *x: logs.append(('warning', x)),
                                 exception=lambda *x: logs.append(('exception', x)))}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
                 'bot.py', 'exec'), ns)
    return ns['cleanup_legacy_guild_commands'], FakeHTTPException, logs


class FakeTree:
    def __init__(self, *, locally_scoped=False, exception=None):
        self.calls = []
        self.locally_scoped = locally_scoped
        self.exception = exception
        self.global_commands = ['start_scheduler', 'end_scheduler', 'scheduler_status']
    def get_commands(self, *, guild):
        self.calls.append(('get', guild.id))
        return ['special_guild_command'] if self.locally_scoped else []
    def clear_commands(self, *, guild):
        self.calls.append(('clear', guild.id))
    async def sync(self, *, guild):
        self.calls.append(('sync', guild.id))
        if self.exception:
            raise self.exception('simulated API rejection')
        return []


class SlashCleanupTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.path = str(Path(self.folder.name) / 'scheduler.db')
        self.store = Store(self.path)
        self.method, self.HTTPException, self.logs = cleanup_method()
    def tearDown(self):
        self.store.db.close()
        self.folder.cleanup()

    def bot(self, *, tree=None):
        return SimpleNamespace(store=self.store, tree=tree or FakeTree())

    def test_removes_old_guild_commands_without_touching_global_commands(self):
        async def run():
            b = self.bot()
            await self.method(b, SimpleNamespace(id=101))
            self.assertEqual(b.tree.calls, [('get', 101), ('clear', 101), ('sync', 101)])
            self.assertEqual(b.tree.global_commands, ['start_scheduler', 'end_scheduler', 'scheduler_status'])
            self.assertTrue(await self.store.legacy_commands_cleared(101))
            await self.method(b, SimpleNamespace(id=101))
            self.assertEqual(len(b.tree.calls), 3)  # Exactly one migration per guild.
        asyncio.run(run())

    def test_each_guild_is_independent(self):
        async def run():
            b = self.bot()
            await self.method(b, SimpleNamespace(id=101))
            self.assertFalse(await self.store.legacy_commands_cleared(202))
            await self.method(b, SimpleNamespace(id=202))
            self.assertEqual([call for call in b.tree.calls if call[0]=='sync'],
                             [('sync', 101), ('sync', 202)])
            self.assertTrue(await self.store.legacy_commands_cleared(202))
        asyncio.run(run())

    def test_marker_persists_through_restart_without_resetting_schedules(self):
        async def run():
            await self.method(self.bot(), SimpleNamespace(id=101))
        asyncio.run(run())
        self.store.db.close()
        replacement = Store(self.path)
        try:
            self.store = replacement
            async def check():
                self.assertTrue(await self.store.legacy_commands_cleared(101))
                b = self.bot()
                await self.method(b, SimpleNamespace(id=101))
                self.assertEqual(b.tree.calls, [])
            asyncio.run(check())
        finally:
            replacement.db.close()

    def test_failure_does_not_mark_guild_and_retries(self):
        async def run():
            tree = FakeTree(exception=self.HTTPException)
            b = self.bot(tree=tree)
            await self.method(b, SimpleNamespace(id=101))
            self.assertFalse(await self.store.legacy_commands_cleared(101))
            self.assertEqual(len([x for x in tree.calls if x[0]=='sync']), 1)
            tree.exception = None
            await self.method(b, SimpleNamespace(id=101))
            self.assertTrue(await self.store.legacy_commands_cleared(101))
            self.assertEqual(len([x for x in tree.calls if x[0]=='sync']), 2)
        asyncio.run(run())

    def test_explicit_new_guild_commands_are_preserved(self):
        async def run():
            b = self.bot(tree=FakeTree(locally_scoped=True))
            await self.method(b, SimpleNamespace(id=101))
            self.assertEqual(b.tree.calls, [('get', 101)])
            self.assertFalse(await self.store.legacy_commands_cleared(101))
        asyncio.run(run())

    def test_global_sync_kept_and_migration_runs_even_without_preferences(self):
        tree = ast.parse((ROOT/'bot.py').read_text(encoding='utf-8'))
        bot_class = next(node for node in tree.body
                         if isinstance(node, ast.ClassDef) and node.name=='SchedulerBot')
        setup = next(n for n in bot_class.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='setup_hook')
        ready = next(n for n in bot_class.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='on_ready')
        join = next(n for n in bot_class.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='on_guild_join')
        self.assertIn('await self.tree.sync()', ast.unparse(setup))
        source=ast.unparse(ready)
        self.assertLess(source.index('await self.cleanup_legacy_guild_commands(guild)'),
                        source.index('channel = self.scheduler_channel(guild)'))
        self.assertIn('await self.cleanup_legacy_guild_commands(guild)', ast.unparse(join))


if __name__ == '__main__':
    unittest.main()
