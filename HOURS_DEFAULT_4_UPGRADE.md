# Four-hour default availability

This patch builds on the latest D&D Scheduler code (after the test auto-RSVP fix).

## What changes

- **`/start_scheduler hours`** remains an optional maximum, but its allowed range is now **4–8**. Its default remains **8**. `hours=4` presents the four options 1, 2, 3 and 4; `hours=8` presents 1–8.
- The **Choose how many hours you can stay** select starts on **Default (4 hours)** whenever the voting panel opens. The 4-hour choice's displayed label changes, but its stored value remains `4`. Players can still explicitly choose other values, and saved availability windows aren't overwritten merely by opening the panel.
- **Legacy schedules** configured with `hours=3` are displayed with options through 4; on `/reset_scheduler` they are automatically normalized to `hours=4`. `/test_scheduler` does the same for its temporary test configuration without changing the saved campaign template.
- Nothing changes about the session's own `event_duration`, attendance requirements, voting, heatmap, role notifications, or calendar exports.

## Apply in Windows PowerShell

Download `dnd-hours-default-four.patch` into your Downloads folder, then run:

```powershell
cd C:\Coding\dnd-with-scheduler

git status --short
git apply --check "$HOME\Downloads\dnd-hours-default-four.patch"
git apply "$HOME\Downloads\dnd-hours-default-four.patch"

python -m unittest discover -s tests -v
python -m compileall -q bot.py availability_ui.py availability.py logic.py storage.py

git add bot.py availability_ui.py tests/test_instant_availability_ui.py tests/test_hours_default_4.py HOURS_DEFAULT_4_UPGRADE.md
git commit -m "Default availability duration to four hours"
git push
fly deploy -a dnd-with-scheduler
```

If `git apply --check` reports a conflict, **don't force it** or reset your checkout. Share the error so the patch can be adapted to your current version. This patch does not require any SQLite or Fly.io schema/configuration migration.
