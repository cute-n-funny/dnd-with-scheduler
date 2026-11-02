"""Persistent, per-guild routing and notification preferences.

This table is deliberately separate from schedulers so /end_scheduler and
/reset_scheduler never erase preferences.  All reads/writes use Store.lock.
"""
from __future__ import annotations

import json


def defaults():
    return {
        'announcements_id': None,
        'forum_id': None,
        'command_channel_ids': [],
        'activate_discord_events': False,
        'ping_role_id': None,
    }


def ensure_schema(store):
    store.db.execute('''
        CREATE TABLE IF NOT EXISTS guild_preferences (
            guild_id INTEGER PRIMARY KEY,
            announcements_id INTEGER,
            forum_id INTEGER,
            command_channel_ids TEXT NOT NULL DEFAULT '[]',
            activate_discord_events INTEGER NOT NULL DEFAULT 0,
            ping_role_id INTEGER
        )
    ''')


def from_row(row):
    if row is None:
        return defaults()
    out = defaults()
    out.update({
        'announcements_id': row['announcements_id'],
        'forum_id': row['forum_id'],
        'command_channel_ids': json.loads(row['command_channel_ids']),
        'activate_discord_events': bool(row['activate_discord_events']),
        'ping_role_id': row['ping_role_id'],
    })
    return out


async def load_all(store):
    async with store.lock:
        return {row['guild_id']: from_row(row)
                for row in store.db.execute('SELECT * FROM guild_preferences')}


async def get(store, guild_id):
    async with store.lock:
        return from_row(store.db.execute(
            'SELECT * FROM guild_preferences WHERE guild_id=?', (guild_id,)).fetchone())


async def update(store, guild_id, *, fields=None, toggle_command_id=None):
    """Atomically apply only validated fields; optionally toggle a command channel.

    Invalid fields should be filtered *before* calling this function so valid
    attributes survive an otherwise partially invalid /set_preferences call.
    """
    allowed = {'announcements_id', 'forum_id', 'activate_discord_events', 'ping_role_id'}
    fields = dict(fields or {})
    if set(fields) - allowed:
        raise ValueError('Unknown preference keys')
    async with store.lock:
        store.db.execute('BEGIN IMMEDIATE')
        try:
            store.db.execute('INSERT OR IGNORE INTO guild_preferences (guild_id) VALUES (?)', (guild_id,))
            data = from_row(store.db.execute(
                'SELECT * FROM guild_preferences WHERE guild_id=?', (guild_id,)).fetchone())
            data.update(fields)
            if toggle_command_id is not None:
                ids = data['command_channel_ids'][:]
                if toggle_command_id in ids:
                    ids.remove(toggle_command_id)
                else:
                    ids.append(toggle_command_id)
                data['command_channel_ids'] = ids
            store.db.execute('''
                UPDATE guild_preferences SET announcements_id=?, forum_id=?,
                  command_channel_ids=?, activate_discord_events=?, ping_role_id=?
                WHERE guild_id=?
            ''', (data['announcements_id'], data['forum_id'],
                  json.dumps(data['command_channel_ids']),
                  int(data['activate_discord_events']), data['ping_role_id'], guild_id))
            store.db.execute('COMMIT')
        except Exception:
            store.db.execute('ROLLBACK')
            raise
    return data
