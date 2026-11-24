"""Discord UI: choose week, dates, start times and continuous hours available."""
from datetime import date, datetime, timedelta, timezone

import discord

from availability import read_windows, save_windows


def _sunday(day):
    return day - timedelta(days=(day.weekday()+1)%7)


def _label(t):
    return datetime.strptime(t,'%H:%M').strftime('%I:%M %p').lstrip('0')


class AvailabilityVotePanel(discord.ui.View):
    def __init__(self, bot, record, user_id, slots, saved_windows, saved_votes):
        super().__init__(timeout=600)
        self.bot=bot
        self.guild_id=record['guild_id']
        self.cycle=record['cycle']
        self.user_id=user_id
        self.config=record['config']
        self.valid_slots=set(slots)
        self.days=sorted({s[:10] for s in slots})
        self.times=sorted({s[11:] for s in slots})
        self.weeks=sorted({_sunday(date.fromisoformat(day)) for day in self.days})
        self.week=self.weeks[0]
        self.target_days=set()
        self.target_starts=set()
        # The configured maximum ranges from 4–8; the selected duration starts at four.
        # Legacy schedules with a maximum of three are displayed with at least four.
        self.max_hours=max(4,min(8,int(self.config.get('hours',8))))
        self.selected_hours=4
        # Legacy per-slot ballots can be reopened and edited without disappearing.
        previous={(w['day'],w['start']):int(w['hours']) for w in saved_windows}
        if not previous:
            duration=min(self.config.get('hours',8),self.config.get('event_duration',4))
            previous={(v['slot'][:10],v['slot'][11:]):duration for v in saved_votes
                      if v['slot'] in self.valid_slots}
        self.selected=dict(previous)
        self.saved=dict(self.selected)

        self.week_select=discord.ui.Select(row=0,placeholder='Select a week',min_values=1,
            max_values=1,options=self.week_options())
        self.week_select.callback=self.choose_week
        self.add_item(self.week_select)
        self.day_select=discord.ui.Select(row=1,placeholder='Choose one or more dates',
            min_values=1,max_values=len(self.visible_days()),options=self.day_options())
        self.day_select.callback=self.choose_days
        self.add_item(self.day_select)
        self.start_select=discord.ui.Select(row=2,placeholder='Choose your earliest start time',
            min_values=1,max_values=len(self.times),options=self.start_options())
        self.start_select.callback=self.choose_starts
        self.add_item(self.start_select)
        self.hours_select=discord.ui.Select(row=3,placeholder='Choose how many hours you can stay',
            min_values=1,max_values=1,options=self.hour_options())
        self.hours_select.callback=self.choose_hours
        self.add_item(self.hours_select)
        for label,style,callback in (
            ('Add Times',discord.ButtonStyle.success,self.add_windows),
            ('Remove Times',discord.ButtonStyle.danger,self.remove_windows),
            ('Close',discord.ButtonStyle.secondary,self.close)):
            button=discord.ui.Button(label=label,style=style,row=4)
            button.callback=callback
            self.add_item(button)

    def visible_days(self):
        return [d for d in self.days if _sunday(date.fromisoformat(d))==self.week]

    def week_options(self):
        return [discord.SelectOption(label=f'{w:%b %d} – {w+timedelta(days=6):%b %d}',
            value=w.isoformat(),default=w==self.week) for w in self.weeks]

    def day_options(self):
        return [discord.SelectOption(label=date.fromisoformat(d).strftime('%a, %b %d'),
            value=d,default=d in self.target_days) for d in self.visible_days()]

    def start_options(self):
        return [discord.SelectOption(label=_label(t),value=t,default=t in self.target_starts)
                for t in self.times]

    def hour_options(self):
        return [discord.SelectOption(
            label='Default (4 hours)' if h == 4 else f'{h} hour'+('' if h==1 else 's'),
            value=str(h),default=h==self.selected_hours)
            for h in range(1,self.max_hours+1)]

    def redraw(self):
        self.week_select.options=self.week_options()
        self.day_select.options=self.day_options()
        self.day_select.max_values=len(self.visible_days())
        self.start_select.options=self.start_options()
        self.hours_select.options=self.hour_options()

    def embed(self,note=''):
        preview=[]
        for (day,start),h in sorted(self.selected.items()):
            preview.append(f'• {date.fromisoformat(day):%a %b %d} — {_label(start)} for {h}h')
        text='\n'.join(preview[:14]) or 'No availability saved yet.'
        if len(preview)>14:
            text+=f'\n…plus {len(preview)-14} more windows.'
        return discord.Embed(title='🗓️ Your Availability',color=0x5865F2,
            description=('Select a **week**, one or more **dates**, **start time(s)**, '
                'and how many **hours** you can stay. **Add Times** and '
                '**Remove Times** save immediately. Press **Close** when finished.\n\n'
                f'**Start:** {", ".join(_label(t) for t in sorted(self.target_starts)) or "Choose a time"}\n'
                f'**Timezone:** `{self.config["timezone"]}`\n\n'
                f'**Saved availability ({len(self.selected)}):**\n{text}\n\n{note}'))

    async def choose_week(self,interaction):
        self.week=date.fromisoformat(self.week_select.values[0])
        self.target_days=set()
        self.redraw()
        await interaction.response.edit_message(embed=self.embed(),view=self)

    async def choose_days(self,interaction):
        self.target_days=set(self.day_select.values)
        self.redraw()
        await interaction.response.edit_message(embed=self.embed(),view=self)

    async def choose_starts(self,interaction):
        self.target_starts=set(self.start_select.values)
        self.redraw()
        await interaction.response.edit_message(embed=self.embed(),view=self)

    async def choose_hours(self,interaction):
        self.selected_hours=int(self.hours_select.values[0])
        self.redraw()
        await interaction.response.edit_message(embed=self.embed(),view=self)

    async def add_windows(self,interaction):
        await self._change_windows(interaction, remove=False)

    async def remove_windows(self,interaction):
        await self._change_windows(interaction, remove=True)

    async def _change_windows(self, interaction, *, remove):
        """Apply the requested edit to the latest DB values, then save atomically.

        There is deliberately no draft/Save step: every successful Add/Remove
        action updates availability_windows and eligible votes in one transaction.
        """
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                'This panel belongs to another player.', ephemeral=True)
        if not self.target_days:
            return await interaction.response.edit_message(
                embed=self.embed('Select one or more dates first.'), view=self)
        if not self.target_starts:
            return await interaction.response.edit_message(
                embed=self.embed('Select one or more start times first.'), view=self)
        edits={(day,start) for day in self.target_days for start in self.target_starts}
        async with self.bot.lock_for(self.guild_id):
            record=await self.bot.store.get(self.guild_id)
            if not record or record['state']!='poll' or record['cycle']!=self.cycle or datetime.now(timezone.utc)>=datetime.fromisoformat(record['poll_deadline']):
                return await interaction.response.edit_message(
                    embed=self.embed('This poll has closed. Reopen a current poll to vote.'),
                    view=None)
            slots=set(self.bot.slots_for(record))
            if not all(f'{day}T{start}' in slots for day,start in edits):
                return await interaction.response.edit_message(
                    embed=self.embed('Some selected dates or times are no longer valid. Reopen this poll.'),
                    view=self)

            # Read the latest vote state under the guild lock to prevent two
            # concurrently open ephemeral panels from clobbering each other.
            existing=await read_windows(self.bot.store,self.guild_id,self.cycle,self.user_id)
            windows={(w['day'],w['start']):int(w['hours']) for w in existing}
            if not existing:
                # Preserve ballots created with the older, per-slot voting UI.
                previous_votes=await self.bot.store.user_votes(self.guild_id,self.cycle,self.user_id)
                legacy_hours=min(self.max_hours, record['config']['event_duration'])
                windows={(v['slot'][:10],v['slot'][11:]):legacy_hours
                         for v in previous_votes if v['slot'] in slots}
            for key in edits:
                if remove:
                    windows.pop(key,None)
                else:
                    windows[key]=self.selected_hours
            try:
                # Test events last one minute; require only one minute of
                # availability instead of the normal campaign event duration.
                required_hours = (1 / 60 if record['config'].get('test_mode')
                                  else record['config']['event_duration'])
                qualifying=await save_windows(
                    self.bot.store,self.guild_id,self.cycle,self.user_id,
                    [(day,start,h) for (day,start),h in windows.items()],
                    slots,required_hours,self.max_hours)
            except ValueError:
                return await interaction.response.edit_message(
                    embed=self.embed('Invalid selection. Reopen the poll and try again.'),view=self)
        self.selected=windows
        self.saved=dict(windows)
        self.redraw()
        action='Removed' if remove else 'Added or updated'
        await interaction.response.edit_message(embed=self.embed(
            f'✅ {action} {len(edits)} date/time combination(s) and saved automatically. '
            f'{qualifying} eligible session start(s).'),view=self)
        await self.bot.refresh_poll(self.guild_id)

    async def close(self,interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                'This panel belongs to another player.',ephemeral=True)
        # Dismiss the ephemeral panel completely. The public voting button
        # remains in place and will open a fresh panel with saved selections.
        await interaction.response.defer()
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            await interaction.edit_original_response(
                content='Availability panel closed.',embed=None,view=None)
        self.stop()
