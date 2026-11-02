"""Regression tests for Player/Players notifications and availability panel labels.

No Discord token or connection required. Tests inspect the real bot/ui source.
"""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load_helpers():
    source = ast.parse((ROOT / 'bot.py').read_text(encoding='utf-8'))
    needed = {'players_mention', 'mention', 'is_test'}
    nodes = [node for node in source.body
             if isinstance(node, ast.FunctionDef) and node.name in needed]
    if {node.name for node in nodes} != needed:
        raise AssertionError('Missing expected announcement helpers')
    warnings = []
    ns = {'log': SimpleNamespace(warning=lambda *args: warnings.append(args))}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<bot.py>', 'exec'), ns)
    return ns, warnings


def role(name, identifier):
    return SimpleNamespace(name=name, id=identifier, mention=f'<@&{identifier}>', mentionable=True)


class PlayerPingTests(unittest.TestCase):
    def test_singular_player_role_is_not_missed(self):
        helpers, _ = load_helpers()
        guild = SimpleNamespace(id=99, roles=[role('Player', 42)])
        self.assertEqual(helpers['mention']({'ping_role_id': 99}, guild), '<@&42>')

    def test_plural_and_mixed_case_works(self):
        helpers, _ = load_helpers()
        guild = SimpleNamespace(id=99, roles=[role('pLaYeRs', 43)])
        self.assertEqual(helpers['mention']({'ping_role_id': None}, guild), '<@&43>')

    def test_prefers_original_singular_role_if_both_exist(self):
        helpers, _ = load_helpers()
        guild = SimpleNamespace(id=99, roles=[role('Players', 43), role('Player', 42)])
        self.assertEqual(helpers['mention']({'ping_role_id': 99}, guild), '<@&42>')

    def test_explicit_custom_ping_role_still_works(self):
        helpers, _ = load_helpers()
        guild = SimpleNamespace(id=99, roles=[role('Player', 42), role('D&D Alerts', 60)])
        self.assertEqual(helpers['mention']({'ping_role_id': 60}, guild), '<@&60>')

    def test_test_mode_never_pings_a_role(self):
        helpers, _ = load_helpers()
        guild = SimpleNamespace(id=99, roles=[role('Player', 42)])
        self.assertEqual(helpers['mention']({'test_mode': True, 'ping_role_id': 42}, guild), '')

    def test_missing_role_never_falls_back_to_everyone(self):
        helpers, warnings = load_helpers()
        guild = SimpleNamespace(id=99, roles=[])
        self.assertEqual(helpers['mention']({'ping_role_id': 99}, guild), '')
        self.assertTrue(warnings)

    def test_unmentionable_role_has_diagnostic_warning(self):
        helpers, warnings = load_helpers()
        unmentionable = role('Player', 42)
        unmentionable.mentionable = False
        guild = SimpleNamespace(id=99, roles=[unmentionable], me=SimpleNamespace(
            guild_permissions=SimpleNamespace(mention_everyone=False)))
        self.assertEqual(helpers['mention']({'ping_role_id': 99}, guild), '<@&42>')
        self.assertTrue(any('not mentionable' in str(args) for args in warnings))

    def test_both_poll_send_paths_explicitly_allow_role_mentions(self):
        root = ast.parse((ROOT / 'bot.py').read_text(encoding='utf-8'))
        cls = next(n for n in root.body if isinstance(n, ast.ClassDef) and n.name == 'SchedulerBot')
        fn = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'publish_poll')
        sends = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and
                 isinstance(n.func, ast.Attribute) and n.func.attr == 'send']
        self.assertEqual(len(sends), 2)  # Main post and image-upload fallback
        for call in sends:
            keys = {kw.arg for kw in call.keywords}
            self.assertIn('allowed_mentions', keys)
        args = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and
                isinstance(n.func, ast.Attribute) and n.func.attr == 'AllowedMentions']
        self.assertEqual(len(args), 1)
        flags = {kw.arg: kw.value.value for kw in args[0].keywords
                 if isinstance(kw.value, ast.Constant)}
        self.assertEqual(flags, {'everyone': False, 'roles': True, 'users': False})

    def test_new_dropdown_labels_and_button_colors(self):
        source = ast.parse((ROOT / 'availability_ui.py').read_text(encoding='utf-8'))
        cls = next(n for n in source.body if isinstance(n, ast.ClassDef)
                   and n.name == 'AvailabilityVotePanel')
        ctor = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '__init__')
        placeholders = [kw.value.value for call in ast.walk(ctor)
                        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and call.func.attr == 'Select' for kw in call.keywords
                        if kw.arg == 'placeholder' and isinstance(kw.value, ast.Constant)]
        self.assertIn('Choose your earliest start time', placeholders)
        self.assertIn('Choose how many hours you can stay', placeholders)
        buttons = next(n for n in ast.walk(ctor) if isinstance(n, ast.For)
                       and isinstance(n.target, ast.Tuple)
                       and len(n.target.elts) == 3)
        styles = {entry.elts[0].value: entry.elts[1].attr for entry in buttons.iter.elts}
        self.assertEqual(styles['Add Times'], 'success')
        self.assertEqual(styles['Remove Times'], 'danger')
        self.assertEqual(styles['Close'], 'secondary')


if __name__ == '__main__':
    unittest.main()
