"""Compact Sunday-first availability heatmaps, with no individual vote counts."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from io import BytesIO
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont
from logic import slot_keys

BACKGROUND = '#171b21'
PANEL = '#242b33'
EMPTY = '#35404a'
LIGHT_GREEN = '#a6df91'  # 0-2 voters, including polls before first vote
GREEN = '#28b567'        # 3+ distinct voters
RED = '#d95761'          # DM has voted but has no availability here
ORANGE = '#e7954e'       # Others voted, but DM is not available here
TEXT = '#f1f4f7'
MUTED = '#b5bdc8'


def _font(size, bold=False):
    try:
        return ImageFont.truetype('DejaVuSans-Bold.ttf' if bold else 'DejaVuSans.ttf', size)
    except OSError:
        return ImageFont.load_default(size=size)


def _color(count, dm_has_voted=False, dm_available=True):
    # Precedence: orange > red > green > light green.
    if dm_has_voted and not dm_available:
        return ORANGE if count > 0 else RED
    return GREEN if count >= 3 else LIGHT_GREEN


def _week_start(day):
    """Sunday of the calendar week that contains day."""
    return day - timedelta(days=(day.weekday() + 1) % 7)


def _in_window(day, time, window):
    if window['day'] != day:
        return False
    instant = datetime.fromisoformat(f'{day}T{time}')
    begin = datetime.fromisoformat(f"{day}T{window['start']}")
    return begin <= instant < begin + timedelta(hours=int(window['hours']))


def render_heatmaps(record, votes, *, windows=None, closed=False, winner=None):
    """Return PNGs only for calendar weeks with at least one submitted vote/window.

    Old-style votes are supported. For users with new interval windows, the
    supplied vote list should exclude their derived eligible event-start votes.
    """
    c = record['config']
    deadline = datetime.fromisoformat(record['poll_deadline'])
    window = timedelta(minutes=1) if c.get('test_mode') else timedelta(days=c['poll_duration'])
    valid = set(slot_keys(c, deadline-window, deadline))
    times = sorted(c['times'])
    windows = windows or []
    voters = defaultdict(set)
    for vote in votes:
        if vote['slot'] in valid:
            voters[vote['slot']].add(vote['user_id'])
    dm = c['dm_id']
    dm_has_voted = any(v['user_id'] == dm for v in votes) or any(v['user_id'] == dm for v in windows)
    active = set()
    for vote in votes:
        if vote['slot'] in valid:
            active.add(_week_start(date.fromisoformat(vote['slot'][:10])))
    valid_days = {s[:10] for s in valid}
    for item in windows:
        if item['day'] in valid_days:
            active.add(_week_start(date.fromisoformat(item['day'])))
    if not active:
        return []

    width, left, top, row_height = 850, 91, 128, 43
    cell_width, gap = 98, 7
    height = top + len(times) * row_height + 48
    title_font, labels, small = _font(22,True), _font(16,True), _font(14)
    result = []
    for index, first in enumerate(sorted(active), 1):
        image = Image.new('RGB', (width,height), BACKGROUND)
        d = ImageDraw.Draw(image)
        d.rounded_rectangle((9,9,width-9,height-9),radius=14,fill=PANEL)
        last = first + timedelta(days=6)
        d.text((26,23), f"Group start availability - {first:%b %d}-{last:%b %d}",
               fill=TEXT,font=title_font)
        d.text((26,60), c['timezone'],fill=MUTED,font=small)
        d.text((width-26,61),'CLOSED' if closed else 'LIVE', fill=MUTED,font=small,anchor='ra')
        for j,abbr in enumerate(('Su','Mo','Tu','We','Th','Fr','Sa')):
            day = first+timedelta(days=j)
            center = left+j*(cell_width+gap)+cell_width//2
            d.text((center,90),abbr,fill=TEXT,font=labels,anchor='mm')
            d.text((center,111),day.strftime('%m/%d'),fill=MUTED,font=small,anchor='mm')
        for i,t in enumerate(times):
            y=top+i*row_height
            label=datetime.strptime(t,'%H:%M').strftime('%I:%M %p').lstrip('0')
            d.text((left-11,y+20),label,fill=MUTED,font=small,anchor='rm')
            for j in range(7):
                day=(first+timedelta(days=j)).isoformat()
                slot=f'{day}T{t}'
                left_x=left+j*(cell_width+gap)
                rect=(left_x,y+2,left_x+cell_width-4,y+row_height-4)
                if slot not in valid:
                    color=EMPTY
                else:
                    active_users=set(voters.get(slot,set()))
                    active_users.update(w['user_id'] for w in windows if _in_window(day,t,w))
                    color=_color(len(active_users),dm_has_voted,dm in active_users)
                d.rounded_rectangle(rect,radius=4,fill=color)
                if winner==slot:
                    d.rounded_rectangle(rect,radius=4,outline=TEXT,width=3)
        d.text((27,height-30),
               'Light green: 0-2  ·  Green: 3+  ·  Red: DM unavailable  ·  Orange: votes, DM unavailable',
               fill=MUTED,font=small)
        buff=BytesIO()
        image.save(buff,'PNG',optimize=True)
        result.append((f'dnd-availability-week-{first.isoformat()}.png',buff.getvalue()))
    return result
