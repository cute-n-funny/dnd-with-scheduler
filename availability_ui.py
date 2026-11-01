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
        self.target_days={self.visible_days()[0]}
        self.target_starts=set()
        self.selected_hours=min(self.config.get('hours',8),8)
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
        self.start_select=discord.ui.Select(row=2,placeholder='Available Time: Start',
            min_values=1,max_values=len(self.times),options=self.start_options())
        self.start_select.callback=self.choose_starts
        self.add_item(self.start_select)
        self.hours_select=discord.ui.Select(row=3,placeholder='Available Time: Hours',
            min_values=1,max_values=1,options=self.hour_options())
        self.hours_select.callback=self.choose_hours
        self.add_item(self.hours_select)
        for label,style,callback in (
            ('Add / Update',discord.ButtonStyle.primary,self.add_windows),
            ('Remove',discord.ButtonStyle.secondary,self.remove_windows),
            ('Clear Dates',discord.ButtonStyle.secondary,self.clear_dates),
            ('Save',discord.ButtonStyle.success,self.save),
            ('Copy Previous',discord.ButtonStyle.secondary,self.copy_previous)):
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
        return [discord.SelectOption(label=f'{h} hour'+('' if h==1 else 's'),
            value=str(h),default=h==self.selected_hours)
            for h in range(1,self.config.get('hours',8)+1)]

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
        state='⚠️ Unsaved changes' if self.selected!=self.saved else '✅ Saved'
        return discord.Embed(title='🗓️ Your Availability',color=0x5865F2,
            description=('Select a **week**, one or more **dates**, **start time(s)**, '
                'and how many **hours** you can stay. Press **Add / Update**, repeat '
                'as needed, then **Save**.\n\n'
                f'**Start:** {", ".join(_label(t) for t in sorted(self.target_starts)) or "Choose a time"}\n'
                f'**Duration:** {self.selected_hours} hours\n'
                f'**Timezone:** `{self.config["timezone"]}`\n'
                f'**Event duration:** {self.config["event_duration"]} hours '
                '(only windows covering the entire event count toward scheduling).\n\n'
                f'**Your windows ({len(self.selected)}):**\n{text}\n\n{state}\n{note}'))

    async def choose_week(self,interaction):
        self.week=date.fromisoformat(self.week_select.values[0])
        self.target_days={self.visible_days()[0]}
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
        if not self.target_starts:
            return await interaction.response.edit_message(embed=self.embed('Choose a start time first.'),view=self)
        for day in self.target_days:
            for start in self.target_starts:
                if f'{day}T{start}' in self.valid_slots:
                    self.selected[(day,start)]=self.selected_hours
        self.redraw()
        await interaction.response.edit_message(embed=self.embed('✅ Added / updated windows. Press Save.'),view=self)

    async def remove_windows(self,interaction):
        for day in self.target_days:
            for start in self.target_starts:
                self.selected.pop((day,start),None)
        self.redraw()
        await interaction.response.edit_message(embed=self.embed('Removed selected windows from draft.'),view=self)

    async def clear_dates(self,interaction):
        self.selected={k:v for k,v in self.selected.items() if k[0] not in self.target_days}
        self.redraw()
        await interaction.response.edit_message(embed=self.embed('Cleared selected dates from draft.'),view=self)

    async def copy_previous(self,interaction):
        old=await read_windows(self.bot.store,self.guild_id,self.cycle-1,self.user_id)
        if not old:
            return await interaction.response.edit_message(embed=self.embed('No previous interval preferences to copy.'),view=self)
        preferences={(date.fromisoformat(w['day']).weekday(),w['start']):w['hours'] for w in old}
        for day in self.days:
            for t in self.times:
                h=preferences.get((date.fromisoformat(day).weekday(),t))
                if h is not None and f'{day}T{t}' in self.valid_slots:
                    self.selected[(day,t)]=min(h,self.config.get('hours',8))
        self.redraw()
        await interaction.response.edit_message(embed=self.embed('Copied matching weekdays into your draft. Review and Save.'),view=self)

    async def save(self,interaction):
        if interaction.user.id!=self.user_id:
            return await interaction.response.send_message('This panel belongs to another player.',ephemeral=True)
        async with self.bot.lock_for(self.guild_id):
            record=await self.bot.store.get(self.guild_id)
            if not record or record['state']!='poll' or record['cycle']!=self.cycle or datetime.now(timezone.utc)>=datetime.fromisoformat(record['poll_deadline']):
                return await interaction.response.send_message('This poll is closed.',ephemeral=True)
            if any(f'{day}T{start}' not in set(self.bot.slots_for(record)) for day,start in self.selected):
                return await interaction.response.send_message('Availability is no longer valid; reopen the poll.',ephemeral=True)
            qualifying=await save_windows(self.bot.store,self.guild_id,self.cycle,self.user_id,
                [(day,start,h) for (day,start),h in self.selected.items()],
                self.bot.slots_for(record),record['config']['event_duration'],record['config'].get('hours',8))
        self.saved=dict(self.selected)
        self.redraw()
        await interaction.response.edit_message(embed=self.embed(
            f'✅ Saved {len(self.selected)} availability windows. '
            f'{qualifying} valid session-start votes.'),view=self)
        await self.bot.refresh_poll(self.guild_id)
