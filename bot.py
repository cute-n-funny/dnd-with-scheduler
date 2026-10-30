"""D&D scheduling Discord bot. Use one process and one persistent SQLite volume."""
from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
import io
import logging
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands, tasks

from logic import (choose_slot, event_datetimes, google_calendar_url, make_ics,
                   parse_days, parse_times, slot_keys)
from storage import Store

log = logging.getLogger("dnd_scheduler")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
UTC = timezone.utc
TZ_NAME = os.getenv("TZ_NAME", "America/Chicago")
DB_PATH = os.getenv("DATABASE_PATH", "./data/scheduler.db")


def utcnow():
    return datetime.now(UTC)


def dtread(s):
    return datetime.fromisoformat(s) if s else None


def mention(config, guild):
    role_id = config.get("ping_role_id")
    return f"<@&{role_id}>" if role_id and role_id != guild.id else "@everyone"


def is_admin(user):
    return (isinstance(user, discord.Member) and
            (user.guild_permissions.administrator or user.guild_permissions.manage_guild or
             any(role.name.lower() == "admin" for role in user.roles)))


def config_defaults(guild, creator_id, channel_id):
    return dict(title="Title", description="Description for event", dm_id=creator_id,
                weeks=2, days=[3, 4, 5, 6], times=["14:00", "16:00", "18:00"],
                poll_duration=3, attendee_limit=12, poll_after_event=3,
                reminder_before_event=6, ping_role_id=guild.id,
                event_duration=4, timezone=TZ_NAME, channel_id=channel_id, looping=True)


def validate_config(c):
    for key, lo, hi in (("weeks", 1, 4), ("poll_duration", 3, 7),
                        ("poll_after_event", 3, 24), ("reminder_before_event", 3, 24),
                        ("attendee_limit", 1, 99), ("event_duration", 1, 24)):
        if not lo <= c[key] <= hi:
            raise ValueError(f"{key} must be between {lo} and {hi}.")
    if not c["title"].strip() or len(c["title"]) > 100:
        raise ValueError("Title must be 1–100 characters.")
    if len(c["description"]) > 900:
        raise ValueError("Description cannot exceed 900 characters.")
    try:
        ZoneInfo(c["timezone"])
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown TZ_NAME timezone: {c['timezone']}") from exc


def event_link(guild_id, event_id):
    return f"https://discord.com/events/{guild_id}/{event_id}"


def build_poll_embed(record, votes, *, closed=False, winner=None):
    c = record["config"]
    end = dtread(record["poll_deadline"])
    slots = slot_keys(c, end - timedelta(days=c["poll_duration"]), end)
    counts = Counter(v["slot"] for v in votes)
    members = len({v["user_id"] for v in votes})
    embed = discord.Embed(title=f"🗓️ {c['title']} — Availability", color=0x5865F2,
                          description=(f"{c['description']}\n\n"
                          f"DM: <@{c['dm_id']}> | **{members}** players voted | Timezone: `{c['timezone']}`\n"
                          f"{'**Poll closed**' if closed else f'Closes <t:{int(end.timestamp())}:R>'}\n"
                          "Choose **Vote / Edit Availability** to add/remove as many slots as you can attend."))
    if winner:
        start, _ = event_datetimes(winner, c)
        embed.add_field(name="Selected date/time", value=f"<t:{int(start.timestamp())}:F>", inline=False)
    # Four independent 7-day windows, each with at most four date columns.
    anchor = (end - timedelta(days=c["poll_duration"])).astimezone(ZoneInfo(c["timezone"])).date() + timedelta(days=7)
    all_dates = sorted({s[:10] for s in slots})
    for week_no in range(c["weeks"]):
        first_day = anchor + timedelta(days=week_no * 7)
        last_day = first_day + timedelta(days=7)
        week_dates = [d for d in all_dates if first_day <= datetime.fromisoformat(d).date() < last_day]
        if not week_dates:
            continue
        headers = [datetime.fromisoformat(d).strftime("%a %m/%d") for d in week_dates]
        lines = ["Time  " + " ".join(f"{h:>9}" for h in headers)]
        for hhmm in c["times"]:
            clock = datetime.strptime(hhmm, "%H:%M").strftime("%I:%M%p").lstrip("0").lower()
            cells = []
            for day in week_dates:
                slot = f"{day}T{hhmm}"
                cells.append(f"{counts[slot]:>9}" if slot in slots else f"{'—':>9}")
            lines.append(f"{clock:<6}" + " ".join(cells))
        embed.add_field(name=f"Week {week_no + 1} — Votes per time", value="```\n" + "\n".join(lines) + "\n```", inline=False)
    embed.set_footer(text="Each number counts people available for that particular slot. One vote per person per slot.")
    return embed


def build_event_embed(record, rsvps):
    c = record["config"]
    start, end = dtread(record["event_start"]), dtread(record["event_end"])
    confirmed = [r["user_id"] for r in rsvps if r["status"] == "confirmed"]
    waitlisted = [r["user_id"] for r in rsvps if r["status"] == "waitlist"]
    desc = (f"{c['description']}\n\n**Starts:** <t:{int(start.timestamp())}:F>\n"
            f"**Ends:** <t:{int(end.timestamp())}:F>\n"
            f"**DM:** <@{c['dm_id']}>\n"
            f"**Seats:** {len(confirmed)}/{c['attendee_limit']} confirmed • {len(waitlisted)} waitlisted\n"
            "Click **Join / Leave RSVP** below. The earliest waitlisted player is promoted when a seat opens.\n"
            "This RSVP is separate from Discord's native event *Interested* button.")
    if record["discord_event_id"]:
        desc += f"\n**Discord Event:** {event_link(record['guild_id'], record['discord_event_id'])}"
    if record["forum_thread_id"]:
        desc += f"\n**Session thread:** <#{record['forum_thread_id']}>"
    embed = discord.Embed(title=f"🐉 {c['title']} — Session RSVP", description=desc,
                          color=0x2ECC71)
    if confirmed:
        shown = " ".join(f"<@{u}>" for u in confirmed[:30])
        embed.add_field(name=f"Confirmed ({len(confirmed)})", value=shown + (" …" if len(confirmed)>30 else ""), inline=False)
    if waitlisted:
        shown = " ".join(f"<@{u}>" for u in waitlisted[:30])
        embed.add_field(name=f"Waitlist ({len(waitlisted)})", value=shown + (" …" if len(waitlisted)>30 else ""), inline=False)
    return embed


class PollMessageView(discord.ui.View):
    def __init__(self, bot, guild_id, cycle):
        super().__init__(timeout=None)
        self.bot, self.guild_id, self.cycle = bot, guild_id, cycle
        b = discord.ui.Button(label="Vote / Edit Availability", emoji="🗳️", style=discord.ButtonStyle.primary,
                              custom_id=f"dnd:poll:{guild_id}:{cycle}")
        b.callback = self.vote
        self.add_item(b)

    async def vote(self, interaction):
        record = await self.bot.store.get(self.guild_id)
        if not record or record["state"] != "poll" or record["cycle"] != self.cycle or utcnow() >= dtread(record["poll_deadline"]):
            return await interaction.response.send_message("This poll is closed.", ephemeral=True)
        slots = self.bot.slots_for(record)
        if not slots:
            return await interaction.response.send_message("There are no future time slots in this poll.", ephemeral=True)
        panel = VotePanel(self.bot, record, interaction.user.id, slots)
        await interaction.response.send_message(embed=panel.embed(), view=panel, ephemeral=True)


class VotePanel(discord.ui.View):
    def __init__(self, bot, record, user_id, slots):
        super().__init__(timeout=600)
        self.bot, self.guild_id, self.cycle, self.user_id = bot, record["guild_id"], record["cycle"], user_id
        self.slots = set(slots)
        self.day = min(s[:10] for s in slots)
        self.time = min(s[11:] for s in slots if s.startswith(self.day))
        days = sorted({s[:10] for s in slots})
        day_select = discord.ui.Select(placeholder="Choose a date", row=0, options=[
            discord.SelectOption(label=datetime.fromisoformat(day).strftime("%a %b %d, %Y"), value=day)
            for day in days])
        day_select.callback = self.choose_day
        self.add_item(day_select)
        self.day_select = day_select
        time_select = discord.ui.Select(placeholder="Choose a time", row=1, options=[
            discord.SelectOption(label=datetime.strptime(t, "%H:%M").strftime("%I:%M %p"), value=t)
            for t in record["config"]["times"]])
        time_select.callback = self.choose_time
        self.add_item(time_select)
        self.time_select = time_select
        button = discord.ui.Button(label="Toggle this availability", emoji="✅", row=2,
                                   style=discord.ButtonStyle.success)
        button.callback = self.toggle
        self.add_item(button)
        show = discord.ui.Button(label="Show my votes", emoji="🔎", row=2,
                                 style=discord.ButtonStyle.secondary)
        show.callback = self.my_votes
        self.add_item(show)

    async def my_votes(self, interaction):
        votes = await self.bot.store.votes(self.guild_id, self.cycle)
        mine = sorted(v["slot"] for v in votes if v["user_id"] == self.user_id)
        if mine:
            lines = [f"• {s[:10]} at {s[11:]}" for s in mine]
            body = "Your selected availability slots (server timezone):\n" + "\n".join(lines)
        else:
            body = "You haven't selected any available date/time slots yet."
        await interaction.response.send_message(body[:1900], ephemeral=True)

    def embed(self, note=""):
        return discord.Embed(title="Your D&D availability",
            description=(f"Selected: **{self.day} at {self.time}** (server timezone).\n"
                         "Select a date and time, then **Toggle** to add or remove your vote. "
                         "Repeat for each slot you can attend.\n" + note), color=0x5865F2)

    async def choose_day(self, interaction):
        self.day = self.day_select.values[0]
        if f"{self.day}T{self.time}" not in self.slots:
            self.time = min(s[11:] for s in self.slots if s.startswith(self.day))
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def choose_time(self, interaction):
        self.time = self.time_select.values[0]
        note = "⚠️ That combination is not available." if f"{self.day}T{self.time}" not in self.slots else ""
        await interaction.response.edit_message(embed=self.embed(note), view=self)

    async def toggle(self, interaction):
        record = await self.bot.store.get(self.guild_id)
        async with self.bot.lock_for(self.guild_id):
            record = await self.bot.store.get(self.guild_id)
            if not record or record["state"] != "poll" or record["cycle"] != self.cycle or utcnow() >= dtread(record["poll_deadline"]):
                return await interaction.response.send_message("This poll has closed.", ephemeral=True)
            slot = f"{self.day}T{self.time}"
            if slot not in self.bot.slots_for(record):
                return await interaction.response.send_message("That slot isn't available.", ephemeral=True)
            added = await self.bot.store.toggle_vote(self.guild_id, self.cycle, interaction.user.id, slot)
        await interaction.response.edit_message(embed=self.embed("✅ Vote added." if added else "🗑️ Vote removed."), view=self)
        await self.bot.refresh_poll(self.guild_id)


class EventMessageView(discord.ui.View):
    def __init__(self, bot, record):
        super().__init__(timeout=None)
        self.bot, self.guild_id, self.cycle = bot, record["guild_id"], record["cycle"]
        rsvp = discord.ui.Button(label="Join / Leave RSVP", emoji="🎟️", style=discord.ButtonStyle.success,
                                 custom_id=f"dnd:rsvp:{self.guild_id}:{self.cycle}")
        rsvp.callback = self.rsvp
        self.add_item(rsvp)
        ical = discord.ui.Button(label="Download iCal (.ics)", emoji="📅", style=discord.ButtonStyle.secondary,
                                 custom_id=f"dnd:ical:{self.guild_id}:{self.cycle}")
        ical.callback = self.ical
        self.add_item(ical)
        c = record["config"]
        if record["event_start"] and record["event_end"]:
            self.add_item(discord.ui.Button(label="Add to Google Calendar", style=discord.ButtonStyle.link,
                url=google_calendar_url(c["title"], c["description"][:180], dtread(record["event_start"]), dtread(record["event_end"]))))

    async def rsvp(self, interaction):
        async with self.bot.lock_for(self.guild_id):
            record = await self.bot.store.get(self.guild_id)
            if not record or record["cycle"] != self.cycle or record["state"] not in ("event", "waiting") or utcnow() >= dtread(record["event_end"]):
                return await interaction.response.send_message("RSVPs for this session are closed.", ephemeral=True)
            status, promoted = await self.bot.store.toggle_rsvp(self.guild_id, self.cycle,
                                 interaction.user.id, record["config"]["attendee_limit"])
        text = {"confirmed": "✅ Your spot is confirmed.", "waitlist": "⏳ You're on the waitlist.",
                "removed": "Your RSVP has been removed."}[status]
        if promoted == interaction.user.id:
            text = "✅ You're now confirmed (promoted from the waitlist)."
        await interaction.response.send_message(text, ephemeral=True)
        await self.bot.refresh_event(self.guild_id)
        if promoted and promoted != interaction.user.id:
            try:
                person = await self.bot.fetch_user(promoted)
                await person.send(f"A seat opened for **{record['config']['title']}**. Your D&D RSVP is now confirmed!")
            except discord.HTTPException:
                pass

    async def ical(self, interaction):
        record = await self.bot.store.get(self.guild_id)
        if not record or record["cycle"] != self.cycle or not record["event_start"]:
            return await interaction.response.send_message("This calendar event is no longer available.", ephemeral=True)
        c = record["config"]
        data = make_ics(c["title"], c["description"], dtread(record["event_start"]),
                        dtread(record["event_end"]), f"{self.guild_id}-{self.cycle}@dnd-scheduler")
        await interaction.response.send_message("Import this `.ics` file into Apple Calendar, Outlook, or another calendar:",
              file=discord.File(io.BytesIO(data), filename="dnd-session.ics"), ephemeral=True)


class SchedulerBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!unused-", intents=discord.Intents.default(),
                         allowed_mentions=discord.AllowedMentions(everyone=True, roles=True, users=False))
        self.store = Store(DB_PATH)
        self.guild_locks = {}

    def lock_for(self, guild_id):
        return self.guild_locks.setdefault(guild_id, asyncio.Lock())

    async def setup_hook(self):
        for r in await self.store.all():
            if r["state"] == "poll" and r["poll_message_id"]:
                self.add_view(PollMessageView(self, r["guild_id"], r["cycle"]), message_id=r["poll_message_id"])
            if r["state"] in ("event", "waiting", "creating_event") and r["event_message_id"]:
                self.add_view(EventMessageView(self, r), message_id=r["event_message_id"])
        guild_id = os.getenv("GUILD_ID", "").strip()
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Registered slash commands in guild %s", guild_id)
        else:
            await self.tree.sync()
            log.info("Registered global slash commands (may take time to appear)")
        self.worker.start()

    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id)

    def text_channel(self, guild, config=None):
        channel_id = int(os.getenv("SCHEDULING_CHANNEL_ID") or 0) or (config or {}).get("channel_id")
        channel = guild.get_channel(channel_id) if channel_id else None
        if isinstance(channel, discord.TextChannel): return channel
        return next((c for c in guild.text_channels if "scheduling" in c.name.lower()), None)

    def forum_channel(self, guild):
        target = int(os.getenv("FORUM_CHANNEL_ID") or 0)
        channel = guild.get_channel(target) if target else None
        if isinstance(channel, discord.ForumChannel): return channel
        return next((c for c in guild.channels if isinstance(c, discord.ForumChannel)
                     and "dnd-session-events" in c.name.lower()), None)

    def voice_channel(self, guild):
        return next((c for c in guild.voice_channels if c.name.lower() in ("voice-chat", "vc")), None)

    def slots_for(self, record):
        c = record["config"]
        end = dtread(record["poll_deadline"])
        return slot_keys(c, end - timedelta(days=c["poll_duration"]), end)

    async def try_delete_message(self, guild, channel_id, message_id):
        if not channel_id or not message_id: return
        try:
            channel = guild.get_channel(channel_id) or await self.fetch_channel(channel_id)
            await (await channel.fetch_message(message_id)).delete()
        except (discord.HTTPException, AttributeError):
            log.info("Could not delete prior bot message %s", message_id)

    async def cleanup(self, guild):
        old = await self.store.get(guild.id)
        if old:
            await self.try_delete_message(guild, old["poll_channel_id"], old["poll_message_id"])
            await self.try_delete_message(guild, old["event_channel_id"], old["event_message_id"])
            if old["forum_thread_id"]:
                try:
                    thread = guild.get_thread(old["forum_thread_id"]) or await self.fetch_channel(old["forum_thread_id"])
                    await thread.delete(reason="Scheduler reset/end")
                except discord.HTTPException:
                    log.warning("Could not remove active session forum post")
        # Delete only future events actually created by this bot, not other admins' events.
        try:
            for event in await guild.fetch_scheduled_events():
                if event.creator_id == self.user.id and (event.start_time > utcnow() or event.status == discord.EventStatus.active):
                    try:
                        await event.delete(reason="D&D scheduler reset/start/end")
                    except discord.HTTPException:
                        log.exception("Could not delete event %s", event.id)
        except discord.HTTPException:
            log.exception("Could not inspect Discord Scheduled Events")
        await self.store.delete(guild.id)

    async def replace_scheduler(self, guild, config):
        async with self.lock_for(guild.id):
            channel = self.text_channel(guild, config)
            if not channel:
                raise ValueError("Create a text channel containing 'scheduling' in its name, or set SCHEDULING_CHANNEL_ID.")
            if not self.forum_channel(guild):
                raise ValueError("Create a Forum channel named dnd-session-events, or set FORUM_CHANNEL_ID.")
            config["channel_id"] = channel.id
            validate_config(config)
            fake = {"config": config, "poll_deadline": (utcnow() + timedelta(days=config["poll_duration"])).isoformat()}
            if not self.slots_for(fake):
                raise ValueError("No valid dates/times remain after poll close; change the weekdays, times, or weeks.")
            await self.cleanup(guild)
            await self.prepare_poll(guild, config, 1)
            await self.publish_poll(guild, await self.store.get(guild.id))

    async def prepare_poll(self, guild, config, cycle):
        now = utcnow()
        deadline = now + timedelta(days=config["poll_duration"])
        await self.store.put(guild.id, config, cycle, "poll_preparing",
                             poll_deadline=deadline.isoformat(), poll_channel_id=config["channel_id"])

    async def publish_poll(self, guild, record):
        if record["state"] != "poll_preparing": return
        channel = self.text_channel(guild, record["config"])
        if not channel:
            raise ValueError("Scheduling text channel is missing")
        if not self.slots_for(record):
            raise ValueError("No slots remain after the poll closes; adjust days/times/weeks.")
        view = PollMessageView(self, guild.id, record["cycle"])
        message = await channel.send(content=mention(record["config"], guild),
                         embed=build_poll_embed(record, []), view=view)
        await self.store.update(guild.id, state="poll", poll_message_id=message.id)
        log.info("Published poll guild=%s cycle=%s", guild.id, record["cycle"])

    async def refresh_poll(self, guild_id):
        record = await self.store.get(guild_id)
        if not record or record["state"] != "poll" or not record["poll_message_id"]: return
        try:
            channel = self.get_channel(record["poll_channel_id"]) or await self.fetch_channel(record["poll_channel_id"])
            message = await channel.fetch_message(record["poll_message_id"])
            await message.edit(embed=build_poll_embed(record, await self.store.votes(guild_id, record["cycle"])))
        except discord.HTTPException:
            log.exception("Poll refresh failed")

    async def close_poll(self, guild, record):
        votes = await self.store.votes(guild.id, record["cycle"])
        slots = [s for s in self.slots_for(record) if event_datetimes(s, record["config"])[0] > utcnow() + timedelta(minutes=5)]
        chosen, users = choose_slot(votes, slots, record["config"]["dm_id"])
        if record["poll_message_id"]:
            try:
                channel = self.get_channel(record["poll_channel_id"]) or await self.fetch_channel(record["poll_channel_id"])
                msg = await channel.fetch_message(record["poll_message_id"])
                await msg.edit(embed=build_poll_embed(record, votes, closed=True, winner=chosen), view=None)
            except discord.HTTPException:
                log.exception("Could not close poll message")
        if not chosen:
            log.info("No valid votes in guild %s; starting another poll", guild.id)
            await self.prepare_poll(guild, record["config"], record["cycle"] + 1)
            await self.publish_poll(guild, await self.store.get(guild.id))
            return
        start, end = event_datetimes(chosen, record["config"])
        await self.store.set_initial_rsvps(guild.id, record["cycle"], users,
                                           record["config"]["attendee_limit"])
        await self.store.update(guild.id, state="creating_event", selected_slot=chosen,
                                event_start=start.isoformat(), event_end=end.isoformat())
        await self.create_event(guild, await self.store.get(guild.id))

    async def create_event(self, guild, record):
        c = record["config"]
        start, end = dtread(record["event_start"]), dtread(record["event_end"])
        marker = f"DND-SCHEDULER:{guild.id}:{record['cycle']}"
        event_id = record["discord_event_id"]
        if not event_id:
            # Find prior matching event on an interrupted create, avoiding duplicates.
            events = await guild.fetch_scheduled_events()
            existing = next((e for e in events if marker in (e.description or "") and
                             e.creator_id == self.user.id), None)
            if existing:
                event_id = existing.id
            else:
                voice = self.voice_channel(guild)
                params = dict(name=c["title"], start_time=start, end_time=end,
                              description=(c["description"][:800] + "\n" + marker))
                if voice:
                    params["channel"] = voice
                else:
                    params["entity_type"] = discord.EntityType.external
                    params["location"] = "Discord — see session forum post"
                event = await guild.create_scheduled_event(**params)
                event_id = event.id
            await self.store.update(guild.id, discord_event_id=event_id)
        thread_id = record["forum_thread_id"]
        if not thread_id:
            forum = self.forum_channel(guild)
            if not forum:
                raise ValueError("Session forum channel not found")
            message = (f"**{c['description']}**\n\n"
                       f"Starts: <t:{int(start.timestamp())}:F>\n"
                       f"Ends: <t:{int(end.timestamp())}:F>\n"
                       f"DM: <@{c['dm_id']}>\n"
                       f"Discord Event: {event_link(guild.id, event_id)}\n\n"
                       "RSVP in the scheduling channel; discuss characters and preparations here.")
            created = await forum.create_thread(name=f"{c['title']} — {start.astimezone(ZoneInfo(c['timezone'])).strftime('%b %d')}",
                                                content=message)
            thread_id = created.thread.id
            await self.store.update(guild.id, forum_thread_id=thread_id)
        record = await self.store.get(guild.id)
        if not record["event_message_id"]:
            channel = self.text_channel(guild, c)
            if not channel: raise ValueError("Scheduling channel not found")
            rsvps = await self.store.rsvps(guild.id, record["cycle"])
            msg = await channel.send(content=mention(c, guild), embed=build_event_embed(record, rsvps),
                                     view=EventMessageView(self, record))
            await self.store.update(guild.id, event_channel_id=channel.id, event_message_id=msg.id)
        await self.store.update(guild.id, state="event")
        log.info("Created session event guild=%s cycle=%s", guild.id, record["cycle"])

    async def refresh_event(self, guild_id):
        record = await self.store.get(guild_id)
        if not record or not record["event_message_id"]: return
        try:
            channel = self.get_channel(record["event_channel_id"]) or await self.fetch_channel(record["event_channel_id"])
            msg = await channel.fetch_message(record["event_message_id"])
            await msg.edit(embed=build_event_embed(record, await self.store.rsvps(guild_id, record["cycle"])))
        except discord.HTTPException:
            log.exception("RSVP message refresh failed")

    async def send_reminder(self, guild, record):
        channel = self.text_channel(guild, record["config"])
        if not channel:
            raise ValueError("Scheduling text channel missing")
        when = int(dtread(record["event_start"]).timestamp())
        await channel.send(f"⏰ {mention(record['config'], guild)} **{record['config']['title']}** begins <t:{when}:R>! "
                           f"RSVP: https://discord.com/channels/{guild.id}/{record['event_channel_id']}/{record['event_message_id']}")
        await self.store.update(guild.id, reminded=1)


    async def sync_native_event(self, guild, record, *, finish=False):
        """Start/end the native Discord Event on schedule; safe across restarts."""
        id_ = record["discord_event_id"]
        flag = "native_ended" if finish else "native_started"
        if not id_ or record[flag]:
            return
        try:
            event = await guild.fetch_scheduled_event(id_)
            if finish:
                if event.status == discord.EventStatus.active:
                    await event.end(reason="Scheduled D&D session duration complete")
            else:
                if event.status == discord.EventStatus.scheduled:
                    await event.start(reason="Scheduled D&D session starting")
            await self.store.update(guild.id, **{flag: 1})
        except discord.NotFound:
            log.warning("Native Discord event %s no longer exists", id_)
            await self.store.update(guild.id, **{flag: 1})
        except (discord.HTTPException, ValueError):
            log.exception("Native Discord event lifecycle action failed: %s", id_)

    @tasks.loop(seconds=45)
    async def worker(self):
        for row in await self.store.all():
            guild = self.get_guild(row["guild_id"])
            if not guild: continue
            if self.lock_for(guild.id).locked(): continue
            async with self.lock_for(guild.id):
                try:
                    r = await self.store.get(guild.id)
                    if not r: continue
                    now = utcnow()
                    if r["state"] == "poll_preparing":
                        await self.publish_poll(guild, r)
                    elif r["state"] == "poll" and now >= dtread(r["poll_deadline"]):
                        await self.close_poll(guild, r)
                    elif r["state"] == "creating_event":
                        await self.create_event(guild, r)
                    elif r["state"] == "event":
                        start, end = dtread(r["event_start"]), dtread(r["event_end"])
                        if not r["reminded"] and start - timedelta(hours=r["config"]["reminder_before_event"]) <= now < end:
                            await self.send_reminder(guild, r)
                        if start <= now < end:
                            await self.sync_native_event(guild, r)
                        if now >= end:
                            await self.sync_native_event(guild, r, finish=True)
                            if r["config"]["looping"]:
                                next_at = end + timedelta(hours=r["config"]["poll_after_event"])
                                await self.store.update(guild.id, state="waiting", next_poll_at=next_at.isoformat())
                            else:
                                await self.store.update(guild.id, state="finished")
                    elif r["state"] == "waiting" and now >= dtread(r["next_poll_at"]):
                        await self.prepare_poll(guild, r["config"], r["cycle"] + 1)
                        await self.publish_poll(guild, await self.store.get(guild.id))
                except Exception:
                    log.exception("Scheduler tick failed for guild %s", guild.id)

    @worker.before_loop
    async def before_worker(self):
        await self.wait_until_ready()


bot = SchedulerBot()


async def admin_gate(interaction):
    if not interaction.guild or not is_admin(interaction.user):
        await interaction.response.send_message("Only server admins or users with Manage Server can control the scheduler.", ephemeral=True)
        return False
    return True


@bot.tree.command(name="start_scheduler", description="Start or replace the recurring D&D schedule")
@app_commands.guild_only()
@app_commands.describe(title="Poll/event title", description="Poll/event description",
    dm="DM whose voted days have priority", weeks="Number of upcoming weeks (1–4)",
    days="Comma-separated weekdays (e.g. thur,fri,sat,sun)",
    times="Comma-separated times (e.g. 2pm,4:15pm,6:30pm)",
    poll_duration="Days the availability poll stays open (3–7)",
    attendee_limit="Maximum number of confirmed attendees (1–99)",
    poll_after_event="Hours after the event ends before next poll (3–24)",
    reminder_before_event="Hours before session START to remind players (3–24)",
    ping_role="Role pinged on polls, events, and reminders (default everyone)",
    event_duration="Session duration in hours (1–24)")
async def start_scheduler(interaction: discord.Interaction, title: str = "Title",
    description: str = "Description for event", dm: discord.Member | None = None,
    weeks: int = 2, days: str = "thur,fri,sat,sun", times: str = "2pm,4pm,6pm",
    poll_duration: int = 3, attendee_limit: int = 12,
    poll_after_event: int = 3, reminder_before_event: int = 6,
    ping_role: discord.Role | None = None, event_duration: int = 4):
    if not await admin_gate(interaction): return
    await interaction.response.defer(ephemeral=True)
    try:
        c = config_defaults(interaction.guild, interaction.user.id, interaction.channel_id)
        c.update(title=title, description=description, dm_id=dm.id if dm else interaction.user.id,
                 weeks=weeks, days=parse_days(days), times=parse_times(times),
                 poll_duration=poll_duration, attendee_limit=attendee_limit,
                 poll_after_event=poll_after_event, reminder_before_event=reminder_before_event,
                 ping_role_id=ping_role.id if ping_role else interaction.guild.id,
                 event_duration=event_duration, looping=True)
        await bot.replace_scheduler(interaction.guild, c)
        await interaction.followup.send("✅ Recurring scheduler started! See your scheduling channel for the poll.", ephemeral=True)
    except (ValueError, discord.HTTPException) as exc:
        log.exception("Start scheduler failed")
        await interaction.followup.send(f"Could not start scheduler: {exc}", ephemeral=True)


@bot.tree.command(name="reset_scheduler", description="Delete this bot's active schedule and restart with saved settings")
@app_commands.guild_only()
async def reset_scheduler(interaction: discord.Interaction):
    if not await admin_gate(interaction): return
    await interaction.response.defer(ephemeral=True)
    try:
        old = await bot.store.get(interaction.guild.id)
        c = old["config"] if old else config_defaults(interaction.guild, interaction.user.id, interaction.channel_id)
        c["looping"] = True
        await bot.replace_scheduler(interaction.guild, c)
        await interaction.followup.send("✅ Scheduler reset. A fresh recurring poll is posted.", ephemeral=True)
    except (ValueError, discord.HTTPException) as exc:
        await interaction.followup.send(f"Could not reset: {exc}", ephemeral=True)


@bot.tree.command(name="end_scheduler", description="Delete active bot scheduling messages/events and stop the cycle")
@app_commands.guild_only()
async def end_scheduler(interaction: discord.Interaction):
    if not await admin_gate(interaction): return
    await interaction.response.defer(ephemeral=True)
    async with bot.lock_for(interaction.guild.id):
        await bot.cleanup(interaction.guild)
    await interaction.followup.send("🛑 Scheduler ended. Active bot poll/RSVP posts and future bot-created Discord events were cleared.", ephemeral=True)


@bot.tree.command(name="once_scheduler", description="Replace any active schedule with one poll and one event (no loop)")
@app_commands.guild_only()
async def once_scheduler(interaction: discord.Interaction):
    if not await admin_gate(interaction): return
    await interaction.response.defer(ephemeral=True)
    try:
        old = await bot.store.get(interaction.guild.id)
        c = old["config"] if old else config_defaults(interaction.guild, interaction.user.id, interaction.channel_id)
        c["looping"] = False
        await bot.replace_scheduler(interaction.guild, c)
        await interaction.followup.send("✅ One-time scheduler started. It will not schedule another poll after the session.", ephemeral=True)
    except (ValueError, discord.HTTPException) as exc:
        await interaction.followup.send(f"Could not start one-time scheduler: {exc}", ephemeral=True)


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN is required (see .env.example)")
    bot.run(token, log_handler=None)
