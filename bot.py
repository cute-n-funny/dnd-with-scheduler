"""D&D scheduling Discord bot. Use one process and one persistent SQLite volume."""
from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
import io
import logging
import os
import signal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands, tasks

from logic import (choose_slot, event_datetimes, google_calendar_url, make_ics,
                   parse_days, parse_times, slot_keys)
from storage import Store
from heatmap import render_heatmaps
from availability import (ensure_schema, read_windows, carry_windows,
                          windows_for_heatmap, test_window_votes)
from availability_ui import AvailabilityVotePanel
from guild_preferences import (ensure_schema as ensure_preferences_schema,
                               load_all as load_preferences, get as get_preferences,
                               update as update_preferences, defaults as preference_defaults)
from forum_poker import (ensure_schema as ensure_poke_schema, parse_ids as parse_poke_ids,
                         list_targets as list_poke_targets, due_targets as due_poke_targets,
                         add_targets as add_poke_targets, remove_targets as remove_poke_targets,
                         resolve_forum_thread, run_one as poke_one_target)

log = logging.getLogger("dnd_scheduler")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
UTC = timezone.utc
TZ_NAME = os.getenv("TZ_NAME", "America/Chicago")
DB_PATH = os.getenv("DATABASE_PATH", "./data/scheduler.db")


def utcnow():
    return datetime.now(UTC)


def dtread(s):
    return datetime.fromisoformat(s) if s else None


def is_test(config):
    return config.get("test_mode", False)


def event_title(config):
    return "Test Event" if is_test(config) else config["title"]


def poll_start(record):
    end = dtread(record["poll_deadline"])
    window = timedelta(minutes=1) if is_test(record["config"]) else timedelta(days=record["config"]["poll_duration"])
    return end - window


def players_mention(guild):
    """Compatibility helper: role names no longer select notification targets."""
    return ""


def mention(config, guild):
    """Ping only the explicitly configured per-server role; never @everyone."""
    if is_test(config):
        return ""
    role_id = config.get("ping_role_id")
    if not role_id or role_id == guild.id:
        return ""
    role = guild.get_role(role_id)
    if role is None:
        return ""
    return role.mention


def session_announcement(config, guild, rsvps):
    """One-time text with calendar reminder and auto-confirmed voters."""
    calendar = ("📅 Remember to save this session using **Download iCal (.ics)** "
                "or **Add to Google Calendar** below.")
    ping = mention(config, guild)
    lines = [f"{ping} {calendar}".strip()]
    confirmed = [r["user_id"] for r in rsvps if r["status"] == "confirmed"]
    if confirmed:
        prefix = "✅ The following users have been added after voting: "
        roster = []
        for uid in confirmed:
            name = f"<@{uid}>"
            if len("\n\n".join(lines)) + len(prefix) + sum(map(len, roster)) + len(roster) + len(name) > 1850:
                break
            roster.append(name)
        remaining = len(confirmed) - len(roster)
        suffix = f" (+{remaining} more confirmed; see RSVP list)" if remaining else ""
        lines.append(prefix + " ".join(roster) + suffix)
    return "\n\n".join(lines)


def is_admin(user):
    return (isinstance(user, discord.Member) and
            (user.guild_permissions.administrator or user.guild_permissions.manage_guild or
             any(role.name.lower() == "admin" for role in user.roles)))


def config_defaults(guild, creator_id, channel_id):
    return dict(title="Title", description="Description for event", dm_id=creator_id,
                weeks=2, days=[6, 0, 1, 2, 3, 4, 5],
                times=["14:00", "14:30", "15:00", "15:30", "16:00", "16:30",
                       "17:00", "17:30", "18:00", "18:30", "19:00"],
                hours=8, poll_duration=2, min_votes=3, attendee_limit=8, poll_after_event=3,
                reminder_before_event=6, ping_role_id=None,
                event_duration=4, timezone=TZ_NAME, channel_id=None, location="", looping=True)


def validate_config(c):
    for key, lo, hi in (("weeks", 1, 4), ("poll_duration", 2, 6),
                        ("poll_after_event", 3, 24), ("reminder_before_event", 3, 24),
                        ("attendee_limit", 1, 99), ("event_duration", 1, 24),
                        ("hours", 4, 8), ("min_votes", 3, 12)):
        default = 8 if key == 'hours' else 3 if key == 'min_votes' else -1
        if not lo <= c.get(key, default) <= hi:
            raise ValueError(f"{key} must be between {lo} and {hi}.")
    if not c["title"].strip() or len(c["title"]) > 100:
        raise ValueError("Title must be 1–100 characters.")
    if len(c["description"]) > 900:
        raise ValueError("Description cannot exceed 900 characters.")
    if not isinstance(c.get("location", ""), str) or len(c.get("location", "")) > 100:
        raise ValueError("Location must be text of at most 100 characters.")
    try:
        ZoneInfo(c["timezone"])
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown TZ_NAME timezone: {c['timezone']}") from exc


def event_link(guild_id, event_id):
    return f"https://discord.com/events/{guild_id}/{event_id}"


def time_label(hhmm):
    return datetime.strptime(hhmm, "%H:%M").strftime("%I:%M %p").lstrip("0")


def slot_label(slot):
    return f"{datetime.fromisoformat(slot[:10]).strftime('%a %b %d')} · {time_label(slot[11:])}"


def build_poll_embed(record, votes, *, closed=False, winner=None):
    """Vertical, mobile-friendly poll: one field per date instead of a wide grid."""
    c = record["config"]
    end = dtread(record["poll_deadline"])
    slots = slot_keys(c, poll_start(record), end)
    counts = Counter(v["slot"] for v in votes)
    day_voters = {}
    for vote in votes:
        day_voters.setdefault(vote["slot"][:10], set()).add(vote["user_id"])
    members = len({v["user_id"] for v in votes})
    instructions = ("Press **Set My Availability**, select multiple dates and times, "
                    "apply your availability, then **Save Availability**. You can edit later.")
    test_note = ("\n🧪 **Test mode:** closes after 1 minute; Test Event starts "
                 "1 minute after closing regardless of votes. No pings or reminders."
                 if is_test(c) else "")
    embed = discord.Embed(
        title=f"🗓️ {c['title']} — Availability",
        color=0x5865F2,
        description=(f"{c['description']}\n\n"
                     f"**{'Closed' if closed else 'Voting open'}** · "
                     f"{members} player{'s' if members != 1 else ''} responded · "
                     f"{len(votes)} selected slots\n"
                     f"**Deadline:** <t:{int(end.timestamp())}:F> "
                     f"(<t:{int(end.timestamp())}:R>)\n"
                     f"**DM:** <@{c['dm_id']}> · **Minimum to win:** {c['attendee_limit']} voters at the same time · "
                     f"**Timezone:** `{c['timezone']}`"
                     f"{test_note}\n\n{instructions}"))
    if c.get("location", "").strip():
        embed.add_field(name="📍 Location", value=c["location"].strip(), inline=False)
    if winner:
        if is_test(c):
            detail = f"Top test vote: {slot_label(winner)} (not the actual test event time)"
        else:
            start, _ = event_datetimes(winner, c)
            voters_for_day = len(day_voters.get(winner[:10], set()))
            voters_for_slot = counts[winner]
            dm_voted = any(v["user_id"] == c["dm_id"] for v in votes)
            detail = (f"<t:{int(start.timestamp())}:F> · **{voters_for_day}** available on the day; "
                      f"**{voters_for_slot}** at the selected time."
                      + (" DM-voted days were prioritized." if dm_voted else ""))
        embed.add_field(name="✅ Selected session", value=detail, inline=False)
    visible = sorted({slot[:10] for slot in slots})
    for day in visible:
        date_title = datetime.fromisoformat(day).strftime("%a, %b %d")
        lines = []
        for time in c["times"]:
            slot = f"{day}T{time}"
            if slot in slots:
                lines.append(f"• {time_label(time)} — **{counts[slot]}**")
        if not lines:
            continue
        n = len(day_voters.get(day, set()))
        embed.add_field(name=f"📆 {date_title}  —  {n} available", value="\n".join(lines), inline=False)
    if not visible:
        embed.add_field(name="No remaining dates", value="No valid future slots in this poll.", inline=False)
    embed.set_footer(text="Earliest slot reaching the attendee limit in distinct voters wins; if none qualifies, popularity decides. DM-voted dates take priority.")
    return embed


def build_visual_poll(record, votes, *, closed=False, winner=None):
    """Return text summary, weekly image embeds, and replaceable PNG uploads.

    If image rendering fails, retain the existing accessible text-grid poll.
    The same message and persistent vote button are kept across updates.
    """
    summary = build_poll_embed(record, votes, closed=closed, winner=winner)
    try:
        charts = render_heatmaps(record, votes, closed=closed, winner=winner)
    except Exception:
        log.exception("Heatmap render failed; using text-grid poll")
        return [summary], []
    if not charts:
        return [summary], []

    # Avoid duplicating the tall grid below the images. Keep readable textual
    # top choices for accessibility and people who disable image previews.
    summary.clear_fields()
    slots = set(slot_keys(record["config"], poll_start(record),
                          dtread(record["poll_deadline"])))
    slot_voters = {}
    for vote in votes:
        if vote["slot"] in slots:
            slot_voters.setdefault(vote["slot"], set()).add(vote["user_id"])
    highest = sorted(slot_voters, key=lambda slot: (-len(slot_voters[slot]), slot))[:5]
    top = "\n".join(f"• {slot_label(slot)}: **{len(slot_voters[slot])}**"
                     for slot in highest) or "No availability votes yet."
    summary.add_field(name="Most popular slots", value=top, inline=False)
    summary.add_field(name="Heatmap legend",
                      value="Gray: 0 · Red: low · Orange: growing · Yellow: nearly full · Green: attendee limit met. "
                            "Each square shows the number of distinct players.", inline=False)
    images = []
    files = []
    for name, image_bytes in charts:
        embed = discord.Embed(color=0x2B2D31)
        embed.set_image(url=f"attachment://{name}")
        images.append(embed)
        files.append(discord.File(io.BytesIO(image_bytes), filename=name))
    return [summary, *images], files


def build_window_visual_poll(record, votes, windows, *, closed=False, winner=None):
    """Short poll description; colored images only after availability was submitted."""
    c = record['config']
    legacy_windows, legacy_votes = windows_for_heatmap(windows, votes)
    end = dtread(record['poll_deadline'])
    label = 'CLOSED' if closed else 'VOTING OPEN'
    description = (f"**{label}** · Deadline <t:{int(end.timestamp())}:F> "
                   f"(<t:{int(end.timestamp())}:R>)\n"
                   f"**DM:** <@{c['dm_id']}> · **Timezone:** `{c['timezone']}`\n"
                   "Use **Set My Availability** to choose dates, start times, and hours.\n"
                   "Light green: 0–2 · Green: 3+ · Red: DM unavailable without votes · "
                   "Orange: votes outside DM availability.")
    if c.get("location", "").strip():
        description += f"\n📍 **Location:** {c['location'].strip()}"
    if winner:
        description += (f"\n\n**Selected:** {slot_label(winner)}" if not is_test(c)
                        else "\n\n**Test poll closed.**")
    summary = discord.Embed(title=f"🗓️ {c['title']} — Availability",
                            description=description, color=0x5865F2)
    try:
        charts = render_heatmaps(record, legacy_votes, windows=legacy_windows,
                                 closed=closed, winner=winner)
    except Exception:
        log.exception('Heatmap render failed; retaining the text voting controls')
        return [summary], []
    if not charts:
        summary.description += "\n\n*Heatmap appears after the first saved availability.*"
    embeds = [summary]
    uploads = []
    for filename, content in charts:
        embed = discord.Embed(color=0x2B2D31)
        embed.set_image(url=f'attachment://{filename}')
        embeds.append(embed)
        uploads.append(discord.File(io.BytesIO(content), filename=filename))
    return embeds, uploads


def build_event_embed(record, rsvps):
    c = record["config"]
    start, end = dtread(record["event_start"]), dtread(record["event_end"])
    confirmed = [r["user_id"] for r in rsvps if r["status"] == "confirmed"]
    waitlisted = [r["user_id"] for r in rsvps if r["status"] == "waitlist"]
    limit = c["attendee_limit"]
    desc = (f"{c['description']}\n\n"
            f"📅 **When:** <t:{int(start.timestamp())}:F> (<t:{int(start.timestamp())}:R>)\n"
            f"⏳ **Ends:** <t:{int(end.timestamp())}:t>\n"
            f"🎲 **DM:** <@{c['dm_id']}>\n"
            f"🎟️ **RSVP:** {len(confirmed)}/{limit} confirmed · {len(waitlisted)} waitlisted\n\n"
            "Use **Manage My RSVP** below to view your personal status, join, or cancel. "
            "When a confirmed spot opens, the earliest waitlisted player moves up.\n"
            "*Bot-managed RSVPs are separate from Discord Event Interested.*")
    if c.get("location", "").strip():
        desc += f"\n📍 **Location:** {c['location'].strip()}"
    if record["discord_event_id"]:
        desc += f"\n**Discord Event:** {event_link(record['guild_id'], record['discord_event_id'])}"
    if record["forum_thread_id"]:
        desc += f"\n**Session discussion:** <#{record['forum_thread_id']}>"
    embed = discord.Embed(title=f"🐉 {event_title(c)} — Session Details", description=desc, color=0x2ECC71)
    if confirmed:
        embed.add_field(name=f"✅ Confirmed ({len(confirmed)})", value=" ".join(f"<@{u}>" for u in confirmed[:30]) +
                        (" …" if len(confirmed) > 30 else ""), inline=False)
    if waitlisted:
        embed.add_field(name=f"⏳ Waitlisted ({len(waitlisted)})", value=" ".join(f"<@{u}>" for u in waitlisted[:30]) +
                        (" …" if len(waitlisted) > 30 else ""), inline=False)
    return embed


class PollMessageView(discord.ui.View):
    def __init__(self, bot, guild_id, cycle):
        super().__init__(timeout=None)
        self.bot, self.guild_id, self.cycle = bot, guild_id, cycle
        button = discord.ui.Button(label="Set My Availability", emoji="🗳️",
                                   style=discord.ButtonStyle.primary,
                                   custom_id=f"dnd:poll:{guild_id}:{cycle}")
        button.callback = self.vote
        self.add_item(button)

    async def vote(self, interaction):
        record = await self.bot.store.get(self.guild_id)
        if not record or record["state"] != "poll" or record["cycle"] != self.cycle or utcnow() >= dtread(record["poll_deadline"]):
            return await interaction.response.send_message("This poll is closed.", ephemeral=True)
        slots = self.bot.slots_for(record)
        if not slots:
            return await interaction.response.send_message("There are no future time slots in this poll.", ephemeral=True)
        saved = await self.bot.store.user_votes(self.guild_id, self.cycle, interaction.user.id)
        windows = await read_windows(self.bot.store, self.guild_id, self.cycle, interaction.user.id)
        panel = AvailabilityVotePanel(self.bot, record, interaction.user.id, slots, windows, saved)
        await interaction.response.send_message(embed=panel.embed(), view=panel, ephemeral=True)


class VotePanel(discord.ui.View):
    """Batch-apply selected time(s) to selected date(s); save draft atomically."""
    def __init__(self, bot, record, user_id, slots, saved_votes):
        super().__init__(timeout=600)
        self.bot, self.guild_id, self.cycle, self.user_id = (
            bot, record["guild_id"], record["cycle"], user_id)
        self.timezone = record["config"]["timezone"]
        self.slots = set(slots)
        self.days = sorted({slot[:10] for slot in slots})
        self.times = sorted({slot[11:] for slot in slots})
        self.target_days = {self.days[0]}
        self.target_times = set()
        self.selected = {v["slot"] for v in saved_votes if v["slot"] in self.slots}
        self.saved_slots = set(self.selected)
        self.note = ""
        self.day_select = discord.ui.Select(
            placeholder="Choose one or more dates", row=0, min_values=1,
            max_values=len(self.days), options=self.day_options())
        self.day_select.callback = self.choose_days
        self.add_item(self.day_select)
        self.time_select = discord.ui.Select(
            placeholder="Choose one or more times", row=1, min_values=1,
            max_values=len(self.times), options=self.time_options())
        self.time_select.callback = self.choose_times
        self.add_item(self.time_select)
        add = discord.ui.Button(label="Add Times to Dates", emoji="✅", row=2,
                                style=discord.ButtonStyle.primary)
        add.callback = self.apply_times
        self.add_item(add)
        remove = discord.ui.Button(label="Remove These Times", row=2,
                                   style=discord.ButtonStyle.secondary)
        remove.callback = self.remove_times
        self.add_item(remove)
        all_times = discord.ui.Button(label="Select All Times", row=3,
                                      style=discord.ButtonStyle.secondary)
        all_times.callback = self.select_all
        self.add_item(all_times)
        clear = discord.ui.Button(label="Clear Selected Dates", row=3,
                                  style=discord.ButtonStyle.secondary)
        clear.callback = self.clear_dates
        self.add_item(clear)
        save = discord.ui.Button(label="Save Availability", emoji="💾", row=4,
                                 style=discord.ButtonStyle.success)
        save.callback = self.save
        self.add_item(save)
        prior = discord.ui.Button(label="Copy Previous Preferences", row=4,
                                  style=discord.ButtonStyle.secondary)
        prior.callback = self.copy_previous
        self.add_item(prior)

    def day_options(self):
        result = []
        for day in self.days:
            count = sum(slot.startswith(day) for slot in self.selected)
            result.append(discord.SelectOption(
                label=datetime.fromisoformat(day).strftime("%a %b %d, %Y"), value=day,
                description=f"{count} time{'s' if count != 1 else ''} in draft",
                default=day in self.target_days))
        return result

    def time_options(self):
        return [discord.SelectOption(label=time_label(t), value=t,
                                     default=t in self.target_times) for t in self.times]

    def redraw(self):
        # Keep menu defaults in sync after each interaction, including edits/copy.
        self.day_select.options = self.day_options()
        self.time_select.options = self.time_options()

    def embed(self, note=""):
        count = len(self.selected)
        dates = len({slot[:10] for slot in self.selected})
        chosen_dates = ", ".join(datetime.fromisoformat(d).strftime("%b %d")
                                 for d in sorted(self.target_days))
        chosen_times = ", ".join(time_label(t) for t in sorted(self.target_times)) or "None yet"
        preview = []
        for day in self.days:
            times = [time_label(s[11:]) for s in sorted(self.selected) if s.startswith(day)]
            if times:
                preview.append(f"• **{datetime.fromisoformat(day).strftime('%a %b %d')}** — {', '.join(times)}")
        draft_summary = "\n".join(preview) or "No availability selected yet."
        saved = ("⚠️ **Unsaved changes** — press Save Availability."
                 if self.selected != self.saved_slots else "✅ All changes saved.")
        return discord.Embed(
            title="🗳️ Your D&D Availability", color=0x5865F2,
            description=(
                "**Step 1:** Select one or more **dates**.\n"
                "**Step 2:** Select one or more **times**.\n"
                "**Step 3:** Press **Add Times to Dates** to apply those times to *every* "
                "chosen date. Repeat or remove times as needed.\n"
                "**Step 4:** Press **Save Availability** once.\n\n"
                f"**Dates to edit:** {chosen_dates}\n"
                f"**Times to apply:** {chosen_times}\n"
                f"**Draft:** {count} time slots across {dates} dates\n"
                f"**Timezone:** `{self.timezone}`\n\n"
                f"**Your availability:**\n{draft_summary}\n\n{saved}\n{note}"))

    async def choose_days(self, interaction):
        self.target_days = set(self.day_select.values)
        self.redraw()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def choose_times(self, interaction):
        self.target_times = set(self.time_select.values)
        self.redraw()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def apply_times(self, interaction):
        if not self.target_times:
            return await interaction.response.edit_message(
                embed=self.embed("ℹ️ Select one or more times before applying."), view=self)
        chosen = {f"{day}T{time}" for day in self.target_days for time in self.target_times}
        valid = chosen & self.slots
        self.selected.update(valid)
        self.redraw()
        await interaction.response.edit_message(embed=self.embed(
            f"✅ Added {len(valid)} valid date/time combinations to your draft."), view=self)

    async def remove_times(self, interaction):
        if not self.target_times:
            return await interaction.response.edit_message(
                embed=self.embed("ℹ️ Select one or more times to remove."), view=self)
        chosen = {f"{day}T{time}" for day in self.target_days for time in self.target_times}
        removed = len(self.selected & chosen)
        self.selected.difference_update(chosen)
        self.redraw()
        await interaction.response.edit_message(embed=self.embed(
            f"🗑️ Removed {removed} date/time combinations from your draft."), view=self)

    async def select_all(self, interaction):
        self.target_times = set(self.times)
        self.redraw()
        await interaction.response.edit_message(embed=self.embed(
            "✅ All available times selected. Press **Add Times to Dates** to apply them."), view=self)

    async def clear_dates(self, interaction):
        self.selected = {s for s in self.selected if s[:10] not in self.target_days}
        self.redraw()
        await interaction.response.edit_message(embed=self.embed(
            "🗑️ Cleared all availability for your selected dates (not saved yet)."), view=self)

    async def copy_previous(self, interaction):
        # Copies weekday/time patterns, not exact dates. Draft only: never auto-saves.
        old = await self.bot.store.previous_user_votes(self.guild_id, self.cycle, self.user_id)
        if not old:
            return await interaction.response.edit_message(embed=self.embed(
                "ℹ️ No previous poll preferences found."), view=self)
        patterns = {(datetime.fromisoformat(v["slot"][:10]).weekday(), v["slot"][11:])
                    for v in old}
        matches = {s for s in self.slots if
                   (datetime.fromisoformat(s[:10]).weekday(), s[11:]) in patterns}
        if not matches:
            return await interaction.response.edit_message(embed=self.embed(
                "ℹ️ No matching weekdays/times in this poll. Your draft is unchanged."), view=self)
        self.selected.update(matches)
        self.redraw()
        await interaction.response.edit_message(embed=self.embed(
            "🔁 Added matching weekday/time preferences from the preceding poll to your draft. "
            "Review the selected dates and save."), view=self)

    async def save(self, interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "This availability form belongs to someone else.", ephemeral=True)
        async with self.bot.lock_for(self.guild_id):
            record = await self.bot.store.get(self.guild_id)
            if not record or record["state"] != "poll" or record["cycle"] != self.cycle or utcnow() >= dtread(record["poll_deadline"]):
                return await interaction.response.send_message(
                    "This poll has closed; your unsaved changes were not recorded.", ephemeral=True)
            if not self.selected <= set(self.bot.slots_for(record)):
                return await interaction.response.send_message(
                    "Some selected times are no longer valid. Reopen the poll.", ephemeral=True)
            await self.bot.store.replace_user_votes(self.guild_id, self.cycle,
                                                    self.user_id, self.selected)
        self.saved_slots = set(self.selected)
        self.redraw()
        count = len(self.saved_slots)
        days = len({s[:10] for s in self.saved_slots})
        await interaction.response.edit_message(embed=self.embed(
            f"✅ Saved! **{count}** time slots across **{days}** dates."), view=self)
        await self.bot.refresh_poll(self.guild_id)


def personal_rsvp_embed(record, rsvps, user_id):
    confirmed = [r for r in rsvps if r["status"] == "confirmed"]
    waitlist = [r for r in rsvps if r["status"] == "waitlist"]
    mine = next((r for r in rsvps if r["user_id"] == user_id), None)
    status = mine["status"] if mine else "none"
    if status == "confirmed":
        message = "✅ **You are confirmed!** Your seat is reserved."
    elif status == "waitlist":
        place = next(i + 1 for i, r in enumerate(waitlist) if r["user_id"] == user_id)
        message = f"⏳ **You are on the waitlist**, position **#{place}**."
    else:
        message = "🎟️ **You have not RSVPed yet.**"
    embed = discord.Embed(title=f"Your RSVP — {event_title(record['config'])}", color=0x5865F2,
        description=(f"{message}\n\n"
                     f"**Seats:** {len(confirmed)}/{record['config']['attendee_limit']} confirmed \n"
                     f"**Waitlist:** {len(waitlist)} people\n\n"
                     "This RSVP is separate from clicking *Interested* on the native Discord event."))
    return embed, status


class RsvpManageView(discord.ui.View):
    """Private, context-aware RSVP action, avoiding accidental cancellations."""
    def __init__(self, bot, record, user_id, status, rsvps):
        super().__init__(timeout=240)
        self.bot, self.guild_id, self.cycle, self.user_id = (
            bot, record["guild_id"], record["cycle"], user_id)
        self.expected_status = status
        confirmed = sum(r["status"] == "confirmed" for r in rsvps)
        if status in ("confirmed", "waitlist"):
            label = "Cancel My RSVP"
            style = discord.ButtonStyle.danger
        else:
            label = ("Confirm Attendance" if confirmed < record["config"]["attendee_limit"]
                     else "Join Waitlist")
            style = discord.ButtonStyle.success
        button = discord.ui.Button(label=label, style=style)
        button.callback = self.change
        self.add_item(button)

    async def change(self, interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This RSVP panel belongs to someone else.", ephemeral=True)
        async with self.bot.lock_for(self.guild_id):
            record = await self.bot.store.get(self.guild_id)
            if not record or record["cycle"] != self.cycle or record["state"] not in ("event", "waiting") or utcnow() >= dtread(record["event_end"]):
                return await interaction.response.send_message("RSVPs for this session are closed.", ephemeral=True)
            current = await self.bot.store.rsvps(self.guild_id, self.cycle)
            existing = next((r["status"] for r in current if r["user_id"] == self.user_id), "none")
            if existing != self.expected_status:
                embed, _ = personal_rsvp_embed(record, current, self.user_id)
                embed.set_footer(text="Your RSVP changed since this panel opened. Review before acting again.")
                return await interaction.response.edit_message(
                    embed=embed, view=RsvpManageView(self.bot, record, self.user_id, existing, current))
            result, promoted = await self.bot.store.toggle_rsvp(
                self.guild_id, self.cycle, self.user_id, record["config"]["attendee_limit"])
            rsvps = await self.bot.store.rsvps(self.guild_id, self.cycle)
        embed, status = personal_rsvp_embed(record, rsvps, self.user_id)
        note = {"confirmed": "✅ Attendance confirmed.", "waitlist": "⏳ Added to the waitlist.",
                "removed": "✅ Your RSVP was cancelled."}[result]
        embed.set_footer(text=note)
        await interaction.response.edit_message(
            embed=embed, view=RsvpManageView(self.bot, record, self.user_id, status, rsvps))
        await self.bot.refresh_event(self.guild_id)
        if promoted and promoted != self.user_id:
            try:
                person = await self.bot.fetch_user(promoted)
                await person.send(
                    f"A spot opened for **{event_title(record['config'])}**. Your RSVP is now confirmed!")
            except discord.HTTPException:
                pass


class EventMessageView(discord.ui.View):
    def __init__(self, bot, record):
        super().__init__(timeout=None)
        self.bot, self.guild_id, self.cycle = bot, record["guild_id"], record["cycle"]
        rsvp = discord.ui.Button(label="Manage My RSVP", emoji="🎟️", style=discord.ButtonStyle.success,
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
                url=google_calendar_url(event_title(c), c["description"][:180], dtread(record["event_start"]), dtread(record["event_end"]))))

    async def rsvp(self, interaction):
        record = await self.bot.store.get(self.guild_id)
        if not record or record["cycle"] != self.cycle or record["state"] not in ("event", "waiting") or utcnow() >= dtread(record["event_end"]):
            return await interaction.response.send_message("RSVPs for this session are closed.", ephemeral=True)
        rsvps = await self.bot.store.rsvps(self.guild_id, self.cycle)
        embed, status = personal_rsvp_embed(record, rsvps, interaction.user.id)
        await interaction.response.send_message(
            embed=embed, view=RsvpManageView(self.bot, record, interaction.user.id, status, rsvps),
            ephemeral=True)

    async def ical(self, interaction):
        record = await self.bot.store.get(self.guild_id)
        if not record or record["cycle"] != self.cycle or not record["event_start"]:
            return await interaction.response.send_message("This calendar event is no longer available.", ephemeral=True)
        c = record["config"]
        data = make_ics(event_title(c), c["description"], dtread(record["event_start"]),
                        dtread(record["event_end"]), f"{self.guild_id}-{self.cycle}@dnd-scheduler")
        await interaction.response.send_message("Import this `.ics` file into Apple Calendar, Outlook, or another calendar:",
              file=discord.File(io.BytesIO(data), filename="dnd-session.ics"), ephemeral=True)


class SchedulerBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!unused-", intents=discord.Intents.default(),
                         allowed_mentions=discord.AllowedMentions(everyone=True, roles=True, users=False))
        self.store = Store(DB_PATH)
        ensure_schema(self.store)
        ensure_preferences_schema(self.store)
        ensure_poke_schema(self.store)
        self.preferences_cache = {}
        self.guild_locks = {}
        self.forum_poke_locks = {}
        self._started_guilds = set()
        self._lifecycle_lock = asyncio.Lock()
        self._closing = False

    def lock_for(self, guild_id):
        return self.guild_locks.setdefault(guild_id, asyncio.Lock())

    def poke_lock_for(self, guild_id):
        return self.forum_poke_locks.setdefault(guild_id, asyncio.Lock())

    async def cleanup_legacy_guild_commands(self, guild):
        """Remove the old GUILD_ID-scoped commands, retaining global commands.

        Older releases used copy_global_to()/sync(guild=...), so switching to
        global-only sync did not remove those older guild registrations.
        Record successful migrations per guild to avoid repeated API writes.
        """
        if await self.store.legacy_commands_cleared(guild.id):
            return
        scope = discord.Object(id=guild.id)
        # No guild-scoped commands are intentionally defined by this bot now.
        # If someone adds explicit guild commands later, don't wipe them.
        if self.tree.get_commands(guild=scope):
            log.warning("Skipping legacy command cleanup in guild %s: local guild commands exist", guild.id)
            return
        try:
            self.tree.clear_commands(guild=scope)
            await self.tree.sync(guild=scope)
        except discord.HTTPException:
            log.exception("Could not clear old guild-specific slash commands in %s", guild.id)
            return  # Retry after the next reconnect; do not mark migrated.
        await self.store.mark_legacy_commands_cleared(guild.id)
        log.info("Cleared obsolete guild-specific slash commands in %s; globals retained", guild.id)

    def preferences_for(self, guild):
        return self.preferences_cache.get(guild.id, preference_defaults())

    def role_config(self, guild, config):
        """Use the live guild role preference, not an old saved campaign setting."""
        return {**config, "ping_role_id": self.preferences_for(guild)['ping_role_id']}

    async def setup_hook(self):
        self.preferences_cache = await load_preferences(self.store)
        for r in await self.store.all():
            if r["state"] == "poll" and r["poll_message_id"]:
                self.add_view(PollMessageView(self, r["guild_id"], r["cycle"]), message_id=r["poll_message_id"])
            if r["state"] in ("event", "waiting", "creating_event") and r["event_message_id"]:
                self.add_view(EventMessageView(self, r), message_id=r["event_message_id"])
        # One global command tree lets the bot serve every Discord server.
        # The former GUILD_ID-only registration silently hid commands elsewhere.
        await self.tree.sync()
        log.info("Registered global slash commands (may take time to appear)")
        self.worker.start()
        self.poke_worker.start()

    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id)
        # on_ready can be called more than once after gateway reconnects.
        async with self._lifecycle_lock:
            if self._closing:
                return
            for guild in self.guilds:
                # Run even for servers without preferences or a lifecycle channel.
                # on_ready may recur; SQLite prevents duplicate migration writes.
                await self.cleanup_legacy_guild_commands(guild)
                if guild.id in self._started_guilds:
                    continue
                channel = self.scheduler_channel(guild)
                if channel is None:
                    log.warning("No #scheduler channel in guild %s for lifecycle notices", guild.id)
                    continue
                previous_run_did_not_stop_cleanly = await self.store.mark_service_started(guild.id)
                try:
                    if previous_run_did_not_stop_cleanly:
                        await asyncio.wait_for(channel.send("Fly Services Have Shutdown\n"
                                           "*(Previous shutdown detected on restart; the bot could not notify while offline.)*"), timeout=5)
                    await asyncio.wait_for(channel.send("Fly Services Have Started"), timeout=5)
                except (discord.HTTPException, asyncio.TimeoutError):
                    log.exception("Could not publish startup notice in guild %s", guild.id)
                else:
                    self._started_guilds.add(guild.id)

    async def close(self):
        # Fly sends SIGINT/SIGTERM on graceful Machine shutdown. This cannot
        # run after SIGKILL, a power loss, or a fatal interpreter failure.
        async with self._lifecycle_lock:
            if not self._closing:
                self._closing = True
                for guild in self.guilds:
                    if guild.id not in self._started_guilds:
                        continue
                    channel = self.scheduler_channel(guild)
                    if channel is None:
                        log.warning("No scheduler channel for shutdown notice: %s", guild.id)
                        continue
                    try:
                        await asyncio.wait_for(channel.send("Fly Services Have Shutdown"), timeout=5)
                        await self.store.mark_service_stopped(guild.id)
                    except (discord.HTTPException, asyncio.TimeoutError):
                        # Preserve the 'running' flag. The next startup will
                        # report the unclean shutdown retrospectively.
                        log.exception("Could not publish shutdown notice in guild %s", guild.id)
        if self.poke_worker.is_running():
            self.poke_worker.cancel()
        await super().close()

    async def on_guild_join(self, guild):
        # Also handle joining a server after startup, without syncing globals
        # into that guild or requiring /set_preferences first.
        await self.cleanup_legacy_guild_commands(guild)

    def scheduler_channel(self, guild):
        # Lifecycle notices use the first configured valid command channel.
        # Without a private command channel, avoid broadcasting Fly notices.
        allowed = self.preferences_for(guild)['command_channel_ids']
        for channel_id in allowed:
            channel = guild.get_channel(channel_id)
            if isinstance(channel, discord.TextChannel):
                return channel
        return None

    def text_channel(self, guild, config=None):
        # Announcements use the current per-guild preference, never channel
        # names, old config.channel_id, or process-wide environment variables.
        channel = guild.get_channel(self.preferences_for(guild)['announcements_id'])
        return channel if isinstance(channel, discord.TextChannel) else None

    def forum_channel(self, guild):
        channel = guild.get_channel(self.preferences_for(guild)['forum_id'])
        return channel if isinstance(channel, discord.ForumChannel) else None

    def voice_channel(self, guild):
        return next((c for c in guild.voice_channels if c.name.lower() in ("voice-chat", "vc")), None)

    def slots_for(self, record):
        c = record["config"]
        end = dtread(record["poll_deadline"])
        return slot_keys(c, poll_start(record), end)

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
            # Forum posts are permanent campaign history. Never delete them when
            # starting, resetting, or ending the scheduler.
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
                raise ValueError("channel-for-announcements is not set. Use /set_preferences.")
            if not self.forum_channel(guild):
                raise ValueError("channel-for-forum is not set. Use /set_preferences.")
            config["channel_id"] = channel.id
            validate_config(config)
            window = timedelta(minutes=1) if is_test(config) else timedelta(days=config["poll_duration"])
            fake = {"config": config, "poll_deadline": (utcnow() + window).isoformat()}
            if not self.slots_for(fake):
                raise ValueError("No valid dates/times remain after poll close; change the weekdays, times, or weeks.")
            await self.cleanup(guild)
            await self.prepare_poll(guild, config, 1)
            await self.publish_poll(guild, await self.store.get(guild.id))

    async def prepare_poll(self, guild, config, cycle):
        now = utcnow()
        deadline = now + (timedelta(minutes=1) if is_test(config) else timedelta(days=config["poll_duration"]))
        await self.store.put(guild.id, config, cycle, "poll_preparing",
                             poll_deadline=deadline.isoformat(), poll_channel_id=config["channel_id"])
        # Future dates already voted on in the previous cycle remain valid. Transfer
        # exact matching slots only, preserving voter identity and RSVP priority.
        if cycle > 1 and not is_test(config):
            record = await self.store.get(guild.id)
            copied = await self.store.carry_forward_votes(
                guild.id, cycle - 1, cycle, self.slots_for(record))
            if copied:
                log.info("Carried %s overlapping availability votes into cycle %s", copied, cycle)

    async def publish_poll(self, guild, record):
        if record["state"] != "poll_preparing": return
        channel = self.text_channel(guild, record["config"])
        if not channel:
            raise ValueError("Scheduling text channel is missing")
        if not self.slots_for(record):
            raise ValueError("No slots remain after the poll closes; adjust days/times/weeks.")
        # Recover vote carry-over if an earlier process stopped after the new
        # cycle was written but before the transfer finished.
        if record["cycle"] > 1 and not is_test(record["config"]):
            await self.store.carry_forward_votes(
                guild.id, record["cycle"] - 1, record["cycle"], self.slots_for(record))
        view = PollMessageView(self, guild.id, record["cycle"])
        votes = await self.store.votes(guild.id, record["cycle"])
        if record["cycle"] > 1 and not is_test(record["config"]):
            await carry_windows(self.store, guild.id, record["cycle"] - 1,
                                record["cycle"], self.slots_for(record))
        windows = await read_windows(self.store, guild.id, record["cycle"])
        embeds, files = build_window_visual_poll(record, votes, windows)
        # Explicitly permit only role pings (never @everyone), even when the
        # image-upload fallback is used. Test mode's mention() stays empty.
        allowed = discord.AllowedMentions(everyone=False, roles=True, users=False)
        try:
            message = await channel.send(content=mention(self.role_config(guild, record["config"]), guild),
                                         embeds=embeds, files=files, view=view,
                                         allowed_mentions=allowed)
        except discord.HTTPException:
            if not files:
                raise
            log.exception("Could not upload heatmap; posting text-grid poll instead")
            message = await channel.send(content=mention(self.role_config(guild, record["config"]), guild),
                                         embed=embeds[0], view=view,
                                         allowed_mentions=allowed)
        await self.store.update(guild.id, state="poll", poll_message_id=message.id)
        log.info("Published poll guild=%s cycle=%s", guild.id, record["cycle"])

    async def refresh_poll(self, guild_id):
        record = await self.store.get(guild_id)
        if not record or record["state"] != "poll" or not record["poll_message_id"]: return
        try:
            channel = self.get_channel(record["poll_channel_id"]) or await self.fetch_channel(record["poll_channel_id"])
            message = await channel.fetch_message(record["poll_message_id"])
            votes = await self.store.votes(guild_id, record["cycle"])
            windows = await read_windows(self.store, guild_id, record["cycle"])
            embeds, files = build_window_visual_poll(record, votes, windows)
            try:
                # Replacing attachments on the original poll message means
                # repeated votes do not spam the channel with new messages.
                await message.edit(embeds=embeds, attachments=files)
            except discord.HTTPException:
                log.exception("Heatmap update failed; falling back to text grid")
                await message.edit(embed=embeds[0], attachments=[])
        except discord.HTTPException:
            log.exception("Poll refresh failed")

    async def close_poll(self, guild, record):
        votes = await self.store.votes(guild.id, record["cycle"])
        slots = [s for s in self.slots_for(record) if event_datetimes(s, record["config"])[0] > utcnow() + timedelta(minutes=5)]
        if is_test(record["config"]):
            # Test runs still choose a winning *voted poll slot*, even though
            # the simulated event starts one minute after the poll closes.
            # A three-hour availability window must count for this one-minute
            # test, regardless of the normal event_duration (often four hours).
            windows = await read_windows(self.store, guild.id, record["cycle"])
            votes = test_window_votes(votes, windows, slots)
        # min_votes is distinct people on a calendar DATE, not raw slots/clicks.
        # Test mode bypasses the minimum, preserving its one-vote/zero-vote smoke tests.
        minimum = 1 if is_test(record["config"]) else record["config"].get("min_votes", 3)
        chosen, users = choose_slot(votes, slots, record["config"]["dm_id"],
                                    attendee_limit=record["config"]["attendee_limit"],
                                    min_votes=minimum)
        if record["poll_message_id"]:
            try:
                channel = self.get_channel(record["poll_channel_id"]) or await self.fetch_channel(record["poll_channel_id"])
                msg = await channel.fetch_message(record["poll_message_id"])
                windows = await read_windows(self.store, guild.id, record["cycle"])
                embeds, files = build_window_visual_poll(record, votes, windows,
                                                         closed=True, winner=chosen)
                try:
                    await msg.edit(embeds=embeds, attachments=files, view=None)
                except discord.HTTPException:
                    log.exception("Could not upload final heatmap; using text grid")
                    await msg.edit(embed=embeds[0], attachments=[], view=None)
            except discord.HTTPException:
                log.exception("Could not close poll message")
        if not chosen and not is_test(record["config"]):
            # No event: persist the retry deadline so Fly restarts do not
            # shorten the cooldown or create duplicate polls.
            delay = timedelta(hours=12 * record["config"]["poll_duration"])
            next_at = utcnow() + delay
            log.info("No eligible date in guild %s; retrying poll at %s", guild.id, next_at)
            await self.store.update(guild.id, state="waiting_retry",
                                    next_poll_at=next_at.isoformat(), selected_slot=None)
            return
        if is_test(record["config"]):
            # The test is a quick smoke-test of the event pipeline. Poll choices
            # are informational and affect RSVP ordering, not the test date/time.
            # Even with zero votes, always create the one-minute test event.
            start = utcnow() + timedelta(minutes=1)
            end = start + timedelta(minutes=1)
        else:
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
        if self.preferences_for(guild)['activate_discord_events'] and not event_id:
            # Find prior matching event on an interrupted create, avoiding duplicates.
            events = await guild.fetch_scheduled_events()
            existing = next((e for e in events if marker in (e.description or "") and
                             e.creator_id == self.user.id), None)
            if existing:
                event_id = existing.id
            else:
                voice = self.voice_channel(guild)
                params = dict(name=event_title(c), start_time=start, end_time=end,
                              description=(c["description"][:800] + "\n" + marker),
                              privacy_level=discord.PrivacyLevel.guild_only)
                if voice:
                    params["channel"] = voice
                else:
                    params["entity_type"] = discord.EntityType.external
                    params["location"] = (c.get("location", "").strip() or
                                          ("Discord — test session" if is_test(c)
                                           else "Discord — see session forum post"))
                event = await guild.create_scheduled_event(**params)
                event_id = event.id
            await self.store.update(guild.id, discord_event_id=event_id)
        thread_id = record["forum_thread_id"]
        if not thread_id and not is_test(c):
            forum = self.forum_channel(guild)
            if not forum:
                raise ValueError("Session forum channel not found")
            message = (f"**{c['description']}**\n\n"
                       f"Starts: <t:{int(start.timestamp())}:F>\n"
                       f"Ends: <t:{int(end.timestamp())}:F>\n"
                       f"DM: <@{c['dm_id']}>\n"
                       + (f"Discord Event: {event_link(guild.id, event_id)}\n" if event_id else "")
                       + "\nRSVP in the scheduling channel; discuss characters and preparations here.")
            created = await forum.create_thread(name=f"{event_title(c)} — {start.astimezone(ZoneInfo(c['timezone'])).strftime('%b %d')}",
                                                content=message)
            thread_id = created.thread.id
            await self.store.update(guild.id, forum_thread_id=thread_id)
        record = await self.store.get(guild.id)
        if not record["event_message_id"]:
            channel = self.text_channel(guild, c)
            if not channel: raise ValueError("Scheduling channel not found")
            rsvps = await self.store.rsvps(guild.id, record["cycle"])
            msg = await channel.send(content=session_announcement(self.role_config(guild, c), guild, rsvps),
                                     embed=build_event_embed(record, rsvps),
                                     view=EventMessageView(self, record),
                                     allowed_mentions=discord.AllowedMentions(
                                         everyone=False, roles=True, users=False))
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
        await channel.send(f"⏰ {mention(self.role_config(guild, record['config']), guild)} **{record['config']['title']}** begins <t:{when}:R>! "
                           f"RSVP: https://discord.com/channels/{guild.id}/{record['event_channel_id']}/{record['event_message_id']}",
                           allowed_mentions=discord.AllowedMentions(everyone=False, roles=True, users=False))
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

    # A short interval keeps the one-minute test flow responsive (regular
    # schedules still only perform actions when their saved deadline is due).
    @tasks.loop(seconds=5)
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
                        if not is_test(r["config"]) and not r["reminded"] and start - timedelta(hours=r["config"]["reminder_before_event"]) <= now < end:
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
                    elif r["state"] in ("waiting", "waiting_retry") and now >= dtread(r["next_poll_at"]):
                        await self.prepare_poll(guild, r["config"], r["cycle"] + 1)
                        await self.publish_poll(guild, await self.store.get(guild.id))
                except Exception:
                    log.exception("Scheduler tick failed for guild %s", guild.id)

    @worker.before_loop
    async def before_worker(self):
        await self.wait_until_ready()

    @tasks.loop(minutes=1)
    async def poke_worker(self):
        # This worker does not depend on any active scheduler or forum preference.
        for target in await due_poke_targets(self.store):
            guild = self.get_guild(target['guild_id'])
            if guild is None:
                continue
            async with self.poke_lock_for(guild.id):
                # A command could have removed the target since this list was read.
                current = await list_poke_targets(self.store, guild.id)
                if any(item['thread_id'] == target['thread_id'] for item in current):
                    await poke_one_target(self, guild, target)

    @poke_worker.before_loop
    async def before_poke_worker(self):
        await self.wait_until_ready()


bot = SchedulerBot()


def missing_preferences(guild, settings):
    """Channel IDs are only set when they still exist with the correct type."""
    missing = []
    if not isinstance(guild.get_channel(settings['announcements_id']), discord.TextChannel):
        missing.append('channel-for-announcements')
    if not isinstance(guild.get_channel(settings['forum_id']), discord.ForumChannel):
        missing.append('channel-for-forum')
    return missing


async def admin_gate(interaction, *, require_preferences=True):
    if not interaction.guild or not is_admin(interaction.user):
        await interaction.response.send_message("Only server admins or users with Manage Server can control the scheduler.", ephemeral=True)
        return False
    guild = interaction.guild
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("Use a server text channel for scheduler commands.", ephemeral=True)
        return False
    settings = await get_preferences(bot.store, guild.id)
    bot.preferences_cache[guild.id] = settings
    # If no allowed command channel remains (none set, or all deleted),
    # administrative commands are permitted from any text channel.
    valid = [id_ for id_ in settings['command_channel_ids']
             if isinstance(guild.get_channel(id_), discord.TextChannel)]
    if valid and interaction.channel_id not in valid:
        await interaction.response.send_message(
            'Scheduler commands can only be used in ' + ', '.join(f'<#{id_}>' for id_ in valid) + '.',
            ephemeral=True)
        return False
    if require_preferences:
        missing = missing_preferences(guild, settings)
        if missing:
            await interaction.response.send_message(
                'Missing required scheduler preferences: ' + ', '.join(missing) +
                '. Use `/set_preferences` before any scheduler commands.', ephemeral=True)
            return False
    return True


def build_scheduler_status(record, votes, rsvps):
    if not record:
        return discord.Embed(title="🛠️ Scheduler Status — Idle",
            description="No scheduler is configured. Use `/start_scheduler` for the campaign "
                        "or `/test_scheduler` for a one-minute smoke test.", color=0x95A5A6)
    c = record["config"]
    phase = record["state"]
    status = {
        "poll_preparing": "Preparing poll", "poll": "Accepting votes",
        "creating_event": "Creating event", "event": "Session scheduled",
        "waiting": "Waiting for next poll", "waiting_retry": "Waiting to retry after insufficient votes",
        "finished": "Finished (no repeat)"
    }.get(phase, phase)
    desc = (f"**Campaign:** {event_title(c)}\n"
            f"**Mode:** {'One-minute test' if is_test(c) else 'Recurring' if c.get('looping') else 'One time'}\n"
            f"**State:** {status}\n"
            f"**Cycle:** {record['cycle']}\n")
    if phase in ("poll", "poll_preparing"):
        end = dtread(record["poll_deadline"])
        n = len({v["user_id"] for v in votes})
        desc += (f"**Responses:** {n} players / {len(votes)} slots\n"
                 f"**Next action:** Close poll <t:{int(end.timestamp())}:R> "
                 f"(<t:{int(end.timestamp())}:F>)")
    elif phase == "creating_event":
        desc += "**Next action:** Finish creating the Discord event and RSVP post."
    elif phase == "event":
        start, end = dtread(record["event_start"]), dtread(record["event_end"])
        confirmed = sum(r["status"] == "confirmed" for r in rsvps)
        waiting = sum(r["status"] == "waitlist" for r in rsvps)
        desc += (f"**Start:** <t:{int(start.timestamp())}:F> (<t:{int(start.timestamp())}:R>)\n"
                 f"**End:** <t:{int(end.timestamp())}:F>\n"
                 f"**RSVPs:** {confirmed}/{c['attendee_limit']} confirmed; {waiting} waitlisted\n")
        if not is_test(c) and not record["reminded"] and utcnow() < start:
            reminder = start - timedelta(hours=c["reminder_before_event"])
            desc += f"**Reminder:** <t:{int(reminder.timestamp())}:R>\n"
        desc += "**Next action:** Start/end the scheduled event; then " + (
            "open another poll." if c["looping"] else "stop.")
    elif phase in ("waiting", "waiting_retry") and record.get("next_poll_at"):
        next_at = dtread(record["next_poll_at"])
        if phase == "waiting_retry":
            desc += (f"**Outcome:** No eligible date reached the minimum of "
                     f"{c.get('min_votes', 3)} distinct voters (or no valid slot remained).\n")
        desc += f"**Next action:** New poll <t:{int(next_at.timestamp())}:R> (<t:{int(next_at.timestamp())}:F>)"
    elif phase == "finished":
        desc += "**Next action:** None. Use `/reset_scheduler` or `/start_scheduler` to resume."
    result = discord.Embed(title="🛠️ Scheduler Status", description=desc, color=0x5865F2)
    if record.get("poll_channel_id") and record.get("poll_message_id"):
        result.add_field(name="Public poll", value=f"[View message](https://discord.com/channels/{record['guild_id']}/{record['poll_channel_id']}/{record['poll_message_id']})")
    if record.get("discord_event_id"):
        result.add_field(name="Discord Event", value=f"[Open event]({event_link(record['guild_id'], record['discord_event_id'])})")
    if record.get("event_channel_id") and record.get("event_message_id"):
        result.add_field(name="RSVP", value=f"[View RSVPs](https://discord.com/channels/{record['guild_id']}/{record['event_channel_id']}/{record['event_message_id']})")
    return result


@bot.tree.command(name="set_preferences", description="Configure channels, event creation and pings for this server")
@app_commands.guild_only()
@app_commands.rename(channel_for_announcements="channel-for-announcements",
                     channel_for_forum="channel-for-forum",
                     channel_for_commands="channel-for-commands",
                     activate_discord_events="activate-discord-events",
                     pingable_role="pingable-role")
@app_commands.describe(
    channel_for_announcements="Text channel for polls, RSVPs and reminders",
    channel_for_forum="Forum channel for session discussions",
    channel_for_commands="Toggle one text channel in the allowed command channels",
    activate_discord_events="Create Discord built-in Scheduled Events (default false)",
    pingable_role="Role to ping on availability and Session Details (optional)")
async def set_preferences(interaction: discord.Interaction,
    channel_for_announcements: discord.abc.GuildChannel | None = None,
    channel_for_forum: discord.abc.GuildChannel | None = None,
    channel_for_commands: discord.abc.GuildChannel | None = None,
    activate_discord_events: bool | None = None,
    pingable_role: discord.Role | None = None):
    if not await admin_gate(interaction, require_preferences=False): return
    guild = interaction.guild
    updates, errors, changed = {}, [], []
    toggle = None
    if channel_for_announcements is not None:
        if isinstance(channel_for_announcements, discord.TextChannel) and channel_for_announcements.guild.id == guild.id:
            updates['announcements_id'] = channel_for_announcements.id
            changed.append('channel-for-announcements updated')
        else:
            errors.append('channel-for-announcements is not valid (must be a text channel)')
    if channel_for_forum is not None:
        if isinstance(channel_for_forum, discord.ForumChannel) and channel_for_forum.guild.id == guild.id:
            updates['forum_id'] = channel_for_forum.id
            changed.append('channel-for-forum updated')
        else:
            errors.append('channel-for-forum is not valid (must be a forum channel)')
    if channel_for_commands is not None:
        if isinstance(channel_for_commands, discord.TextChannel) and channel_for_commands.guild.id == guild.id:
            toggle = channel_for_commands.id
            changed.append('channel-for-commands toggled')
        else:
            errors.append('channel-for-commands is not valid (must be a text channel)')
    if activate_discord_events is not None:
        if type(activate_discord_events) is bool:
            updates['activate_discord_events'] = activate_discord_events
            changed.append('activate-discord-events updated')
        else:
            errors.append('activate-discord-events must be true or false')
    if pingable_role is not None:
        if pingable_role.id != guild.id and pingable_role.guild.id == guild.id:
            updates['ping_role_id'] = pingable_role.id
            changed.append('pingable-role updated')
        else:
            errors.append('pingable-role cannot be @everyone or a role from another server')
    if updates or toggle is not None:
        settings = await update_preferences(bot.store, guild.id,
                                             fields=updates, toggle_command_id=toggle)
    else:
        settings = await get_preferences(bot.store, guild.id)
    bot.preferences_cache[guild.id] = settings
    missing = missing_preferences(guild, settings)
    lines = ['✅ ' + ', '.join(changed) if changed else 'No preferences changed.']
    if errors:
        lines.append('⚠️ ' + '\n⚠️ '.join(errors))
    if missing:
        lines.append('⚠️ Required preferences not set: ' + ', '.join(missing) +
                     '. Use `/set_preferences` to configure them.')
    lines.append('Announcements: ' + (f"<#{settings['announcements_id']}>" if settings['announcements_id'] else 'not set'))
    lines.append('Forum: ' + (f"<#{settings['forum_id']}>" if settings['forum_id'] else 'not set'))
    lines.append('Command channels: ' + (', '.join(f'<#{x}>' for x in settings['command_channel_ids'])
                                          or 'any text channel'))
    lines.append('Discord events: ' + ('enabled' if settings['activate_discord_events'] else 'disabled'))
    lines.append('Pingable role: ' + (f"<@&{settings['ping_role_id']}>" if settings['ping_role_id'] else 'none'))
    await interaction.response.send_message('\n'.join(lines), ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="poke_forums", description="Keep selected forum threads active every three days")
@app_commands.guild_only()
@app_commands.rename(ping_now="ping-now")
@app_commands.describe(
    add="Thread ID or comma-separated thread IDs to monitor",
    remove="Thread ID or comma-separated IDs to stop monitoring",
    ping_now="Refresh all monitored threads now (default false)")
async def poke_forums(interaction: discord.Interaction, add: str | None = None,
                      remove: str | None = None, ping_now: bool = False):
    # This feature is independent of channel-for-forum and the required
    # scheduler preferences, but honors the configured command whitelist.
    if not await admin_gate(interaction, require_preferences=False):
        return
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    additions, add_errors = parse_poke_ids(add)
    removals, remove_errors = parse_poke_ids(remove)
    errors = add_errors + remove_errors
    valid_adds = []
    for thread_id in additions:
        try:
            await resolve_forum_thread(bot, guild, thread_id)
        except (ValueError, discord.HTTPException) as exc:
            errors.append(f'Thread {thread_id}: {exc}')
        else:
            valid_adds.append(thread_id)

    async with bot.poke_lock_for(guild.id):
        removed = await remove_poke_targets(bot.store, guild.id, removals)
        added = await add_poke_targets(bot.store, guild.id, valid_adds)
        results = []
        if ping_now is True:
            for target in await list_poke_targets(bot.store, guild.id):
                ok, error = await poke_one_target(bot, guild, target)
                results.append((target['thread_id'], ok, error))
        watched = await list_poke_targets(bot.store, guild.id)

    lines = []
    if not additions and not removals and ping_now is not True:
        lines.append('No changes made.')
    else:
        lines.append(f'Updated monitored threads: {added} added, {removed} removed.')
    if ping_now is True:
        passed = sum(ok for _, ok, _ in results)
        lines.append(f'Poked now: {passed}/{len(results)} thread(s).')
        if not results:
            lines.append('No threads are currently monitored.')
        for thread_id, ok, error in results:
            if not ok:
                errors.append(f'Thread {thread_id}: {error}')
    lines.append(f'Monitored in this server: {len(watched)} thread(s).')
    if watched:
        for row in watched[:15]:
            thread_id = row['thread_id']
            try:
                # Resolve archived posts too; do not depend on the guild cache.
                thread = await resolve_forum_thread(bot, guild, thread_id)
                thread_name = str(getattr(thread, 'name', None) or f'Thread {thread_id}')
                thread_name = thread_name.replace('\r', ' ').replace('\n', ' ')
            except (ValueError, discord.HTTPException):
                thread_name = 'Unavailable or inaccessible'
            lines.append(f'• {thread_name} (ID: `{thread_id}`)')
        if len(watched) > 15:
            lines.append(f'...and {len(watched) - 15} more.')
    if errors:
        lines.append('⚠️ ' + '\n⚠️ '.join(errors[:10]))
        if len(errors) > 10:
            lines.append(f'...and {len(errors)-10} more errors.')
    await interaction.followup.send('\n'.join(lines)[:1950], ephemeral=True,
                                    allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="scheduler_status", description="Show the current scheduler phase and upcoming actions (private)")
@app_commands.guild_only()
async def scheduler_status(interaction: discord.Interaction):
    if not await admin_gate(interaction): return
    await interaction.response.defer(ephemeral=True)
    record = await bot.store.get(interaction.guild.id)
    votes = await bot.store.votes(interaction.guild.id, record["cycle"]) if record else []
    rsvps = await bot.store.rsvps(interaction.guild.id, record["cycle"]) if record else []
    await interaction.followup.send(embed=build_scheduler_status(record, votes, rsvps), ephemeral=True)


@bot.tree.command(name="start_scheduler", description="Start or replace the recurring D&D schedule")
@app_commands.guild_only()
@app_commands.describe(title="Poll/event title", description="Poll/event description",
    dm="DM whose voted days have priority", weeks="Number of upcoming weeks (1–4)",
    days="Comma-separated weekdays (e.g. thur,fri,sat,sun)",
    times="Comma-separated times (e.g. 2pm,4:15pm,6:30pm)",
    poll_duration="Days the availability poll stays open (2–6; default 2)",
    attendee_limit="Maximum number of confirmed attendees (1–99)",
    min_votes="Minimum distinct voters on a date (3–12; default 3)",
    poll_after_event="Hours after the event ends before next poll (3–24)",
    reminder_before_event="Hours before session START to remind players (3–24)",
    ping_role="Deprecated: use /set_preferences pingable-role for guild pings",
    event_duration="Session duration in hours (1–24)",
    hours="Maximum availability duration in hours (4–8; 1-hour steps)",
    location="Optional physical or online session location (default blank)")
async def start_scheduler(interaction: discord.Interaction, title: str = "Title",
    description: str = "Description for event", dm: discord.Member | None = None,
    weeks: int = 2, days: str = "", times: str = "",
    poll_duration: int = 2, attendee_limit: int = 8,
    poll_after_event: int = 3, reminder_before_event: int = 6,
    ping_role: discord.Role | None = None, event_duration: int = 4,
    hours: int = 8, min_votes: int = 3, location: str = ""):
    if not await admin_gate(interaction): return
    await interaction.response.defer(ephemeral=True)
    try:
        c = config_defaults(interaction.guild, interaction.user.id, interaction.channel_id)
        c.update(title=title, description=description, dm_id=dm.id if dm else interaction.user.id,
                 weeks=weeks, days=parse_days(days), times=parse_times(times),
                 hours=hours, poll_duration=poll_duration, attendee_limit=attendee_limit,
                 min_votes=min_votes,
                 poll_after_event=poll_after_event, reminder_before_event=reminder_before_event,
                 ping_role_id=bot.preferences_for(interaction.guild)['ping_role_id'],
                 event_duration=event_duration, location=location.strip(), looping=True)
        await bot.replace_scheduler(interaction.guild, c)
        await interaction.followup.send("✅ Recurring scheduler started! See the public voting channel for the poll.", ephemeral=True)
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
        previous = old["config"] if old else None
        # A test run never becomes the new campaign template.
        if previous and is_test(previous):
            previous = previous.get("previous_normal_config")
        c = dict(previous) if previous else config_defaults(interaction.guild, interaction.user.id, interaction.channel_id)
        # Legacy campaigns with a three-hour maximum can still be reset.
        c["hours"] = max(4, c.get("hours", 8))
        c["looping"] = True
        c["test_mode"] = False
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


@bot.tree.command(name="test_scheduler", description="Run a one-minute test poll followed by a one-minute test event")
@app_commands.guild_only()
async def test_scheduler(interaction: discord.Interaction):
    if not await admin_gate(interaction): return
    await interaction.response.defer(ephemeral=True)
    try:
        old = await bot.store.get(interaction.guild.id)
        previous = old["config"] if old else None
        if previous and is_test(previous):
            previous = previous.get("previous_normal_config")
        normal_config = (dict(previous) if previous else
                         config_defaults(interaction.guild, interaction.user.id, interaction.channel_id))
        c = dict(normal_config)
        # Older saved campaigns may have a now-invalid seven-day poll. Keep the
        # original settings for a later reset but normalize the test copy.
        if not 2 <= c.get("poll_duration", 2) <= 6:
            c["poll_duration"] = 2
        c["hours"] = max(4, c.get("hours", 8))
        c.update(title="Test Poll", description="One-minute automated scheduler test.",
                 dm_id=interaction.user.id, test_mode=True, looping=False,
                 ping_role_id=None, previous_normal_config=normal_config)
        await bot.replace_scheduler(interaction.guild, c)
        await interaction.followup.send(
            "✅ Test poll posted in the public voting channel (1 minute, no pings). "
            "After closing, **Test Event** starts in about 1 minute, lasts 1 minute, "
            "has no reminder, and does not repeat. The event is created even without votes.",
            ephemeral=True)
    except (ValueError, discord.HTTPException) as exc:
        await interaction.followup.send(f"Could not start the scheduler test: {exc}", ephemeral=True)


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN is required (see .env.example)")
    async def run_service():
        stop_requested = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_requested.set)
            except (NotImplementedError, RuntimeError):
                # Signal handling differs on Windows; Fly.io runs Linux.
                pass
        async with bot:
            run_task = asyncio.create_task(bot.start(token))
            stop_task = asyncio.create_task(stop_requested.wait())
            try:
                done, _ = await asyncio.wait({run_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
                if run_task in done:
                    await run_task
            finally:
                stop_task.cancel()
                await bot.close()
                if not run_task.done():
                    try:
                        await asyncio.wait_for(run_task, timeout=10)
                    except asyncio.TimeoutError:
                        run_task.cancel()

    asyncio.run(run_service())
