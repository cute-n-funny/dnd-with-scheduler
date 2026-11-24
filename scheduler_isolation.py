"""Keep the one-minute test scheduler isolated from recurring campaigns.

The campaign retains DATABASE_PATH, and the test scheduler uses a separate SQLite
file in the same Fly volume.  Legacy versions placed test runs in the campaign
file; migrate those rows once before restoring persistent Discord views.
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger('dnd_scheduler.isolation')


async def migrate_legacy_test_runs(campaign_store, test_store):
    """Move old test-mode rows into the test database without copying guild prefs.

    Preserve a prior campaign *configuration* as a finished record when it was
    embedded in the old test config. Previously deleted campaign ballots cannot
    be reconstructed; this migration never pretends otherwise.

    Test data is committed in the destination before the source is removed. A
    retry after a crash is safe: already migrated test data is never overwritten.
    """
    async with campaign_store.lock:
        async with test_store.lock:
            rows = campaign_store.db.execute(
                'SELECT * FROM schedulers ORDER BY guild_id').fetchall()
            for row in rows:
                config = json.loads(row['config'])
                if not config.get('test_mode'):
                    continue
                guild_id = row['guild_id']
                exists = test_store.db.execute(
                    'SELECT 1 FROM schedulers WHERE guild_id=?',
                    (guild_id,)).fetchone()
                if exists is None:
                    test_store.db.execute('BEGIN IMMEDIATE')
                    try:
                        for table in ('schedulers', 'votes', 'rsvps', 'availability_windows'):
                            cols = [r[1] for r in campaign_store.db.execute(
                                f'PRAGMA table_info({table})')]
                            if not cols:
                                continue
                            values = campaign_store.db.execute(
                                f'SELECT {",".join(cols)} FROM {table} WHERE guild_id=?',
                                (guild_id,)).fetchall()
                            if values:
                                placeholders = ','.join('?' for _ in cols)
                                test_store.db.executemany(
                                    f'INSERT OR IGNORE INTO {table} ({",".join(cols)}) '
                                    f'VALUES ({placeholders})',
                                    [tuple(v) for v in values])
                        test_store.db.execute('COMMIT')
                    except Exception:
                        test_store.db.execute('ROLLBACK')
                        raise
                # Do not disturb other guilds, preferences, forum pokes, or the
                # test destination. Restoring the old campaign means *finished*,
                # never a live poll with missing ballots or a doubled reminder.
                previous = config.get('previous_normal_config')
                campaign_store.db.execute('BEGIN IMMEDIATE')
                try:
                    for table in ('availability_windows', 'votes', 'rsvps', 'schedulers'):
                        campaign_store.db.execute(
                            f'DELETE FROM {table} WHERE guild_id=?', (guild_id,))
                    if isinstance(previous, dict) and previous:
                        previous = {**previous, 'test_mode': False}
                        campaign_store.db.execute(
                            'INSERT INTO schedulers (guild_id,cycle,state,config) '
                            'VALUES (?,?,?,?)',
                            (guild_id, 0, 'finished', json.dumps(previous)))
                    campaign_store.db.execute('COMMIT')
                except Exception:
                    campaign_store.db.execute('ROLLBACK')
                    raise
                log.info('Migrated legacy test scheduler for guild %s', guild_id)
