"""Render per-week availability heatmap PNGs for D&D Scheduler.

Intentionally Discord-independent; works in unit tests and on Fly.io.
Never creates new message IDs or changes the saved availability records.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from io import BytesIO
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

from logic import slot_keys

# Shared dark theme for Discord's dark background; numbers remain readable.
BACKGROUND = "#181d27"
PANEL = "#252d3a"
EMPTY = "#3f4755"
UNAVAILABLE = "#282f3b"
TEXT = "#f2f4f8"
MUTED = "#b9c2d3"
RED = "#e15868"
ORANGE = "#e7954e"
YELLOW = "#f3cc55"
GREEN = "#35c88d"


def _font(size: int, bold: bool = False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(name, size)
    except OSError:
        # Pillow supplies a default font even in minimal python:slim images.
        return ImageFont.load_default(size=size)


def _color(count: int, limit: int) -> str:
    if not count:
        return EMPTY
    ratio = count / max(limit, 1)
    if ratio >= 1:
        return GREEN
    if ratio < 0.34:
        return RED
    if ratio < 0.7:
        return ORANGE
    return YELLOW


def render_heatmaps(record: dict, votes: list[dict], *, closed=False, winner=None) -> list[tuple[str, bytes]]:
    """One PNG for each poll week, showing configured times against all 7 days.

    Each square counts *distinct users* for an exact date+time. Grey cells with
    an em dash are times/days excluded by the scheduler, not available votes.
    Image dimensions are fixed/compact enough for Discord embeds and phones.
    """
    config = record["config"]
    end = datetime.fromisoformat(record["poll_deadline"])
    window = timedelta(minutes=1) if config.get("test_mode", False) else timedelta(days=config["poll_duration"])
    start_date = (end - window).astimezone(ZoneInfo(config["timezone"])).date() + timedelta(days=7)
    valid_slots = set(slot_keys(config, end - window, end))
    # Defensive de-duplication even if another input ever supplies repeat votes.
    voters = defaultdict(set)
    for vote in votes:
        slot = vote["slot"]
        if slot in valid_slots:
            voters[slot].add(vote["user_id"])

    times = sorted(config["times"])
    limit = max(1, int(config["attendee_limit"]))
    width = 970
    left = 105
    gap = 6
    col_w = (width - left - 34) // 7
    row_h = 49
    top = 166
    bottom = 98
    height = top + len(times) * row_h + bottom
    header = _font(25, True)
    font = _font(18)
    bold = _font(19, True)
    small = _font(16)
    results = []

    for week_index in range(int(config["weeks"])):
        first = start_date + timedelta(days=7 * week_index)
        last = first + timedelta(days=6)
        image = Image.new("RGB", (width, height), BACKGROUND)
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((14, 13, width - 14, height - 15), radius=19, fill=PANEL)
        title = f"Week {week_index + 1}  |  {first:%b %d} - {last:%b %d, %Y}"
        draw.text((34, 32), title, fill=TEXT, font=header)
        mode = "CLOSED" if closed else "LIVE AVAILABILITY"
        draw.text((width - 34, 43), mode, fill=MUTED, font=small, anchor="ra")
        draw.text((34, 79), f"Timezone: {config['timezone']}    |    {limit} players needed for green", fill=MUTED, font=small)

        for day_index in range(7):
            day = first + timedelta(days=day_index)
            cx = left + day_index * (col_w + gap) + col_w // 2
            draw.text((cx, 119), day.strftime("%a"), fill=TEXT, font=bold, anchor="mm")
            draw.text((cx, 145), day.strftime("%m/%d"), fill=MUTED, font=small, anchor="mm")

        for row_index, time_text in enumerate(times):
            y = top + row_index * row_h
            label = datetime.strptime(time_text, "%H:%M").strftime("%I:%M %p").lstrip("0")
            draw.text((96, y + 22), label, fill=MUTED, font=small, anchor="rm")
            for day_index in range(7):
                day = first + timedelta(days=day_index)
                slot = f"{day.isoformat()}T{time_text}"
                x0 = left + day_index * (col_w + gap)
                y0 = y + 3
                rect = (x0, y0, x0 + col_w - 4, y0 + 41)
                enabled = slot in valid_slots
                count = len(voters[slot]) if enabled else 0
                shade = _color(count, limit) if enabled else UNAVAILABLE
                draw.rounded_rectangle(rect, radius=6, fill=shade)
                if winner == slot:
                    draw.rounded_rectangle(rect, radius=6, outline="#ffffff", width=4)
                label = str(count) if enabled else "–"
                foreground = BACKGROUND if enabled and (count >= limit or 0 < count / limit >= 0.7) else TEXT
                draw.text(((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2),
                          label, fill=foreground if enabled else "#8992a3", font=bold, anchor="mm")

        legend = [(EMPTY, "0"), (RED, "Low"), (ORANGE, "Growing"),
                  (YELLOW, "Almost"), (GREEN, f"{limit}+")]
        start_x = 46
        step = 178
        legend_y = height - 57
        for index, (color, label) in enumerate(legend):
            x = start_x + index * step
            draw.rounded_rectangle((x, legend_y, x + 26, legend_y + 24), radius=5, fill=color)
            draw.text((x + 34, legend_y + 12), label, fill=TEXT, font=small, anchor="lm")

        content = BytesIO()
        image.save(content, format="PNG", optimize=True)
        results.append((f"dnd-availability-week-{week_index + 1}.png", content.getvalue()))
    return results
