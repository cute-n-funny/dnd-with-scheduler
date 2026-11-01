# Scheduler availability windows and compact heatmap

This patch is based on the bot **after** the live-heatmap and Dockerfile fixes already present in `cute-n-funny/dnd-with-scheduler`.

## Behavior

- `/start_scheduler` defaults when `days` or `times` is left blank:
  - Days: Sunday through Saturday.
  - Start times: 2 PM, 2:30 PM, 3 PM, 3:30 PM, 4 PM, 5 PM, 6 PM, **7 PM**. (You listed 7 PM explicitly, so the last row extends beyond 6 PM.)
  - `hours`: 8. Valid range 1–8. `hours` is the **maximum** duration a player can choose, not the duration of the Discord event.
- Players use **Set My Availability** → **week** → **dates** → **Available Time: Start** → **Available Time: Hours** → **Add / Update** → **Save**. They can apply several starts to several dates, remove selections, clear dates, or copy the previous cycle's weekly preferences. The 5-row Discord component limit is respected.
- New availability is stored as continuous windows, not just isolated votes. A player available 2–4 PM can appear on the 2 PM, 2:30 PM and 3 PM squares, but **will not count as available for a 4-hour session**. The scheduling and auto-RSVP system only count someone if their window covers the **entire** configured `event_duration` starting at that slot. Several overlapping windows do not combine into a single longer window; choose a longer duration when appropriate.
- **Heatmap**: Sunday-first weekly columns, configured time rows. Light-green means 0–2 people (including zero), green means 3 or more, red means the DM has submitted availability but is not available at that time. Outside configured dates/times remains dark. Cells display **no numbers**.
- Polls start with a small text announcement and **no blank images**. The bot adds only weekly heatmaps containing submitted availability and updates the existing message in place after saved votes. When a poll closes, the selected slot gets an outline.
- Previous saved votes are still supported. Legacy votes display in heatmaps and count for scheduling as before. New interval ballots update the old session-start vote ledger only for fully covered sessions. When recurring polls roll forward, matching future windows carry with them.
- No changes to `#scheduler` private commands, public voting channel, reminders, attendee-limit threshold, DM priority, `/test_scheduler`, or Fly configuration.

## Deploy (PowerShell)

```powershell
cd C:\Coding\dnd-with-scheduler
git status --short
git apply --check "$HOME\Downloads\dnd-scheduler-window-heatmap-v2.patch"
git apply "$HOME\Downloads\dnd-scheduler-window-heatmap-v2.patch"
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m compileall -q bot.py logic.py storage.py heatmap.py availability.py availability_ui.py
git add bot.py logic.py Dockerfile heatmap.py availability.py availability_ui.py tests/test_availability_windows.py HEATMAP_V2_UPGRADE.md
git commit -m "Use duration-based availability and compact weekly heatmaps"
git push
fly deploy -a dnd-with-scheduler
```

To see the default days/times/hours immediately, start a **new** scheduler (or use `/reset_scheduler` only if your saved settings already match them). Existing saved configuration remains unchanged unless replaced. The SQLite schema adds an `availability_windows` table automatically; existing votes remain in place.

`/test_scheduler` remains a destructive smoke test that replaces the active recurring schedule while testing.
