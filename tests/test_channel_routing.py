"""Test the real channel-routing methods without requiring a Discord connection.

Our GitHub unit-test workflow intentionally does not install discord.py. The
methods are extracted from bot.py so regressions in the production methods
are still caught by these tests.
"""
import ast
import logging
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch


class TextChannel:
    def __init__(self, channel_id, name):
        self.id = channel_id
        self.name = name


class Guild:
    def __init__(self, channels):
        self.text_channels = channels
        self.channels_by_id = {c.id: c for c in channels}

    def get_channel(self, channel_id):
        return self.channels_by_id.get(channel_id)


def load_channel_router():
    script = ast.parse((Path(__file__).resolve().parents[1] / 'bot.py').read_text(encoding='utf-8'))
    cls = next(n for n in script.body if isinstance(n, ast.ClassDef) and n.name == 'SchedulerBot')
    selected = [n for n in cls.body if isinstance(n, ast.FunctionDef)
                and n.name in {'scheduler_channel', 'text_channel'}]
    namespace = {'os': os, 'discord': SimpleNamespace(TextChannel=TextChannel),
                 'log': logging.getLogger('test_channel_routing')}
    exec(compile(ast.Module(body=selected, type_ignores=[]), 'bot.py', 'exec'), namespace)
    return type('ChannelRouter', (), {name: namespace[name]
                                    for name in ('scheduler_channel', 'text_channel')})()


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.router = load_channel_router()
        self.private = TextChannel(101, 'scheduler')
        self.public = TextChannel(202, '⚠️scheduling-&-voting⚠️')
        self.guild = Guild([self.private, self.public])

    def test_channel_names_and_old_saved_command_channel_are_safe(self):
        with patch.dict(os.environ, {'SCHEDULER_CHANNEL_ID': '', 'SCHEDULING_CHANNEL_ID': ''}):
            self.assertIs(self.router.scheduler_channel(self.guild), self.private)
            # Old config may have stored command channel as poll channel.
            self.assertIs(self.router.text_channel(self.guild, {'channel_id': 101}), self.public)

    def test_explicit_ids_are_independent(self):
        with patch.dict(os.environ, {'SCHEDULER_CHANNEL_ID': '101', 'SCHEDULING_CHANNEL_ID': '202'}):
            self.assertIs(self.router.scheduler_channel(self.guild), self.private)
            self.assertIs(self.router.text_channel(self.guild, {'channel_id': 101}), self.public)

    def test_public_poll_channel_must_not_be_private_control_channel(self):
        with patch.dict(os.environ, {'SCHEDULER_CHANNEL_ID': '101', 'SCHEDULING_CHANNEL_ID': '101'}):
            self.assertIsNone(self.router.text_channel(self.guild, {'channel_id': 101}))

    def test_saved_public_channel_can_have_custom_name(self):
        custom = TextChannel(303, 'adventure-planning')
        guild = Guild([self.private, custom])
        with patch.dict(os.environ, {'SCHEDULER_CHANNEL_ID': '', 'SCHEDULING_CHANNEL_ID': ''}):
            self.assertIs(self.router.text_channel(guild, {'channel_id': 303}), custom)


if __name__ == '__main__':
    unittest.main()
