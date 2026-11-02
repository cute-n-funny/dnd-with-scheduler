"""Independent three-day forum keepalive with one reusable bot-authored message.

The scheduler and guild_preferences forum_id do not participate. These records
survive scheduler resets and Fly restarts, using the same persisted SQLite volume.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

log = logging.getLogger('dnd_scheduler.forum_poker')
INTERVAL = timedelta(days=3)
RETRY = timedelta(hours=1)
MAX_BATCH_IDS = 40


def utcnow():
    return datetime.now(timezone.utc)


def ensure_schema(store):
    store.db.execute('''
        CREATE TABLE IF NOT EXISTS forum_pokes (
            guild_id INTEGER NOT NULL,
            thread_id INTEGER NOT NULL,
            message_id INTEGER,
            last_poked_at TEXT,
            next_poke_at TEXT NOT NULL,
            last_error TEXT,
            PRIMARY KEY (guild_id, thread_id)
        )
    ''')


def parse_ids(value):
    """Accept a single Discord thread ID or comma-delimited IDs; keep valid IDs."""
    if not value or not value.strip():
        return [], []
    valid, errors = [], []
    for raw in value.split(','):
        word = raw.strip()
        if not word or not re.fullmatch(r'[0-9]{1,22}', word) or int(word) == 0:
            errors.append(f'Invalid thread ID: {word or "(empty)"}')
            continue
        if int(word) not in valid:
            if len(valid) >= MAX_BATCH_IDS:
                errors.append(f'Only the first {MAX_BATCH_IDS} unique IDs are processed at once.')
                break
            valid.append(int(word))
    return valid, errors


async def list_targets(store, guild_id):
    async with store.lock:
        return [dict(r) for r in store.db.execute(
            'SELECT * FROM forum_pokes WHERE guild_id=? ORDER BY thread_id', (guild_id,))]


async def due_targets(store, now=None):
    stamp = (now or utcnow()).isoformat()
    async with store.lock:
        return [dict(r) for r in store.db.execute(
            'SELECT * FROM forum_pokes WHERE next_poke_at<=? ORDER BY next_poke_at,guild_id,thread_id',
            (stamp,))]


async def add_targets(store, guild_id, thread_ids, now=None):
    # Adding is silent by default; /poke_forums ping-now makes it immediate.
    first_due = ((now or utcnow()) + INTERVAL).isoformat()
    async with store.lock:
        before = store.db.total_changes
        store.db.executemany(
            'INSERT OR IGNORE INTO forum_pokes (guild_id,thread_id,next_poke_at) VALUES (?,?,?)',
            [(guild_id, thread_id, first_due) for thread_id in thread_ids])
        return store.db.total_changes - before


async def remove_targets(store, guild_id, thread_ids):
    async with store.lock:
        before = store.db.total_changes
        store.db.executemany('DELETE FROM forum_pokes WHERE guild_id=? AND thread_id=?',
                             [(guild_id, thread_id) for thread_id in thread_ids])
        return store.db.total_changes - before


async def record_success(store, guild_id, thread_id, message_id, now=None):
    at = now or utcnow()
    async with store.lock:
        store.db.execute('''UPDATE forum_pokes SET message_id=?, last_poked_at=?,
            next_poke_at=?, last_error=NULL WHERE guild_id=? AND thread_id=?''',
            (message_id, at.isoformat(), (at + INTERVAL).isoformat(), guild_id, thread_id))


async def record_failure(store, guild_id, thread_id, error, now=None):
    at = now or utcnow()
    error = str(error)[:400]
    async with store.lock:
        old = store.db.execute('SELECT last_error FROM forum_pokes WHERE guild_id=? AND thread_id=?',
                               (guild_id, thread_id)).fetchone()
        if old is None:
            return False  # Removed while network work was in flight.
        store.db.execute('''UPDATE forum_pokes SET last_error=?, next_poke_at=?
            WHERE guild_id=? AND thread_id=?''',
            (error, (at + RETRY).isoformat(), guild_id, thread_id))
        return old['last_error'] != error


async def resolve_forum_thread(bot, guild, thread_id):
    """Look up active or archived threads and reject other servers/channel types."""
    import discord
    thread = guild.get_thread(thread_id) or bot.get_channel(thread_id)
    if thread is None:
        thread = await bot.fetch_channel(thread_id)  # Works for archived threads.
    if not isinstance(thread, discord.Thread) or thread.guild.id != guild.id:
        raise ValueError('ID is not a thread belonging to this server')
    parent = guild.get_channel(thread.parent_id)
    if not isinstance(parent, discord.ForumChannel):
        raise ValueError('Thread is not a post within a forum channel')
    return thread


async def refresh_activity(thread):
    """Edit alone is NOT activity; change archive metadata to reset inactivity.

    Discord explicitly counts unarchiving or changing auto_archive_duration as
    activity. The two duration edits preserve the original auto-archive policy.
    Bot needs Manage Threads on posts belonging to other users.
    """
    if thread.archived:
        await thread.edit(archived=False, reason='Scheduled forum keepalive')
    else:
        original = thread.auto_archive_duration
        alternate = 1440 if original != 1440 else 60
        await thread.edit(auto_archive_duration=alternate, reason='Refresh forum activity')
        try:
            await thread.edit(auto_archive_duration=original, reason='Restore forum archive setting')
        except Exception:
            # Restore once more if a transient API failure interrupts the pair.
            try:
                await thread.edit(auto_archive_duration=original, reason='Restore forum archive setting')
            except Exception:
                log.exception('Could not restore auto archive duration on thread %s', thread.id)
            raise


async def poke_one(bot, guild, target):
    """Touch thread activity, then edit the one bot-owned poke message (or send it)."""
    import discord
    thread = await resolve_forum_thread(bot, guild, target['thread_id'])
    await refresh_activity(thread)
    content = f"Poked on: **{utcnow():%m/%d/%Y}**"  # UTC calendar date.
    message = None
    if target.get('message_id'):
        try:
            candidate = await thread.fetch_message(target['message_id'])
            if candidate.author.id == bot.user.id and candidate.content.startswith('Poked on:'):
                message = candidate
        except discord.NotFound:
            pass
    if message is None:
        # Recovers a message sent just before a bot restart / DB write failure.
        async for candidate in thread.history(limit=250):
            if candidate.author.id == bot.user.id and candidate.content.startswith('Poked on:'):
                message = candidate
                break
    if message is None:
        message = await thread.send(content, allowed_mentions=discord.AllowedMentions.none(), silent=True)
    else:
        await message.edit(content=content, allowed_mentions=discord.AllowedMentions.none())
    return message.id


async def report_error(bot, guild, thread_id, message):
    """Use only the configured command-channel whitelist for error notices."""
    import discord
    settings = bot.preferences_for(guild)
    channels = settings.get('command_channel_ids', [])
    for channel_id in channels:
        channel = guild.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(
                    f'⚠️ `/poke_forums` could not refresh thread `{thread_id}`: {message[:350]}',
                    allowed_mentions=discord.AllowedMentions.none())
                return
            except discord.HTTPException:
                log.warning('Could not report forum poke error in channel %s', channel_id)
    log.error('Forum poke error guild=%s thread=%s: %s', guild.id, thread_id, message)


async def run_one(bot, guild, target):
    try:
        message_id = await poke_one(bot, guild, target)
        await record_success(bot.store, guild.id, target['thread_id'], message_id)
        return True, None
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'
        new = await record_failure(bot.store, guild.id, target['thread_id'], error)
        if new:
            await report_error(bot, guild, target['thread_id'], error)
        log.exception('Could not poke guild %s thread %s', guild.id, target['thread_id'])
        return False, error
