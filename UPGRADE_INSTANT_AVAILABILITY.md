# D&D Scheduler — Instant Availability UI

This patch requires the prior **duration-based availability / compact heatmap** upgrade (`availability_ui.py`, `availability.py`) to already be installed.

## Changes

- Opening **Set My Availability** no longer preselects a day. The day selector remains empty when switching to another week, and players must intentionally select date(s).
- **Available Time: Hours** starts at **1 hour**. The `/start_scheduler hours` parameter now accepts **3–8**. Each valid setting offers every whole-hour choice from 1 through that maximum (e.g., `hours=3` gives 1, 2, 3 hours).
- The popup shows exactly four selects (**Select a Week**, **Choose one or more dates**, **Available Time: Start**, **Available Time: Hours**) and three buttons (**Add Times**, **Remove Times**, **Close**).
- **Add Times** and **Remove Times** immediately persist changes to the existing SQLite availability-window and eligible-session-vote tables. After saving, the public heatmap is refreshed in place. The original session-start voting rules and DM restrictions remain unchanged.
- Multiple simultaneously open panels will read and change the latest persisted availability, rather than overwriting previously saved changes from another panel.
- **Close** dismisses the ephemeral UI. Clicking **Set My Availability** again reopens it, showing the latest saved windows.
- Existing configured schedules remain intact. New validation applies when starting a schedule; previously stored `hours` below 3 are treated as a three-hour selectable maximum by the user interface until a new schedule is created.

## Installation (PowerShell)

Download `dnd-scheduler-instant-availability-ui.patch` to your Downloads folder. In your repository:

```powershell
cd C:\Coding\dnd-with-scheduler
git status --short
git apply --check "$HOME\Downloads\dnd-scheduler-instant-availability-ui.patch"
git apply "$HOME\Downloads\dnd-scheduler-instant-availability-ui.patch"
python -m unittest discover -s tests -v
python -m compileall -q bot.py availability_ui.py availability.py logic.py heatmap.py storage.py
git add bot.py availability_ui.py tests/test_instant_availability_ui.py UPGRADE_INSTANT_AVAILABILITY.md
git commit -m "Make availability additions and removals save instantly"
git push
fly deploy -a dnd-with-scheduler
```

The upgraded Discord slash-command description may take a short time to refresh after the bot reconnects and syncs commands. There is **no schema migration** in this patch. A pre-deployment backup of the persistent SQLite volume is advisable.

Testing with `/test_scheduler` is optional, but it **replaces the active schedule**, just as before. Do not run it while you want to preserve an existing campaign poll.
