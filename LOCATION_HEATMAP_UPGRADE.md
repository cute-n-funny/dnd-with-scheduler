# Scheduler: location option and DM-priority heatmap colors

This patch is written for the current GitHub project **after** the Player-role and forum-preservation upgrades.

## Changes

- The weekly PNG heading is now **Group start availability - Nov 08-Nov 14** (example); it uses a standard ASCII `-` between dates.
- Heatmap precedence on *offered* slots is **Orange > Red > Green > Light green**. When the DM has submitted availability but cannot attend that time: **orange** means one or more *other* people are available; **red** means no one is available. Otherwise **green** means at least three people, and **light green** means zero to two. Unoffered date/time cells remain dark gray. This changes visualization only, not the scheduling rules.
- The private **Your Availability** panel no longer repeats `Duration: X hours` or the event-duration restriction. Hours choices and the saved `for Xh` entries are unchanged, and session-start eligibility still requires the full configured event duration.
- `/start_scheduler` adds an optional **`location`** string (blank default, maximum 100 characters). **`min_votes` is unchanged**; the user's location bullet appears to have a copy/paste typo. A nonblank location appears in Availability and Session Details messages. If a Discord Scheduled Event is **external** (not tied to a voice channel), its native location also uses this value. Voice-linked events keep their voice channel, while the embed still displays location.
- Old saved schedules missing `location` remain compatible. New schedules with blank location display no Location field. `/test_scheduler` inherits any saved location as part of its existing copied configuration.

## Install (PowerShell)

Download `dnd-location-heatmap.patch` into Downloads. With a **clean** working tree:

```powershell
cd C:\Coding\dnd-with-scheduler
git status --short
git pull --ff-only
git apply --check "$HOME\Downloads\dnd-location-heatmap.patch"
git apply "$HOME\Downloads\dnd-location-heatmap.patch"

python -m unittest discover -s tests -v
python -m compileall -q bot.py heatmap.py availability_ui.py logic.py storage.py

git add bot.py heatmap.py availability_ui.py tests/test_heatmap.py tests/test_availability_windows.py tests/test_location_heatmap.py LOCATION_HEATMAP_UPGRADE.md
git commit -m "Add session locations and improve availability heatmap colors"
git push
fly deploy -a dnd-with-scheduler
```

If `git apply --check` fails, stop and share the error; don't force it. No SQLite schema migration, Fly environment variable, or additional service is needed.

## Windows test error from an earlier local log

The previously shared `cp1252` Unicode errors came from tests reading UTF-8 `bot.py` with Windows' default encoding, and the `WinError 32` was caused by SQLite remaining open when a temporary directory was deleted. Those cases are **already fixed in the current GitHub test files**. If the old errors still occur locally, review `git status`, update your local checkout and compare the test files before editing anything. Do not run `git pull` with uncommitted work.
